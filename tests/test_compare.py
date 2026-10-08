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
# Πέρσι όλο το έτος (μαζί με τον Δεκέμβριο: P3 9.999)· προβολή = φετινό YTD + περσινά μετά τις 8/10 × 1.700 / 1.500 (έσοδα), × 200 / 50 (έξοδα).
C = {cc["key"]: cc for cc in d["cums"]}  # τα τρία σωρευτικά γραφήματα: in / out / res
assert C["res"]["chart"]["lines"][0]["v"] == 1500.0 + 9999 - 50 and C["res"]["prev_full"] == 1500.0 + 9999 - 50
inc_prev = C["in"]["chart"]["lines"][0]
assert inc_prev["v"] == 1500.0 + 9999 and "C" in inc_prev["d"]  # σωρευτικά έσοδα όλο το έτος, ομαλή καμπύλη
assert C["out"]["chart"]["lines"][0]["v"] == 50.0 and C["out"]["proj_v"] == 200.0  # έξοδα: πέρσι τίποτα μετά τις 8/10
assert abs(C["in"]["proj_v"] - (1700.0 + 9999 * 1700 / 1500)) < 0.05, C["in"]["proj_v"]
assert abs(C["res"]["proj_v"] - (1500.0 + 9999 * 1700 / 1500)) < 0.05, C["res"]["proj_v"]
assert "έσοδα ×1,13" in d["proj_method"] and "έξοδα ×4,00" in d["proj_method"], d["proj_method"]
assert any("Έσοδα αυξήθηκαν" in t for _, _, t in d["insights"]), d["insights"]



def stock(mark, year, month, day, cat, amount):
    db.upsert_document(cid, "expense", {"mark": mark, "issue_date": f"{year}-{month:02d}-{day:02d}", "invoice_type": "17.1",
                                        "total_net": amount, "total_vat": 0, "total_gross": amount,
                                        "cls_info": [{"type": "E3_101", "category": cat, "amount": amount}]}, "classified")


# Αποθέματα: μετρούν μόνο σε χρήση με έναρξης ΚΑΙ λήξης. Φέτος μόνο έναρξης (ανοιχτή χρήση) → αγνοούνται.
stock("S1", y, 1, 1, "category2_13", 400)
d = A._compare(cid, now)
cur = d["cur"]
assert cur["cogs"] == 0 and cur["expense"] == 200.0 and cur["profit"] == 1500.0 and not cur["stock_used"], cur
assert cur["stock_src"] == "own" and "ανοιχτή χρήση" in cur["stock_why"] and d["prev"]["stock_why"] == "χωρίς εγγραφές αποθεμάτων"
assert any("Αποθέματα εκτός υπολογισμού: " + str(y) + " (ανοιχτή χρήση" in t for _, _, t in d["insights"]), d["insights"]

# Πέρσι έναρξης τον Δεκέμβριο (μετά το σημείο σύγκρισης), χωρίς λήξης → αγνοούνται, με μήνυμα.
stock("S0", y - 1, 12, 20, "category2_13", 300)
p = A._compare(cid, now)["prev"]
assert p["expense"] == 50.0 and p["profit"] == 1450.0 and p["stock_why"] == "χωρίς αποθέματα λήξης", p

# + λήξης 31/12 → πλήρης χρήση: ολόκληρη η μεταβολή (300 − 120) μετρά και στο year to date, στον Ιανουάριο.
stock("S9", y - 1, 12, 31, "category2_14", 120)
d = A._compare(cid, now)
p = d["prev"]
assert p["stock_used"] and p["s_open"] == 300.0 and p["purchases"] == 0 and p["s_close"] == 120.0 and p["cogs"] == 180.0, p
assert p["expense"] == 230.0 and p["profit"] == 1270.0 and p["n_out"] == 1
assert p["e3_out"]["category2_13"] == 300.0 and p["e3_out"]["category2_14"] == -120.0
assert p["cum"][-1] == 1270.0 and p["cum_full"][0] == 1270.0 and p["cum_full"][-1] == 1270.0 + 9999  # χωρίς διπλομέτρηση
assert any("Αποθέματα εκτός υπολογισμού: " + str(y) in t for _, _, t in d["insights"]), d["insights"]
# Εταιρεία με αποθέματα: φέτος χωρίς λήξης → επισφαλές αποτέλεσμα, διακεκομμένη καμπύλη (μόνο στο γράφημα αποτελέσματος).
C = {cc["key"]: cc for cc in d["cums"]}
assert [ln["unsure"] for ln in C["res"]["chart"]["lines"]] == [False, True] and d["unsure"] == [str(y)]
assert not any(ln["unsure"] for ln in C["in"]["chart"]["lines"] + C["out"]["chart"]["lines"])
# Προβολή εξόδων: λόγος χωρίς αποθέματα (200 / 50), όχι 200 / 230.
assert "έξοδα ×4,00" in d["proj_method"], d["proj_method"]
bars = {m["label"]: m["vals"] for m in d["totals"]["months"]}  # αθροιστικά ανά μέγεθος: (έτος, slot, ποσό), φέτος πρώτο
assert bars["Σύνολο εξόδων"][1] == (str(y - 1), 1, 230.0, False, None), bars
html = c.get("/reports/compare").data.decode()
assert "Σύγκριση ετών" in html and "cmp-line" in html and "1.700,00" in html and "Νέοι πελάτες" in html
assert "is-unsure" in html and "Επισφαλές αποτέλεσμα " + str(y) in html
assert "✓ Μετρούν" in html and "Αγνοούνται" in html and "= Κόστος πωληθέντων" in html
print("ok")

# Μόνο ένα έτος: χωρίς μεταβολές, η σελίδα ανοίγει.
with db.get_conn() as conn:
    conn.execute("DELETE FROM documents WHERE mark IN ('P1', 'P2', 'P3', 'E2', 'S0', 'S9')")
d = A._compare(cid, now)
assert d["years"] == [str(y)] and d["prev"] is None and d["kpis"][0]["d"] is None and not d["insights"]
assert c.get("/reports/compare").status_code == 200
assert "1.500,00" in c.get("/reports/compare").data.decode()
print("ok single year")

# Year to date: ο τρέχων μήνας μετρά έως σήμερα (5/10 μέσα, 20/10 έξω) — και πέρσι στην ίδια ημερομηνία.
before = A._compare(cid, now)["cur"]["income"]
doc("income", "T1", y, 10, "444", 70, day=5)
doc("income", "T2", y, 10, "444", 30, day=20)
doc("income", "T3", y - 1, 10, "444", 40, day=8)
d = A._compare(cid, now)
assert d["cur"]["income"] == before + 70 and d["prev"]["income"] == 40.0, (d["cur"]["income"], d["prev"]["income"])
assert [cc["chart"]["today"]["v"] for cc in d["cums"]] == [d["cur"]["income"], d["cur"]["expense"], d["cur"]["profit"]] and len(d["cur"]["cum"]) == 9  # σημείο «σήμερα» = YTD
print("ok ytd")
# Προβολή από το YTD: έσοδα — πέρσι τίποτα μετά τις 8/10, άρα μόνο το ήδη καταχωρημένο 20/10 (T2 30, ελάχιστο)·
# έξοδα — πέρσι 0 έως 8/10 (χωρίς βάση) → ημερήσιος ρυθμός φετινού YTD × μέρες που απομένουν.
days = 366 if A.calendar.isleap(y) else 365
elapsed = now.timetuple().tm_yday
assert abs(d["cums"][2]["proj_v"] - (d["cur"]["profit"] + 30 - d["cur"]["expense"] / elapsed * (days - elapsed))) < 0.05, d["cums"][2]["proj_v"]
assert "ημερήσιος ρυθμός" in d["proj_method"]
print("ok proj")

# Πάγια: αγορά πέρσι μετά τη σημερινή ημερομηνία — εκτός σύγκρισης (YTD), αλλά στο «όλο το έτος».
doc("expense", "A0", y - 1, 11, "555", 1000, e3=("E3_882_001", "category2_7"), day=15)
d = A._compare(cid, now)
assert d["prev"]["assets"] == 0 and d["prev"]["assets_full"] == 1000.0, d["prev"]
print("ok assets full")

# Ομαλή καμπύλη: περνά από τα σημεία και δεν ξεπερνά επίπεδο τμήμα (μονότονη).
p = A._smooth([(0, 100), (10, 50), (20, 50), (30, 0)])
assert p.startswith("M0.0,100.0") and p.endswith("30.0,0.0") and "C" in p
seg = p.split("C")[2]  # 2ο τμήμα (10,50)→(20,50): οριζόντια σημεία ελέγχου
assert seg.startswith("13.3,50.0 16.7,50.0"), seg
print("ok smooth")

# Αποθέματα έναρξης από τα αποθέματα λήξης της προηγούμενης χρήσης (όταν λείπει η εγγραφή 2.13).
with db.get_conn() as conn:
    conn.execute("DELETE FROM documents WHERE mark = 'S1'")
stock("S8", y - 1, 12, 31, "category2_14", 120)
stock("S7", y, 10, 1, "category2_14", 50)
d = A._compare(cid, now)
cur, p = d["cur"], d["prev"]
assert cur["stock_used"] and cur["stock_src"] == "prev" and cur["s_open"] == 120.0 and cur["s_close"] == 50.0, cur
assert cur["cogs"] == 70.0 and cur["expense"] == cur["exp_ns"] + 70.0
assert not p["stock_used"] and p["stock_why"] == f"χωρίς αποθέματα έναρξης (ούτε λήξης στο {y - 2})", p
assert "λήξης " + str(y - 1) in c.get("/reports/compare").data.decode()
print("ok stock chain")

# Οι άλλες αναφορές: χρήση χωρίς αποθέματα λήξης (έναρξης = λήξης περσινά) → προειδοποίηση «Επισφαλές αποτέλεσμα».
with db.get_conn() as conn:
    conn.execute("DELETE FROM documents WHERE mark = 'S7'")
chk = A._stock_check(cid, str(y))
assert chk["unsure"] and chk["src"] == "prev" and chk["open"] == 120.0 and "ανοιχτή χρήση" in chk["why"], chk
for url in (f"/reports/yearly/e3?year={y}", f"/reports/yearly/e3?year={y}&print=1", "/dashboard", "/reports/forecast",
            f"/reports/yearly?year={y}"):
    h = c.get(url).data.decode()
    assert "Επισφαλές αποτέλεσμα " + str(y) in h or "⚠ Επισφαλές:" in h, url
assert "fc-line--res is-unsure" in c.get("/reports/forecast").data.decode()
print("ok stock warn")
