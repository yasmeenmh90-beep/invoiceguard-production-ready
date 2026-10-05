# InvoiceGuard 🛡️

> **Agentic AI-Powered Invoice Verification with Controlled Human Autonomy**  
> Built for **GIBC V2 — Track 02: Applied (Finance)**

InvoiceGuard combines a functional multi-agent invoice review application with an empirical evaluation on a public pseudonymized invoice dataset.

**Data disclosure:** The interactive demo uses synthetic vendors, purchase orders, and test invoices so that financial workflows can be demonstrated safely. Separately, InvoiceGuard's historical amount-anomaly logic is evaluated on public pseudonymized empirical invoice records.

---

## 1. Overview

InvoiceGuard is an agentic financial operations system designed to assist Accounts Payable teams with invoice verification, anomaly detection, duplicate detection, purchase-order validation, and human review.

Instead of allowing an AI system to autonomously approve financial transactions, InvoiceGuard follows a **Controlled Autonomy** design.

The agents perform the analysis automatically, but every processed invoice enters:

```text
pending_review
```

A human operator must explicitly approve or reject the invoice.

### Core Principle: Controlled Autonomy

InvoiceGuard separates **machine analysis** from **financial authorization**.

The system provides:

1. **Automated Invoice Analysis**
   - PDF or text ingestion
   - Structured field extraction
   - Vendor lookup
   - Purchase-order matching
   - Duplicate detection
   - Historical amount anomaly detection
   - Risk scoring

2. **Traceable Decisions**
   - Agent actions are logged
   - Validation issues are retained
   - Risk flags are explainable
   - Human decisions record the reviewer's self-reported identity and notes

3. **Hard Human Approval Gate**
   - AI analysis cannot automatically approve an invoice
   - Every invoice remains `pending_review`
   - A human operator explicitly chooses `approved` or `rejected`

---

## 2. System Architecture

```text
                         Incoming Invoice
                     (PDF Document / Raw Text)
                               │
                               ▼
                    ┌───────────────────────┐
                    │   ORCHESTRATE AGENT   │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │    EXTRACT AGENT      │
                    │ Invoice fields / OCR  │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │    RETRIEVE AGENT     │
                    │ Vendor / PO / history │
                    │ Duplicate lookup      │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │    VALIDATE AGENT     │
                    │ PO / vendor / items   │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │     ASSESS AGENT      │
                    │ Risk score + flags    │
                    │ LOW / MEDIUM / HIGH   │
                    └───────────┬───────────┘
                                │
                                ▼
              ┌─────────────────────────────────┐
              │      STATUS = pending_review    │
              │        HARD HUMAN GATE          │
              └────────────────┬────────────────┘
                               │
                   ┌───────────┴───────────┐
                   ▼                       ▼
          ┌────────────────┐       ┌────────────────┐
          │ MONITOR AGENT  │       │ HUMAN OPERATOR │
          │ KPIs / alerts  │       │ Approve/Reject │
          └────────────────┘       └────────────────┘
```

---

## 3. Specialized Agents

| Agent | Responsibility | Key Mechanics |
|---|---|---|
| **Extract Agent** | Document parsing | Extracts invoice number, vendor, PO number, amount, dates and line items. Supports an LLM-assisted path when configured and deterministic parsing fallback. |
| **Retrieve Agent** | Context retrieval | Retrieves vendor records, purchase orders, historical vendor amounts, supplier knowledge and previous invoice numbers. |
| **Validate Agent** | Deterministic verification | Checks vendor approval, PO matching, amount differences and line-item consistency. |
| **Assess Agent** | Risk scoring | Scores duplicate invoices, historical amount anomalies, unknown vendors and validation failures. |
| **Monitor Agent** | Monitoring | Aggregates operational metrics and generates alerts for high-risk invoices awaiting review. |
| **Orchestrate Agent** | Workflow control | Executes the agent pipeline, persists results and enforces the human approval gate. |

---

## 4. Risk Scoring

InvoiceGuard produces explicit risk flags rather than a black-box decision.

The current scoring logic includes:

| Signal | Risk contribution |
|---|---:|
| Duplicate invoice number | +50 |
| Amount ≥ configured historical-average multiplier | +30 |
| First-time / unknown vendor | +15 |
| Validation issue | +20 per issue |

Risk levels:

```text
Score < 20       → LOW
Score 20–49      → MEDIUM
Score ≥ 50       → HIGH
```

The default amount anomaly threshold is configured through:

```text
ANOMALY_MULTIPLIER=2.5
```

This means an invoice amount at least **2.5× its vendor's historical average** triggers the amount-anomaly signal.

---

## 5. Technology Stack

### Backend

- **FastAPI**
- **Python 3.10+**
- **SQLAlchemy**
- **SQLite**
- **Pydantic**
- **pdfplumber**
- **pypdf**
- **pytesseract**
- **pdf2image**
- Optional LLM-assisted extraction
- Deterministic extraction fallback

### Frontend

- **React 19**
- **TypeScript**
- **Vite**
- **Tailwind CSS**
- **Framer Motion**
- **Three.js**
- **Lucide React**

---

# 6. Empirical Public-Dataset Evaluation

The interactive InvoiceGuard application uses synthetic financial entities for safe demonstrations.

To evaluate the historical anomaly-detection component against empirical financial records, InvoiceGuard also includes a separate reproducible evaluation using a **public pseudonymized invoice dataset**.

The raw empirical dataset is intentionally **not committed to this repository**. It must be obtained separately from its public source.

## Dataset Fields

The evaluation uses records containing fields such as:

```text
uid
debtor
creditor
amount_cents
issue_date
due_date
cohort
```

The creditor is treated as the supplier/vendor identity for historical amount profiling.

All released organization identifiers are pseudonymized.

---

## 7. Evaluation Methodology

A chronological holdout design is used rather than randomly mixing historical and future invoices.

```text
2012 ───────────────────────────── 2022
             HISTORICAL DATA
                    │
                    │
                    ▼
        Build creditor profiles
        ├── historical mean amount
        ├── historical median
        └── invoice count
                    │
                    ▼
                  2023
             UNSEEN HOLDOUT
                    │
                    ▼
         Apply InvoiceGuard rule
                    │
          amount / historical mean
                    │
                    ▼
             >= 2.5x ?
             /       \
           YES        NO
            │          │
       anomaly       normal
        signal       signal
```

### Historical Training/Profile Window

```text
2012–2022
```

Number of historical invoice records:

```text
663,350
```

Historical creditors:

```text
7,010
```

### Holdout Evaluation Year

```text
2023
```

Number of unseen evaluation invoices:

```text
86,602
```

No 2023 invoice is used to construct its historical creditor average.

---

## 8. Empirical Results

InvoiceGuard evaluated:

```text
86,602 unseen 2023 invoices
```

Results:

| Metric | Result |
|---|---:|
| Historical invoices (2012–2022) | 663,350 |
| Holdout invoices (2023) | 86,602 |
| Historical creditors | 7,010 |
| Holdout invoices from historically known creditors | 81,913 |
| First-seen creditor invoices | 4,689 |
| Amount anomalies ≥ 2.5× historical average | 8,174 |
| Amount-anomaly rate | **9.44%** |

The empirical evaluation therefore identified **8,174 amount-anomaly signals among 86,602 unseen 2023 invoices** using the existing InvoiceGuard historical-average rule.

### Important Interpretation

**9.44% is an anomaly rate — not fraud accuracy.**

The empirical dataset does not provide verified fraud/non-fraud ground-truth labels.

Therefore, InvoiceGuard does **not** claim that:

```text
9.44% of invoices were fraudulent
```

and does not report supervised fraud-classification accuracy, precision or recall from this dataset.

Instead, the experiment demonstrates that InvoiceGuard's historical anomaly logic can be applied reproducibly to a large empirical invoice dataset using information available before the holdout period.

---

## 9. Reproduce the Empirical Evaluation

After obtaining the public dataset, place the yearly files under:

```text
data/real_invoices/
└── Atomic Common-Day Invoice Clearing Pseudonymized I/
    └── data/
        └── annual_inputs/
            ├── 2012.csv
            ├── 2013.csv
            ├── ...
            ├── 2022.csv
            └── 2023.csv
```

Install pandas if required:

```bash
python -m pip install pandas
```

Run:

```bash
python evaluate_real_invoice_dataset.py
```

The evaluator creates:

```text
evaluation_results/
├── real_invoice_evaluation_2023.csv
├── real_invoice_evaluation_summary.csv
└── top_20_real_invoice_anomalies.csv
```

The full per-invoice evaluation file is intentionally ignored by Git because it contains all 86,602 evaluated records.

The repository retains the compact summary and top-anomaly results for reproducibility and inspection.

---

# 10. Quick Start

## Prerequisites

Install:

- Python 3.10+
- Node.js 20+
- Git

---

## 11. Clone the Repository

```bash
git clone https://github.com/yasmeenmh90-beep/invoiceguard.git
cd invoiceguard
```

Repository:

https://github.com/yasmeenmh90-beep/invoiceguard

---

## 12. Backend Setup

### Create a virtual environment

```bash
python -m venv .venv
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Windows:

```bash
.venv\Scripts\activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Configure environment

```bash
cp .env.example .env
```

External AI credentials are optional for the deterministic demo path.

### Seed Demo Data

```bash
python -m app.seed_data
```

### Start FastAPI

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Backend:

```text
http://127.0.0.1:8000
```

Swagger/OpenAPI documentation:

```text
http://127.0.0.1:8000/docs
```

---

# 13. Frontend Setup

Open another terminal:

```bash
cd frontend
npm install
```

Optionally create the local frontend environment file:

```bash
cp .env.example .env
```

Run:

```bash
npm run dev
```

Open:

```text
http://localhost:5173
```

The development frontend communicates with the local FastAPI backend running on port `8000`.

---

# 14. Demo Personas

InvoiceGuard provides demo operator personas for exercising the human-review workflow.

| Persona | Role | Department |
|---|---|---|
| **Marcus Vance** | Senior Financial Controller | Financial Operations |
| **Sarah Chen** | Lead AP Auditor | Accounts Payable |
| **Elena Rostova** | Risk & Compliance Specialist | Risk Management |

These personas represent demo operators and should not be interpreted as production authentication.

---

# 15. Synthetic End-to-End Scenarios

The synthetic test scenarios exercise risk paths that are not available as labels in the empirical dataset.

### Clean Match

Invoice matches an approved vendor and corresponding purchase order.

Expected result:

```text
LOW RISK
```

### Duplicate Invoice

A previously observed invoice number is submitted again for the same vendor.

Risk contribution:

```text
+50
```

### Amount Anomaly

Invoice substantially exceeds the vendor's historical average.

Risk contribution:

```text
+30
```

### Unapproved / Unknown Vendor

Invoice originates from an unknown or unapproved supplier.

### PO Vendor Mismatch

Invoice references a purchase order associated with a different vendor.

These controlled scenarios complement the empirical evaluation by allowing deterministic testing of the complete:

```text
Extract
   ↓
Retrieve
   ↓
Validate
   ↓
Assess
   ↓
Monitor
   ↓
Human Decision
```

workflow.

---

# 16. API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | API health/service information |
| `/invoices/upload` | POST | Upload invoice and execute the agent pipeline |
| `/invoices/submit-text` | POST | Submit raw invoice text |
| `/invoices` | GET | List invoices |
| `/invoices/{id}` | GET | Retrieve invoice details and agent results |
| `/invoices/{id}/approve` | POST | Human approval |
| `/invoices/{id}/reject` | POST | Human rejection |
| `/vendors` | GET | List vendors |
| `/vendors` | POST | Create vendor |
| `/purchase-orders` | GET | List purchase orders |
| `/purchase-orders` | POST | Create purchase order |
| `/dashboard/stats` | GET | Operational dashboard metrics |
| `/dashboard/alerts` | GET | High-risk pending-review alerts |

---

# 17. Testing

## Frontend

```bash
cd frontend

npm run lint
npm test
npm run build
```

## Backend

From the project root:

```bash
python -m unittest discover -s tests -v
```

The backend regression suite covers core functionality including vendor registration, purchase orders, dashboard statistics, invoice processing and human decision-state enforcement.

---

# 18. Data Strategy

InvoiceGuard deliberately separates two types of data.

### Synthetic Application Data

Used for the interactive application and controlled end-to-end demonstrations:

- Vendors
- Purchase orders
- Demo invoices
- Duplicate scenarios
- Vendor approval scenarios
- PO mismatch scenarios

Synthetic data allows the complete financial workflow to be demonstrated without exposing private financial information.

### Public Pseudonymized Empirical Data

Used for historical anomaly evaluation:

- Historical invoice amounts
- Pseudonymized debtors
- Pseudonymized creditors
- Issue dates
- Due dates

This allows the anomaly component to be evaluated against empirical invoice distributions while maintaining privacy.

---

# 19. Security & Production Hardening

InvoiceGuard is a functional hackathon prototype rather than a production payment system.

Production deployment would require additional controls including:

- Restricted CORS origins
- Enterprise authentication such as OAuth2/OIDC/SAML
- Role-based access control
- Secure secret management
- Sandboxed PDF/OCR processing
- Production-grade database infrastructure
- Encryption at rest and in transit
- Rate limiting
- Structured security logging
- Vendor-master integration
- ERP integration
- Stronger operator authorization
- Independent security review

Most importantly, payment execution should remain outside the autonomous agent boundary unless protected by appropriate enterprise controls and authorization.

---

# 20. Known Limitations

### No Fraud Ground-Truth Labels

The public empirical dataset does not contain verified fraud labels.

Therefore the empirical experiment evaluates **anomaly detection behavior**, not fraud-classification accuracy.

### Purchase Orders Are Synthetic

The empirical dataset does not provide InvoiceGuard-compatible purchase-order records.

PO matching and PO mismatch detection are therefore demonstrated through controlled synthetic scenarios.

### Historical Average Sensitivity

Mean-based anomaly detection can be sensitive to highly skewed invoice distributions and extreme historical values.

Future versions can compare:

- median-based baselines
- robust z-scores
- MAD
- percentile thresholds
- creditor/debtor pair histories
- temporal models
- unsupervised anomaly detection

### OCR Dependencies

Image-only PDFs require appropriate OCR system dependencies such as Tesseract and Poppler.

Text-layer PDFs and direct text submissions do not require OCR.

### Authentication

The current demo personas simulate operator identity for hackathon demonstration. Production deployment would require genuine authentication and authorization.

Reviewer identity is self-reported. The backend API has no authentication: `decided_by` on `/invoices/{id}/approve` and `/invoices/{id}/reject` is required and must not be blank, but it is whatever name the client sends (the frontend pre-fills it from the selected demo persona). The audit trail therefore records who claimed a decision, not a verified identity.

---

# 21. Reproducibility

The project repository contains:

```text
invoiceguard/
├── app/                              # FastAPI backend
│   ├── agents/                       # Multi-agent workflow
│   ├── routers/                      # API endpoints
│   └── services/                     # Parsing, KB, notifications
│
├── frontend/                         # React/TypeScript application
│
├── tests/                            # Backend regression tests
│
├── evaluate_real_invoice_dataset.py  # Empirical evaluator
│
├── evaluation_results/
│   ├── real_invoice_evaluation_summary.csv
│   └── top_20_real_invoice_anomalies.csv
│
├── requirements.txt
└── README.md
```

The raw empirical dataset and full 86,602-row evaluation output are intentionally excluded from version control.

---

# 22. GIBC V2 Track Alignment

InvoiceGuard is designed for:

**Track 02 — Applied (Medical Technology & Finance)**

The project demonstrates:

- A functional financial prototype
- Automated financial analysis
- Empirical invoice data evaluation
- Historical anomaly detection
- Data engineering across hundreds of thousands of invoice records
- Multi-agent orchestration
- Explainable risk signals
- Human-in-the-loop financial controls
- Full-stack frontend/backend integration
- Reproducible evaluation methodology

The empirical evaluation complements the controlled application scenarios rather than replacing them.

---

# 23. Responsible Use

InvoiceGuard is a decision-support prototype.

A risk flag indicates that an invoice deserves additional review; it does not establish fraud, misconduct, or wrongdoing.

Automated anomaly detection should be treated as one signal within a broader financial-control process.

---

# 24. License & Compliance

InvoiceGuard was developed as a hackathon prototype for GIBC V2.

See:

```text
frontend/src/pages/PrivacyPage.tsx
frontend/src/pages/TermsPage.tsx
```

for prototype privacy and terms information.
