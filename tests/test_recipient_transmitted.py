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

# Νέα εγγραφή «Διαβίβαση από Λήπτη λόγω παράλειψης Εκδότη»: invoiceVariationType=1, εκδότης ο
# προμηθευτής, αντισυμβαλλόμενος εμείς, με ΦΠΑ.
import xml.etree.ElementTree as ET  # noqa: E402

sent = {}
c.session.post = lambda url, data, **k: sent.update(xml=data) or NS(status_code=200, content=b"<x/>")
import mydata_client  # noqa: E402

mydata_client.parse_response_doc = lambda b: {}
c.send_self_expense_invoice("2.1", "A", "7", "2026-01-23", [
    {"amount": 100.0, "classification_type": "E3_585_016", "classification_category": "category2_5",
     "vat_category": "1", "vat_amount": 24.0, "vat_type": "VAT_361"}],
    issuer_vat="800000000", own_vat="999758350")
inv = ET.fromstring(sent["xml"])[0]
q = lambda path: inv.findtext(path, namespaces={"i": "http://www.aade.gr/myDATA/invoice/v1.0"})  # noqa: E731
assert q("i:issuer/i:vatNumber") == "800000000" and q("i:counterpart/i:vatNumber") == "999758350"
header = [el.tag.split("}")[1] for el in inv.find("{http://www.aade.gr/myDATA/invoice/v1.0}invoiceHeader")]
assert header[-2:] == ["currency", "invoiceVariationType"] and q("i:invoiceHeader/i:invoiceVariationType") == "1", header
assert q("i:invoiceSummary/i:totalVatAmount") == "24.00"
print("OK omission send")
