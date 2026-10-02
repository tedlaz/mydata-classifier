"""Καθαρό κέρδος με αποθέματα: κόστος πωληθέντων = έναρξης + αγορές − λήξης (python -m tests.test_stock_cogs)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
c = A.app.test_client()
c.post("/companies/add", data={"company_name": "Εμπορική", "use_accountant": "1", "entity_type": "legal"})
cid = db.list_companies()[0]["id"]
db.set_active_company_id(cid)
y = A.datetime.now(A.ATHENS).year


def doc(kind, mark, net, typ, cat):
    db.upsert_document(cid, kind, {"mark": mark, "issue_date": f"{y}-01-15", "invoice_type": "2.1" if kind == "income" else "17.1",
                                   "total_net": net, "total_vat": 0, "total_gross": net,
                                   "cls_info": [{"type": typ, "category": cat, "amount": net}]}, "classified")


doc("income", "I1", 10000, "E3_561_001", "category1_1")
doc("expense", "P1", 6000, "E3_102_001", "category2_1")  # αγορές εμπορευμάτων
doc("expense", "S1", 1000, "E3_101", "category2_13")     # αποθέματα έναρξης
doc("expense", "S2", 2500, "E3_104", "category2_14")     # αποθέματα λήξης

# 10.000 − (1.000 + 6.000 − 2.500) = 5.500
assert A._year_profit(cid, str(y))[0] == 5500.0, A._year_profit(cid, str(y))
html = c.get(f"/reports/yearly/e3?year={y}").data.decode()
assert "5.500,00" in html and "4.500,00" in html and "2. Κόστος πωληθέντων" in html and "3. Έξοδα χρήσης" in html, "κέρδος / κόστος πωληθέντων στο modal Ε3"
for flag in ("1", "0"):  # ίδιο κέρδος με ή χωρίς τα αποθέματα στα έξοδα του πίνακα
    db.set_setting("stock_in_yearly", flag)
    assert "5.500,00" in c.get(f"/reports/yearly?year={y}").data.decode(), flag
print("ok")

# Πινακάκι «Καθαρό κέρδος»: κόστος πωληθέντων σε ένα πλαίσιο, αγορές παγίων κάτω από το κέρδος.
doc("expense", "A1", 800, "E3_882_001", "category2_7")
h = c.get(f"/reports/yearly?year={y}").data.decode()
g = h.index("yr-wf-group")
assert g < h.index("Αποθέματα λήξης", g) < h.index("= Κόστος πωληθέντων", g) < h.index("yr-wf-res") < h.index("Αγορές παγίων", g)
assert "5.500,00" in h and "--w:" in h
print("ok wf")

# ΦΠΑ μη εκπιπτόμενος (χωρίς χαρακτηρισμό ΦΠΑ): πληροφοριακά, στα ποσά Ε3 ή εκτός — το κέρδος δεν αλλάζει από αυτόν.
for mark, net, vat, e3 in (("V1", 100, 24, 124), ("V2", 50, 10, 50)):
    db.upsert_document(cid, "expense", {"mark": mark, "issue_date": f"{y}-02-10", "invoice_type": "2.1", "total_net": net,
                                        "total_vat": vat, "total_gross": net + vat,
                                        "cls_info": [{"type": "E3_585_009", "category": "category2_5", "amount": e3}]},
                       "classified")
docs = lambda k: db.yearly_documents(cid, k, A._INCOME_CLASSIFIED_STATUSES if k == "income" else A._EXPENSE_CLASSIFIED_STATUSES, str(y))  # noqa: E731
pl = A._pl(docs("income"), docs("expense"))
assert pl["nd_vat"] == {"total": 34.0, "in_e3": 24.0, "out_e3": 10.0}, pl["nd_vat"]
assert pl["profit"] == 5500.0 - 124 - 50, pl["profit"]  # μόνο τα ποσά Ε3
html = c.get(f"/reports/yearly/e3?year={y}").data.decode()
assert "4. Πληροφοριακά στοιχεία" in html and "ΦΠΑ μη εκπιπτόμενος" in html and "Αγορές παγίων" in html
print("ok nd vat")
