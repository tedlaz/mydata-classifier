"""Διόρθωση χαρακτηρισμού από λίστα: επιστροφή στην ίδια οθόνη (python -m tests.test_classify_back)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583", "allow_classify": True})
db.set_active_company_id(cid)
doc = {"mark": "4001", "issue_date": "2026-10-01", "issuer_vat": "123456789", "invoice_type": "1.1",
       "total_net": 100.0, "total_vat": 24.0, "total_gross": 124.0,
       "lines": [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"}]}
db.upsert_document(cid, "expense", doc, "unclassified")
db.save_local_classification(cid, "4001", [
    {"line_number": 1, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 100.0}])
c = A.app.test_client()
back = "/invoices?view=classified&f_issuer=123"

# Η λίστα στέλνει το back· η σελίδα διόρθωσης το δείχνει («← Βιβλίο εξόδων», Άκυρο, hidden πεδίο).
page = c.get("/invoices?view=classified&f_issuer=123").get_data(as_text=True).replace("&amp;", "&")
assert "/classify/4001?back=" in page, page[:500]
page = c.get("/classify/4001", query_string={"back": back}).get_data(as_text=True).replace("&amp;", "&")
assert f'href="{back}">← Βιβλίο εξόδων' in page and f'name="back" value="{back}"' in page

# Εξωτερικό back αγνοείται.
page = c.get("/classify/4001", query_string={"back": "//evil.com/x"}).get_data(as_text=True)
assert 'href="//evil' not in page and 'name="back" value="//evil' not in page and "← Βιβλίο εξόδων" not in page

# Αποθήκευση με back → πίσω στη λίστα (με τα φίλτρα).
form = {"back": back, "post_mode": "0", "line_number": "1", "category": "category2_4", "type": "E3_585_016",
        "amount": "100.00", "vat_type": "VAT_361"}
r = c.post("/submit/4001", data=form)
assert r.status_code == 302 and r.headers["Location"].endswith(back), r.headers.get("Location")

# Αχαρακτήριστα ταξινομημένα: ο χαρακτηρισμός συνεχίζει στο επόμενο κρατώντας το back, στο τέλος γυρίζει στη λίστα.
for m in ("4002", "4003"):
    db.upsert_document(cid, "expense", dict(doc, mark=m), "unclassified")
uback = "/invoices?view=unclassified&sort=name&dir=desc"
page = c.get(uback).get_data(as_text=True).replace("&amp;", "&")
assert "/classify/4002?back=" in page
r = c.post("/submit/4002", data=dict(form, back=uback))
assert "/classify/4003?back=" in r.headers["Location"] and "sort%3Dname" in r.headers["Location"], r.headers["Location"]
r = c.post("/submit/4003", data=dict(form, back=uback))
assert r.headers["Location"].endswith(uback), r.headers["Location"]
print("ok")
