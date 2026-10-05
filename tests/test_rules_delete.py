"""Διαγραφή προτάσεων χαρακτηρισμού από τους Συναλλασσόμενους (python -m tests.test_rules_delete)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "A", "AADE_VAT_NUMBER": "046949583"})
other = db.add_company({"company_name": "B", "AADE_VAT_NUMBER": "099999999"})
db.set_active_company_id(cid)
rule = {"category": "category2_4", "type": "E3_585_016", "vat_type": "VAT_361"}
for vat, name in (("111111111", "Άλφα ΑΕ"), ("222222222", "Βήτα ΟΕ")):
    db.upsert_supplier(vat, name)
    db.save_rule_patterns(cid, vat, {"1": rule, "*": rule})
    db.save_rule_patterns(other, vat, {"1": rule})
    db.upsert_document(cid, "expense", {"mark": vat[:4], "issue_date": "2026-10-01", "issuer_vat": vat, "lines": []}, "confirmed")
c = A.app.test_client()

page = c.get("/suppliers").get_data(as_text=True)
assert "Διαγραφή προτάσεων (2)" in page and page.count('aria-label="Διαγραφή προτάσεων') == 2

# Ένας συναλλασσόμενος: μόνο οι δικές του, μόνο στην ενεργή εταιρεία· πίσω στην ίδια οθόνη.
r = c.post("/suppliers/rules-delete", data={"vat": "111111111", "back": "/suppliers?q=Άλφα"})
assert r.headers["Location"].endswith("/suppliers?q=%CE%86%CE%BB%CF%86%CE%B1") or "q=" in r.headers["Location"], r.headers["Location"]
assert "111111111" not in db.load_rule_patterns(cid) and "111111111" not in db.load_rules(cid)
assert "222222222" in db.load_rule_patterns(cid) and "111111111" in db.load_rule_patterns(other)

# Μαζικά, με αναζήτηση: μόνο όσοι ταιριάζουν.
c.post("/suppliers/rules-delete", data={"q": "Βήτα"})
assert db.load_rule_patterns(cid) == {} and set(db.load_rule_patterns(other)) == {"111111111", "222222222"}
assert "Διαγραφή προτάσεων (" not in c.get("/suppliers").get_data(as_text=True)

# Συναλλασσόμενος με κινήσεις δεν διαγράφεται (κουμπί ανενεργό, και ο server αρνείται)· χωρίς κινήσεις διαγράφεται.
db.upsert_supplier("333333333", "Γάμμα")
db.save_rule_patterns(cid, "333333333", {"1": rule})
page = c.get("/suppliers").get_data(as_text=True)
assert page.count("δεν διαγράφεται\"><button type=\"button\" class=\"btn-danger\" disabled>") == 2
c.post("/suppliers/delete/111111111")
assert db.get_supplier_name("111111111") == "Άλφα ΑΕ"
c.post("/suppliers/delete/333333333")
assert db.get_supplier_name("333333333") is None
print("ok")
