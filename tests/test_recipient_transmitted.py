"""Έλεγχος: Διαβίβαση από Λήπτη λόγω παράλειψης Εκδότη (εκδότης ≠ εμείς) πάει στα ΕΞΟΔΑ, όχι στα έσοδα.
Εκτέλεση: python -m tests.test_recipient_transmitted"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402
from mydata_client import MyDataClient  # noqa: E402

c = MyDataClient("u", "k", "dev", own_vat="999758350")
docs = [NS(mark="1", issuer_vat="999758350", invoice_type="2.1", is_movement_doc=False, is_cancelled=False,
           has_income_line_classification=False),
        NS(mark="2", issuer_vat="800000000", invoice_type="2.1", is_movement_doc=False, is_cancelled=False,
           has_income_line_classification=False)]
c._parse_requested_doc = lambda root: docs
c._parse_income_classifications = lambda root: {}
c._parse_expenses_classifications = lambda root: {}
c._parse_cancelled_invoices = lambda root: set()
c.session.get = lambda *a, **k: NS(status_code=200, content=b"<x/>")

unc, cls, _ = c.request_income("01/01/2026", "31/01/2026")
assert [i.mark for i in unc + cls] == ["1"], unc
self_inv, _, _ = c.request_transmitted("01/01/2026", "31/01/2026")
assert [i.mark for i in self_inv] == ["2"], self_inv

# Παλιά εγγραφή στο λάθος βιβλίο: η ανάκτηση εξόδων τη μεταφέρει.
db.init_db()
cid = db.add_company({"company_name": "Test"})
doc = {"mark": "2", "issue_date": "2026-01-10", "issuer_vat": "800000000", "invoice_type": "2.1"}
db.upsert_document(cid, "income", doc, "unclassified")
db.upsert_document(cid, "expense", doc, "unclassified")
assert db.get_document(cid, "2")["kind"] == "expense"
print("OK")
