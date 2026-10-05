#!/usr/bin/env python3
"""Snapshot what the InvoiceGuard backend actually implements and compare it with qa/baseline.json.

Why this exists
---------------
The scenario expectations in run_scenarios.py encode rules: score weights, risk thresholds,
the PO amount tolerance, seed vendors, API routes. If the code has moved on since those
expectations were written, a "failure" may only be a stale test, and a "pass" may be testing
the wrong thing. Run this before every test session and read whatever it reports as drift.

The static part uses only the standard library (it parses the source with `ast`), so it works
even when the backend's dependencies are not installed. The dynamic part imports the app
against a throwaway SQLite file and never touches the project's real database.

Usage (from any directory):
    python qa/inspect_implementation.py                    # human-readable snapshot + drift
    python qa/inspect_implementation.py --json             # machine-readable
    python qa/inspect_implementation.py --write-baseline   # accept the current code as baseline

Exit codes: 0 = matches baseline, 3 = drift (or no baseline yet), 2 = could not inspect.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the project folder

QA_DIR = Path(__file__).resolve().parent
ROOT = QA_DIR.parent
BASELINE = QA_DIR / "baseline.json"

# Keys compared against the baseline. Everything else in the snapshot is informational
# (it describes this machine/run, not the implementation).
COMPARED_KEYS = [
    "risk_score_increments",
    "risk_level_rules",
    "amount_tolerance_pct",
    "anomaly_multiplier_default",
    "status_assignments",
    "invoice_status_default",
    "routes",
    "seed_vendors",
    "seed_purchase_orders",
    "supplier_kb_vendors",
    "invoice_response_fields",
]


# --------------------------------------------------------------------------- isolation

_ISOLATED_DB: Path | None = None


def apply_isolation_env(db_path: Path, allow_llm: bool = False) -> None:
    """Point the app at a throwaway database and switch off every outbound side effect.

    Must be called BEFORE anything under `app` is imported, because app/config.py reads the
    environment at import time. python-dotenv does not override variables that already exist,
    so setting them here (even to "") wins over the project's .env file.
    """
    global _ISOLATED_DB
    _ISOLATED_DB = Path(db_path)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    if not allow_llm:
        os.environ["ANTHROPIC_API_KEY"] = ""      # force the deterministic extractor
    for key in ("SLACK_WEBHOOK_URL", "SMTP_HOST", "ALERT_EMAIL_TO"):
        os.environ[key] = ""                      # high-risk alerts must not leave the machine


def bound_to_throwaway_db(engine) -> bool:
    """True only when the app's engine points at the file apply_isolation_env() created."""
    return _ISOLATED_DB is not None and str(_ISOLATED_DB) in str(engine.url)


# --------------------------------------------------------------------------- static (ast)

def _tree(rel: str) -> ast.AST | None:
    path = ROOT / rel
    if not path.exists():
        return None
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _risk_rules() -> tuple[list[dict], list[dict]]:
    """Every `score += N` and every `risk_level = "..."` in AssessAgent.run, with its conditions."""
    increments: list[dict] = []
    levels: list[dict] = []
    tree = _tree("app/agents/assess_agent.py")
    if tree is None:
        return increments, levels

    def walk(stmts, conds):
        for s in stmts:
            when = " AND ".join(conds) if conds else "always"
            if isinstance(s, ast.If):
                test = ast.unparse(s.test)
                walk(s.body, conds + [test])
                walk(s.orelse, conds + [f"not ({test})"])
            elif isinstance(s, ast.AugAssign) and isinstance(s.target, ast.Name) and s.target.id == "score":
                increments.append({"when": when, "add": ast.unparse(s.value), "line": s.lineno})
            elif isinstance(s, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "risk_level" for t in s.targets
            ):
                levels.append({"when": when, "level": ast.unparse(s.value).strip("'\""), "line": s.lineno})
            elif isinstance(s, (ast.For, ast.While, ast.With)):
                walk(s.body, conds)
            elif isinstance(s, ast.Try):
                walk(s.body, conds)
                for h in s.handlers:
                    walk(h.body, conds)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            walk(node.body, [])
    return increments, levels


def _module_constant(rel: str, name: str):
    tree = _tree(rel)
    if tree is None:
        return None
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except Exception:
                return ast.unparse(node.value)
    return None


def _getenv_default(rel: str, var: str):
    tree = _tree(rel)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == var
        ):
            if len(node.args) > 1:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return ast.unparse(node.args[1])
            return None
    return None


class _StatusVisitor(ast.NodeVisitor):
    """Finds every place the code writes an object's `.status` (the human-gate surface)."""

    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.found: list[dict] = []

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def _record(self, target: str, value: str, line: int):
        self.found.append({
            "file": self.rel,
            "where": ".".join(self.stack) or "<module>",
            "target": target,
            "value": value,
            "line": line,
        })

    def visit_Assign(self, node):
        for t in node.targets:
            if isinstance(t, ast.Attribute) and t.attr == "status":
                self._record(ast.unparse(t), ast.unparse(node.value), node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Attribute) and node.target.attr == "status":
            self._record(ast.unparse(node.target), ast.unparse(node.value), node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node):
        callee = ast.unparse(node.func)
        if callee.split(".")[-1] == "Invoice":
            for kw in node.keywords:
                if kw.arg == "status":
                    self._record("Invoice(status=...)", ast.unparse(kw.value), node.lineno)
        if callee.endswith("setattr") and len(node.args) >= 2:
            if isinstance(node.args[1], ast.Constant) and node.args[1].value == "status":
                self._record("setattr(..., 'status', ...)", ast.unparse(node.args[2]) if len(node.args) > 2 else "?", node.lineno)
        self.generic_visit(node)


def _status_assignments() -> list[dict]:
    found: list[dict] = []
    app_dir = ROOT / "app"
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            found.append({"file": rel, "where": "<unparseable>", "target": "?", "value": str(exc), "line": 0})
            continue
        v = _StatusVisitor(rel)
        v.visit(tree)
        found.extend(v.found)
    return found


def _invoice_status_default():
    tree = _tree("app/models.py")
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Invoice":
            for s in node.body:
                if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "status" for t in s.targets):
                    if isinstance(s.value, ast.Call):
                        for kw in s.value.keywords:
                            if kw.arg == "default":
                                return ast.unparse(kw.value).strip("'\"")
    return None


def static_snapshot() -> dict:
    increments, levels = _risk_rules()
    return {
        "risk_score_increments": increments,
        "risk_level_rules": levels,
        "amount_tolerance_pct": _module_constant("app/agents/validate_agent.py", "AMOUNT_TOLERANCE_PCT"),
        "anomaly_multiplier_default": _getenv_default("app/config.py", "ANOMALY_MULTIPLIER"),
        "status_assignments": _status_assignments(),
        "invoice_status_default": _invoice_status_default(),
    }


# --------------------------------------------------------------------------- dynamic (import)

def collect_dynamic() -> dict:
    """Routes, seed data and response fields, read from the imported app.

    Assumes apply_isolation_env() already ran in this process. Re-creates and re-seeds the
    throwaway database, so it refuses to do anything if the engine points anywhere else.
    """
    from app.main import app
    from app.config import settings
    from app.database import Base, SessionLocal, engine
    from app.models import PurchaseOrder, Vendor
    from app import seed_data
    from app.schemas import InvoiceOut
    from app.services.supplier_kb import SUPPLIER_KNOWLEDGE_BASE

    if not bound_to_throwaway_db(engine):
        return {"dynamic_error": "engine is not bound to the throwaway DB; refusing to seed"}

    routes = sorted(
        f"{m} {r.path}"
        for r in app.routes
        if getattr(r, "methods", None)
        for m in r.methods
        if m not in ("HEAD", "OPTIONS")
        and not r.path.startswith(("/docs", "/redoc", "/openapi"))
    )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with contextlib.redirect_stdout(io.StringIO()):
        seed_data.seed()
    db = SessionLocal()
    try:
        vendors = {v.id: v for v in db.query(Vendor).all()}
        seed_vendors = sorted(
            (
                {
                    "name": v.name,
                    "approved": bool(v.approved),
                    "avg_invoice_amount": v.avg_invoice_amount,
                    "invoice_count": v.invoice_count,
                }
                for v in vendors.values()
            ),
            key=lambda d: d["name"],
        )
        seed_pos = sorted(
            (
                {
                    "po_number": p.po_number,
                    "vendor": vendors[p.vendor_id].name if p.vendor_id in vendors else None,
                    "amount": p.amount,
                    "status": p.status,
                    "line_items": [li.get("description") for li in (p.line_items or [])],
                }
                for p in db.query(PurchaseOrder).all()
            ),
            key=lambda d: d["po_number"],
        )
    finally:
        db.close()

    return {
        "routes": routes,
        "seed_vendors": seed_vendors,
        "seed_purchase_orders": seed_pos,
        "supplier_kb_vendors": sorted(SUPPLIER_KNOWLEDGE_BASE),
        "invoice_response_fields": sorted(InvoiceOut.model_fields),
        "anomaly_multiplier_effective": settings.ANOMALY_MULTIPLIER,
    }


def dynamic_snapshot() -> dict:
    """Imports the app against a throwaway DB. Returns {"dynamic_error": ...} if it cannot."""
    tmp = Path(tempfile.mkdtemp(prefix="invoiceguard-inspect-"))
    apply_isolation_env(tmp / "inspect.db")
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        return collect_dynamic()
    except Exception as exc:  # missing deps, wrong Python, import error in the app itself
        return {"dynamic_error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- run facts

def _git(*args: str) -> str | None:
    # --no-optional-locks: `git status` otherwise takes .git/index.lock to refresh the index. In a
    # sandbox that may create but not delete files, that leaves a stale lock which blocks the next
    # `git add` / `git commit`. A read-only tool has no business locking the index anyway.
    try:
        out = subprocess.run(["git", "--no-optional-locks", *args], cwd=ROOT, capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def project_env_flags() -> dict:
    """What the project's own .env would configure. Booleans only: never values."""
    flags = {"env_file_present": (ROOT / ".env").exists()}
    values: dict = {}
    try:
        from dotenv import dotenv_values
        values = dict(dotenv_values(ROOT / ".env")) if flags["env_file_present"] else {}
    except Exception:
        flags["env_file_readable"] = False
    db_url = values.get("DATABASE_URL") or "sqlite:///./invoiceguard.db"
    flags.update({
        "llm_key_configured": bool(values.get("ANTHROPIC_API_KEY")),
        "slack_configured": bool(values.get("SLACK_WEBHOOK_URL")),
        "smtp_configured": bool(values.get("SMTP_HOST") and values.get("ALERT_EMAIL_TO")),
        "database_scheme": db_url.split(":", 1)[0],
    })
    return flags


def run_facts() -> dict:
    status = _git("status", "--porcelain") or ""
    dirty = [line[3:] for line in status.splitlines() if line[3:].startswith(("app/", "requirements", "frontend/src"))]
    return {
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "uncommitted_app_changes": dirty,
        "python": sys.version.split()[0],
        "project_env": project_env_flags(),
    }


# --------------------------------------------------------------------------- baseline compare

def _strip_lines(value):
    if isinstance(value, list):
        return [_strip_lines(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_lines(v) for k, v in value.items() if k != "line"}
    return value


def comparable(snapshot: dict) -> dict:
    return {k: _strip_lines(snapshot.get(k)) for k in COMPARED_KEYS if k in snapshot}


def diff_against_baseline(snapshot: dict) -> list[dict]:
    """Returns drift entries: [{"key", "baseline", "current"}]. A missing baseline is itself drift."""
    if not BASELINE.exists():
        return [{"key": "<baseline>", "baseline": None, "current": "qa/baseline.json does not exist yet"}]
    base = json.loads(BASELINE.read_text(encoding="utf-8")).get("implementation", {})
    current = comparable(snapshot)
    drift = []
    for key in COMPARED_KEYS:
        if key not in current:
            continue  # could not be determined in this run (e.g. dynamic import failed)
        if base.get(key) != current[key]:
            drift.append({"key": key, "baseline": base.get(key), "current": current[key]})
    return drift


def full_snapshot() -> dict:
    snap = static_snapshot()
    snap.update(dynamic_snapshot())
    snap["run_facts"] = run_facts()
    return snap


def _print_human(snap: dict, drift: list[dict]) -> None:
    facts = snap["run_facts"]
    print("InvoiceGuard implementation snapshot")
    print("=" * 60)
    print(f"commit: {facts['git_commit']} ({facts['git_branch']})   python: {facts['python']}")
    if facts["uncommitted_app_changes"]:
        print(f"uncommitted app changes: {', '.join(facts['uncommitted_app_changes'])}")
    print(f"project .env: {facts['project_env']}")
    if "dynamic_error" in snap:
        print(f"\n!! could not import the app: {snap['dynamic_error']}")
        print("   (routes, seed data and effective settings were NOT inspected)")
    print("\nRisk score increments (app/agents/assess_agent.py):")
    for r in snap["risk_score_increments"]:
        print(f"  +{r['add']:<40} when {r['when']}   [line {r['line']}]")
    print("Risk level rules:")
    for r in snap["risk_level_rules"]:
        print(f"  {r['level']:<7} when {r['when']}   [line {r['line']}]")
    print(f"PO amount tolerance: {snap['amount_tolerance_pct']}   "
          f"anomaly multiplier: default {snap['anomaly_multiplier_default']}, "
          f"effective {snap.get('anomaly_multiplier_effective', 'not inspected')}")
    print(f"Invoice.status column default: {snap['invoice_status_default']}")
    print("Places that write a .status attribute (human-gate surface):")
    for s in snap["status_assignments"]:
        print(f"  {s['file']}:{s['line']}  {s['where']}  {s['target']} = {s['value']}")
    if "routes" in snap:
        print("Routes:")
        for r in snap["routes"]:
            print(f"  {r}")
        print("Seed vendors:")
        for v in snap["seed_vendors"]:
            print(f"  {v['name']:<28} approved={v['approved']!s:<5} avg={v['avg_invoice_amount']:<8} count={v['invoice_count']}")
        print("Seed purchase orders:")
        for p in snap["seed_purchase_orders"]:
            print(f"  {p['po_number']}  {p['vendor']:<28} amount={p['amount']:<8} items={p['line_items']}")
    print("\n" + "=" * 60)
    if not drift:
        print("NO DRIFT: implementation matches qa/baseline.json")
    else:
        print(f"DRIFT in {len(drift)} area(s): the scenario expectations may be stale.")
        for d in drift:
            print(f"\n- {d['key']}")
            print(f"    baseline: {json.dumps(d['baseline'], default=str)}")
            print(f"    current : {json.dumps(d['current'], default=str)}")
        print("\nRead the changed code before trusting any scenario result in that area.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print the snapshot and drift as JSON")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record the current implementation as the baseline (only after the change is confirmed intentional and the scenarios were updated)")
    args = ap.parse_args()

    if not (ROOT / "app").is_dir():
        print(f"error: {ROOT} does not look like the InvoiceGuard project (no app/ directory)", file=sys.stderr)
        return 2

    snap = full_snapshot()

    if args.write_baseline:
        if "dynamic_error" in snap:
            print(f"refusing to write a baseline without the dynamic part: {snap['dynamic_error']}", file=sys.stderr)
            return 2
        payload = {
            "recorded_at_commit": snap["run_facts"]["git_commit"],
            "note": "Fingerprint of the implementation the scenarios in run_scenarios.py were last reconciled with.",
            "implementation": comparable(snap),
        }
        BASELINE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {BASELINE.relative_to(ROOT)} at commit {payload['recorded_at_commit']}")
        return 0

    drift = diff_against_baseline(snap)
    if args.json:
        print(json.dumps({"snapshot": snap, "drift": drift}, indent=2, default=str))
    else:
        _print_human(snap, drift)
    if "dynamic_error" in snap:
        return 2
    return 3 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
