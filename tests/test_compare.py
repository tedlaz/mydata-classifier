"""Σύγκριση ετών: ίδιοι ολοκληρωμένοι μήνες κάθε έτους, μεταβολές, νέοι/χαμένοι πελάτες (python -m tests.test_compare)."""
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
now = A.datetime.now(A.ATHENS)
y = now.year


def doc(kind, mark, year, month, vat, net, typ="2.1", e3=("E3_561_001", "category1_1")):
    db.upsert_document(cid, kind, {"mark": mark, "issue_date": f"{year}-{month:02d}-15", "invoice_type": typ,
                                   "issuer_vat": vat, "total_net": net, "total_vat": 0, "total_gross": net,
                                   "cls_info": [{"type": e3[0], "category": e3[1], "amount": net}]}, "classified")


# Πέρσι: A 1.000 + B 500 τον Ιανουάριο· ο Δεκέμβριος είναι πάντα μετά το σημείο σύγκρισης → εκτός.
doc("income", "P1", y - 1, 1, "111", 1000)
doc("income", "P2", y - 1, 1, "222", 500)
doc("income", "P3", y - 1, 12, "111", 9999)
# Φέτος: A 1.500 − πιστωτικό 100, νέος πελάτης C 300· ο B χάθηκε.
doc("income", "C1", y, 1, "111", 1500)
doc("income", "C2", y, 1, "111", 100, typ="5.1")
doc("income", "C3", y, 1, "333", 300)
doc("expense", "E1", y, 1, "999", 200, e3=("E3_585_009", "category2_5"))
doc("expense", "E2", y - 1, 1, "999", 50, e3=("E3_585_009", "category2_5"))

d = A._compare(cid, now)
assert d["years"] == [str(y - 1), str(y)], d["years"]
assert d["cut"] == max(now.month - 1, 1)
assert d["cur"]["income"] == 1700.0 and d["prev"]["income"] == 1500.0, (d["cur"]["income"], d["prev"]["income"])
inc = d["kpis"][0]
assert inc["d"] == {"v": 200.0, "pct": 13.3}, inc["d"]
assert d["cur"]["profit"] == 1500.0 and d["prev"]["profit"] == 1450.0
cu = d["customers"]
assert cu["new_n"] == 1 and cu["new"][0]["vat"] == "333" and cu["lost_n"] == 1 and cu["lost"][0]["vat"] == "222", cu
assert cu["rows"][0]["vals"] == [1000.0, 1400.0] and cu["active"] == [2, 2]
assert d["e3_out"]["rows"][0]["vals"] == [50.0, 200.0] and d["e3_out"]["rows"][0]["d"]["pct"] == 300.0
assert d["cum"]["lines"][-1]["v"] == 1500.0  # φέτος: 1.700 − 200 (= καθαρό κέρδος Ε3)
assert any("Έσοδα αυξήθηκαν" in t for _, _, t in d["insights"]), d["insights"]

# Αποθέματα έναρξης (1/1): μετρούν στο κόστος πωληθέντων και στα έξοδα, όπως στην «Ανάλυση Ε3».
db.upsert_document(cid, "expense", {"mark": "S1", "issue_date": f"{y}-01-01", "invoice_type": "17.1", "total_net": 400,
                                    "total_vat": 0, "total_gross": 400,
                                    "cls_info": [{"type": "E3_101", "category": "category2_13", "amount": 400}]}, "classified")
d = A._compare(cid, now)
cur = d["cur"]
assert cur["cogs"] == 400.0 and cur["expense"] == 600.0 and cur["profit"] == 1100.0, cur
assert round(sum(cur["e3_out"].values()), 2) == cur["expense"] and cur["e3_out"]["category2_13"] == 400.0
assert d["cum"]["lines"][-1]["v"] == cur["profit"] and sum(cur["m_out"]) == cur["expense"]

html = c.get("/reports/compare").data.decode()
assert "Σύγκριση ετών" in html and "cmp-line" in html and "1.700,00" in html and "Νέοι πελάτες" in html
print("ok")

# Μόνο ένα έτος: χωρίς μεταβολές, η σελίδα ανοίγει.
with db.get_conn() as conn:
    conn.execute("DELETE FROM documents WHERE mark IN ('P1', 'P2', 'P3', 'E2')")
d = A._compare(cid, now)
assert d["years"] == [str(y)] and d["prev"] is None and d["kpis"][0]["d"] is None and not d["insights"]
assert c.get("/reports/compare").status_code == 200
assert "1.100,00" in c.get("/reports/compare").data.decode()
print("ok single year")
