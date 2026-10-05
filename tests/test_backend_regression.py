import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.main import app
from app.database import Base, get_db
from app.models import Vendor, PurchaseOrder, Invoice

# Create isolated test in-memory SQLite database with StaticPool
TEST_SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestBackendRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def setUp(self):
        # Create fresh tables for each test
        Base.metadata.create_all(bind=test_engine)

    def tearDown(self):
        # Drop tables after each test
        Base.metadata.drop_all(bind=test_engine)

    def test_root_endpoint(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("service"), "InvoiceGuard API")
        self.assertEqual(data.get("status"), "ok")

    def test_vendor_and_po_flow(self):
        # 1. Create a vendor
        vendor_res = self.client.post("/vendors", json={"name": "Acme Industrial Tools", "approved": True})
        self.assertEqual(vendor_res.status_code, 200)
        vendor_data = vendor_res.json()
        self.assertEqual(vendor_data["name"], "Acme Industrial Tools")
        self.assertTrue(vendor_data["approved"])

        # 2. List vendors
        vendors_list = self.client.get("/vendors")
        self.assertEqual(vendors_list.status_code, 200)
        self.assertEqual(len(vendors_list.json()), 1)

        # 3. Create a PO
        po_res = self.client.post(
            "/purchase-orders",
            json={
                "po_number": "PO-TEST-1001",
                "vendor_name": "Acme Industrial Tools",
                "amount": 5400.0,
                "line_items": [
                    {"description": "Industrial drill kit", "qty": 10.0, "unit_price": 540.0}
                ]
            }
        )
        self.assertEqual(po_res.status_code, 200)
        po_data = po_res.json()
        self.assertEqual(po_data["po_number"], "PO-TEST-1001")

        # 4. List POs
        pos_list = self.client.get("/purchase-orders")
        self.assertEqual(pos_list.status_code, 200)
        self.assertEqual(len(pos_list.json()), 1)

    def test_dashboard_stats_empty(self):
        response = self.client.get("/dashboard/stats")
        self.assertEqual(response.status_code, 200)
        stats = response.json()
        self.assertEqual(stats["total_invoices"], 0)
        self.assertEqual(stats["pending_review"], 0)
        self.assertEqual(stats["approved"], 0)
        self.assertEqual(stats["rejected"], 0)

    def test_invoice_submit_and_decision_lifecycle(self):
        # First ensure vendor & PO exist for matching. Both fixture calls are asserted:
        # if either one fails, the rest of the test would exercise a different scenario
        # (an invoice with no purchase order) than the one it is meant to cover.
        vendor_res = self.client.post("/vendors", json={"name": "Apex Logistics", "approved": True})
        self.assertEqual(vendor_res.status_code, 200, vendor_res.text)
        po_res = self.client.post(
            "/purchase-orders",
            json={
                "po_number": "PO-9921",
                "vendor_name": "Apex Logistics",
                "amount": 1250.0,
                "line_items": [{"description": "Standard freight shipping", "qty": 1, "unit_price": 1250.0}]
            }
        )
        self.assertEqual(po_res.status_code, 200, po_res.text)
        self.assertEqual(po_res.json()["po_number"], "PO-9921")
        self.assertEqual(po_res.json()["vendor_id"], vendor_res.json()["id"])

        sample_raw_invoice = """
INVOICE
Vendor: Apex Logistics
Invoice Number: INV-2026-001
Date: 2026-09-15
PO Number: PO-9921

Line Items:
1. Standard freight shipping - Qty: 1 - Unit Price: $1250.00 - Total: $1250.00

Total: $1250.00
Payment Terms: Net 30
        """

        # Submit text
        submit_res = self.client.post(
            "/invoices/submit-text",
            data={"raw_text": sample_raw_invoice}
        )
        self.assertEqual(submit_res.status_code, 200)
        inv = submit_res.json()
        self.assertEqual(inv["status"], "pending_review")
        invoice_id = inv["id"]

        # The invoice must be the clean, PO-matched one this lifecycle is about:
        # matched to PO-9921 by its cited number, validation passed, low risk.
        self.assertEqual(inv["retrieval_context"]["matched_po_number"], "PO-9921")
        self.assertEqual(inv["retrieval_context"]["po_match_method"], "po_number")
        self.assertFalse(inv["retrieval_context"]["po_vendor_mismatch"])
        self.assertTrue(inv["validation_result"]["passed"], inv["validation_result"]["issues"])
        self.assertEqual(inv["assessment_result"]["risk_score"], 0)
        self.assertEqual(inv["risk_level"], "low")
        self.assertIsNone(inv["decided_by"])

        # Fetch detail
        detail_res = self.client.get(f"/invoices/{invoice_id}")
        self.assertEqual(detail_res.status_code, 200)
        self.assertEqual(detail_res.json()["id"], invoice_id)

        # Human Approval
        decision_res = self.client.post(
            f"/invoices/{invoice_id}/approve",
            json={"decided_by": "demo@invoiceguard.local", "note": "Verified against PO-9921."}
        )
        self.assertEqual(decision_res.status_code, 200)
        self.assertEqual(decision_res.json()["status"], "approved")

        # Try approving again (should fail with 400 because already approved)
        conflict_res = self.client.post(
            f"/invoices/{invoice_id}/approve",
            json={"decided_by": "demo@invoiceguard.local", "note": "Duplicate approval"}
        )
        self.assertEqual(conflict_res.status_code, 400)


    def test_po_vendor_mismatch_is_high_risk(self):
        # Create two different approved vendors.
        self.client.post(
            "/vendors",
            json={"name": "Meridian Test Services", "approved": True},
        )
        self.client.post(
            "/vendors",
            json={"name": "Sterling Test Signage", "approved": True},
        )

        # PO belongs to Meridian.
        po_res = self.client.post(
            "/purchase-orders",
            json={
                "po_number": "PO-MISMATCH-001",
                "vendor_name": "Meridian Test Services",
                "amount": 2000.0,
                "line_items": [
                    {
                        "description": "Cloud hosting monthly",
                        "qty": 1,
                        "unit_price": 2000.0,
                    }
                ],
            },
        )
        self.assertEqual(po_res.status_code, 200)

        # Invoice claims to be from Sterling but explicitly cites
        # Meridian's purchase order.
        raw_invoice = """
Invoice Number: INV-MISMATCH-001
Vendor: Sterling Test Signage
PO Number: PO-MISMATCH-001
Due Date: 2026-10-28
Cloud hosting monthly 1 x $2000.00
Total Amount Due: $2000.00
"""

        submit_res = self.client.post(
            "/invoices/submit-text",
            data={"raw_text": raw_invoice},
        )

        self.assertEqual(submit_res.status_code, 200)

        invoice = submit_res.json()

        # Retrieval must identify the cross-vendor PO reference.
        self.assertTrue(
            invoice["retrieval_context"]["po_vendor_mismatch"]
        )
        self.assertEqual(
            invoice["retrieval_context"]["po_match_method"],
            "po_number",
        )

        # Validation must reject the vendor/PO relationship.
        self.assertFalse(invoice["validation_result"]["passed"])
        self.assertTrue(
            any(
                "different vendor" in issue.lower()
                for issue in invoice["validation_result"]["issues"]
            )
        )

        # A cross-vendor PO mismatch is a high-risk event.
        self.assertEqual(invoice["risk_level"], "high")
        self.assertGreaterEqual(
            invoice["assessment_result"]["risk_score"],
            50,
        )
        self.assertTrue(
            any(
                "po vendor mismatch" in flag.lower()
                for flag in invoice["assessment_result"]["flags"]
            )
        )


if __name__ == "__main__":
    unittest.main()
