from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import Vendor, PurchaseOrder, Invoice
from app.services.supplier_kb import get_supplier_profile


class RetrieveAgent(BaseAgent):
    """Looks up supplier records, matching purchase orders, prior
    transaction history, and supplier policy/risk notes from the knowledge
    base so the Validate/Assess agents have context to reason against,
    instead of judging the invoice in isolation.
    """

    name = "retrieve_agent"

    def run(self, invoice_id: int, extracted: dict) -> dict:
        db: Session = self.db
        vendor_name = (extracted.get("vendor_name") or "").strip()
        invoice_number = extracted.get("invoice_number")
        po_number = (extracted.get("po_number") or "").strip() or None

        # Exact, case-insensitive match. (Not ilike/LIKE: that would treat "%"
        # and "_" in an invoice's vendor name as wildcards.)
        vendor = (
            db.query(Vendor)
            .filter(func.lower(Vendor.name) == func.lower(vendor_name))
            .first()
            if vendor_name
            else None
        )

        matching_po = None
        po_match_method = None
        po_vendor_mismatch = False

        # Prefer matching on an explicit PO number when the invoice states
        # one — it's a direct reference, not a guess. Amount-proximity is
        # the fallback for invoices that don't cite a PO number at all.
        if po_number:
            matching_po = (
                db.query(PurchaseOrder)
                .filter(func.lower(PurchaseOrder.po_number) == func.lower(po_number))
                .first()
            )
            if matching_po:
                po_match_method = "po_number"
                if vendor is None or matching_po.vendor_id != vendor.id:
                    # Either the PO belongs to a different known vendor, or
                    # the invoice's vendor name is not on file at all (typo,
                    # unlisted payee). The vendor is deliberately NOT taken
                    # from the PO: the stated payee is not the PO's vendor,
                    # so the invoice stays unresolved and is flagged for the
                    # human reviewer.
                    po_vendor_mismatch = True

        if po_number is None and vendor:
            # Same vendor, still-open PO closest in amount to the invoice
            # total. Weaker signal than a stated PO number, used only when
            # the invoice doesn't reference one directly. If a PO number WAS
            # cited but doesn't exist, there is no fallback: Validate reports
            # the unknown PO number instead.
            candidate_pos = (
                db.query(PurchaseOrder)
                .filter(PurchaseOrder.vendor_id == vendor.id, PurchaseOrder.status == "open")
                .all()
            )
            if candidate_pos:
                target = extracted.get("amount") or 0
                matching_po = min(candidate_pos, key=lambda po: abs(po.amount - target))
                po_match_method = "amount_proximity"

        # Duplicate check happens here since it's a retrieval question
        # ("have we seen this invoice number from this vendor before?").
        prior_matches = []
        if vendor and invoice_number:
            prior_matches = (
                db.query(Invoice)
                .filter(
                    Invoice.vendor_id == vendor.id,
                    func.lower(Invoice.invoice_number) == func.lower(invoice_number),
                    Invoice.id != invoice_id,
                )
                .all()
            )

        supplier_profile = get_supplier_profile(vendor_name)

        context = {
            "vendor_found": vendor is not None,
            "vendor_id": vendor.id if vendor else None,
            "vendor_approved": vendor.approved if vendor else False,
            "vendor_avg_amount": vendor.avg_invoice_amount if vendor else None,
            "vendor_invoice_count": vendor.invoice_count if vendor else 0,
            "cited_po_number": po_number,
            "matched_po_id": matching_po.id if matching_po else None,
            "matched_po_number": matching_po.po_number if matching_po else None,
            "matched_po_amount": matching_po.amount if matching_po else None,
            "matched_po_line_items": matching_po.line_items if matching_po else [],
            "po_match_method": po_match_method,
            "po_vendor_mismatch": po_vendor_mismatch,
            "duplicate_invoice_ids": [inv.id for inv in prior_matches],
            "supplier_profile": supplier_profile,
        }

        self.log(invoice_id, "retrieved_context", {
            "vendor_found": context["vendor_found"],
            "matched_po_number": context["matched_po_number"],
            "po_match_method": po_match_method,
            "duplicate_count": len(context["duplicate_invoice_ids"]),
            "supplier_profile_found": supplier_profile is not None,
        })
        return context
