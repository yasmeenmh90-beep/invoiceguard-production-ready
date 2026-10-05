# Frontend UI checklist (React app)

**Status of this file:** the steps and expected results below were written from reading
`frontend/src` at commit `563b796`. The numbered steps have not been executed on the Mac. Until a
step has actually been performed, report it as NOT RUN, never as passed.

Executed once, on 2026-10-05, in headless Chromium on Linux against the Vite dev server and a real
uvicorn backend with a throwaway database (ad hoc script, 30 of 30 checks passed): demo sign-in;
upload error messages for a corrupt PDF, a zero-byte text file, a corrupt PNG (HTTP 422) and a
scanned PDF with the OCR tools unavailable (HTTP 503); a readable PDF upload; blank pasted text
stopped in the browser; the invoice page for an unlisted payee citing another vendor's PO and for a
cited PO that does not exist; the decision dialog (confirm disabled while the reviewer field is
blank, the API's 422 message shown when a blank `decided_by` is forced on the wire, a normal
approval, state kept after reload); and the "PO number already exists" message for a case-variant
PO number. That run does not cover Safari or Chrome on macOS, the Mac's own Python environment,
or the remaining steps below.

## What the UI adds on top of the API

The backend scenarios already cover scoring and the gate. In the browser, test only what the UI
itself is responsible for: sending the right request, showing what the API returned without
distorting it, and refusing to offer a decision on an invoice that has already been decided.

## Setup

The browser runs on the user's machine, so both servers must run there, started by the user.
Use a separate database so the demo data in `invoiceguard.db` is not touched:

```bash
# terminal 1, project root
export DATABASE_URL=sqlite:///./qa_ui.db ANTHROPIC_API_KEY=
python -m app.seed_data
uvicorn app.main:app --port 8000

# terminal 2
cd frontend && npm run dev          # http://localhost:5173
```

`frontend/.env` must point `VITE_API_BASE_URL` at `http://127.0.0.1:8000` (the default).
For a repeat run, stop the backend, remove `qa_ui.db` and seed again: the expected results assume a
fresh database.

If the servers are not running, stop and ask the user to start them. Do not test the UI against
`invoiceguard.db` or a deployed URL unless the user says so.

## Evidence

For every step keep one piece of evidence: a screenshot, the page text, or the network request and
response. Compare what the page shows with `GET /invoices/{id}` for the same invoice; the API is
the source of truth.

## Steps

Routes: `/auth`, `/dashboard`, `/invoices`, `/invoices/:id`, `/upload`, `/vendors`, `/purchase-orders`.

| # | Step | Expected |
|---|---|---|
| 1 | Open `/dashboard` while signed out | Redirect to `/auth` |
| 2 | On `/auth`, click "Continue as Demo Reviewer (1-Click)" | Lands on the page originally requested; sidebar shows the backend as online |
| 3 | Open `/upload`. The "Clean Match (PO-1001)" preset is selected by default. Run it | Pipeline animation, then redirect to `/invoices/<id>`; risk shown as low; status pending review; Approve and Reject both enabled |
| 4 | Back on `/upload`, run "Duplicate Invoice Check" | High risk; a duplicate flag that names the first invoice's id |
| 5 | Run "Amount Anomaly (3.3x Average)" | High risk; the anomaly flag and the PO amount issue are both visible |
| 6 | Run "Unapproved Vendor & Compliance Hold" | High risk; vendor shown as unapproved; supplier policy note visible |
| 7 | Run "Explicit PO Reference Match" | Low risk; matched to PO-1003 by PO number |
| 8 | Run "PO Vendor Cross-Mismatch" | High risk; the cross-vendor PO issue is visible |
| 9 | Upload tab: choose `sample_invoices_pdf/invoice_freight_style.pdf` and process it | Low risk; amount 4,494.00 (not the 4,200.00 subtotal); vendor Brightline Logistics |
| 10 | Paste tab: clear the text and submit | Inline error "Please provide invoice text to process."; no request is sent |
| 11 | Open `/invoices` | Every invoice from steps 3 to 9 is listed with the same risk level the detail page showed; status and risk filters narrow the list; search by invoice number works |
| 12 | Open the step 3 invoice and click "Approve Invoice" | Modal "Confirm Invoice Approval" with the reviewer identity prefilled from the signed-in persona and a default note |
| 13 | Clear the reviewer identity field | The confirm button is disabled |
| 14 | Restore the identity, confirm | Toast "Invoice INV-9001 approved successfully."; status shows Approved by that reviewer; both action buttons disabled |
| 15 | Reload the page and expand the collapsible Audit Trail | Still approved (the state came from the API, not from local state); the trail shows a human approval entry with the reviewer and note |
| 16 | Open the step 5 invoice, click "Reject Invoice", confirm | Status Rejected; buttons disabled; the invoice no longer counts as an open high-risk item on `/dashboard` |
| 17 | Open `/dashboard` | Counters match `GET /dashboard/stats` |
| 18 | Stop the backend, wait about 15 seconds | Sidebar shows the backend as Offline; actions fail with a visible error rather than silently |

## Human-gate checks specific to the UI

- No page offers a bulk or one-click approval that skips the confirmation modal.
- The request sent on approval is `POST /invoices/{id}/approve` with `decided_by` and `note`
  taken from the modal, not hardcoded. Confirm in the network panel.
- An already-decided invoice opened in a second tab cannot be decided again: the buttons are
  disabled after reload, and if a stale tab still sends the request, the API's 400 is shown as
  an error.

## Known limits to state in the report

- The login is client-side only (localStorage). Anyone who can reach the API can approve or
  reject without signing in. This is a property of the current design, not a UI defect.
- Step 9 depends on `sample_invoices_pdf/`, which is gitignored and may be missing on a fresh clone.
