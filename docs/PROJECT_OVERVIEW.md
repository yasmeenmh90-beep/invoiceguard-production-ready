# InvoiceGuard — Project Overview

**Multi-agent invoice verification with a mandatory human approval gate.**
FastAPI · SQLAlchemy · PostgreSQL / SQLite · React 19 · TypeScript · Claude (optional)

Built for GIBC V2 — Track 02: Applied (Finance).

---

## 1. Problem

Accounts Payable teams check every invoice against vendor records, purchase orders and payment history before releasing money. Done manually, this is slow and error-prone: duplicate invoices get paid twice, inflated amounts slip through, and invoices from unapproved or unknown suppliers reach payment.

Fully automating approval creates a different risk: an AI system that can move money on its own judgement.

InvoiceGuard automates the **analysis** and leaves the **authorisation** to a person. Agents extract, cross-check and score every invoice, then stop. Only a human reviewer can approve or reject.

---

## 2. Architecture

```text
 React + TypeScript dashboard (Vite)
            │  REST (axios)
            ▼
 FastAPI ── routers: /invoices  /vendors  /purchase-orders  /dashboard
            │
            ▼
 Orchestrate Agent
   Extract → Retrieve → Validate → Assess → status = pending_review
                                               │
                         Monitor Agent ◄───────┤ (high-risk alert)
                                               ▼
                                  Human: /approve or /reject
            │
            ▼
 SQLAlchemy ORM ── Vendor · PurchaseOrder · Invoice · AuditLog
            │
 PostgreSQL (psycopg 3) or SQLite, selected by DATABASE_URL
```

**Backend layers**

| Layer | Location | Role |
|---|---|---|
| API | `app/routers/` | HTTP endpoints, input validation (Pydantic), decision-state checks |
| Agents | `app/agents/` | Pipeline logic; every agent writes to the audit log via `BaseAgent.log()` |
| Services | `app/services/` | PDF/OCR parsing, LLM extraction, supplier knowledge base, Slack/email alerts |
| Data | `app/models.py`, `app/database.py` | ORM models; normalises `postgres://` URLs for Psycopg 3, falls back to SQLite |

Each invoice row stores the full output of every stage (`extracted_data`, `retrieval_context`, `validation_result`, `assessment_result`) as JSON, and every agent action is written to `audit_logs`. A reviewer can see *why* an invoice was flagged, not just *that* it was.

---

## 3. AI Agents

| Agent | Responsibility | Implementation |
|---|---|---|
| **Orchestrate** | Runs the pipeline, persists results, enforces the approval gate | Never sets `approved`; always writes `pending_review`. `apply_decision()` is the only path out of that state. |
| **Extract** | Turns raw invoice text into structured fields (vendor, invoice no., PO no., amount, due date, line items) | Uses Claude (`claude-sonnet-4-6`) with a JSON-only prompt when `ANTHROPIC_API_KEY` is set. Falls back to a deterministic regex extractor if no key is set or the LLM call fails. The method used is recorded on each invoice. |
| **Retrieve** | Gathers context | Case-insensitive vendor lookup; PO match by cited PO number, or by closest open-PO amount when none is cited; duplicate check (same vendor + invoice number); supplier profile from the knowledge base |
| **Validate** | Deterministic checks against retrieved context | Vendor exists and is approved; supplier policy notes requiring sign-off; PO exists and belongs to the same vendor; amount within **2%** of the PO; invoice line items appear on the PO |
| **Assess** | Risk scoring | Additive score → `low` / `medium` / `high`, with a human-readable flag for each signal |
| **Monitor** | Alerts and metrics | Raises alerts for high-risk invoices (audit log always; Slack/email if configured, best-effort); computes dashboard stats |

Only the Extract agent uses an LLM. Validation and risk scoring are deterministic rules, so every flag is reproducible and explainable.

---

## 4. Invoice Validation Workflow

1. **Ingest** — `POST /invoices/upload` (PDF, image or `.txt`) or `POST /invoices/submit-text`.
2. **Parse** — `pdfplumber` reads the PDF text layer. Scanned PDFs with no text layer fall back to OCR (`pdf2image` + `pytesseract`); images go straight to OCR. Corrupt files return `422`; missing OCR tools return `503` rather than an empty result.
3. **Extract** — structured fields via LLM or regex fallback.
4. **Retrieve** — vendor, matching PO, prior invoices and supplier profile.
5. **Validate** — issues are collected into `validation_result.issues`.
6. **Assess** — risk score, level and flags.
7. **Gate** — invoice saved as `pending_review`; Monitor alerts if `high`.
8. **Decide** — a reviewer approves or rejects (see §6).

---

## 5. Risk Detection

| Signal | Score |
|---|---:|
| Duplicate invoice number for the same vendor | +50 |
| PO cited belongs to a different vendor | +50 |
| Amount ≥ `ANOMALY_MULTIPLIER` × vendor's historical average (default **2.5×**) | +30 |
| Unknown / first-time vendor | +15 |
| Each other validation issue (when no PO-vendor mismatch) | +20 |

| Score | Level |
|---|---|
| < 20 | Low |
| 20–49 | Medium |
| ≥ 50 | High |

Vendor history is updated only when a human **approves** an invoice, so the anomaly baseline is built from reviewed transactions.

### Empirical evaluation of the amount-anomaly rule

`evaluate_real_invoice_dataset.py` applies the same 2.5× rule to a public pseudonymised invoice dataset using a chronological holdout: creditor profiles are built from 2012–2022 and applied to unseen 2023 invoices.

| Metric | Value |
|---|---:|
| Historical invoices (2012–2022) | 663,350 |
| Holdout invoices (2023) | 86,602 |
| From known creditors / first-seen creditors | 81,913 / 4,689 |
| Amount-anomaly signals | 8,174 (9.44%) |

The dataset has no fraud labels, so 9.44% is an **anomaly rate**, not fraud-detection accuracy. No precision or recall is claimed.

---

## 6. Human Approval Process

- Every processed invoice lands in `pending_review`, regardless of risk level.
- `POST /invoices/{id}/approve` and `/reject` are the only ways to change status. Both reject a second decision (`400 Invoice is already …`).
- Each decision records `decided_by`, `decided_at` and an optional note, and is logged as `human_approved` / `human_rejected` in the audit trail.
- `GET /dashboard/alerts` lists high-risk invoices still awaiting a decision.

**Current limitation:** the API has no authentication. `decided_by` is self-reported; the frontend fills it from the logged-in demo persona. Frontend login is client-side only (demo personas and a localStorage-based sign-up), not production identity management.

---

## 7. Frontend

React 19 + TypeScript single-page app (Vite, Tailwind CSS, React Router), talking to the API through an axios client configured by `VITE_API_BASE_URL`.

Pages: landing, auth, dashboard (KPIs and alerts), invoice queue, invoice detail with approve/reject, upload, vendors, purchase orders.

---

## 8. Technologies

| Area | Stack |
|---|---|
| Backend | Python, FastAPI, Pydantic v2, SQLAlchemy 2, Uvicorn |
| Database | PostgreSQL via Psycopg 3 (`DATABASE_URL`); SQLite default for local development |
| Document processing | pdfplumber, pytesseract, pdf2image, Pillow |
| AI | Anthropic Claude API (optional extraction path) with deterministic regex fallback |
| Notifications | Slack incoming webhook, SMTP email (both optional) |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS, React Router, axios, Framer Motion, Three.js, Lucide |
| Evaluation | pandas (public-dataset evaluation script) |

---

## 9. Testing and Data

- **Backend:** 33 `unittest` regression tests (`tests/`) covering vendor and PO management, invoice processing, dashboard stats, decision-state enforcement and previously fixed extraction bugs.
- **Frontend:** lint (oxlint), unit tests for formatters and auth logic, type-checked build.
- **QA harness:** `qa/run_scenarios.py` runs end-to-end scenarios against a baseline.
- **Data:** the interactive app uses only synthetic vendors, POs and invoices (`app/seed_data.py`, five fictional suppliers). Real data appears only in the separate public-dataset evaluation, which is not committed to the repository.

---

## 10. Known Limitations

- No backend authentication or role-based access control.
- Line-item matching is an exact, case-insensitive description match.
- The amount-anomaly check needs prior approved history for the vendor.
- The supplier knowledge base is a hardcoded stand-in for ERP vendor master data.
