"""Regression tests for the issues confirmed in the October 2026 probe review.

Each test class covers one confirmed issue and was written BEFORE the fix, so that the
failure was reproducible against the unmodified code:

1. Vendor (and PO number) lookups must be exact, case-insensitive matches: no SQL wildcards.
2. An unlisted vendor citing another vendor's PO must not be silently resolved to that vendor.
3. Amount-proximity PO matching is only a fallback for invoices that cite no PO number.
4. A human decision needs a non-blank `decided_by` (identity is self-reported: there is no
   authentication, and these tests do not pretend otherwise).
5. Duplicate invoice-number detection is case-insensitive.
6. Blank submissions and unreadable uploads are answered with a 4xx, never a 500, and a
   missing OCR dependency is reported as a 503.

Run with:  python -m unittest discover -s tests -v
"""
import io
import os
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.main import app
from app.database import Base, get_db
from app.models import Invoice, PurchaseOrder, Vendor
from app.agents.retrieve_agent import RetrieveAgent

test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


ACME = "Acme Office Supplies"
MERIDIAN = "Meridian Cloud Services"
ACME_ITEMS = [("Office chairs", 5, 150.0), ("Standing desks", 2, 250.0)]
MERIDIAN_ITEMS = [("Cloud hosting monthly", 1, 2000.0)]


def invoice_text(number, vendor, items, total, po=None):
    lines = [f"Invoice Number: {number}", f"Vendor: {vendor}"]
    if po:
        lines.append(f"PO Number: {po}")
    lines.append("Due Date: 2026-10-15")
    lines += [f"{d} {q:g} x ${p:.2f}" for d, q, p in items]
    lines.append(f"Total Amount Due: ${total:.2f}")
    return "\n".join(lines)


class RegressionBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[get_db] = override_get_db
        # raise_server_exceptions=False: an unhandled error must show up as the HTTP 500 a
        # real client would get, so the assertion on the status code is what fails.
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def setUp(self):
        Base.metadata.create_all(bind=test_engine)
        db = TestingSessionLocal()
        try:
            acme = Vendor(name=ACME, approved=True, avg_invoice_amount=1200.0, invoice_count=8)
            meridian = Vendor(name=MERIDIAN, approved=True, avg_invoice_amount=2000.0, invoice_count=6)
            db.add_all([acme, meridian])
            db.commit()
            db.add_all([
                PurchaseOrder(po_number="PO-1001", vendor_id=acme.id, amount=1250.0, line_items=[
                    {"description": "Office chairs", "qty": 5, "unit_price": 150.0},
                    {"description": "Standing desks", "qty": 2, "unit_price": 250.0}]),
                PurchaseOrder(po_number="PO-1003", vendor_id=meridian.id, amount=2000.0, line_items=[
                    {"description": "Cloud hosting monthly", "qty": 1, "unit_price": 2000.0}]),
            ])
            db.commit()
            self.acme_id, self.meridian_id = acme.id, meridian.id
        finally:
            db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=test_engine)

    def submit(self, raw_text):
        res = self.client.post("/invoices/submit-text", data={"raw_text": raw_text})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def invoice_count(self):
        return self.client.get("/dashboard/stats").json()["total_invoices"]

    def vendor(self, vendor_id):
        return next(v for v in self.client.get("/vendors").json() if v["id"] == vendor_id)


class TestExactVendorMatching(RegressionBase):
    """Fix 1: `ilike` treated % and _ in a vendor name as wildcards."""

    def test_wildcard_vendor_names_do_not_resolve_to_a_real_vendor(self):
        for i, name in enumerate(["%", "Acme%", "A%s", "Acme_Office_Supplies"]):
            with self.subTest(vendor_name=name):
                inv = self.submit(invoice_text(f"INV-W{i}", name, ACME_ITEMS, 1250.0))
                self.assertFalse(inv["retrieval_context"]["vendor_found"])
                self.assertIsNone(inv["vendor_id"])
                self.assertNotEqual(inv["risk_level"], "low")

    def test_vendor_lookup_is_still_case_insensitive(self):
        inv = self.submit(invoice_text("INV-CI", ACME.upper(), ACME_ITEMS, 1250.0))
        self.assertTrue(inv["retrieval_context"]["vendor_found"])
        self.assertEqual(inv["vendor_id"], self.acme_id)
        self.assertEqual(inv["risk_level"], "low")

    def test_creating_a_vendor_named_with_a_wildcard_is_not_reported_as_existing(self):
        res = self.client.post("/vendors", json={"name": "100% Organic Supplies", "approved": True})
        self.assertEqual(res.status_code, 200, res.text)
        res = self.client.post("/vendors", json={"name": "%", "approved": False})
        self.assertEqual(res.status_code, 200, res.text)

    def test_creating_a_vendor_that_differs_only_by_case_is_still_refused(self):
        res = self.client.post("/vendors", json={"name": ACME.lower(), "approved": True})
        self.assertEqual(res.status_code, 400)

    def test_po_created_for_a_wildcard_vendor_name_is_not_attached_to_a_real_vendor(self):
        res = self.client.post("/purchase-orders", json={
            "po_number": "PO-WILD", "vendor_name": "%", "amount": 10.0, "line_items": []})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertNotIn(res.json()["vendor_id"], (self.acme_id, self.meridian_id))

    def test_po_number_lookup_does_not_treat_underscore_as_a_wildcard(self):
        # The regex extractor cannot produce "_" in a PO number, but the LLM extractor can
        # return any string, so the lookup itself is exercised directly.
        db = TestingSessionLocal()
        try:
            invoice = Invoice(invoice_number="PENDING-EXTRACTION", raw_text="")
            db.add(invoice)
            db.commit()
            context = RetrieveAgent(db).run(invoice.id, {
                "vendor_name": ACME, "invoice_number": "INV-PO-WILD", "po_number": "PO-100_", "amount": 1250.0})
        finally:
            db.close()
        self.assertNotEqual(context["po_match_method"], "po_number")


class TestPONumberMatching(RegressionBase):
    """PO lookup is an exact match that ignores letter case and surrounding whitespace, the two
    things the original code already ignored. Nothing else is normalised, and a cited number
    can never match by wildcard or by being part of a real PO number."""

    def lookup(self, po_number):
        db = TestingSessionLocal()
        try:
            invoice = Invoice(invoice_number="PENDING-EXTRACTION", raw_text="")
            db.add(invoice)
            db.commit()
            context = RetrieveAgent(db).run(invoice.id, {
                "vendor_name": MERIDIAN, "invoice_number": "INV-PO-MATRIX", "po_number": po_number, "amount": 2000.0})
        finally:
            db.close()
        return context["po_match_method"], context["matched_po_number"]

    def test_exact_number_matches_regardless_of_case_and_surrounding_whitespace(self):
        for cited in ("PO-1003", "po-1003", "Po-1003", "  PO-1003  ", "PO-1003\n", "\tPO-1003"):
            with self.subTest(cited=cited):
                self.assertEqual(self.lookup(cited), ("po_number", "PO-1003"))

    def test_nonexistent_partial_and_reformatted_numbers_do_not_match(self):
        cases = ("PO-9999",                                                   # does not exist
                 "PO-100", "PO-10", "1003", "O-1003", "PO-10031", "PO-1003-A",  # part of, or longer than, a real number
                 "PO 1003", "PO1003", "PO-01003")                              # no normalisation beyond case and trim
        for cited in cases:
            with self.subTest(cited=cited):
                self.assertEqual(self.lookup(cited), (None, None))

    def test_sql_wildcards_and_escapes_are_ordinary_characters(self):
        for cited in ("%", "PO-%", "%1003", "PO-100_", "_O-1003", "PO-____", "PO-1003%", "PO-1003_", "PO-1003\\", "PO-100\\_"):
            with self.subTest(cited=cited):
                self.assertEqual(self.lookup(cited), (None, None))

    def test_invoice_citing_its_po_in_lower_case_is_matched_and_clean(self):
        inv = self.submit(invoice_text("INV-POCASE", MERIDIAN, MERIDIAN_ITEMS, 2000.0, po="po-1003"))
        self.assertEqual(inv["retrieval_context"]["po_match_method"], "po_number")
        self.assertEqual(inv["retrieval_context"]["matched_po_number"], "PO-1003")
        self.assertEqual(inv["risk_level"], "low")

    def test_invoice_citing_part_of_a_po_number_is_not_matched(self):
        inv = self.submit(invoice_text("INV-POPART", MERIDIAN, MERIDIAN_ITEMS, 2000.0, po="PO-100"))
        self.assertIsNone(inv["retrieval_context"]["matched_po_number"])
        self.assertIsNone(inv["retrieval_context"]["po_match_method"])
        self.assertTrue(any("PO-100" in issue for issue in inv["validation_result"]["issues"]))
        self.assertNotEqual(inv["risk_level"], "low")

    def test_a_po_number_that_differs_only_by_case_cannot_be_created(self):
        # Lookup ignores case, so two purchase orders whose numbers differ only by case would
        # make a citation ambiguous: one of them could never be matched.
        res = self.client.post("/purchase-orders", json={
            "po_number": "po-1003", "vendor_name": ACME, "amount": 800.0, "line_items": []})
        self.assertEqual(res.status_code, 400, res.text)
        numbers = [po["po_number"] for po in self.client.get("/purchase-orders").json()]
        self.assertEqual(sorted(numbers), ["PO-1001", "PO-1003"])


class TestUnlistedVendorCitingPO(RegressionBase):
    """Fix 2: an unknown vendor name was replaced by the vendor that owns the cited PO."""

    def assert_not_resolved(self, inv):
        ctx = inv["retrieval_context"]
        self.assertFalse(ctx["vendor_found"])
        self.assertIsNone(inv["vendor_id"])
        self.assertTrue(ctx["po_vendor_mismatch"])
        self.assertFalse(inv["validation_result"]["passed"])
        self.assertTrue(any("different vendor" in issue.lower() for issue in inv["validation_result"]["issues"]))
        self.assertEqual(inv["risk_level"], "high")
        self.assertEqual(inv["status"], "pending_review")

    def test_unlisted_payee_citing_another_vendors_po_is_flagged(self):
        inv = self.submit(invoice_text("INV-UP1", "Northwind Phantom Traders", MERIDIAN_ITEMS, 2000.0, po="PO-1003"))
        self.assert_not_resolved(inv)

    def test_a_misspelled_vendor_name_is_left_for_the_human_to_resolve(self):
        inv = self.submit(invoice_text("INV-UP2", "Meridian Cloud Servces", MERIDIAN_ITEMS, 2000.0, po="PO-1003"))
        self.assert_not_resolved(inv)

    def test_approving_it_does_not_change_the_po_vendors_history(self):
        before = self.vendor(self.meridian_id)
        inv = self.submit(invoice_text("INV-UP3", "Northwind Phantom Traders", MERIDIAN_ITEMS, 2000.0, po="PO-1003"))
        res = self.client.post(f"/invoices/{inv['id']}/approve", json={"decided_by": "reviewer@example.test"})
        self.assertEqual(res.status_code, 200, res.text)  # the human may still approve it
        after = self.vendor(self.meridian_id)
        self.assertEqual(after["invoice_count"], before["invoice_count"])

    def test_known_vendor_citing_its_own_po_is_unaffected(self):
        inv = self.submit(invoice_text("INV-UP4", MERIDIAN, MERIDIAN_ITEMS, 2000.0, po="PO-1003"))
        self.assertEqual(inv["retrieval_context"]["po_match_method"], "po_number")
        self.assertFalse(inv["retrieval_context"]["po_vendor_mismatch"])
        self.assertEqual(inv["risk_level"], "low")


class TestCitedPONotFound(RegressionBase):
    """Fix 3: a cited PO number that does not exist silently fell back to amount matching."""

    def test_nonexistent_cited_po_is_a_validation_issue(self):
        inv = self.submit(invoice_text("INV-NP1", MERIDIAN, MERIDIAN_ITEMS, 2000.0, po="PO-9999"))
        ctx = inv["retrieval_context"]
        self.assertIsNone(ctx["matched_po_number"])
        self.assertIsNone(ctx["po_match_method"])
        self.assertFalse(inv["validation_result"]["passed"])
        self.assertTrue(any("PO-9999" in issue for issue in inv["validation_result"]["issues"]))
        self.assertNotEqual(inv["risk_level"], "low")

    def test_amount_fallback_still_applies_when_no_po_is_cited(self):
        inv = self.submit(invoice_text("INV-NP2", MERIDIAN, MERIDIAN_ITEMS, 2000.0))
        ctx = inv["retrieval_context"]
        self.assertEqual(ctx["po_match_method"], "amount_proximity")
        self.assertEqual(ctx["matched_po_number"], "PO-1003")
        self.assertTrue(inv["validation_result"]["passed"])
        self.assertEqual(inv["risk_level"], "low")


class TestDecidedByRequired(RegressionBase):
    """Fix 4: `decided_by` defaulted to "staff_user" and accepted blank values."""

    def test_blank_or_missing_decided_by_is_rejected_and_nothing_is_decided(self):
        inv = self.submit(invoice_text("INV-DB1", ACME, ACME_ITEMS, 1250.0))
        for decision in ("approve", "reject"):
            for payload in ({}, {"decided_by": ""}, {"decided_by": "   "}, {"note": "no reviewer given"}):
                with self.subTest(decision=decision, payload=payload):
                    res = self.client.post(f"/invoices/{inv['id']}/{decision}", json=payload)
                    self.assertEqual(res.status_code, 422, res.text)
        current = self.client.get(f"/invoices/{inv['id']}").json()
        self.assertEqual(current["status"], "pending_review")
        self.assertIsNone(current["decided_by"])
        self.assertFalse(any(a["action"].startswith("human_") for a in current["audit_logs"]))

    def test_decided_by_is_stored_trimmed(self):
        inv = self.submit(invoice_text("INV-DB2", ACME, ACME_ITEMS, 1250.0))
        res = self.client.post(f"/invoices/{inv['id']}/approve",
                               json={"decided_by": "  reviewer@example.test  ", "note": "ok"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "approved")
        self.assertEqual(res.json()["decided_by"], "reviewer@example.test")


class TestCaseInsensitiveDuplicates(RegressionBase):
    """Fix 5: `inv-9001` after `INV-9001` was not recognised as a duplicate."""

    def test_same_invoice_number_in_another_case_is_a_duplicate(self):
        first = self.submit(invoice_text("INV-9001", ACME, ACME_ITEMS, 1250.0))
        second = self.submit(invoice_text("inv-9001", ACME, ACME_ITEMS, 1250.0))
        self.assertEqual(second["retrieval_context"]["duplicate_invoice_ids"], [first["id"]])
        self.assertTrue(any("duplicate" in flag.lower() for flag in second["assessment_result"]["flags"]))
        self.assertEqual(second["risk_level"], "high")

    def test_a_different_invoice_number_is_not_a_duplicate(self):
        self.submit(invoice_text("INV-9001", ACME, ACME_ITEMS, 1250.0))
        other = self.submit(invoice_text("INV-9002", ACME, ACME_ITEMS, 1250.0))
        self.assertEqual(other["retrieval_context"]["duplicate_invoice_ids"], [])


def _image_only_pdf() -> bytes:
    """A PDF whose only content is a picture: no text layer, so it can only be read by OCR."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buffer, format="PDF")
    return buffer.getvalue()


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class TestBlankAndUnreadableInput(RegressionBase):
    """Fix 6: blank input was stored as an invoice; unreadable uploads returned HTTP 500."""

    def upload(self, name, content):
        return self.client.post("/invoices/upload", files={"file": (name, content, "application/octet-stream")})

    def test_blank_text_submission_is_rejected(self):
        for raw in ("", "   \n\t  "):
            with self.subTest(raw_text=raw):
                res = self.client.post("/invoices/submit-text", data={"raw_text": raw})
                self.assertEqual(res.status_code, 422, res.text)
        self.assertEqual(self.invoice_count(), 0)

    def test_empty_file_upload_is_rejected(self):
        res = self.upload("empty.txt", b"")
        self.assertEqual(res.status_code, 422, res.text)
        self.assertEqual(self.invoice_count(), 0)

    def test_corrupt_pdf_is_a_client_error_with_a_message(self):
        for name, content in (("corrupt.pdf", b"this is not a pdf"), ("empty.pdf", b"")):
            with self.subTest(file=name):
                res = self.upload(name, content)
                self.assertEqual(res.status_code, 422, res.text)
                self.assertIsInstance(res.json().get("detail"), str)
        self.assertEqual(self.invoice_count(), 0)

    def test_corrupt_image_is_a_client_error_with_a_message(self):
        res = self.upload("corrupt.png", b"this is not an image")
        self.assertEqual(res.status_code, 422, res.text)
        self.assertIsInstance(res.json().get("detail"), str)
        self.assertEqual(self.invoice_count(), 0)

    def test_missing_ocr_tools_are_reported_as_service_unavailable(self):
        # With an empty PATH neither poppler (pdftoppm) nor tesseract can be found, which is
        # what a host without the OCR system packages looks like.
        cases = (("scan.pdf", _image_only_pdf()), ("scan.png", _png()))
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-invoiceguard-test-path"}):
            for name, content in cases:
                with self.subTest(file=name):
                    res = self.upload(name, content)
                    self.assertEqual(res.status_code, 503, res.text)
                    self.assertIsInstance(res.json().get("detail"), str)
        self.assertEqual(self.invoice_count(), 0)

    def test_a_valid_text_upload_still_works(self):
        res = self.upload("invoice.txt", invoice_text("INV-TXT", ACME, ACME_ITEMS, 1250.0).encode())
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["risk_level"], "low")


if __name__ == "__main__":
    unittest.main()
