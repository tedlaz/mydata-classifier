"""Έλεγχος: έξοδα με μηδενική αξία και ΦΠΑ χαρακτηρίζονται τοπικά ως 2.5 / E3_585_016 στην ανάκτηση.
Εκτέλεση: python -m tests.test_zero_docs"""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
for mark, net, vat, status in (("2001", 0.0, 0.0, "unclassified"), ("2002", 100.0, 24.0, "unclassified"),
                               ("2003", 0.0, 0.0, "unclassified"), ("2004", 0.0, 0.0, "unclassified")):
    db.upsert_document(cid, "expense", {
        "mark": mark, "issue_date": "2026-01-10", "issuer_vat": "111", "invoice_type": "1.1",
        "total_net": net, "total_vat": vat, "total_gross": net + vat,
        "lines": [{"line_number": 1, "net_value": net, "vat_amount": vat, "vat_category": "1"}] if mark != "2004" else [],
    }, status)
# Ήδη τοπικά χαρακτηρισμένο μηδενικό: δεν αγγίζεται.
db.save_local_classification(cid, "2003", [{"line_number": 1, "classification_type": "E3_102_001",
                                            "classification_category": "category2_1", "amount": 0.0}])

assert A._classify_zero_docs(cid, ["2001", "2002", "2003", "2004", "9999"]) == 2
for mark in ("2001", "2004"):  # 2004: χωρίς γραμμές → συμβολική γραμμή 1
    row = db.get_document(cid, mark)
    assert row["status"] == "confirmed" and row["source"] == "manual", dict(row)
    assert [(c["type"], c["category"]) for c in json.loads(row["cls_json"])] == [("E3_585_016", "category2_5")], row["cls_json"]
assert db.get_document(cid, "2002")["status"] == "unclassified"
assert db.get_document(cid, "2003")["status"] == "classified"
assert A._classify_zero_docs(cid, ["2001"]) == 0  # δεύτερη ανάκτηση: τίποτα
print("OK")
