"""Έλεγχος: τρόπος μαζικού χαρακτηρισμού (bulk_post_mode) — auto / invoice / line.
Εκτέλεση: python -m tests.test_bulk_post_mode"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "allow_send": True, "allow_classify": True, "allow_new": True})
db.set_active_company_id(cid)
db.save_rule(cid, "111", "E3_102_001", "category2_1", "VAT_361")
line = lambda n: {"line_number": n, "net_value": 50.0, "vat_amount": 12.0, "vat_category": "1"}  # noqa: E731
for mark, k in (("1", 1), ("2", 2)):
    db.upsert_document(cid, "expense", {
        "mark": mark, "issue_date": "2026-01-10", "issuer_vat": "111", "invoice_type": "1.1",
        "total_net": 50.0 * k, "total_vat": 12.0 * k, "total_gross": 62.0 * k,
        "lines": [line(n) for n in range(1, k + 1)],
    }, "unclassified")
c = A.app.test_client()
mode_of = lambda m: int(db.get_document(cid, m).get("cls_post_mode") or 0)  # noqa: E731


def run(mode, action="rules", **extra):
    for m in ("1", "2"):
        db.unclassify(cid, [m])
    data = {"marks": ["1", "2"], "action": action, **extra}
    if mode:
        data["bulk_post_mode"] = mode
    c.post("/bulk_classify", data=data)
    return mode_of("1"), mode_of("2")


assert run(None) == (0, 1)  # default auto: μονόγραμμο ανά γραμμή, πολύγραμμο συγκεντρωτικά
assert run("invoice") == (1, 1)
assert run("line") == (0, 0)
db.set_rule_post_mode(cid, "111", 1)
assert run("rule") == (1, 1)
db.set_rule_post_mode(cid, "111", 0)
assert run("rule") == (0, 0)
manual = {"bulk_type": "E3_102_001", "bulk_category": "category2_1", "bulk_vat_type": "none"}
assert run("auto", "manual", **manual) == (0, 1)
e = db.get_local_classification(db.get_document(cid, "2")["id"])
assert [x["amount"] for x in e] == [124.0], e  # «χωρίς ΦΠΑ» → ο ΦΠΑ στο Ε3
print("OK")
