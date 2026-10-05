import datetime as dt
from typing import Any, Optional

from pydantic import BaseModel, field_validator


class VendorCreate(BaseModel):
    name: str
    approved: bool = True


class VendorOut(BaseModel):
    id: int
    name: str
    approved: bool
    avg_invoice_amount: float
    invoice_count: int

    class Config:
        from_attributes = True


class LineItem(BaseModel):
    description: str
    qty: float
    unit_price: float


class PurchaseOrderCreate(BaseModel):
    po_number: str
    vendor_name: str
    amount: float
    line_items: list[LineItem] = []


class PurchaseOrderOut(BaseModel):
    id: int
    po_number: str
    vendor_id: int
    amount: float
    line_items: list
    status: str

    class Config:
        from_attributes = True


class AuditLogOut(BaseModel):
    agent_name: str
    action: str
    detail: dict
    timestamp: dt.datetime

    class Config:
        from_attributes = True


class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    vendor_id: Optional[int]
    po_id: Optional[int]
    amount: Optional[float]
    due_date: Optional[str]
    status: str
    risk_level: str
    extracted_data: dict
    validation_result: dict
    assessment_result: dict
    retrieval_context: dict
    created_at: dt.datetime
    decided_at: Optional[dt.datetime]
    decided_by: Optional[str]
    audit_logs: list[AuditLogOut] = []

    class Config:
        from_attributes = True


class InvoiceSummary(BaseModel):
    id: int
    invoice_number: str
    vendor_id: Optional[int]
    amount: Optional[float]
    status: str
    risk_level: str
    created_at: dt.datetime

    class Config:
        from_attributes = True


class DecisionRequest(BaseModel):
    """A human reviewer's approve/reject decision.

    `decided_by` is required and must not be blank. It is SELF-REPORTED: the
    API has no authentication, so this is whatever name the client sends (the
    frontend pre-fills it from the selected demo persona). The audit trail
    records who claimed the decision, not a verified identity.
    """

    decided_by: str
    note: Optional[str] = None

    @field_validator("decided_by")
    @classmethod
    def decided_by_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("decided_by must not be blank")
        return value


class DashboardStats(BaseModel):
    total_invoices: int
    pending_review: int
    approved: int
    rejected: int
    high_risk_open: int
    total_amount_pending: float
    flagged_reasons: dict[str, int]


class AlertOut(BaseModel):
    invoice_id: int
    invoice_number: str
    vendor_id: Optional[int]
    amount: Optional[float]
    risk_level: str
    flags: list[str]
    created_at: dt.datetime
