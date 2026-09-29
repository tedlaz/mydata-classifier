"""Έλεγχος: CSRF, οθόνη επιβεβαίωσης αυτόματου χαρακτηρισμού, επαναφορά σε αχαρακτήριστα.
Εκτέλεση: python test_auto_classify.py"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
db.save_rule(cid, "111", "E3_102_001", "category2_1", "VAT_361")
for mark, vat in (("1001", "111"), ("1002", "999")):
    db.upsert_document(cid, "expense", {
        "mark": mark, "issue_date": "2026-01-10", "issuer_vat": vat, "invoice_type": "1.1",
        "total_net": 100.0, "total_vat": 24.0, "total_gross": 124.0,
        "lines": [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"}],
    }, "unclassified")
A.get_client = lambda: NS(request_unclassified_expenses=lambda *a: [], request_classified_expenses=lambda *a: [])
A.enrich_names = lambda *a, **k: None
c = A.app.test_client()
status = lambda m: db.get_document(cid, m)["status"]  # noqa: E731
fetch = {"date_from": "2026-01-01", "date_to": "2026-01-31"}

# 1) CSRF: ξένο Origin → 403, ίδιο/κανένα → περνά.
assert c.post("/fetch", data=fetch, headers={"Origin": "http://evil.com"}).status_code == 403
assert c.post("/fetch", data=fetch, headers={"Origin": "http://localhost"}).status_code == 302

# 2) Ρύθμιση ανενεργή → /invoices· ενεργή → οθόνη επιβεβαίωσης μόνο με όσα έχουν πρόταση.
assert c.post("/fetch", data=fetch).location.endswith("/invoices")
c.post("/parameters/automation", data={"auto_classify": "1"})
assert c.post("/fetch", data=fetch).location.endswith("/invoices/auto-classify")
page = c.get("/invoices/auto-classify").get_data(as_text=True)
assert "1001" in page and "1002" not in page
assert status("1001") == "unclassified"  # τίποτα πριν την επιβεβαίωση

# 3) Επιβεβαίωση → classified· επαναφορά → unclassified χωρίς γραμμές χαρακτηρισμού.
c.post("/bulk_classify", data={"marks": ["1001"], "action": "rules"})
assert status("1001") == "classified"
c.post("/unclassify", data={"marks": ["1001"]})
assert status("1001") == "unclassified"
assert db.get_local_classification(db.get_document(cid, "1001")["id"]) == []

# 4) Απεσταλμένο δεν επηρεάζεται.
db.upsert_document(cid, "expense", {"mark": "1003", "issuer_vat": "111"}, "sent")
assert db.unclassify(cid, ["1003"]) == 0 and status("1003") == "sent"
print("OK")
