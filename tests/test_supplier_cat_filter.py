"""Φίλτρο συναλλασσόμενων ανά κατηγορία χαρακτηρισμού (python -m tests.test_supplier_cat_filter)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
with db.get_conn() as c:
    cid = c.execute("INSERT INTO companies (company_name) VALUES ('Α')").lastrowid
db.set_setting("active_company_id", str(cid))
for vat, name in (("111", "ΕΜΠΟΡΟΣ"), ("222", "ΠΑΓΙΑ ΑΕ"), ("333", "ΧΩΡΙΣ ΠΡΟΤΑΣΗ")):
    db.upsert_supplier(vat, name)
db.save_rule(cid, "111", "E3_102_001", "category2_1", "VAT_361")
db.save_rule_patterns(cid, "222", {"1": {"category": "category2_7", "type": "E3_102_001", "vat_type": "VAT_362"}})

cl = A.app.test_client()
html = cl.get("/suppliers").get_data(as_text=True)
assert 'name="cat"' in html and "Αγορές Παγίων" in html and "Λήψη Υπηρεσιών" not in html
html = cl.get("/suppliers?cat=category2_7").get_data(as_text=True)
assert "ΠΑΓΙΑ ΑΕ" in html and "ΕΜΠΟΡΟΣ" not in html
html = cl.get("/suppliers?cat=category2_1").get_data(as_text=True)
assert "ΕΜΠΟΡΟΣ" in html and "ΠΑΓΙΑ ΑΕ" not in html
db.upsert_supplier("444", "ΜΟΝΟ ΠΑΡΑΣΤΑΤΙΚΟ")
with db.get_conn() as c:
    c.execute("INSERT INTO documents (company_id, kind, mark, counterparty_vat, status) "
              "VALUES (?, 'expense', 'm1', '444', 'unclassified')", (cid,))
html = cl.get("/suppliers?cat=none").get_data(as_text=True)
assert "ΜΟΝΟ ΠΑΡΑΣΤΑΤΙΚΟ" in html and "ΕΜΠΟΡΟΣ" not in html and "ΠΑΓΙΑ ΑΕ" not in html
print("ok")
