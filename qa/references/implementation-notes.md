# InvoiceGuard: how the pipeline behaves

Recorded against commit `563b796` plus the fixes applied on 2026-10-05 (uncommitted at the time of writing). Each statement is marked:

- **[run]** confirmed by an executed scenario in `qa/run_scenarios.py` (scenario id in brackets)
- **[read]** taken from reading the code, not exercised by a scenario

Line numbers move. Trust `python qa/inspect_implementation.py` over this file, and re-read the code
whenever it reports drift.

## Contents

1. Pipeline and human gate
2. Extraction
3. Retrieval: vendor, PO and duplicate lookup
4. Validation
5. Risk scoring
6. Decisions
7. Monitoring
8. API surface
9. Seed data
10. Gaps and open questions
11. Documentation that disagrees with the code

## 1. Pipeline and human gate

`OrchestrateAgent.run_pipeline` (`app/agents/orchestrate_agent.py`) runs
Extract -> Retrieve -> Validate -> Assess, saves the results on the invoice, sets
`status = "pending_review"`, then calls `MonitorAgent.alert_if_high_risk`.

- Every pipeline outcome lands at `pending_review` with `decided_by` and `decided_at` empty.
  **[run]** (checked on every submission in every scenario; `gate_pipeline_never_decides`)
- Only two statements in `app/` write an invoice's `.status`: `run_pipeline` (always
  `"pending_review"`) and `apply_decision` (the human's decision). **[run]** (static scan inside
  `gate_pipeline_never_decides`; also reported by `inspect_implementation.py`)
- Each agent writes one audit entry: `pipeline_started`, `extracted_fields`, `retrieved_context`,
  `validated`, `assessed`, `pipeline_completed`, plus `alert_raised` for high risk.
  **[run]** (`clean_amount_proximity`, `duplicate_same_vendor`)

## 2. Extraction

`app/services/llm_client.py`. With `ANTHROPIC_API_KEY` set, the LLM is tried first and any error
falls back to the regex extractor; without a key the regex extractor is used directly. The result
carries `extraction_method`: `llm`, `deterministic_fallback` or `deterministic_fallback_after_llm_error`.

Regex extractor:

- Vendor from `Vendor:` / `From:` / `Supplier:`; invoice number from `Invoice Number|No|#`;
  PO from `PO Number|No|#`; due date only in numeric `d/m/y` or `y-m-d` forms. **[run]**
  (`extraction_fields`, `extraction_subtotal_tax_total`)
- Amount from the first of: total amount due, total due, grand total, amount due, balance due,
  total, amount. `Subtotal` does not match. **[run]** (`extraction_subtotal_tax_total`,
  `pdf_freight_style`, `pdf_marketing_style`)
- Line items only in the form `<description> <qty> x $<price>`. **[run]** Other layouts yield no
  line items (`pdf_marketing_style`).
- Works when line breaks are collapsed into spaces. **[run]** (`extraction_collapsed_single_line`)
- Missing fields become `"Unknown Vendor"`, `"UNKNOWN-INV"` and amount `0.0`. **[run]**
  (`extraction_unparseable_text`, `probe_missing_total`)
- The LLM path (`_llm_extract`, model `claude-sonnet-4-6`) is **[read]** only. Scenarios force the
  regex extractor unless `--llm` is passed.

`app/services/pdf_parser.py`: PDFs use the text layer, and fall back to OCR (pdf2image + pytesseract,
needs the `tesseract` and `pdftoppm` binaries) when the text layer is empty; images go straight to
OCR; anything else is read as plain text. **[run]** (`pdf_*`, `pdf_scanned_no_text_layer`,
`upload_plain_text_file`)

Input errors (`app/routers/invoices.py`, `pdf_parser.py`):

- Blank or whitespace-only `raw_text`, and uploads that yield no text, return 422 and create no
  invoice. **[run]** (`input_blank_submission_rejected`)
- A PDF or image that cannot be parsed raises `UnreadableUploadError` and returns 422 with a
  message. **[run]** (`upload_unreadable_file_rejected`)
- When OCR is needed and Tesseract or Poppler cannot be found, `OcrUnavailableError` returns 503.
  **[run]** (`upload_ocr_tools_missing`, simulated by emptying `PATH`)
- The `ImportError` branches (OCR Python packages missing), `TesseractError`, password-protected
  PDFs and an upload with no filename are **[read]** only.
- A binary file with an unrecognised extension is still decoded as text and processed. **[run]**
  (ad hoc, 2026-10-05; not a harness scenario)

## 3. Retrieval: vendor, PO and duplicate lookup

`app/agents/retrieve_agent.py`.

- Vendor: exact, case-insensitive name match (`func.lower(Vendor.name) == func.lower(name)`).
  `%` and `_` are ordinary characters. **[run]** (`vendor_name_case_insensitive`,
  `vendor_wildcard_name`) The vendor and PO-creation endpoints use the same comparison.
  **[run]** (`tests/test_confirmed_fixes_regression.py`)
- PO, when the invoice cites a number: looked up by number. The match is exact, ignoring only
  letter case and surrounding whitespace, which is what the original `ilike` + `strip()` code
  ignored. Partial numbers, `%`, `_`, and reformatted numbers (`PO 1003`, `PO1003`, `PO-01003`) do
  not match. **[run]** (`po_number_exact_match`, `TestPONumberMatching`) If the PO belongs to
  another vendor, `po_vendor_mismatch = true`. **[run]** (`po_vendor_mismatch`)
- `POST /purchase-orders` refuses a PO number that differs from an existing one only by letter
  case, so a case-insensitive citation is never ambiguous. **[run]**
  (`po_number_case_variant_cannot_be_created`) A PO number sent to the API with surrounding
  whitespace is stored as sent and can then never be matched; the frontend trims before sending.
  **[run]** (ad hoc, 2026-10-05; not a harness scenario)
- If the vendor name is not on file and the cited PO exists, the vendor stays unresolved
  (`vendor_found = false`, `vendor_id = null`) and `po_vendor_mismatch = true`. The vendor is never
  taken from the PO. **[run]** (`vendor_unlisted_payee_citing_valid_po`)
- If the invoice cites no PO number, the vendor's open PO closest in amount is used:
  `po_match_method = "amount_proximity"`. **[run]** (`clean_amount_proximity`)
- If the invoice cites a PO number that does not exist, nothing is matched and there is no amount
  fallback. The cited number is passed on as `cited_po_number`. **[run]** (`po_cited_number_not_found`)
- Duplicates: same `vendor_id` and the same `invoice_number` ignoring letter case, any status.
  No other normalisation (hyphens, leading zeros). **[run]** (`duplicate_same_vendor`,
  `duplicate_case_insensitive`, `duplicate_number_other_vendor`, `probe_duplicate_format_variants`,
  `probe_duplicate_after_rejection`) No duplicate check runs when the vendor is unresolved. **[read]**
- Supplier profile from the hardcoded knowledge base, keyed by lower-cased vendor name. **[read]**

## 4. Validation

`app/agents/validate_agent.py`. `passed` is true only when there are no issues. Issues:

| Issue | Condition | Verified |
|---|---|---|
| Vendor not found | no vendor resolved | **[run]** `vendor_unknown` |
| Vendor not on approved list | vendor found, `approved = false` | **[run]** `vendor_unapproved` |
| Supplier policy note | KB `policy_notes` contains "sign-off" or `risk_notes` contains "review" | **[run]** `vendor_unapproved` |
| No matching purchase order | no PO matched and none cited | **[run]** `vendor_unknown`, `vendor_unapproved` |
| Cited PO does not exist | a PO number was cited but no PO has that number | **[run]** `po_cited_number_not_found` |
| PO belongs to a different vendor | `po_vendor_mismatch` (another known vendor, or a vendor name not on file) | **[run]** `po_vendor_mismatch`, `vendor_unlisted_payee_citing_valid_po` |
| Amount differs from PO | difference greater than 2% (`AMOUNT_TOLERANCE_PCT = 0.02`); exactly 2% passes | **[run]** `po_tolerance_boundary`, `po_amount_mismatch_is_medium` |
| Line items not on the PO | an invoice line description is not among the PO's descriptions | **[run]** `po_line_item_mismatch` |

When the PO belongs to a different vendor, the amount and line-item checks are skipped. **[read]**

## 5. Risk scoring

`app/agents/assess_agent.py`.

| Signal | Score | Verified |
|---|---:|---|
| Duplicate invoice number (same vendor) | +50 | **[run]** `duplicate_same_vendor` |
| Amount >= `ANOMALY_MULTIPLIER` x vendor average (only when the average is above 0) | +30 | **[run]** `amount_anomaly_*` |
| Vendor not found | +15 | **[run]** `vendor_unknown` |
| PO belongs to a different vendor | +50 | **[run]** `po_vendor_mismatch` |
| Otherwise, validation failed | +20 per issue | **[run]** `vendor_unapproved`, `po_*` |

`high` at 50 or more, `medium` at 20 to 49, `low` below 20. **[run]**

The PO-mismatch branch replaces the per-issue rule instead of adding to it, so a mismatch on its own
scores exactly 50. **[run]** (`po_vendor_mismatch` observation)

`ANOMALY_MULTIPLIER` defaults to 2.5 and is read from the environment. **[run]**
(`amount_anomaly_at_threshold`, `amount_anomaly_just_below`)

## 6. Decisions

`POST /invoices/{id}/approve` and `/reject` (`app/routers/invoices.py`) call
`OrchestrateAgent.apply_decision`.

- 404 for an unknown invoice; 400 when the invoice is no longer `pending_review`. **[run]**
  (`gate_unknown_invoice`, `gate_approve`, `gate_reject`)
- Sets `status`, `decided_by`, `decided_at` and writes a `human_approved` / `human_rejected`
  audit entry with `decided_by` and `note`. **[run]**
- Approval adds the invoice to the vendor's history (`invoice_count + 1`, running average).
  Rejection does not. **[run]** (`gate_approve`, `gate_reject`)
- `decided_by` is required and must not be blank; it is stored trimmed. A missing body, `{}`, an
  empty string or whitespace returns 422 and decides nothing. **[run]** (`gate_decided_by_required`)
- Identity is self-reported: any non-blank name is accepted. **[run]**
  (`probe_gate_identity_is_self_reported`)
- There is no authentication or authorisation on any endpoint. The frontend login is
  client-side only (`frontend/src/context/AuthContext.tsx`, localStorage). **[read]**

## 7. Monitoring

`app/agents/monitor_agent.py`.

- High-risk invoices get an `alert_raised` audit entry; Slack/email are attempted only when
  configured. **[run]** (`monitor_alert_on_high_risk`; real delivery is not exercised)
- `GET /dashboard/alerts` lists high-risk invoices that are still `pending_review`. **[run]**
  (`monitor_alert_on_high_risk`, `gate_reject`)
- `GET /dashboard/stats` counts by status, open high-risk invoices, pending amount and flag
  buckets. **[run]** (`monitor_dashboard_stats`)

## 8. API surface

`GET /`, `POST /invoices/upload`, `POST /invoices/submit-text`, `GET /invoices` (optional
`?status=`), `GET /invoices/{id}`, `POST /invoices/{id}/approve`, `POST /invoices/{id}/reject`,
`GET|POST /vendors`, `GET|POST /purchase-orders`, `GET /dashboard/stats`, `GET /dashboard/alerts`.

## 9. Seed data

`python -m app.seed_data` (skipped when any vendor already exists).

| Vendor | Approved | Average | Count | PO |
|---|---|---:|---:|---|
| Acme Office Supplies | yes | 1200 | 8 | PO-1001, 1250, Office chairs + Standing desks |
| Brightline Logistics | yes | 4500 | 5 | PO-1002, 4500, Freight shipping Q3 |
| Meridian Cloud Services | yes | 2000 | 6 | PO-1003, 2000, Cloud hosting monthly |
| Sterling Print & Signage | yes | 800 | 3 | PO-1004, 800, Trade show banners |
| Shadow Consulting LLC | no | 0 | 0 | none |

`python -m app.reset_demo` deletes invoices and audit logs but does not restore vendor averages
changed by approvals. **[read]**

## 10. Gaps and open questions

Fixed on 2026-10-05, each now an enforced check: wildcard vendor names, unlisted payee adopting a
PO's vendor, silent fallback for a nonexistent PO, case-sensitive duplicates, blank submissions,
HTTP 500 on unreadable uploads, blank or defaulted `decided_by`.

Still open. These are probes or ad hoc observations with no agreed expectation, so they are decisions
for the project owner rather than bugs.

| Finding | Evidence |
|---|---|
| Invoice numbers that differ by a hyphen or leading zero are not treated as duplicates | `probe_duplicate_format_variants` |
| A resubmission after rejection is flagged like any duplicate | `probe_duplicate_after_rejection` |
| Line-item quantities and prices are not checked against the PO or the invoice total | `probe_line_items_do_not_add_up` |
| A missing total is stored as 0.0 | `probe_missing_total` |
| Reviewer identity is self-reported; no backend authentication | `probe_gate_identity_is_self_reported` |
| A high-risk invoice can be approved with no note or second approver | `probe_gate_high_risk_approval` |
| Purchase orders never close: a second invoice with a new number against an already-invoiced PO scores 0 | ad hoc run 2026-10-05, not a harness scenario |
| Approving an anomalous amount raises the vendor average, so the same amount is not an anomaly next time | ad hoc run 2026-10-05, not a harness scenario |
| A binary file with an unrecognised extension is decoded as text and stored as an unknown invoice | ad hoc run 2026-10-05, not a harness scenario |

## 11. Documentation that disagrees with the code

- README section 4 lists four score signals and omits the PO/vendor mismatch (+50). **[read]**
- README says Python 3.10+, but the code calls `datetime.UTC`, which needs 3.11+. **[run]**
  (`python3.10 -c "import datetime as dt; dt.UTC"` raises AttributeError)
- `requirements.txt` does not list `httpx`, which Starlette's `TestClient` (used by the tests) needs.
  It is installed only as a dependency of `anthropic`, so the tests would stop importing if that
  package were ever dropped. **[run]** (`uv pip install --dry-run -r requirements.txt`)
- Fixed 2026-10-05: `tests/test_backend_regression.py::test_invoice_submit_and_decision_lifecycle`
  used `quantity`/`total` keys for its PO, got an unasserted 422 and approved a medium-risk invoice.
  It now uses `qty`, asserts the fixtures, and checks the invoice is low risk and matched to PO-9921
  before approving. **[run]**
