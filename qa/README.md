# InvoiceGuard QA harness

Scenario tests for the invoice pipeline: clean invoices, duplicates, amount anomalies, unknown and
unapproved vendors, PO/vendor mismatches, risk scoring, validation results, PDF/OCR uploads, monitoring,
and the human approve/reject gate.

Unit-level regression tests for the issues fixed on 2026-10-05 live in
`tests/test_confirmed_fixes_regression.py`; `--with-existing-suites` runs them together with the scenarios.

The harness never touches `invoiceguard.db`. By default it imports the app in-process against a
throwaway SQLite file that is re-seeded before every scenario, with the LLM extractor and Slack/SMTP
alerts switched off.

## Quick start

```bash
# 1. An interpreter that can run the backend (reuses .venv if it works, otherwise builds a
#    throwaway virtualenv under ~/.cache; nothing is installed into the project)
PY="$(bash qa/bootstrap_env.sh | tail -n 1)"

# 2. What does the code implement right now, and has it drifted from the recorded baseline?
"$PY" qa/inspect_implementation.py

# 3. Run the scenarios (writes test_reports/invoice-testing-<timestamp>.md and .json)
"$PY" qa/run_scenarios.py --with-existing-suites
```

Useful variations:

```bash
"$PY" qa/run_scenarios.py --list                     # catalog only, nothing executed
"$PY" qa/run_scenarios.py --area duplicate,po        # clean | duplicate | amount | vendor | po | extraction | gate | monitor | pdf
"$PY" qa/run_scenarios.py --only vendor_wildcard_name
"$PY" qa/run_scenarios.py --llm                      # exercise the LLM extractor (API cost, not repeatable)
"$PY" qa/run_scenarios.py --base-url http://127.0.0.1:8000 --live-writes-ok   # a running server: WRITES test invoices to its DB
```

## Files

| File | Purpose |
|---|---|
| `bootstrap_env.sh` | Finds or builds a Python 3.11+ interpreter with the backend's dependencies plus `httpx`. |
| `inspect_implementation.py` | Reads the scoring rules, thresholds, tolerance, routes, seed data and every `.status` writer from the code and compares them with `baseline.json`. |
| `baseline.json` | Fingerprint of the implementation the scenarios were last reconciled with. |
| `run_scenarios.py` | The scenarios, the harness and the report writer. `SPEC` at the top holds the rules the scenarios enforce. |
| `references/implementation-notes.md` | How the pipeline behaves, with file references, and which statements were verified by execution. |
| `references/ui-checklist.md` | Manual/browser checklist for the React frontend. |

## Statuses

| Status | Meaning |
|---|---|
| PASS | Executed; every check matched. |
| FAIL | Executed; at least one check did not match. |
| ERROR | The scenario crashed or checked nothing. |
| OBSERVED | A probe: behaviour recorded where there is no agreed expectation. Not a pass. |
| BLOCKED | Not executed: a precondition (seed data, vendor average) was not met. |
| SKIPPED | Not executed: does not apply to this run (missing PDF, missing OCR binary, live mode). |

## When the code changes on purpose

1. `inspect_implementation.py` reports drift. Read the changed code.
2. Update `SPEC` and the affected scenarios in `run_scenarios.py`.
3. Run the scenarios again.
4. `"$PY" qa/inspect_implementation.py --write-baseline` to record the new fingerprint.

## Adding a scenario

```python
@scenario("po_closed_po_is_not_matched", "po", "A closed PO is not used for amount matching",
          "RetrieveAgent: only open POs are candidates")
def _(t: T):
    pre_acme(t)                                   # preconditions -> BLOCKED if the seed state is not there
    inv = t.submit(invoice_text(t.n("INV-9900"), ACME, ACME_ITEMS, 1250.0))
    t.expect("matched PO", context(inv).get("matched_po_number"), "eq", "PO-1001", "where this rule comes from")
```

Use `kind="probe"` and `t.observe(...)` when there is no agreed expectation yet. A probe can never be
reported as a pass, which keeps "what the code happens to do" separate from "what it is supposed to do".
