from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Invoice
from app.schemas import InvoiceOut, InvoiceSummary, DecisionRequest
from app.services.pdf_parser import (
    extract_text_from_upload,
    OcrUnavailableError,
    UnreadableUploadError,
)
from app.agents.orchestrate_agent import OrchestrateAgent

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("/upload", response_model=InvoiceOut)
async def upload_invoice(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Runs the full Extract -> Retrieve -> Validate -> Assess pipeline on
    an uploaded invoice (PDF or .txt for quick synthetic testing). The
    resulting invoice always lands at status=pending_review.
    """
    content = await file.read()
    try:
        raw_text = extract_text_from_upload(file.filename or "", content)
    except UnreadableUploadError as exc:
        raise HTTPException(422, str(exc))
    except OcrUnavailableError as exc:
        raise HTTPException(503, str(exc))
    if not raw_text.strip():
        raise HTTPException(422, "No readable text was found in the uploaded file.")

    invoice = Invoice(invoice_number="PENDING-EXTRACTION", raw_text=raw_text)
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    orchestrator = OrchestrateAgent(db)
    invoice = orchestrator.run_pipeline(invoice, raw_text)
    return invoice


@router.post("/submit-text", response_model=InvoiceOut)
def submit_invoice_text(raw_text: str = Form(...), db: Session = Depends(get_db)):
    """Same pipeline as /upload but takes raw text directly — handy for
    testing with the synthetic dataset without generating PDFs.
    """
    if not raw_text.strip():
        raise HTTPException(422, "Invoice text is empty.")

    invoice = Invoice(invoice_number="PENDING-EXTRACTION", raw_text=raw_text)
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    orchestrator = OrchestrateAgent(db)
    invoice = orchestrator.run_pipeline(invoice, raw_text)
    return invoice


@router.get("", response_model=list[InvoiceSummary])
def list_invoices(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Invoice)
    if status:
        query = query.filter(Invoice.status == status)
    return query.order_by(Invoice.created_at.desc()).all()


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return invoice


@router.post("/{invoice_id}/approve", response_model=InvoiceOut)
def approve_invoice(invoice_id: int, payload: DecisionRequest, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status != "pending_review":
        raise HTTPException(400, f"Invoice is already {invoice.status}")
    return OrchestrateAgent(db).apply_decision(invoice, "approved", payload.decided_by, payload.note)


@router.post("/{invoice_id}/reject", response_model=InvoiceOut)
def reject_invoice(invoice_id: int, payload: DecisionRequest, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status != "pending_review":
        raise HTTPException(400, f"Invoice is already {invoice.status}")
    return OrchestrateAgent(db).apply_decision(invoice, "rejected", payload.decided_by, payload.note)
