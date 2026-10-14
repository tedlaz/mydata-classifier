"""Επαναφορά: απεσταλμένο που αλλάχτηκε τοπικά γυρνά στον αρχικό χαρακτηρισμό, όχι στα αχαρακτήριστα."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
doc = {"mark": "4001", "issue_date": "2026-10-01", "issuer_vat": "123456789", "invoice_type": "1.1",
       "total_net": 100.0, "total_vat": 24.0, "total_gross": 124.0, "lines": []}
A = [{"line_number": 1, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 100.0}]
B = [{"line_number": 1, "classification_type": "E3_102_001", "classification_category": "category2_1", "amount": 100.0}]
for m in ("4001", "4002"):
    db.upsert_document(cid, "expense", dict(doc, mark=m), "unclassified")
    db.save_local_classification(cid, m, A)
d = db.get_document(cid, "4001")
db.mark_sent(d["id"], "999")

for m in ("4001", "4002"):
    db.save_local_classification(cid, m, B)   # αλλαγή χαρακτηρισμού
db.save_local_classification(cid, "4001", B)  # 2η αλλαγή: το αντίγραφο μένει το αρχικό
assert db.unclassify(cid, ["4001", "4002"]) == 2

d = db.get_document(cid, "4001")
assert d["status"] == "sent" and not d["prev_state"], dict(d)
assert [r["classification_type"] for r in db.get_local_classification(d["id"])] == ["E3_585_016"]
assert "E3_585_016" in d["cls_json"]
d = db.get_document(cid, "4002")  # ποτέ απεσταλμένο → αχαρακτήριστο
assert d["status"] == "unclassified" and d["cls_json"] == "[]"
print("ok")
