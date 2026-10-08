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
y = A.datetime.now(A.ATHENS).year
now = A.datetime(y, 10, 8, 12, tzinfo=A.ATHENS)  # σταθερό «σήμερα»: year to date = 1/1 – 8/10, 9 ολοκληρωμένοι μήνες


def doc(kind, mark, year, month, vat, net, typ="2.1", e3=("E3_561_001", "category1_1"), day=1):
    # Ημέρα 1: τα παραστατικά του Ιανουαρίου μετρούν και στη σελίδα (πραγματική ημερομηνία), όποια μέρα κι αν τρέχει.
    db.upsert_document(cid, kind, {"mark": mark, "issue_date": f"{year}-{month:02d}-{day:02d}", "invoice_type": typ,
                                   "issuer_vat": vat, "total_net": net, "total_vat": 0, "total_gross": net,
                                   "cls_info": [{"type": e3[0], "category": e3[1], "amount": net}]}, "classified")


# Πέρσι: A 1.000 + B 500 τον Ιανουάριο· ο Δεκέμβριος είναι μετά τη σημερινή ημερομηνία → εκτός.
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
assert d["span"] == "1/1 – 8/10"
assert d["cur"]["income"] == 1700.0 and d["prev"]["income"] == 1500.0, (d["cur"]["income"], d["prev"]["income"])
inc = d["kpis"][0]
assert inc["d"] == {"v": 200.0, "pct": 13.3}, inc["d"]
assert d["cur"]["profit"] == 1500.0 and d["prev"]["profit"] == 1450.0
cu = d["customers"]
assert cu["new_n"] == 1 and cu["new"][0]["vat"] == "333" and cu["lost_n"] == 1 and cu["lost"][0]["vat"] == "222", cu
assert cu["rows"][0]["vals"] == [1000.0, 1400.0] and cu["active"] == [2, 2]
assert d["e3_out"]["rows"][0]["vals"] == [50.0, 200.0] and d["e3_out"]["rows"][0]["d"]["pct"] == 300.0
assert d["cur"]["cum"][-1] == 1500.0  # φέτος: 1.700 − 200 (= καθαρό κέρδος Ε3)
# Πέρσι όλο το έτος (μαζί με τον Δεκέμβριο: P3 9.999)· προβολή φέτος = περσινοί μήνες × 1.700 / 1.500 (έσοδα), × 200 / 50 (έξοδα).
assert d["cum"]["lines"][0]["v"] == 1500.0 + 9999 - 50
assert abs(d["proj"]["v"] - (1500.0 + 9999 * 1700 / 1500)) < 0.05, d["proj"]
assert "εποχικότητα" in d["proj"]["method"]
assert any("Έσοδα αυξήθηκαν" in t for _, _, t in d["insights"]), d["insights"]

# Αποθέματα έναρξης (1/1): μετρούν στο κόστος πωληθέντων και στα έξοδα, όπως στην «Ανάλυση Ε3».
db.upsert_document(cid, "expense", {"mark": "S1", "issue_date": f"{y}-01-01", "invoice_type": "17.1", "total_net": 400,
                                    "total_vat": 0, "total_gross": 400,
                                    "cls_info": [{"type": "E3_101", "category": "category2_13", "amount": 400}]}, "classified")
d = A._compare(cid, now)
cur = d["cur"]
assert cur["cogs"] == 400.0 and cur["expense"] == 600.0 and cur["profit"] == 1100.0, cur
assert round(sum(cur["e3_out"].values()), 2) == cur["expense"] and cur["e3_out"]["category2_13"] == 400.0
assert cur["cum"][-1] == cur["profit"]
bars = {m["label"]: m["vals"] for m in d["totals"]["months"]}  # αθροιστικά ανά μέγεθος: (έτος, slot, ποσό), φέτος πρώτο
assert bars["Σύνολο εξόδων"][0] == (str(y), 0, 600.0, False) and bars["Καθαρό κέρδος"][0][2] == 1100.0, bars

# Περσινά αποθέματα έναρξης καταχωρημένα τον Δεκέμβριο (μετά το σημείο σύγκρισης): μετρούν, στον Ιανουάριο·
# ο Δεκέμβριος των υπόλοιπων εγγραφών (P3) μένει εκτός.
db.upsert_document(cid, "expense", {"mark": "S0", "issue_date": f"{y - 1}-12-20", "invoice_type": "17.1", "total_net": 300,
                                    "total_vat": 0, "total_gross": 300,
                                    "cls_info": [{"type": "E3_101", "category": "category2_13", "amount": 300}]}, "classified")
d = A._compare(cid, now)
p = d["prev"]
assert p["cogs"] == 300.0 and p["expense"] == 350.0 and p["profit"] == 1150.0 and p["income"] == 1500.0, p
assert p["e3_out"]["category2_13"] == 300.0 and p["n_out"] == 1
assert p["cum"][-1] == 1150.0 and p["cum_full"][0] == 1150.0  # τα αποθέματα έναρξης μετρούν, στον Ιανουάριο
assert p["cum_full"][-1] == 1150.0 + 9999  # όλο το έτος: χωρίς διπλομέτρηση του 2.13 του Δεκεμβρίου

html = c.get("/reports/compare").data.decode()
assert "Σύγκριση ετών" in html and "cmp-line" in html and "1.700,00" in html and "Νέοι πελάτες" in html
print("ok")

# Μόνο ένα έτος: χωρίς μεταβολές, η σελίδα ανοίγει.
with db.get_conn() as conn:
    conn.execute("DELETE FROM documents WHERE mark IN ('P1', 'P2', 'P3', 'E2', 'S0')")
d = A._compare(cid, now)
assert d["years"] == [str(y)] and d["prev"] is None and d["kpis"][0]["d"] is None and not d["insights"]
assert c.get("/reports/compare").status_code == 200
assert "1.100,00" in c.get("/reports/compare").data.decode()
print("ok single year")

# Year to date: ο τρέχων μήνας μετρά έως σήμερα (5/10 μέσα, 20/10 έξω) — και πέρσι στην ίδια ημερομηνία.
before = A._compare(cid, now)["cur"]["income"]
doc("income", "T1", y, 10, "444", 70, day=5)
doc("income", "T2", y, 10, "444", 30, day=20)
doc("income", "T3", y - 1, 10, "444", 40, day=8)
d = A._compare(cid, now)
assert d["cur"]["income"] == before + 70 and d["prev"]["income"] == 40.0, (d["cur"]["income"], d["prev"]["income"])
assert d["cum"]["today"][2] == d["cur"]["profit"] and len(d["cur"]["cum"]) == 9  # σημείο «σήμερα» = YTD
print("ok ytd")
