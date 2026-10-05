from app.agents.base import BaseAgent

AMOUNT_TOLERANCE_PCT = 0.02  # 2% tolerance on amount matching


class ValidateAgent(BaseAgent):
    """Checks the extracted invoice against the matched purchase order:
    do the amounts match? Is the vendor approved? Are line items consistent?
    """

    name = "validate_agent"

    def run(self, invoice_id: int, extracted: dict, context: dict) -> dict:
        issues = []
        checks = {}

        vendor_approved = bool(context.get("vendor_approved"))
        checks["vendor_approved"] = vendor_approved
        if not context.get("vendor_found"):
            issues.append("Vendor not found in system — cannot verify approval status.")
        elif not vendor_approved:
            issues.append("Vendor is not on the approved vendor list.")

        # Supplier knowledge base check: surface policy notes (e.g. pending
        # onboarding/compliance review) even when the vendor row itself
        # doesn't capture that nuance.
        supplier_profile = context.get("supplier_profile")
        if supplier_profile:
            checks["supplier_payment_terms"] = supplier_profile.get("payment_terms")
            checks["supplier_policy_notes"] = supplier_profile.get("policy_notes")
            if "sign-off" in (supplier_profile.get("policy_notes") or "").lower() or \
               "review" in (supplier_profile.get("risk_notes") or "").lower():
                issues.append(
                    f"Supplier policy note: {supplier_profile.get('policy_notes')}"
                )

        po_id = context.get("matched_po_id")
        checks["po_found"] = po_id is not None
        checks["po_match_method"] = context.get("po_match_method")
        if po_id is None:
            cited_po = context.get("cited_po_number")
            if cited_po:
                issues.append(
                    f"Invoice cites PO {cited_po}, but no purchase order with that number exists."
                )
            else:
                issues.append("No matching purchase order found for this vendor.")
        elif context.get("po_vendor_mismatch"):
            issues.append(
                f"Invoice cites PO {context.get('matched_po_number')}, but that PO "
                f"belongs to a different vendor than the invoice's stated vendor."
            )
        else:
            invoice_amount = extracted.get("amount") or 0.0
            po_amount = context.get("matched_po_amount") or 0.0
            checks["po_number"] = context.get("matched_po_number")
            checks["invoice_amount"] = invoice_amount
            checks["po_amount"] = po_amount

            if po_amount > 0:
                diff_pct = abs(invoice_amount - po_amount) / po_amount
                checks["amount_diff_pct"] = round(diff_pct * 100, 2)
                if diff_pct > AMOUNT_TOLERANCE_PCT:
                    issues.append(
                        f"Invoice amount (${invoice_amount:,.2f}) differs from PO "
                        f"amount (${po_amount:,.2f}) by {diff_pct * 100:.1f}%."
                    )

            # Line-item consistency: every invoice line item description
            # should reasonably appear on the PO.
            po_descriptions = {
                li.get("description", "").lower()
                for li in (context.get("matched_po_line_items") or [])
            }
            unmatched = [
                li.get("description")
                for li in (extracted.get("line_items") or [])
                if li.get("description", "").lower() not in po_descriptions
            ]
            checks["unmatched_line_items"] = unmatched
            if unmatched and po_descriptions:
                issues.append(
                    f"{len(unmatched)} line item(s) on the invoice don't appear on the PO."
                )

        result = {
            "passed": len(issues) == 0,
            "issues": issues,
            "checks": checks,
        }

        self.log(invoice_id, "validated", {
            "passed": result["passed"],
            "issue_count": len(issues),
        })
        return result
