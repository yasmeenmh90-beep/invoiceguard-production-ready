from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Vendor, PurchaseOrder
from app.schemas import VendorCreate, VendorOut, PurchaseOrderCreate, PurchaseOrderOut

router = APIRouter(tags=["vendors & purchase orders"])


@router.post("/vendors", response_model=VendorOut)
def create_vendor(payload: VendorCreate, db: Session = Depends(get_db)):
    existing = db.query(Vendor).filter(func.lower(Vendor.name) == func.lower(payload.name)).first()
    if existing:
        raise HTTPException(400, "Vendor already exists")
    vendor = Vendor(name=payload.name, approved=payload.approved)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.get("/vendors", response_model=list[VendorOut])
def list_vendors(db: Session = Depends(get_db)):
    return db.query(Vendor).all()


@router.post("/purchase-orders", response_model=PurchaseOrderOut)
def create_po(payload: PurchaseOrderCreate, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(func.lower(Vendor.name) == func.lower(payload.vendor_name)).first()
    if not vendor:
        vendor = Vendor(name=payload.vendor_name, approved=True)
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

    # Case-insensitive, like the PO lookup in RetrieveAgent: two PO numbers that
    # differ only by letter case would make an invoice's citation ambiguous.
    existing_po = (
        db.query(PurchaseOrder)
        .filter(func.lower(PurchaseOrder.po_number) == func.lower(payload.po_number))
        .first()
    )
    if existing_po:
        raise HTTPException(400, "PO number already exists")

    po = PurchaseOrder(
        po_number=payload.po_number,
        vendor_id=vendor.id,
        amount=payload.amount,
        line_items=[li.model_dump() for li in payload.line_items],
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


@router.get("/purchase-orders", response_model=list[PurchaseOrderOut])
def list_pos(db: Session = Depends(get_db)):
    return db.query(PurchaseOrder).all()
