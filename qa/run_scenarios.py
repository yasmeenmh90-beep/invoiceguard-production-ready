#!/usr/bin/env python3
"""InvoiceGuard scenario runner: executes invoice scenarios and records expected vs actual.

What it does
------------
Runs every scenario against the real FastAPI app and writes a Markdown report plus raw JSON to
test_reports/. A scenario only gets PASS when its checks were actually executed and matched.
Anything that did not run is reported as SKIPPED or BLOCKED with the reason, never as a pass.

Modes
-----
isolated (default)  The app is imported in-process and bound to a throwaway SQLite file that
                    is re-seeded before every scenario. The project's invoiceguard.db is never
                    opened, the LLM extractor is off, and Slack/SMTP alerts are off.
live (opt-in)       --base-url http://127.0.0.1:8000 --live-writes-ok
                    Talks to a running server. This WRITES invoices into that server's database,
                    so it needs the explicit flag. Scenarios that need a pristine database are
                    skipped; approvals (which change vendor averages) need --allow-approve too.

Usage (from any directory, with the interpreter printed by qa/bootstrap_env.sh):
    python qa/run_scenarios.py                         # everything, isolated
    python qa/run_scenarios.py --list                  # the scenario catalog, nothing executed
    python qa/run_scenarios.py --area duplicate,po     # only some areas
    python qa/run_scenarios.py --only po_vendor_mismatch
    python qa/run_scenarios.py --with-existing-suites  # also run tests/ and the frontend tests
    python qa/run_scenarios.py --llm                   # exercise the LLM extractor (costs API calls)

Exit codes: 0 = no FAIL/ERROR, 1 = at least one FAIL or ERROR, 2 = the harness could not run.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the project folder

QA_DIR = Path(__file__).resolve().parent
ROOT = QA_DIR.parent
sys.path.insert(0, str(QA_DIR))
import inspect_implementation as impl  # noqa: E402

# --------------------------------------------------------------------------- the spec
# The rules the scenarios hold the app to. Sources: README section 4 ("Risk Scoring") and the
# PO/vendor mismatch rule added in commit 5ab765b (also asserted by tests/test_backend_regression.py).
# If a rule changes on purpose, change it HERE (and re-record qa/baseline.json), not in a scenario.
SPEC = {
    "weights": {
        "duplicate": 50,
        "amount_anomaly": 30,
        "unknown_vendor": 15,
        "validation_issue": 20,   # per issue
        "po_vendor_mismatch": 50,
    },
    "levels": {"medium": 20, "high": 50},
    "amount_tolerance": 0.02,
}
W = SPEC["weights"]

ACME = "Acme Office Supplies"
BRIGHT = "Brightline Logistics"
SHADOW = "Shadow Consulting LLC"
MERIDIAN = "Meridian Cloud Services"
STERLING = "Sterling Print & Signage"
ACME_ITEMS = [("Office chairs", 5, 150.0), ("Standing desks", 2, 250.0)]

REVIEWER = "qa.reviewer@invoiceguard.local"

_UNSET = object()


def level_for(score: float) -> str:
    if score >= SPEC["levels"]["high"]:
        return "high"
    if score >= SPEC["levels"]["medium"]:
        return "medium"
    return "low"


def invoice_text(number, vendor, items, total, po=None, due="2026-10-15") -> str:
    """Same layout as app/seed_data.py SAMPLE_INVOICES and the frontend demo presets."""
    lines = [f"Invoice Number: {number}", f"Vendor: {vendor}"]
    if po:
        lines.append(f"PO Number: {po}")
    lines.append(f"Due Date: {due}")
    lines += [f"{desc} {qty:g} x ${price:.2f}" for desc, qty, price in items]
    lines.append(f"Total Amount Due: ${total:.2f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- harness

class Skip(Exception):
    """Scenario does not apply in this run (missing file, missing OCR binary, wrong mode)."""


class Blocked(Exception):
    """A precondition the scenario depends on is not true, so its result would be meaningless."""


class Abort(Exception):
    """A step the rest of the scenario depends on failed; the failed check is already recorded."""


def _ansi_c(text: str) -> str:
    return "$'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def _contains(actual, needle: str) -> bool:
    needle = needle.lower()
    if isinstance(actual, str):
        return needle in actual.lower()
    if isinstance(actual, (list, tuple)):
        return any(needle in str(item).lower() for item in actual)
    return False


OPS = {
    "eq": lambda a, e: a == e,
    "ne": lambda a, e: a != e,
    "ge": lambda a, e: a is not None and a >= e,
    "approx": lambda a, e: isinstance(a, (int, float)) and abs(a - e) <= 0.01,
    "contains": lambda a, e: _contains(a, e),
    "not_contains": lambda a, e: not _contains(a, e),
}


class T:
    """Per-scenario context: makes HTTP calls, records every call, check and observation."""

    def __init__(self, run: "Run", scenario: dict):
        self.run = run
        self.cfg = run.cfg
        self.scenario = scenario
        self.checks: list[dict] = []
        self.observations: list[dict] = []
        self.calls: list[dict] = []

    # ---- naming -------------------------------------------------------------------------
    def n(self, invoice_number: str) -> str:
        """In live mode there is no reset between scenarios, so invoice numbers get a suffix that is
        unique per run and per scenario. Earlier data and other scenarios can then never collide,
        while the same number used twice inside one scenario still collides (duplicate tests)."""
        if self.run.mode == "isolated":
            return invoice_number
        return f"{invoice_number}-Q{self.run.run_id}{SCENARIOS.index(self.scenario):02d}"

    # ---- recording ----------------------------------------------------------------------
    def expect(self, name: str, actual, op: str, expected, basis: str = "") -> bool:
        passed = bool(OPS[op](actual, expected))
        self.checks.append({"name": name, "op": op, "expected": expected, "actual": actual,
                            "passed": passed, "basis": basis})
        return passed

    def observe(self, name: str, value, note: str = "") -> None:
        """Record behaviour that has no agreed expectation. Never counts as a pass or a failure."""
        self.observations.append({"name": name, "value": value, "note": note})

    def require(self, condition: bool, reason: str) -> None:
        if not condition:
            raise Blocked(reason)

    # ---- state the scenarios depend on (read fresh every time: approvals change averages) ---
    def _state(self):
        vendors = self.run.client.request("GET", "/vendors").json()
        pos = self.run.client.request("GET", "/purchase-orders").json()
        return {v["name"]: v for v in vendors}, {p["po_number"]: p for p in pos}

    def vendor(self, name: str) -> dict | None:
        return self._state()[0].get(name)

    def po(self, number: str) -> dict | None:
        return self._state()[1].get(number)

    def need_vendor(self, name: str, approved: bool | None = None) -> dict:
        v = self.vendor(name)
        self.require(v is not None, f"vendor '{name}' is not in the database (seed data missing)")
        if approved is not None:
            self.require(v["approved"] == approved, f"vendor '{name}' approved={v['approved']}, scenario needs {approved}")
        return v

    def need_po(self, number: str, vendor_name: str, amount: float) -> dict:
        vendors, pos = self._state()
        po = pos.get(number)
        self.require(po is not None, f"purchase order {number} is not in the database (seed data missing)")
        self.require(po["status"] == "open", f"{number} is {po['status']}, scenario needs it open")
        self.require(abs(po["amount"] - amount) < 0.005, f"{number} amount is {po['amount']}, scenario assumes {amount}")
        owner = vendors.get(vendor_name)
        self.require(owner is not None and po["vendor_id"] == owner["id"], f"{number} does not belong to '{vendor_name}'")
        return po

    # ---- HTTP -----------------------------------------------------------------------------
    def call(self, method: str, path: str, *, json_body=_UNSET, data=None, upload=None):
        kwargs: dict = {}
        curl = ["curl", "-s", "-X", method, f'"$BASE{path}"']
        setup = None
        if json_body is not _UNSET:
            kwargs["json"] = json_body
            curl += ["-H", "'Content-Type: application/json'", "-d", shlex.quote(json.dumps(json_body))]
        if data is not None:
            kwargs["data"] = data
            for key, value in data.items():
                curl += ["--data-urlencode", _ansi_c(f"{key}={value}")]
        if upload is not None:
            if isinstance(upload, (str, Path)):
                path_obj = Path(upload)
                name, content = path_obj.name, path_obj.read_bytes()
                try:
                    shown = path_obj.resolve().relative_to(ROOT).as_posix()
                except ValueError:
                    shown = str(path_obj)
            else:
                name, content = upload
                shown = f"/tmp/{name}"
                try:
                    setup = f"printf %s {_ansi_c(content.decode('utf-8'))} > {shown}"
                except UnicodeDecodeError:
                    setup = f"# {shown}: {len(content)} bytes of binary test data built by the scenario (see qa/run_scenarios.py)"
            kwargs["files"] = {"file": (name, content, "application/octet-stream")}
            curl += ["-F", shlex.quote(f"file=@{shown}")]
        resp = self.run.client.request(method, path, **kwargs)
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:2000]
        self.calls.append({"method": method, "path": path, "status": resp.status_code,
                           "curl": " ".join(curl), "setup": setup})
        return resp.status_code, body

    def _pipeline(self, label: str, status: int, body) -> dict:
        self.expect(f"{label}: HTTP status", status, "eq", 200, "the pipeline must not crash on an invoice")
        if status != 200:
            self.observe(f"{label}: error body", body)
            raise Abort(label)
        self.expect(
            f"{label}: human gate (pending_review, no decision recorded)",
            {"status": body.get("status"), "decided_by": body.get("decided_by"), "decided_at": body.get("decided_at")},
            "eq",
            {"status": "pending_review", "decided_by": None, "decided_at": None},
            "core principle: AI analyzes, humans authorize (OrchestrateAgent.run_pipeline)",
        )
        self.observe(f"{label}: extraction_method", (body.get("extracted_data") or {}).get("extraction_method"))
        return body

    def submit(self, raw_text: str, label: str = "submit") -> dict:
        status, body = self.call("POST", "/invoices/submit-text", data={"raw_text": raw_text})
        return self._pipeline(label, status, body)

    def upload(self, file, label: str = "upload") -> dict:
        status, body = self.call("POST", "/invoices/upload", upload=file)
        return self._pipeline(label, status, body)

    def fetch(self, invoice_id: int) -> dict:
        status, body = self.call("GET", f"/invoices/{invoice_id}")
        if status != 200:
            self.expect(f"GET /invoices/{invoice_id}", status, "eq", 200)
            raise Abort("fetch")
        return body

    def decide(self, invoice_id: int, decision: str, body=_UNSET):
        if body is _UNSET:
            body = {"decided_by": REVIEWER, "note": f"QA {decision}"}
        return self.call("POST", f"/invoices/{invoice_id}/{decision}", json_body=body)

    # ---- shared assertions ------------------------------------------------------------------
    def expect_risk(self, inv: dict, label: str, score: int, basis: str = "README section 4 risk scoring"):
        self.expect(f"{label}: risk_score", assessment(inv).get("risk_score"), "eq", score, basis)
        self.expect(f"{label}: risk_level", inv.get("risk_level"), "eq", level_for(score), basis)


def context(inv): return inv.get("retrieval_context") or {}
def validation(inv): return inv.get("validation_result") or {}
def assessment(inv): return inv.get("assessment_result") or {}
def extracted(inv): return inv.get("extracted_data") or {}
def issues(inv): return validation(inv).get("issues") or []
def flags(inv): return assessment(inv).get("flags") or []
def actions(inv): return sorted(f"{a['agent_name']}.{a['action']}" for a in inv.get("audit_logs") or [])


SCENARIOS: list[dict] = []


def scenario(id: str, area: str, title: str, basis: str, kind: str = "check",
             isolated_only: bool = False, needs_approve: bool = False):
    """kind='check' asserts against the spec. kind='probe' records behaviour that has no agreed
    expectation yet (candidate gaps), so it can never be reported as a pass."""
    def register(fn):
        SCENARIOS.append({"id": id, "area": area, "title": title, "basis": basis, "kind": kind,
                          "isolated_only": isolated_only, "needs_approve": needs_approve, "fn": fn})
        return fn
    return register


# --------------------------------------------------------------------------- preconditions

def pre_acme(t: T):
    v = t.need_vendor(ACME, approved=True)
    t.need_po("PO-1001", ACME, 1250.0)
    t.require(v["avg_invoice_amount"] > 0 and 1250.0 / v["avg_invoice_amount"] < t.cfg["multiplier"],
              f"Acme average is {v['avg_invoice_amount']}; $1,250 would trip the anomaly rule")
    return v


def pre_meridian(t: T):
    v = t.need_vendor(MERIDIAN, approved=True)
    t.need_po("PO-1003", MERIDIAN, 2000.0)
    t.require(v["avg_invoice_amount"] > 0 and 2000.0 / v["avg_invoice_amount"] < t.cfg["multiplier"],
              f"Meridian average is {v['avg_invoice_amount']}; $2,000 would trip the anomaly rule")
    return v


def pre_brightline(t: T):
    v = t.need_vendor(BRIGHT, approved=True)
    t.need_po("PO-1002", BRIGHT, 4500.0)
    t.require(v["avg_invoice_amount"] > 0, "Brightline has no historical average")
    return v


def clean_acme(t: T, number="INV-9001") -> str:
    return invoice_text(t.n(number), ACME, ACME_ITEMS, 1250.0)


def clean_meridian(t: T, number="INV-9200") -> str:
    return invoice_text(t.n(number), MERIDIAN, [("Cloud hosting monthly", 1, 2000.0)], 2000.0,
                        po="PO-1003", due="2026-10-25")


def brightline(t: T, amount: float, number="INV-9050") -> str:
    return invoice_text(t.n(number), BRIGHT, [("Freight shipping Q3", 1, amount)], amount, due="2026-11-01")


def unknown_vendor_text(t: T, number="INV-9300") -> str:
    return invoice_text(t.n(number), "Northwind Phantom Traders", [("Consulting retainer", 1, 3000.0)], 3000.0)


# =========================================================================== CLEAN

@scenario("clean_amount_proximity", "clean", "Clean invoice, no PO number cited (matched by amount)",
          "seed_data.SAMPLE_INVOICES clean_match; README section 4")
def _(t: T):
    pre_acme(t)
    inv = t.submit(clean_acme(t))
    t.expect("vendor resolved", context(inv).get("vendor_found"), "eq", True)
    t.expect("PO match method", context(inv).get("po_match_method"), "eq", "amount_proximity",
             "RetrieveAgent fallback when no PO number is cited")
    t.expect("matched PO", context(inv).get("matched_po_number"), "eq", "PO-1001")
    t.expect("no duplicates found", context(inv).get("duplicate_invoice_ids"), "eq", [])
    t.expect("validation passed", validation(inv).get("passed"), "eq", True)
    t.expect("validation issues", issues(inv), "eq", [])
    t.expect("risk flags", flags(inv), "eq", [])
    t.expect_risk(inv, "clean invoice", 0)
    full = t.fetch(inv["id"])
    t.expect("audit trail (one entry per agent step, no alert)", actions(full), "eq", sorted([
        "orchestrate_agent.pipeline_started", "extract_agent.extracted_fields",
        "retrieve_agent.retrieved_context", "validate_agent.validated",
        "assess_agent.assessed", "orchestrate_agent.pipeline_completed"]),
        "BaseAgent.log: every agent records what it did")


@scenario("clean_explicit_po", "clean", "Clean invoice citing its own PO number",
          "seed_data.SAMPLE_INVOICES po_number_direct_match")
def _(t: T):
    pre_meridian(t)
    inv = t.submit(clean_meridian(t))
    t.expect("PO match method", context(inv).get("po_match_method"), "eq", "po_number",
             "RetrieveAgent prefers an explicit PO reference")
    t.expect("matched PO", context(inv).get("matched_po_number"), "eq", "PO-1003")
    t.expect("PO/vendor mismatch", context(inv).get("po_vendor_mismatch"), "eq", False)
    t.expect("validation passed", validation(inv).get("passed"), "eq", True)
    t.expect_risk(inv, "clean invoice with PO", 0)


# =========================================================================== DUPLICATES

@scenario("duplicate_same_vendor", "duplicate", "Same invoice number from the same vendor, submitted twice",
          "README section 4: duplicate invoice number +50")
def _(t: T):
    pre_acme(t)
    first = t.submit(clean_acme(t), "first submission")
    t.expect_risk(first, "first submission", 0)
    second = t.submit(clean_acme(t), "second submission")
    t.expect("duplicate_invoice_ids points at the first invoice", context(second).get("duplicate_invoice_ids"), "eq", [first["id"]])
    t.expect("duplicate flag raised", flags(second), "contains", "duplicate")
    t.expect_risk(second, "second submission", W["duplicate"])
    full = t.fetch(second["id"])
    t.expect("high-risk alert logged by monitor_agent", actions(full), "contains", "monitor_agent.alert_raised",
             "MonitorAgent.alert_if_high_risk")
    t.observe("first invoice after the duplicate arrived",
              {"risk_level": t.fetch(first["id"]).get("risk_level")},
              "The earlier invoice is not re-assessed when a duplicate shows up.")


@scenario("duplicate_number_other_vendor", "duplicate", "Same invoice number from a different vendor is not a duplicate",
          "RetrieveAgent: duplicates are scoped to the vendor ('this invoice number from this vendor')")
def _(t: T):
    pre_acme(t)
    pre_meridian(t)
    t.submit(clean_acme(t, "INV-7700"), "Acme invoice")
    other = t.submit(clean_meridian(t, "INV-7700"), "Meridian invoice, same number")
    t.expect("no duplicate ids", context(other).get("duplicate_invoice_ids"), "eq", [])
    t.expect("no duplicate flag", flags(other), "not_contains", "duplicate")
    t.expect_risk(other, "Meridian invoice", 0)


@scenario("duplicate_case_insensitive", "duplicate", "The same invoice number in a different letter case is a duplicate",
          "Decision 2026-10-05 (probe review): duplicate detection compares invoice numbers case-insensitively")
def _(t: T):
    pre_acme(t)
    first = t.submit(clean_acme(t, "INV-9001"), "original")
    second = t.submit(clean_acme(t, "inv-9001"), "lower-case resubmission")
    t.expect("duplicate_invoice_ids points at the original", context(second).get("duplicate_invoice_ids"), "eq", [first["id"]])
    t.expect("duplicate flag raised", flags(second), "contains", "duplicate")
    t.expect_risk(second, "lower-case resubmission", W["duplicate"])


@scenario("probe_duplicate_format_variants", "duplicate", "Invoice numbers that differ by a hyphen or a leading zero",
          "No agreed expectation: by decision (2026-10-05) no normalisation beyond letter case was introduced", kind="probe")
def _(t: T):
    pre_acme(t)
    for label, original, variant in (("hyphen removed", "INV-9001", "INV9001"), ("leading zero added", "INV-9011", "INV-09011")):
        t.submit(clean_acme(t, original), f"original {original}")
        second = t.submit(clean_acme(t, variant), label)
        t.observe(f"{label}: {variant} after {original}", {
            "duplicate_invoice_ids": context(second).get("duplicate_invoice_ids"), "risk_level": second.get("risk_level")},
            "Not flagged: only letter case is ignored when comparing invoice numbers.")


@scenario("probe_duplicate_after_rejection", "duplicate", "Resubmitting an invoice that a human already rejected",
          "No agreed expectation: the duplicate query does not look at status", kind="probe")
def _(t: T):
    pre_acme(t)
    first = t.submit(clean_acme(t), "original")
    status, _body = t.decide(first["id"], "reject")
    t.expect("rejection accepted", status, "eq", 200)
    second = t.submit(clean_acme(t), "resubmission after rejection")
    t.observe("resubmission after rejection", {
        "duplicate_invoice_ids": context(second).get("duplicate_invoice_ids"),
        "risk_level": second.get("risk_level")},
        "A corrected invoice resubmitted after rejection is treated exactly like a duplicate payment attempt.")


# =========================================================================== AMOUNT

@scenario("amount_anomaly_seed_example", "amount", "Amount far above the vendor's historical average",
          "README section 4: amount >= ANOMALY_MULTIPLIER x average +30; PO amount mismatch +20")
def _(t: T):
    v = pre_brightline(t)
    t.require(15000.0 / v["avg_invoice_amount"] >= t.cfg["multiplier"],
              f"Brightline average {v['avg_invoice_amount']} with multiplier {t.cfg['multiplier']}: $15,000 is not an anomaly")
    inv = t.submit(brightline(t, 15000.0))
    t.expect("anomaly flag raised", flags(inv), "contains", "historical average")
    t.expect("PO amount issue raised", issues(inv), "contains", "differs from PO")
    t.expect("validation failed", validation(inv).get("passed"), "eq", False)
    t.expect("exactly one validation issue", len(issues(inv)), "eq", 1)
    t.expect_risk(inv, "anomalous invoice", W["amount_anomaly"] + W["validation_issue"])


@scenario("amount_anomaly_at_threshold", "amount", "Amount exactly at multiplier x average is flagged (>=)",
          "README section 4: 'at least 2.5x'; AssessAgent uses >=", isolated_only=True)
def _(t: T):
    v = pre_brightline(t)
    amount = round(v["avg_invoice_amount"] * t.cfg["multiplier"], 2)
    t.require(amount / v["avg_invoice_amount"] >= t.cfg["multiplier"], "threshold amount is not representable in cents")
    inv = t.submit(brightline(t, amount))
    t.observe("amount used", {"amount": amount, "average": v["avg_invoice_amount"], "multiplier": t.cfg["multiplier"]})
    t.expect("anomaly flag raised at the threshold", flags(inv), "contains", "historical average")
    t.expect_risk(inv, "at threshold", W["amount_anomaly"] + W["validation_issue"])


@scenario("amount_anomaly_just_below", "amount", "One cent below the threshold is not an anomaly",
          "README section 4; AssessAgent uses >=", isolated_only=True)
def _(t: T):
    v = pre_brightline(t)
    amount = round(v["avg_invoice_amount"] * t.cfg["multiplier"] - 0.01, 2)
    inv = t.submit(brightline(t, amount))
    t.expect("no anomaly flag below the threshold", flags(inv), "not_contains", "historical average")
    t.expect("PO amount issue still raised", issues(inv), "contains", "differs from PO")
    t.expect_risk(inv, "just below threshold", W["validation_issue"])


@scenario("amount_anomaly_only_is_medium", "amount", "Anomaly with a matching PO scores 30 (medium)",
          "README section 4: anomaly alone is +30, which is MEDIUM (20-49)", isolated_only=True)
def _(t: T):
    v = t.need_vendor(STERLING, approved=True)
    amount = round(v["avg_invoice_amount"] * (t.cfg["multiplier"] + 0.5), 2)
    status, _po = t.call("POST", "/purchase-orders", json_body={
        "po_number": "PO-QA-2001", "vendor_name": STERLING, "amount": amount,
        "line_items": [{"description": "Trade show banners", "qty": 12, "unit_price": amount / 12}]})
    t.expect("fixture PO created", status, "eq", 200)
    if status != 200:
        raise Abort("fixture")
    inv = t.submit(invoice_text(t.n("INV-9400"), STERLING, [("Trade show banners", 12, amount / 12)], amount, po="PO-QA-2001"))
    t.expect("validation passed (PO matches)", validation(inv).get("passed"), "eq", True)
    t.expect("anomaly flag raised", flags(inv), "contains", "historical average")
    t.expect_risk(inv, "anomaly only", W["amount_anomaly"])


# =========================================================================== VENDORS

@scenario("vendor_unapproved", "vendor", "Known but unapproved vendor with a compliance hold",
          "seed_data.SAMPLE_INVOICES unapproved_vendor; supplier_kb policy note; +20 per validation issue")
def _(t: T):
    v = t.need_vendor(SHADOW, approved=False)
    inv = t.submit(invoice_text(t.n("INV-9099"), SHADOW, [("Strategy consulting", 1, 8000.0)], 8000.0, due="2026-10-20"))
    t.expect("vendor found", context(inv).get("vendor_found"), "eq", True)
    t.expect("vendor_approved is false", context(inv).get("vendor_approved"), "eq", False)
    t.expect("issue: not on approved list", issues(inv), "contains", "not on the approved vendor list")
    t.expect("issue: supplier policy note surfaced", issues(inv), "contains", "supplier policy note")
    t.expect("issue: no purchase order", issues(inv), "contains", "no matching purchase order")
    t.expect("exactly three validation issues", len(issues(inv)), "eq", 3)
    t.expect("validation failed", validation(inv).get("passed"), "eq", False)
    if v["avg_invoice_amount"] == 0:
        t.expect("no anomaly flag without history", flags(inv), "not_contains", "historical average")
        t.expect_risk(inv, "unapproved vendor", 3 * W["validation_issue"])
    else:
        t.expect("risk_level", inv.get("risk_level"), "eq", "high")


@scenario("vendor_unknown", "vendor", "Vendor that does not exist in the system",
          "README section 4: first-time / unknown vendor +15, plus +20 per validation issue")
def _(t: T):
    t.require(t.vendor("Northwind Phantom Traders") is None, "the 'unknown' vendor name already exists in this database")
    inv = t.submit(unknown_vendor_text(t))
    t.expect("vendor_found is false", context(inv).get("vendor_found"), "eq", False)
    t.expect("no vendor linked", inv.get("vendor_id"), "eq", None)
    t.expect("flag: first-time payee", flags(inv), "contains", "first-time payee")
    t.expect("issue: vendor not found", issues(inv), "contains", "vendor not found")
    t.expect("issue: no purchase order", issues(inv), "contains", "no matching purchase order")
    t.expect("exactly two validation issues", len(issues(inv)), "eq", 2)
    t.expect_risk(inv, "unknown vendor", W["unknown_vendor"] + 2 * W["validation_issue"])


@scenario("vendor_name_case_insensitive", "vendor", "Vendor lookup ignores letter case",
          "RetrieveAgent uses a case-insensitive name match")
def _(t: T):
    pre_acme(t)
    inv = t.submit(invoice_text(t.n("INV-9310"), ACME.upper(), ACME_ITEMS, 1250.0))
    t.expect("vendor resolved despite upper-case name", context(inv).get("vendor_found"), "eq", True)
    t.observe("supplier profile found for the upper-case name", context(inv).get("supplier_profile") is not None)
    t.expect("risk_level", inv.get("risk_level"), "eq", "low")


@scenario("vendor_wildcard_name", "vendor", "A vendor name made of SQL wildcards must not resolve to a real vendor",
          "Safety: vendor identity must come from a real name match (RetrieveAgent passes the extracted name to ilike unescaped)")
def _(t: T):
    pre_acme(t)
    inv = t.submit(invoice_text(t.n("INV-9320"), "%", ACME_ITEMS, 1250.0), "vendor name '%'")
    t.observe("how '%' was resolved", {"vendor_found": context(inv).get("vendor_found"), "vendor_id": inv.get("vendor_id"),
                                       "risk_level": inv.get("risk_level"), "validation_passed": validation(inv).get("passed")})
    t.expect("vendor '%' is not found", context(inv).get("vendor_found"), "eq", False)
    t.expect("vendor '%' is not low risk", inv.get("risk_level"), "ne", "low")
    inv2 = t.submit(invoice_text(t.n("INV-9321"), "Acme_Office_Supplies", ACME_ITEMS, 1250.0), "vendor name with '_'")
    t.expect("vendor 'Acme_Office_Supplies' is not found", context(inv2).get("vendor_found"), "eq", False)
    inv3 = t.submit(invoice_text(t.n("INV-9322"), "Acme%", ACME_ITEMS, 1250.0), "vendor name 'Acme%'")
    t.expect("vendor 'Acme%' is not found", context(inv3).get("vendor_found"), "eq", False)
    t.expect("vendor 'Acme%' is not low risk", inv3.get("risk_level"), "ne", "low")


@scenario("vendor_unlisted_payee_citing_valid_po", "vendor",
          "A vendor name that is not on file is never replaced by the vendor that owns the cited PO",
          "Decision 2026-10-05 (probe review): keep the vendor unresolved and raise the PO/vendor mismatch; "
          "score = unknown vendor +15 and PO/vendor mismatch +50")
def _(t: T):
    before = pre_meridian(t)
    cases = [("unlisted payee", "Northwind Phantom Traders", "INV-9330"),
             ("misspelled vendor", "Meridian Cloud Servces", "INV-9331")]
    for label, name, number in cases:
        t.require(t.vendor(name) is None, f"the vendor name '{name}' already exists in this database")
        inv = t.submit(invoice_text(t.n(number), name, [("Cloud hosting monthly", 1, 2000.0)], 2000.0, po="PO-1003"), label)
        t.expect(f"{label}: vendor_found is false", context(inv).get("vendor_found"), "eq", False)
        t.expect(f"{label}: no vendor linked to the invoice", inv.get("vendor_id"), "eq", None)
        t.expect(f"{label}: po_vendor_mismatch raised", context(inv).get("po_vendor_mismatch"), "eq", True)
        t.expect(f"{label}: validation failed", validation(inv).get("passed"), "eq", False)
        t.expect(f"{label}: issue names the cross-vendor PO", issues(inv), "contains", "different vendor")
        t.expect(f"{label}: flag PO vendor mismatch", flags(inv), "contains", "po vendor mismatch")
        t.expect_risk(inv, label, W["unknown_vendor"] + W["po_vendor_mismatch"])
    after = t.vendor(MERIDIAN)
    t.expect("the PO owner's history is untouched", {"avg": after["avg_invoice_amount"], "count": after["invoice_count"]},
             "eq", {"avg": before["avg_invoice_amount"], "count": before["invoice_count"]})


# =========================================================================== PURCHASE ORDERS

@scenario("po_vendor_mismatch", "po", "Invoice cites a PO that belongs to a different vendor",
          "Commit 5ab765b + tests/test_backend_regression.py: cross-vendor PO reference is high risk (score >= 50)")
def _(t: T):
    t.need_vendor(STERLING, approved=True)
    pre_meridian(t)
    inv = t.submit(invoice_text(t.n("INV-9201"), STERLING, [("Trade show banners", 4, 200.0)], 800.0,
                                po="PO-1003", due="2026-10-28"))
    t.expect("po_vendor_mismatch detected", context(inv).get("po_vendor_mismatch"), "eq", True)
    t.expect("matched by PO number", context(inv).get("po_match_method"), "eq", "po_number")
    t.expect("validation failed", validation(inv).get("passed"), "eq", False)
    t.expect("issue names the cross-vendor PO", issues(inv), "contains", "different vendor")
    t.expect("flag: PO vendor mismatch", flags(inv), "contains", "po vendor mismatch")
    t.expect("risk_score >= 50", assessment(inv).get("risk_score"), "ge", W["po_vendor_mismatch"])
    t.expect("risk_level", inv.get("risk_level"), "eq", "high")
    t.observe("exact score", assessment(inv).get("risk_score"),
              "The mismatch branch replaces the +20-per-issue rule rather than adding to it.")


@scenario("po_number_exact_match", "po", "A cited PO number matches exactly, ignoring only letter case and surrounding whitespace",
          "Release check 2026-10-05: the original lookup ignored case (ilike) and trimmed the cited number; "
          "partial and wildcard matches are never allowed")
def _(t: T):
    pre_meridian(t)
    items = [("Cloud hosting monthly", 1, 2000.0)]

    def cite(number: str, po_line: str) -> dict:
        text = invoice_text(t.n(number), MERIDIAN, items, 2000.0, po="@@").replace("PO Number: @@", po_line)
        return t.submit(text, po_line.strip())

    for number, po_line in (("INV-9220", "PO Number: po-1003"), ("INV-9221", "PO Number:     PO-1003   ")):
        inv = cite(number, po_line)
        t.expect(f"{po_line.strip()!r}: matched by PO number", context(inv).get("po_match_method"), "eq", "po_number")
        t.expect(f"{po_line.strip()!r}: matched PO", context(inv).get("matched_po_number"), "eq", "PO-1003")
        t.expect_risk(inv, repr(po_line.strip()), 0)
    # "PO-100_" and "PO-%" reach the lookup as "PO-100" and "PO-": the regex extractor stops at the first
    # character that is not a letter, digit or hyphen. Wildcards themselves are covered at unit level.
    for number, po_line, cited in (("INV-9222", "PO Number: PO-100", "PO-100"), ("INV-9223", "PO Number: PO-100_", "PO-100"),
                                   ("INV-9224", "PO Number: PO-%", "PO-"), ("INV-9225", "PO Number: PO-10031", "PO-10031")):
        t.require(t.po(cited) is None, f"{cited} exists in this database")
        inv = cite(number, po_line)
        t.expect(f"{po_line!r}: no PO matched", context(inv).get("matched_po_number"), "eq", None)
        t.expect(f"{po_line!r}: no match method", context(inv).get("po_match_method"), "eq", None)
        t.expect(f"{po_line!r}: issue names the cited number", issues(inv), "contains", f"cites PO {cited},")
        t.expect_risk(inv, repr(po_line), W["validation_issue"])


@scenario("po_number_case_variant_cannot_be_created", "po", "A PO number that differs from an existing one only by letter case is refused",
          "Release check 2026-10-05: lookup ignores case, so case-variant PO numbers would make a citation ambiguous",
          isolated_only=True)
def _(t: T):
    pre_meridian(t)
    status, body = t.call("POST", "/purchase-orders", json_body={
        "po_number": "po-1003", "vendor_name": STERLING, "amount": 800.0, "line_items": []})
    t.expect("creating 'po-1003' while 'PO-1003' exists", status, "eq", 400)
    _s, pos = t.call("GET", "/purchase-orders")
    t.expect("no case-variant PO was stored", sorted(p["po_number"] for p in pos if p["po_number"].lower() == "po-1003"), "eq", ["PO-1003"])


@scenario("po_amount_mismatch_is_medium", "po", "Invoice 12% over its PO scores one validation issue (medium)",
          "ValidateAgent: amount differs from PO by more than the tolerance; README: +20 per issue")
def _(t: T):
    pre_acme(t)
    inv = t.submit(invoice_text(t.n("INV-9110"), ACME, [("Office chairs", 5, 150.0), ("Standing desks", 2, 325.0)], 1400.0))
    t.expect("issue: amount differs from PO", issues(inv), "contains", "differs from PO")
    t.expect("exactly one validation issue", len(issues(inv)), "eq", 1)
    t.expect("amount_diff_pct recorded", (validation(inv).get("checks") or {}).get("amount_diff_pct"), "approx", 12.0)
    t.expect_risk(inv, "amount mismatch", W["validation_issue"])


@scenario("po_tolerance_boundary", "po", "Exactly at the amount tolerance passes; just over fails",
          "ValidateAgent.AMOUNT_TOLERANCE_PCT: an issue is raised only when the difference is greater than the tolerance")
def _(t: T):
    pre_acme(t)
    at = round(1250.0 * (1 + SPEC["amount_tolerance"]), 2)
    over = at + 1.0
    inv_at = t.submit(invoice_text(t.n("INV-9120"), ACME, ACME_ITEMS, at), f"at tolerance (${at:.2f})")
    t.expect(f"${at:.2f} passes validation", validation(inv_at).get("passed"), "eq", True)
    t.expect_risk(inv_at, "at tolerance", 0)
    inv_over = t.submit(invoice_text(t.n("INV-9121"), ACME, ACME_ITEMS, over), f"over tolerance (${over:.2f})")
    t.expect(f"${over:.2f} raises the amount issue", issues(inv_over), "contains", "differs from PO")
    t.expect_risk(inv_over, "over tolerance", W["validation_issue"])


@scenario("po_line_item_mismatch", "po", "Invoice line item that is not on the PO",
          "ValidateAgent: line-item consistency against the PO; README: +20 per issue")
def _(t: T):
    pre_acme(t)
    inv = t.submit(invoice_text(t.n("INV-9130"), ACME, [("Ergonomic keyboards", 5, 150.0), ("Standing desks", 2, 250.0)], 1250.0))
    t.expect("unmatched line item listed", (validation(inv).get("checks") or {}).get("unmatched_line_items"), "eq", ["Ergonomic keyboards"])
    t.expect("issue: line item not on PO", issues(inv), "contains", "don't appear on the PO")
    t.expect_risk(inv, "line item mismatch", W["validation_issue"])


@scenario("po_cited_number_not_found", "po", "A cited PO number that does not exist is an issue, not an amount match",
          "Decision 2026-10-05 (probe review) and RetrieveAgent's own comments: amount proximity is only the fallback "
          "for invoices that cite no PO number")
def _(t: T):
    pre_meridian(t)
    t.require(t.po("PO-9999") is None, "PO-9999 exists in this database")
    inv = t.submit(invoice_text(t.n("INV-9210"), MERIDIAN, [("Cloud hosting monthly", 1, 2000.0)], 2000.0, po="PO-9999"))
    t.expect("the cited number was extracted", extracted(inv).get("po_number"), "eq", "PO-9999")
    t.expect("no PO matched", context(inv).get("matched_po_number"), "eq", None)
    t.expect("no amount-proximity fallback", context(inv).get("po_match_method"), "eq", None)
    t.expect("validation failed", validation(inv).get("passed"), "eq", False)
    t.expect("issue names the cited PO", issues(inv), "contains", "PO-9999")
    t.expect("exactly one validation issue", len(issues(inv)), "eq", 1)
    t.expect_risk(inv, "cited PO not found", W["validation_issue"])


@scenario("probe_line_items_do_not_add_up", "po", "Line-item quantities that do not add up to the stated total",
          "No agreed expectation: ValidateAgent compares line-item descriptions only", kind="probe")
def _(t: T):
    pre_acme(t)
    inv = t.submit(invoice_text(t.n("INV-9140"), ACME, [("Office chairs", 50, 150.0), ("Standing desks", 20, 250.0)], 1250.0))
    t.observe("items sum to $12,500 but total says $1,250", {
        "validation_passed": validation(inv).get("passed"), "issues": issues(inv), "risk_level": inv.get("risk_level")},
        "Quantities and unit prices are not checked against the PO or against the invoice total.")


# =========================================================================== EXTRACTION

@scenario("extraction_fields", "extraction", "Field extraction from the standard invoice layout",
          "llm_client extraction contract: vendor_name, invoice_number, po_number, amount, due_date, line_items")
def _(t: T):
    pre_acme(t)
    inv = t.submit(clean_acme(t))
    e = extracted(inv)
    t.expect("vendor_name", e.get("vendor_name"), "eq", ACME)
    t.expect("invoice_number", e.get("invoice_number"), "eq", t.n("INV-9001"))
    t.expect("po_number absent", e.get("po_number"), "eq", None)
    t.expect("amount", e.get("amount"), "approx", 1250.0)
    t.expect("due_date", e.get("due_date"), "eq", "2026-10-15")
    t.expect("line item descriptions", [li.get("description") for li in e.get("line_items") or []], "eq",
             ["Office chairs", "Standing desks"])
    t.expect("invoice.amount mirrors the extracted amount", inv.get("amount"), "approx", 1250.0)
    t.expect("invoice.invoice_number replaces the placeholder", inv.get("invoice_number"), "eq", t.n("INV-9001"))


@scenario("extraction_collapsed_single_line", "extraction", "Same invoice pasted as one line (line breaks collapsed)",
          "llm_client._deterministic_extract docstring: must hold up when line breaks collapse into spaces")
def _(t: T):
    pre_acme(t)
    inv = t.submit(clean_acme(t).replace("\n", " "))
    e = extracted(inv)
    t.expect("vendor_name", e.get("vendor_name"), "eq", ACME)
    t.expect("invoice_number", e.get("invoice_number"), "eq", t.n("INV-9001"))
    t.expect("amount", e.get("amount"), "approx", 1250.0)
    t.expect("line item descriptions", [li.get("description") for li in e.get("line_items") or []], "eq",
             ["Office chairs", "Standing desks"])
    t.expect_risk(inv, "collapsed invoice", 0)


@scenario("extraction_subtotal_tax_total", "extraction", "Subtotal / tax / total breakdown: the total wins",
          "llm_client comment on the 'Subtotal' substring trap; generate_realistic_invoices.py freight layout")
def _(t: T):
    pre_brightline(t)
    text = "\n".join([
        "From: Brightline Logistics", "Bill To: Acme Manufacturing (Customer)", f"Invoice #: {t.n('INV-7742')}",
        "PO#: PO-1002", "Date: 10/20/2026", "Due Date: 11/19/2026", "Freight shipping Q3 1 x $4,200.00",
        "Subtotal: $4,200.00", "Tax (7%): $294.00", "Total Amount Due: $4,494.00"])
    inv = t.submit(text)
    e = extracted(inv)
    t.expect("vendor from 'From:' (not 'Bill To:')", e.get("vendor_name"), "eq", BRIGHT)
    t.expect("invoice number from 'Invoice #:'", e.get("invoice_number"), "eq", t.n("INV-7742"))
    t.expect("PO number from 'PO#:'", e.get("po_number"), "eq", "PO-1002")
    t.expect("amount is the total, not the subtotal", e.get("amount"), "approx", 4494.0)
    t.expect("thousands separator parsed in line items", [li.get("unit_price") for li in e.get("line_items") or []], "eq", [4200.0])
    t.expect("within PO tolerance, validation passes", validation(inv).get("passed"), "eq", True)
    t.expect_risk(inv, "freight-style invoice", 0)


@scenario("extraction_unparseable_text", "extraction", "Text that is not an invoice must not come out low risk",
          "Principle: an invoice that cannot be verified must not look safe; the pipeline must not crash")
def _(t: T):
    inv = t.submit("hello team, please find attached the thing we discussed. thanks!")
    t.observe("what was extracted", {k: extracted(inv).get(k) for k in ("vendor_name", "invoice_number", "amount", "po_number")})
    t.expect("risk_level is not low", inv.get("risk_level"), "ne", "low")
    t.expect("validation failed", validation(inv).get("passed"), "eq", False)


@scenario("probe_missing_total", "extraction", "Invoice with no total line",
          "No agreed expectation: the deterministic extractor substitutes 0.0 for a missing amount", kind="probe")
def _(t: T):
    pre_acme(t)
    text = "\n".join(clean_acme(t, "INV-9150").split("\n")[:-1])
    inv = t.submit(text)
    t.observe("no 'Total' line", {"extracted_amount": extracted(inv).get("amount"), "invoice_amount": inv.get("amount"),
                                  "issues": issues(inv), "risk_level": inv.get("risk_level")},
              "A missing amount is stored as 0.0, which is indistinguishable from a real zero-value invoice.")


@scenario("input_blank_submission_rejected", "extraction", "Blank input is refused and no invoice record is created",
          "Decision 2026-10-05 (probe review): blank submissions get a 4xx instead of an UNKNOWN-INV invoice and an alert")
def _(t: T):
    _s, before = t.call("GET", "/dashboard/stats")
    for label, raw in (("empty raw_text", ""), ("whitespace-only raw_text", "   \n\t  ")):
        status, body = t.call("POST", "/invoices/submit-text", data={"raw_text": raw})
        t.expect(f"{label}: HTTP status", status, "eq", 422)
    status, body = t.call("POST", "/invoices/upload", upload=("qa_empty.txt", b""))
    t.expect("zero-byte .txt upload: HTTP status", status, "eq", 422)
    _s, after = t.call("GET", "/dashboard/stats")
    t.expect("no invoice record was created", after.get("total_invoices"), "eq", before.get("total_invoices"))


# =========================================================================== HUMAN GATE

@scenario("gate_pipeline_never_decides", "gate", "No pipeline outcome approves or rejects an invoice",
          "Core principle: AI analyzes, humans authorize. OrchestrateAgent docstring: every invoice lands at pending_review")
def _(t: T):
    pre_acme(t)
    for label, text in [
        ("low-risk invoice", clean_acme(t, "INV-9500")),
        ("medium-risk invoice", invoice_text(t.n("INV-9501"), ACME, [("Office chairs", 5, 150.0), ("Standing desks", 2, 325.0)], 1400.0)),
        ("high-risk invoice", unknown_vendor_text(t, "INV-9502")),
    ]:
        inv = t.submit(text, label)  # the pending_review / no-decision check is recorded by submit()
        full = t.fetch(inv["id"])
        human = [a for a in actions(full) if ".human_" in a]
        t.expect(f"{label}: no human_* entry in the audit trail", human, "eq", [])
    snapshot = impl.static_snapshot()
    writers = [f"{s['where']}: {s['target']} = {s['value']}" for s in snapshot["status_assignments"]]
    t.expect("static scan: the only code that writes .status",
             sorted(writers),
             "eq",
             sorted(["OrchestrateAgent.run_pipeline: invoice.status = 'pending_review'",
                     "OrchestrateAgent.apply_decision: invoice.status = decision"]),
             "Source scan of app/ (qa/inspect_implementation.py). A new writer is a new way past the gate.")
    t.expect("Invoice.status column default", snapshot["invoice_status_default"], "eq", "pending_review")


@scenario("gate_approve", "gate", "Human approval: recorded, audited, and fed back into vendor history",
          "OrchestrateAgent.apply_decision; README section 1: decisions record operator identity and notes",
          needs_approve=True)
def _(t: T):
    before = pre_acme(t)
    inv = t.submit(clean_acme(t))
    status, body = t.decide(inv["id"], "approve", {"decided_by": REVIEWER, "note": "QA approve: matches PO-1001"})
    t.expect("approve returns 200", status, "eq", 200)
    if status != 200:
        raise Abort("approve")
    t.expect("status", body.get("status"), "eq", "approved")
    t.expect("decided_by recorded", body.get("decided_by"), "eq", REVIEWER)
    t.expect("decided_at recorded", body.get("decided_at") is not None, "eq", True)
    full = t.fetch(inv["id"])
    entry = next((a for a in full.get("audit_logs") or [] if a["action"] == "human_approved"), None)
    t.expect("audit entry human_approved exists", entry is not None, "eq", True)
    t.expect("audit entry carries reviewer and note", (entry or {}).get("detail"), "eq",
             {"decided_by": REVIEWER, "note": "QA approve: matches PO-1001"})
    after = t.vendor(ACME)
    expected_avg = (before["avg_invoice_amount"] * before["invoice_count"] + 1250.0) / (before["invoice_count"] + 1)
    t.expect("vendor invoice_count + 1", after["invoice_count"], "eq", before["invoice_count"] + 1,
             "approved invoices feed the vendor's history")
    t.expect("vendor average updated", after["avg_invoice_amount"], "approx", expected_avg)
    status, _b = t.decide(inv["id"], "approve")
    t.expect("second approval refused (400)", status, "eq", 400, "a decided invoice cannot be decided again")
    status, _b = t.decide(inv["id"], "reject")
    t.expect("reject after approve refused (400)", status, "eq", 400)
    t.expect("status still approved", t.fetch(inv["id"]).get("status"), "eq", "approved")


@scenario("gate_reject", "gate", "Human rejection: recorded, audited, clears the alert, leaves vendor history alone",
          "OrchestrateAgent.apply_decision; MonitorAgent.get_active_alerts lists pending high-risk invoices only")
def _(t: T):
    before = pre_brightline(t)
    t.require(15000.0 / before["avg_invoice_amount"] >= t.cfg["multiplier"], "Brightline $15,000 is not high risk in this database")
    inv = t.submit(brightline(t, 15000.0))
    _s, alerts = t.call("GET", "/dashboard/alerts")
    t.expect("invoice is in /dashboard/alerts before the decision", inv["id"] in [a["invoice_id"] for a in alerts], "eq", True)
    status, body = t.decide(inv["id"], "reject", {"decided_by": REVIEWER, "note": "QA reject: 3.3x average"})
    t.expect("reject returns 200", status, "eq", 200)
    if status != 200:
        raise Abort("reject")
    t.expect("status", body.get("status"), "eq", "rejected")
    t.expect("decided_by recorded", body.get("decided_by"), "eq", REVIEWER)
    t.expect("decided_at recorded", body.get("decided_at") is not None, "eq", True)
    full = t.fetch(inv["id"])
    entry = next((a for a in full.get("audit_logs") or [] if a["action"] == "human_rejected"), None)
    t.expect("audit entry human_rejected carries reviewer and note", (entry or {}).get("detail"), "eq",
             {"decided_by": REVIEWER, "note": "QA reject: 3.3x average"})
    _s, alerts = t.call("GET", "/dashboard/alerts")
    t.expect("invoice left /dashboard/alerts after the decision", inv["id"] in [a["invoice_id"] for a in alerts], "eq", False)
    after = t.vendor(BRIGHT)
    t.expect("vendor history unchanged by a rejection",
             {"avg": after["avg_invoice_amount"], "count": after["invoice_count"]}, "eq",
             {"avg": before["avg_invoice_amount"], "count": before["invoice_count"]})
    status, _b = t.decide(inv["id"], "approve")
    t.expect("approve after reject refused (400)", status, "eq", 400, "a decided invoice cannot be decided again")
    status, _b = t.decide(inv["id"], "reject")
    t.expect("second rejection refused (400)", status, "eq", 400)
    final = t.fetch(inv["id"])
    t.expect("status still rejected, reviewer unchanged", {"status": final.get("status"), "decided_by": final.get("decided_by")},
             "eq", {"status": "rejected", "decided_by": REVIEWER})


@scenario("gate_unknown_invoice", "gate", "Deciding an invoice that does not exist returns 404",
          "invoices router: HTTPException(404, 'Invoice not found')")
def _(t: T):
    for decision in ("approve", "reject"):
        status, _b = t.decide(987654321, decision)
        t.expect(f"{decision} on a missing invoice", status, "eq", 404)


@scenario("gate_decided_by_required", "gate", "A decision without a non-blank decided_by is refused and decides nothing",
          "Decision 2026-10-05 (probe review): decided_by is required, non-blank, stored trimmed; "
          "README section 1: human decisions record operator identity")
def _(t: T):
    inv = t.submit(unknown_vendor_text(t, "INV-9601"))  # unknown vendor: no vendor history can be affected
    status, _body = t.call("POST", f"/invoices/{inv['id']}/reject")
    t.expect("reject with no request body", status, "eq", 422)
    for label, body in (("empty JSON object", {}), ("decided_by ''", {"decided_by": ""}),
                        ("decided_by whitespace", {"decided_by": "   "}), ("note but no decided_by", {"note": "no reviewer"})):
        for decision in ("approve", "reject"):
            status, _body = t.decide(inv["id"], decision, body)
            t.expect(f"{decision} with {label}", status, "eq", 422)
    current = t.fetch(inv["id"])
    t.expect("invoice is still undecided", {"status": current.get("status"), "decided_by": current.get("decided_by")},
             "eq", {"status": "pending_review", "decided_by": None})
    t.expect("no human_* entry in the audit trail", [a for a in actions(current) if ".human_" in a], "eq", [])
    status, body = t.decide(inv["id"], "reject", {"decided_by": f"  {REVIEWER}  ", "note": "QA reject"})
    t.expect("a named reviewer can still decide", status, "eq", 200)
    t.expect("decided_by is stored trimmed", body.get("decided_by") if isinstance(body, dict) else None, "eq", REVIEWER)


@scenario("probe_gate_identity_is_self_reported", "gate", "Reviewer identity is whatever the client sends",
          "No agreed expectation: by decision (2026-10-05) no authentication was added; identity is self-reported", kind="probe")
def _(t: T):
    inv = t.submit(unknown_vendor_text(t, "INV-9602"))
    status, body = t.decide(inv["id"], "reject", {"decided_by": "Chief Financial Officer", "note": "claimed identity"})
    t.observe("reject as an arbitrary claimed identity, with no credentials", {
        "http_status": status, "decided_by": body.get("decided_by") if isinstance(body, dict) else None},
        "Accepted. The API has no authentication, so the audit trail records a self-reported name.")


@scenario("probe_gate_high_risk_approval", "gate", "A human can approve a high-risk invoice with no extra step",
          "No agreed expectation: by design the human may approve anything; no second approver or mandatory note",
          kind="probe", needs_approve=True, isolated_only=True)
def _(t: T):
    inv = t.submit(unknown_vendor_text(t, "INV-9610"))
    status, body = t.decide(inv["id"], "approve", {"decided_by": REVIEWER})
    t.observe("approve a high-risk invoice from an unknown vendor, no note", {
        "risk_level_before": inv.get("risk_level"), "http_status": status,
        "status_after": body.get("status") if isinstance(body, dict) else None},
        "Allowed by design. Recommended hardening: require a note (or a second approver) for high-risk approvals.")


# =========================================================================== MONITOR

@scenario("monitor_alert_on_high_risk", "monitor", "High-risk invoices raise an alert; low-risk ones do not",
          "MonitorAgent.alert_if_high_risk and GET /dashboard/alerts")
def _(t: T):
    pre_acme(t)
    high = t.submit(unknown_vendor_text(t, "INV-9700"), "high-risk invoice")
    low = t.submit(clean_acme(t, "INV-9701"), "low-risk invoice")
    t.expect("high-risk invoice is high", high.get("risk_level"), "eq", "high")
    t.expect("low-risk invoice is low", low.get("risk_level"), "eq", "low")
    full = t.fetch(high["id"])
    entry = next((a for a in full.get("audit_logs") or [] if a["action"] == "alert_raised"), None)
    t.expect("alert_raised logged by monitor_agent", (entry or {}).get("agent_name"), "eq", "monitor_agent")
    t.expect("alert reason", ((entry or {}).get("detail") or {}).get("reason"), "eq", "high_risk_invoice_pending_review")
    if t.run.mode == "isolated":
        detail = (entry or {}).get("detail") or {}
        t.expect("no real notification left the machine during the test",
                 {"slack_sent": detail.get("slack_sent"), "email_sent": detail.get("email_sent")},
                 "eq", {"slack_sent": False, "email_sent": False}, "isolation: Slack/SMTP are disabled for test runs")
    t.expect("low-risk invoice has no alert entry", actions(t.fetch(low["id"])), "not_contains", "alert_raised")
    _s, alerts = t.call("GET", "/dashboard/alerts")
    ids = [a["invoice_id"] for a in alerts]
    t.expect("high-risk invoice listed in /dashboard/alerts", high["id"] in ids, "eq", True)
    t.expect("low-risk invoice not listed in /dashboard/alerts", low["id"] in ids, "eq", False)
    mine = next((a for a in alerts if a["invoice_id"] == high["id"]), {})
    t.expect("alert carries the risk flags", mine.get("flags"), "eq", flags(high))


@scenario("monitor_dashboard_stats", "monitor", "Dashboard counters follow submissions and decisions",
          "MonitorAgent.compute_dashboard_stats", isolated_only=True)
def _(t: T):
    pre_acme(t)
    pre_brightline(t)
    keys = ("total_invoices", "pending_review", "approved", "rejected", "high_risk_open")
    _s, stats = t.call("GET", "/dashboard/stats")
    t.expect("empty database", {k: stats.get(k) for k in keys},
             "eq", {"total_invoices": 0, "pending_review": 0, "approved": 0, "rejected": 0, "high_risk_open": 0})
    clean = t.submit(clean_acme(t), "clean")
    anomaly = t.submit(brightline(t, 15000.0), "anomaly")
    unknown = t.submit(unknown_vendor_text(t), "unknown vendor")
    _s, stats = t.call("GET", "/dashboard/stats")
    t.expect("after three submissions", {k: stats.get(k) for k in keys},
             "eq", {"total_invoices": 3, "pending_review": 3, "approved": 0, "rejected": 0, "high_risk_open": 2})
    t.expect("total_amount_pending", stats.get("total_amount_pending"), "approx", 1250.0 + 15000.0 + 3000.0)
    t.expect("flagged_reasons is populated", len(stats.get("flagged_reasons") or {}) > 0, "eq", True)
    t.decide(anomaly["id"], "reject")
    t.decide(clean["id"], "approve")
    _s, stats = t.call("GET", "/dashboard/stats")
    t.expect("after one rejection and one approval", {k: stats.get(k) for k in keys},
             "eq", {"total_invoices": 3, "pending_review": 1, "approved": 1, "rejected": 1, "high_risk_open": 1})
    t.expect("total_amount_pending counts only pending invoices", stats.get("total_amount_pending"), "approx", 3000.0)
    for status_name, expected_id in (("pending_review", unknown["id"]), ("approved", clean["id"]), ("rejected", anomaly["id"])):
        _s, rows = t.call("GET", f"/invoices?status={status_name}")
        t.expect(f"GET /invoices?status={status_name}", [r["id"] for r in rows], "eq", [expected_id])


# =========================================================================== PDF UPLOAD + OCR

PDF_DIR = ROOT / "sample_invoices_pdf"


def _pdf(name: str) -> Path:
    path = PDF_DIR / name
    if not path.exists():
        raise Skip(f"sample_invoices_pdf/{name} is not present (the folder is gitignored; regenerate it with "
                   "generate_synthetic_pdfs.py / generate_realistic_invoices.py, which need reportlab)")
    return path


def _pdf_case(name, title, basis, *, score=None, level=None, vendor=None, number=None, amount=None, po_number=_UNSET,
              method=None, flag=None, issue=None, upload_first=None, ocr=False, extra=None):
    @scenario(f"pdf_{name.removeprefix('invoice_').removesuffix('.pdf')}", "pdf", title, basis, isolated_only=True)
    def _(t: T):
        path = _pdf(name)
        if ocr:
            missing = [b for b in ("tesseract", "pdftoppm") if shutil.which(b) is None]
            missing += [m for m in ("pytesseract", "pdf2image") if importlib.util.find_spec(m) is None]
            if missing:
                raise Skip(f"OCR path needs {', '.join(missing)} (tesseract-ocr and poppler-utils binaries, plus the Python packages)")
        if upload_first:
            t.upload(_pdf(upload_first), f"upload {upload_first} first")
        inv = t.upload(path, f"upload {name}")
        e = extracted(inv)
        if vendor is not None:
            t.expect("extracted vendor_name", e.get("vendor_name"), "eq", vendor)
        if number is not None:
            t.expect("extracted invoice_number", e.get("invoice_number"), "eq", number)
        if amount is not None:
            t.expect("extracted amount", e.get("amount"), "approx", amount)
        if po_number is not _UNSET:
            t.expect("extracted po_number", e.get("po_number"), "eq", po_number)
        if method is not None:
            t.expect("PO match method", context(inv).get("po_match_method"), "eq", method)
        if flag is not None:
            t.expect(f"flag contains '{flag}'", flags(inv), "contains", flag)
        if issue is not None:
            t.expect(f"issue contains '{issue}'", issues(inv), "contains", issue)
        if score is not None:
            t.expect_risk(inv, name, score)
        if level is not None:
            t.expect("risk_level", inv.get("risk_level"), "eq", level)
        if extra:
            extra(t, inv)
    return _


_pdf_case("invoice_clean_match.pdf", "PDF: clean match (Acme, PO-1001 by amount)", "generate_synthetic_pdfs.py #1: low risk",
          score=0, vendor=ACME, number="INV-9001", amount=1250.0, po_number=None, method="amount_proximity")
_pdf_case("invoice_clean_brightline.pdf", "PDF: second clean invoice (Brightline)", "generate_synthetic_pdfs.py #5: low risk",
          score=0, vendor=BRIGHT, number="INV-9051", amount=4500.0)
_pdf_case("invoice_duplicate.pdf", "PDF: duplicate of the clean match", "generate_synthetic_pdfs.py #2: upload after the clean match",
          score=W["duplicate"], number="INV-9001", flag="duplicate", upload_first="invoice_clean_match.pdf")
_pdf_case("invoice_amount_anomaly.pdf", "PDF: amount anomaly (Brightline, $15,000)", "generate_synthetic_pdfs.py #3: anomaly + PO amount mismatch",
          score=W["amount_anomaly"] + W["validation_issue"], amount=15000.0, flag="historical average", issue="differs from PO")
_pdf_case("invoice_unapproved_vendor.pdf", "PDF: unapproved vendor (Shadow Consulting)", "generate_synthetic_pdfs.py #4",
          score=3 * W["validation_issue"], vendor=SHADOW, issue="not on the approved vendor list")
_pdf_case("invoice_po_number_match.pdf", "PDF: explicit PO number match (Meridian, PO-1003)", "generate_synthetic_pdfs.py #6",
          score=0, vendor=MERIDIAN, po_number="PO-1003", method="po_number")
_pdf_case("invoice_po_vendor_mismatch.pdf", "PDF: PO belongs to a different vendor", "generate_synthetic_pdfs.py #7",
          level="high", vendor=STERLING, po_number="PO-1003", flag="po vendor mismatch", issue="different vendor")
_pdf_case("invoice_freight_style.pdf", "PDF: freight layout (From:/Invoice #:/PO#:, subtotal + tax)",
          "generate_realistic_invoices.py: the total must win over the subtotal; 'Bill To' is not the vendor",
          score=0, vendor=BRIGHT, number="INV-7742", amount=4494.0, po_number="PO-1002", method="po_number")
_pdf_case("invoice_marketing_style.pdf", "PDF: marketing layout (Grand Total, written-out date, unlisted vendor)",
          "generate_realistic_invoices.py: must not crash; line items in this layout are a documented limitation",
          score=W["unknown_vendor"] + 2 * W["validation_issue"], vendor="Skyline Marketing Co", number="SM-2026-118", amount=1200.0,
          extra=lambda t, inv: t.observe("known limitation: fields this layout does not yield",
                                         {"due_date": extracted(inv).get("due_date"), "line_items": extracted(inv).get("line_items")}))
_pdf_case("invoice_scanned_no_text_layer.pdf", "PDF: scanned image with no text layer (OCR fallback)",
          "pdf_parser OCR fallback; generate_realistic_invoices.py: Acme, INV-9500, $450.00 against PO-1001 ($1,250)",
          score=W["validation_issue"], vendor=ACME, number="INV-9500", amount=450.0, issue="differs from PO", ocr=True)


@scenario("upload_plain_text_file", "pdf", "Uploading a .txt file runs the same pipeline as submit-text",
          "pdf_parser: non-PDF, non-image uploads are read as plain text")
def _(t: T):
    pre_acme(t)
    inv = t.upload(("qa_clean_invoice.txt", clean_acme(t, "INV-9800").encode()), "upload .txt")
    t.expect("extracted vendor_name", extracted(inv).get("vendor_name"), "eq", ACME)
    t.expect_risk(inv, "uploaded text invoice", 0)


@scenario("upload_unreadable_file_rejected", "pdf", "Unreadable PDF and image uploads get a 4xx with a message, not a 500",
          "Decision 2026-10-05 (probe review): unreadable uploads are a client error; no invoice record is created")
def _(t: T):
    _s, before = t.call("GET", "/dashboard/stats")
    for label, name, content in (("corrupt .pdf", "qa_corrupt.pdf", b"this is not a pdf"),
                                 ("zero-byte .pdf", "qa_empty.pdf", b""),
                                 ("corrupt .png", "qa_corrupt.png", b"this is not an image")):
        status, body = t.call("POST", "/invoices/upload", upload=(name, content))
        t.expect(f"{label}: HTTP status", status, "eq", 422)
        t.expect(f"{label}: readable error message", isinstance(body, dict) and isinstance(body.get("detail"), str), "eq", True)
    _s, after = t.call("GET", "/dashboard/stats")
    t.expect("no invoice record was created", after.get("total_invoices"), "eq", before.get("total_invoices"))


@scenario("upload_ocr_tools_missing", "pdf", "A scanned upload on a host without the OCR tools returns 503, not 500",
          "Decision 2026-10-05 (probe review): a missing OCR dependency is reported as a service error, "
          "never as a successful extraction", isolated_only=True)
def _(t: T):
    if importlib.util.find_spec("PIL") is None:
        raise Skip("Pillow is needed to build the image-only test files")
    from PIL import Image

    def render(fmt: str) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (400, 200), "white").save(buffer, format=fmt)
        return buffer.getvalue()

    files = (("image-only .pdf", "qa_scan.pdf", render("PDF")), ("image .png", "qa_scan.png", render("PNG")))
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = "/nonexistent-invoiceguard-qa-path"  # neither pdftoppm nor tesseract can be found
    try:
        for label, name, content in files:
            status, body = t.call("POST", "/invoices/upload", upload=(name, content))
            t.expect(f"{label} with the OCR tools unavailable: HTTP status", status, "eq", 503)
            t.expect(f"{label}: readable error message", isinstance(body, dict) and isinstance(body.get("detail"), str), "eq", True)
    finally:
        os.environ["PATH"] = saved_path
    _s, stats = t.call("GET", "/dashboard/stats")
    t.expect("no invoice record was created", stats.get("total_invoices"), "eq", 0)


# --------------------------------------------------------------------------- existing suites

def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_existing_suites() -> list[dict]:
    """Runs the project's own tests. The backend suite gets the same isolation as the scenarios."""
    results = []
    tmp = Path(tempfile.mkdtemp(prefix="invoiceguard-suite-"))
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{tmp / 'suite.db'}", "ANTHROPIC_API_KEY": "", "SLACK_WEBHOOK_URL": "",
                "SMTP_HOST": "", "ALERT_EMAIL_TO": "", "PYTHONDONTWRITEBYTECODE": "1"})
    shown = "python -m unittest discover -s tests -v"
    try:
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        output = proc.stderr + proc.stdout
        ran = next((line for line in output.splitlines() if line.startswith("Ran ")), "test count not reported")
        verdict = output.strip().splitlines()[-1] if output.strip() else ""
        results.append({"suite": "backend: tests/ (unittest)", "command": shown,
                        "status": "PASS" if proc.returncode == 0 else "FAIL",
                        "summary": f"{ran}: {verdict}", "output_tail": _tail(output)})
    except Exception as exc:
        results.append({"suite": "backend: tests/ (unittest)", "command": shown, "status": "ERROR",
                        "summary": f"{type(exc).__name__}: {exc}", "output_tail": ""})

    results.append(_frontend_suite())
    return results


_FOREIGN_NODE_MODULES = ("another platform", "@esbuild/", "Exec format error", "cannot execute binary")


def _npm_test(cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(["npm", "test"], cwd=cwd, capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def _npm_counts(output: str) -> str:
    lines = [line.strip().lstrip("# ℹ").strip() for line in output.splitlines()]
    return ", ".join(line for line in lines if line.startswith(("tests ", "pass ", "fail "))) or "exit code 0"


def _frontend_suite() -> dict:
    """`npm test` in frontend/. If node_modules there was installed on another operating system,
    the suite is run in a scratch copy instead; nothing is ever reinstalled inside the project."""
    frontend = ROOT / "frontend"
    entry = {"suite": "frontend: npm test", "command": "cd frontend && npm test", "output_tail": ""}
    if shutil.which("npm") is None:
        entry.update(status="NOT RUN", summary="npm is not installed on this machine")
        return entry
    try:
        where = ""
        code, output = (1, "node_modules missing") if not (frontend / "node_modules").is_dir() else _npm_test(frontend)
        if code != 0 and (not (frontend / "node_modules").is_dir() or any(m in output for m in _FOREIGN_NODE_MODULES)):
            scratch = Path(tempfile.mkdtemp(prefix="invoiceguard-frontend-")) / "frontend"
            shutil.copytree(frontend, scratch, ignore=shutil.ignore_patterns("node_modules", "dist", ".env*"))
            install = subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=scratch, capture_output=True, text=True, timeout=600)
            if install.returncode != 0:
                entry.update(status="NOT RUN", output_tail=_tail(install.stdout + install.stderr),
                             summary="frontend/node_modules is missing or was installed for another operating system, and "
                                     "`npm ci` in a scratch copy failed (no registry access?). Run `npm test` on the machine that owns the checkout")
                return entry
            code, output = _npm_test(scratch)
            where = " (run in a scratch copy of frontend/: the project's node_modules is missing or built for another OS)"
        if code == 0:
            entry.update(status="PASS", summary=_npm_counts(output) + where, output_tail=_tail(output))
        else:
            entry.update(status="FAIL", summary=f"exit code {code}{where}", output_tail=_tail(output))
    except Exception as exc:
        entry.update(status="ERROR", summary=f"{type(exc).__name__}: {exc}")
    return entry


# --------------------------------------------------------------------------- run setup

class Run:
    def __init__(self, mode: str, client, cfg: dict, reset=None):
        self.mode = mode
        self.client = client
        self.cfg = cfg
        self.reset = reset
        self.run_id = uuid.uuid4().hex[:6].upper()


def make_isolated(allow_llm: bool) -> Run:
    tmp = Path(tempfile.mkdtemp(prefix="invoiceguard-qa-"))
    impl.apply_isolation_env(tmp / "qa.db", allow_llm=allow_llm)
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from starlette.testclient import TestClient
    from app.main import app
    from app.config import settings
    from app.database import Base, engine
    from app import seed_data

    # Guard rails. drop_all() below must never be able to reach a real database.
    if not impl.bound_to_throwaway_db(engine):
        raise SystemExit("refusing to run: the app's engine is not bound to the throwaway test database")
    if settings.SLACK_WEBHOOK_URL or settings.SMTP_HOST:
        raise SystemExit("refusing to run: notification channels are still configured inside the test process")
    if settings.ANTHROPIC_API_KEY and not allow_llm:
        raise SystemExit("refusing to run: the LLM extractor is still enabled (pass --llm to allow it)")

    def reset():
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        with contextlib.redirect_stdout(io.StringIO()):
            seed_data.seed()

    uses_llm = bool(settings.ANTHROPIC_API_KEY)
    cfg = {
        "multiplier": settings.ANOMALY_MULTIPLIER,
        "multiplier_source": "app.config.settings in this process",
        "uses_llm": uses_llm,
        "extraction": "LLM extractor (--llm passed and ANTHROPIC_API_KEY is set)" if uses_llm
                      else "deterministic regex extractor (LLM disabled for the run)",
        "database": "throwaway SQLite file, re-seeded before every scenario",
        "notifications": "disabled for the run",
    }
    return Run("isolated", TestClient(app, raise_server_exceptions=False), cfg, reset)


def make_live(base_url: str) -> Run:
    import httpx
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=120)
    root = client.get("/")
    if root.status_code != 200 or root.json().get("service") != "InvoiceGuard API":
        raise SystemExit(f"{base_url} did not answer as the InvoiceGuard API (GET / -> {root.status_code})")
    try:
        from dotenv import dotenv_values
        local = dotenv_values(ROOT / ".env").get("ANOMALY_MULTIPLIER")
    except Exception:
        local = None
    multiplier = float(local or impl.static_snapshot()["anomaly_multiplier_default"] or 2.5)
    cfg = {
        "multiplier": multiplier,
        "multiplier_source": "ASSUMED from the local .env / code default; the server does not expose its own value",
        "uses_llm": None,
        "extraction": "decided by the server (see extraction_method in the raw results)",
        "database": f"the live database behind {base_url}; this run wrote test invoices into it",
        "notifications": "decided by the server: high-risk test invoices may have sent real Slack/email alerts",
    }
    return Run("live", client, cfg)


# --------------------------------------------------------------------------- execution

def run_scenario(run: Run, sc: dict, allow_approve: bool) -> dict:
    result = {k: sc[k] for k in ("id", "area", "title", "basis", "kind")}
    t = T(run, sc)
    status = reason = None
    try:
        if run.mode == "live" and sc["isolated_only"]:
            raise Skip("needs a pristine database; only runs in isolated mode")
        if run.mode == "live" and sc["needs_approve"] and not allow_approve:
            raise Skip("approving changes the vendor's stored average on the live database; pass --allow-approve to include it")
        if run.reset:
            run.reset()
        sc["fn"](t)
    except Skip as exc:
        status, reason = "SKIPPED", str(exc)
    except Blocked as exc:
        status, reason = "BLOCKED", str(exc)
    except Abort:
        pass
    except Exception:
        status, reason = "ERROR", traceback.format_exc(limit=6)

    if status is None:
        if any(not c["passed"] for c in t.checks):
            status = "FAIL"
        elif sc["kind"] == "probe":
            status, reason = ("OBSERVED", None) if t.observations else ("ERROR", "probe recorded no observations")
        elif t.checks:
            status = "PASS"
        else:
            status, reason = "ERROR", "scenario executed no checks"
    result.update({"status": status, "reason": reason, "checks": t.checks, "observations": t.observations, "calls": t.calls})
    return result


def select(args) -> list[dict]:
    chosen = SCENARIOS
    if args.area:
        areas = {a.strip() for a in args.area.split(",")}
        unknown = areas - {s["area"] for s in SCENARIOS}
        if unknown:
            raise SystemExit(f"unknown area(s): {', '.join(sorted(unknown))}")
        chosen = [s for s in chosen if s["area"] in areas]
    if args.only:
        ids = {i.strip() for i in args.only.split(",")}
        unknown = ids - {s["id"] for s in SCENARIOS}
        if unknown:
            raise SystemExit(f"unknown scenario id(s): {', '.join(sorted(unknown))}")
        chosen = [s for s in chosen if s["id"] in ids]
    return chosen


# --------------------------------------------------------------------------- reporting

def _fmt(value) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False).replace("|", "\\|")
    return text if len(text) <= 300 else text[:297] + "..."


def _repro(result: dict) -> list[str]:
    lines = []
    for call in result["calls"]:
        if call.get("setup"):
            lines.append(call["setup"])
        lines.append(f"{call['curl']}    # -> HTTP {call['status']}")
    return lines


def render_report(meta: dict, results: list[dict], suites: list[dict] | None, drift: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    executed = [r for r in results if r["status"] in ("PASS", "FAIL", "ERROR", "OBSERVED")]
    not_run = [r for r in results if r["status"] in ("SKIPPED", "BLOCKED")]
    bad = [r for r in results if r["status"] in ("FAIL", "ERROR")]
    probes = [r for r in results if r["kind"] == "probe" and r["status"] == "OBSERVED"]
    out: list[str] = []
    w = out.append

    w(f"# InvoiceGuard invoice testing report: {meta['started']}")
    w("")
    w("Every status below comes from an executed run. PASS means the checks ran and matched. "
      "Nothing in the results tables was inferred from reading code.")
    w("")
    w("## Run facts")
    w("")
    w("| Item | Value |")
    w("|---|---|")
    w(f"| Mode | {meta['mode']} |")
    w(f"| Commit | `{meta['facts'].get('git_commit')}` on `{meta['facts'].get('git_branch')}` |")
    dirty = meta["facts"].get("uncommitted_app_changes") or []
    w(f"| Uncommitted app changes | {', '.join(f'`{d}`' for d in dirty) if dirty else 'none'} |")
    w(f"| Python | {meta['facts'].get('python')} |")
    w(f"| Database | {meta['cfg']['database']} |")
    w(f"| Extraction | {meta['cfg']['extraction']} |")
    w(f"| ANOMALY_MULTIPLIER | {meta['cfg']['multiplier']} ({meta['cfg']['multiplier_source']}) |")
    w(f"| Notifications | {meta['cfg']['notifications']} |")
    w(f"| Scenario selection | {meta['selection']} |")
    w(f"| Command | `{meta['command']}` |")
    w("")
    w("## Summary")
    w("")
    w(f"{len(executed)} of {len(results)} selected scenarios were executed.")
    w("")
    w("| Status | Count | Meaning |")
    w("|---|---:|---|")
    for name, meaning in [
        ("PASS", "executed, every check matched the expectation"),
        ("FAIL", "executed, at least one check did not match"),
        ("ERROR", "the scenario itself crashed or checked nothing"),
        ("OBSERVED", "probe: behaviour recorded, no agreed expectation, not a pass"),
        ("BLOCKED", "not executed: a precondition was not met"),
        ("SKIPPED", "not executed: does not apply to this run"),
    ]:
        w(f"| {name} | {counts.get(name, 0)} | {meaning} |")
    w("")

    w("## Implementation drift")
    w("")
    if drift:
        w("The code differs from `qa/baseline.json`, so expectations in these areas may be stale. "
          "Results that touch them need the code re-read before they are trusted.")
        w("")
        for d in drift:
            w(f"- **{d['key']}**: baseline `{_fmt(d['baseline'])}` vs current `{_fmt(d['current'])}`")
    else:
        w("None. The implementation matches `qa/baseline.json` (score rules, thresholds, tolerance, routes, seed data, status writers).")
    w("")

    w("## Results by area")
    w("")
    w("| Area | Scenario | Status | Checks | What it covers |")
    w("|---|---|---|---:|---|")
    for r in results:
        passed = sum(1 for c in r["checks"] if c["passed"])
        checks = f"{passed}/{len(r['checks'])}" if r["checks"] else "-"
        w(f"| {r['area']} | `{r['id']}` | {r['status']} | {checks} | {r['title']} |")
    w("")

    w("## Failures and errors")
    w("")
    if not bad:
        w("None in this run.")
        w("")
    for r in bad:
        w(f"### {r['status']}: `{r['id']}`")
        w("")
        w(f"**What was tested:** {r['title']}")
        w("")
        w(f"**Where the expectation comes from:** {r['basis']}")
        w("")
        if r["status"] == "ERROR":
            w("```text")
            w((r["reason"] or "").strip())
            w("```")
            w("")
        failed = [c for c in r["checks"] if not c["passed"]]
        if failed:
            w("| Check | Expected | Actual |")
            w("|---|---|---|")
            for c in failed:
                w(f"| {c['name']} | {c['op']} `{_fmt(c['expected'])}` | `{_fmt(c['actual'])}` |")
            w("")
            w(f"{sum(1 for c in r['checks'] if c['passed'])} other check(s) in this scenario passed.")
            w("")
        notes = [o for o in r["observations"] if not o["name"].endswith("extraction_method")]
        if notes:
            w("Recorded alongside:")
            w("")
            for o in notes:
                w(f"- {o['name']}: `{_fmt(o['value'])}`")
            w("")
        w("**Reproduce:**")
        w("")
        w("```bash")
        w(f"python qa/run_scenarios.py --only {r['id']}        # re-run just this scenario")
        w("")
        w("# or by hand, against a freshly seeded throwaway database:")
        w("export DATABASE_URL=sqlite:///./qa_repro.db ANTHROPIC_API_KEY=")
        w("python -m app.seed_data && uvicorn app.main:app --port 8000 &")
        w("BASE=http://127.0.0.1:8000")
        for line in _repro(r):
            w(line)
        w("```")
        w("")
        w("**Likely cause:** _not analysed yet_")
        w("")
        w("**Suggested fix:** _not analysed yet_")
        w("")

    w("## Observed behaviour with no agreed expectation")
    w("")
    if not probes:
        w("No probes were executed in this run.")
        w("")
    else:
        w("These probes record what the system does today in situations the spec does not settle. "
          "They are not passes. Each one is a decision for the project owner: accept the behaviour, or turn it into a rule.")
        w("")
    for r in probes:
        w(f"### `{r['id']}`: {r['title']}")
        w("")
        for o in r["observations"]:
            if o["name"].endswith("extraction_method"):
                continue
            w(f"- **{o['name']}:** `{_fmt(o['value'])}`")
            if o["note"]:
                w(f"  - {o['note']}")
        w("")

    w("## Not executed")
    w("")
    if not not_run:
        w("Every selected scenario was executed.")
    for r in not_run:
        w(f"- `{r['id']}` ({r['status']}): {r['reason']}")
    w("")

    w("## Existing test suites")
    w("")
    if suites is None:
        w("NOT RUN in this session (pass `--with-existing-suites`).")
    else:
        w("| Suite | Status | Result |")
        w("|---|---|---|")
        for s in suites:
            w(f"| {s['suite']} (`{s['command']}`) | {s['status']} | {s['summary']} |")
        for s in suites:
            if s["status"] in ("FAIL", "ERROR") and s.get("output_tail"):
                w("")
                w(f"Output tail of {s['suite']}:")
                w("")
                w("```text")
                w(s["output_tail"])
                w("```")
    w("")

    w("## Not covered by this run")
    w("")
    for line in meta["not_covered"]:
        w(f"- {line}")
    w("")
    return "\n".join(out)


def console_summary(results: list[dict], suites, drift, paths) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + "=" * 72)
    print("  ".join(f"{k}={v}" for k, v in sorted(counts.items())) + f"   (of {len(results)} selected)")
    if drift:
        print(f"IMPLEMENTATION DRIFT in: {', '.join(d['key'] for d in drift)}")
    for r in results:
        if r["status"] in ("FAIL", "ERROR"):
            print(f"\n{r['status']}  {r['id']}: {r['title']}")
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"    - {c['name']}: expected {c['op']} {_fmt(c['expected'])}, got {_fmt(c['actual'])}")
            if r["status"] == "ERROR":
                print("    " + (r["reason"] or "").strip().splitlines()[-1])
    for r in results:
        if r["status"] in ("SKIPPED", "BLOCKED"):
            print(f"{r['status']}  {r['id']}: {r['reason']}")
    for s in suites or []:
        print(f"SUITE {s['status']}  {s['suite']}: {s['summary']}")
    if paths:
        print(f"\nreport: {paths[0]}\nraw results: {paths[1]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run InvoiceGuard invoice scenarios and write an expected-vs-actual report.")
    ap.add_argument("--list", action="store_true", help="print the scenario catalog and exit (nothing is executed)")
    ap.add_argument("--area", help="comma-separated areas: clean, duplicate, amount, vendor, po, extraction, gate, monitor, pdf")
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--base-url", help="run against a running server instead of the isolated in-process app")
    ap.add_argument("--live-writes-ok", action="store_true",
                    help="required with --base-url: acknowledges that test invoices are written to that server's database")
    ap.add_argument("--allow-approve", action="store_true",
                    help="live mode only: include scenarios that approve invoices (changes vendor averages)")
    ap.add_argument("--llm", action="store_true",
                    help="isolated mode: keep ANTHROPIC_API_KEY so the LLM extractor is exercised (costs API calls, not repeatable)")
    ap.add_argument("--with-existing-suites", action="store_true", help="also run tests/ (unittest) and the frontend npm tests")
    ap.add_argument("--out-dir", default=str(ROOT / "test_reports"), help="where the report and raw JSON are written")
    ap.add_argument("--no-report", action="store_true", help="print results only; write nothing")
    args = ap.parse_args()

    if not (ROOT / "app").is_dir():
        print(f"error: {ROOT} does not look like the InvoiceGuard project (no app/ directory)", file=sys.stderr)
        return 2

    chosen = select(args)
    if args.list:
        print("| Area | Id | Kind | Isolated only | What it covers | Where the expectation comes from |")
        print("|---|---|---|---|---|---|")
        for s in chosen:
            print(f"| {s['area']} | `{s['id']}` | {s['kind']} | {'yes' if s['isolated_only'] else ''} | {s['title']} | {s['basis']} |")
        print(f"\n{len(chosen)} scenarios. Nothing was executed.")
        return 0

    if args.base_url and not args.live_writes_ok:
        print("error: --base-url writes test invoices into that server's database. Re-run with --live-writes-ok "
              "once the project owner has agreed to that.", file=sys.stderr)
        return 2

    started = dt.datetime.now()
    try:
        run = make_live(args.base_url) if args.base_url else make_isolated(args.llm)
    except Exception as exc:
        print(f"error: could not start the harness: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("hint: use the interpreter printed by `bash qa/bootstrap_env.sh | tail -n 1`", file=sys.stderr)
        return 2

    results = []
    for sc in chosen:
        res = run_scenario(run, sc, args.allow_approve)
        results.append(res)
        print(f"{res['status']:<9} {res['id']}")

    suites = run_existing_suites() if args.with_existing_suites else None

    snapshot = impl.static_snapshot()
    if run.mode == "isolated":
        try:
            snapshot.update(impl.collect_dynamic())
        except Exception:
            pass
    drift = impl.diff_against_baseline(snapshot)

    not_covered = []
    if run.mode == "isolated":
        if not run.cfg["uses_llm"]:
            not_covered.append("LLM extraction path (`_llm_extract`): the run forces the deterministic extractor. Use `--llm` to exercise it.")
        not_covered.append("Real Slack / SMTP delivery: notification channels are disabled during test runs.")
        not_covered.append("PostgreSQL: the run uses SQLite. Production sets `DATABASE_URL` to PostgreSQL (psycopg).")
        not_covered.append("A running server or deployment: use `--base-url ... --live-writes-ok` for that.")
    else:
        not_covered.append("Scenarios that need a pristine database (listed under Not executed).")
    not_covered.append("The React frontend (upload page, queue, decision modal): see `qa/references/ui-checklist.md`.")
    not_covered.append("Authentication and authorisation: the backend has none to test; the login is client-side only.")
    if suites is None:
        not_covered.append("The project's own test suites (`--with-existing-suites`).")

    selection = " ".join(part for part in (f"areas: {args.area}" if args.area else "", f"ids: {args.only}" if args.only else "") if part)
    meta = {
        "started": started.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": run.mode,
        "facts": impl.run_facts(),
        "cfg": run.cfg,
        "selection": selection or "all scenarios",
        "command": ("python qa/run_scenarios.py " + " ".join(shlex.quote(a) for a in sys.argv[1:])).strip(),
        "not_covered": not_covered,
    }

    paths = None
    if not args.no_report:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"invoice-testing-{started.strftime('%Y%m%d-%H%M%S')}"
        md_path, json_path = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
        md_path.write_text(render_report(meta, results, suites, drift), encoding="utf-8")
        json_path.write_text(json.dumps({"meta": meta, "drift": drift, "results": results, "existing_suites": suites},
                                        indent=2, default=str), encoding="utf-8")
        paths = (md_path, json_path)

    console_summary(results, suites, drift, paths)
    failed_suites = any(s["status"] in ("FAIL", "ERROR") for s in suites or [])
    return 1 if failed_suites or any(r["status"] in ("FAIL", "ERROR") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
