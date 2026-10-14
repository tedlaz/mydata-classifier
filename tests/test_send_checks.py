"""Σφάλματα myDATA στα ελληνικά, έλεγχος πριν την αποστολή, ένδειξη «αλλαγή απεσταλμένου»."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402
from mydata_client import parse_response_doc  # noqa: E402

# 1) Εξήγηση σφάλματος.
r = parse_response_doc(b'<ResponseDoc><response><statusCode>ValidationError</statusCode><errors><error>'
                       b'<message>Duplicate classification type</message><code>305</code></error></errors></response></ResponseDoc>')
assert "δύο φορές" in r["errors"][0], r

db.init_db()
cid = db.add_company({"company_name": "T", "AADE_VAT_NUMBER": "046949583", "allow_classify": True, "allow_send": True})
db.set_active_company_id(cid)
lines = [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"},
         {"line_number": 2, "net_value": 50.0, "vat_amount": 12.0, "vat_category": "1"}]
doc = {"mark": "5001", "issue_date": "2026-10-01", "issuer_vat": "123456789", "invoice_type": "1.1",
       "total_net": 150.0, "total_vat": 36.0, "total_gross": 186.0, "lines": lines}
E = {"line_number": 1, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 100.0}
db.upsert_document(cid, "expense", doc, "unclassified")
db.save_local_classification(cid, "5001", [E])  # λείπει η γραμμή 2

# 2) Ελλιπής χαρακτηρισμός: ⚠ στη λίστα, η αποστολή δεν καλεί το myDATA.
# Δεύτερο, σωστό παραστατικό στην ίδια αποστολή.
E2 = dict(E, line_number=2, amount=50.0)
db.upsert_document(cid, "expense", dict(doc, mark="5002"), "unclassified")
db.save_local_classification(cid, "5002", [E, E2])

calls = []


class Recorder:
    def send_expenses_classification(self, mark, *a, **k):
        calls.append(mark)
        return {"status": "Success", "classification_mark": "9" + mark, "errors": []}


A.get_client = Recorder
c = A.app.test_client()
page = c.get("/invoices?view=classified").get_data(as_text=True)
assert "⚠ θέλει διόρθωση" in page and "η γραμμή 2" in page, page
# Ένα λάθος → όλη η αποστολή ακυρώνεται: καμία κλήση, και τα δύο μένουν χαρακτηρισμένα.
c.post("/send", data={"marks": ["5001", "5002"]})
page = c.get("/invoices?view=classified").get_data(as_text=True)
assert calls == [], calls
assert "δεν στάλθηκε κανένα" in page and 'href="/classify/5001">Διόρθωση</a>' in page, page
assert db.get_document(cid, "5001")["status"] == db.get_document(cid, "5002")["status"] == "classified"
# Μετά τη διόρθωση στέλνονται και τα δύο.
db.save_local_classification(cid, "5001", [E, E2])
c.post("/send", data={"marks": ["5001", "5002"]})
assert sorted(calls) == ["5001", "5002"], calls
assert db.get_document(cid, "5001")["status"] == "sent"

# 3) Απεσταλμένο που αλλάχτηκε → «↺ αλλαγή απεσταλμένου» με τον παλιό τύπο.
db.save_local_classification(cid, "5001", [dict(E, classification_type="E3_102_001", classification_category="category2_1"), E2])
page = c.get("/invoices?view=classified").get_data(as_text=True)
assert "↺ αλλαγή απεσταλμένου" in page and "Πριν: E3_585_016" in page and "⚠ θέλει διόρθωση" not in page, page
print("ok")
