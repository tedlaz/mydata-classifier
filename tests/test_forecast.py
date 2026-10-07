"""Πρόβλεψη έτους: σημειακή πρόβλεψη, σενάρια, σελίδα (python -m tests.test_forecast)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

prev = [100.0] * 6 + [200.0] * 6
cur = [120.0, 110.0, 130.0] + [0.0] * 9

# 1) Εποχικότητα: φέτος 360 / πέρσι 300 = ×1,2 → υπόλοιποι μήνες = πέρσι × 1,2.
f, label = A._forecast(cur, prev, 3)
assert f[:3] == cur[:3] and f[3:6] == [120.0] * 3 and f[6:] == [240.0] * 6, f
assert "εποχικότητα" in label

# 2) Χωρίς ιστορικό: μέσος όρος ολοκληρωμένων μηνών.
f, label = A._forecast(cur, [0.0] * 12, 3)
assert f[3:] == [120.0] * 9 and "μέσος" in label

# 3) Κανένας ολοκληρωμένος μήνας: τα περσινά.
assert A._forecast([0.0] * 12, prev, 0)[0] == prev

# 4) Νέα εταιρεία (κίνηση από τον 3ο μήνα): οι μηδενικοί μήνες πριν δεν μετρούν στον μέσο όρο.
late = [0.0, 0.0, 90.0, 110.0] + [0.0] * 8
f, _ = A._forecast(late, [0.0] * 12, 4, start=2)
assert f[4:] == [100.0] * 8, f
assert len(A._forecast([0.0] * 12, prev, 5, start=5)[0]) == 12  # χωρίς κίνηση: τα περσινά

# 5) Τρέχων μήνας με καταχωρημένα πάνω από τη στατιστική: η πρόβλεψη δεν πέφτει κάτω από αυτά.
f, _ = A._forecast(cur[:3] + [500.0] + [0.0] * 8, prev, 3)
assert f[3] == 500.0 and f[4:6] == [120.0] * 2, f

# Σενάρια: P10 ≤ P50 ≤ P90, ντετερμινιστικά με ίδιο seed, κανένα με < 2 μήνες.
out = [50.0, 80.0, 40.0] + [0.0] * 9
s = A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=2026)
assert s["p10"] <= s["p50"] <= s["p90"] and s["p10"] < s["p90"], s
assert len(s["lo"]) == len(s["hi"]) == 9 and all(lo <= hi for lo, hi in zip(s["lo"], s["hi"]))
assert s == A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=2026)
# Εύρος εσόδων / εξόδων: από τα έως τώρα και πάνω, 9 μήνες σωρευτικά.
assert s["in"]["lo"][0] >= sum(cur[:3]) and s["out"]["lo"][0] >= sum(out[:3]), s
assert s["in"]["p10"] <= s["in"]["p50"] <= s["in"]["p90"] and s["out"]["p10"] <= s["out"]["p90"]
assert len(s["in"]["hi"]) == len(s["out"]["hi"]) == 9
# Μεταβλητότητα: 0 → χωρίς εύρος· 2 → φαρδύτερο εύρος· ανεξάρτητα για έσοδα / έξοδα.
s0 = A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=2026, vol_in=0, vol_out=0)
assert s0["in"]["p10"] == s0["in"]["p90"] and s0["out"]["p10"] == s0["out"]["p90"], s0
s2x = A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=2026, vol_in=2)
assert s2x["in"]["p90"] - s2x["in"]["p10"] > s["in"]["p90"] - s["in"]["p10"] and s2x["out"] == s["out"]
assert A._scenarios(cur, out, prev, prev, 1, seed=1) is None
assert A._scenarios(late, late, prev, prev, 4, seed=1, start=3) is None  # 1 μήνας με κίνηση
# Τρέχων μήνας με καταχωρημένα 10.000: κανένα σενάριο κάτω από αυτά.
sb = A._scenarios(cur[:3] + [10000.0] + [0.0] * 8, out, prev, [60.0] * 12, 3, seed=2026)
assert sb["lo"][0] >= 360 - 170 + 10000 - 60 * 12, sb
# Περσινός μήνας ≈ 0, φετινός μεγάλος: η ζώνη μένει στην τάξη των πραγματικών ποσών (όχι ×λόγος).
spiky = A._scenarios([100.0, 100.0, 16000.0] + [0.0] * 9, out, [1000.0, 1000.0, 1.0] + [1000.0] * 9,
                     [60.0] * 12, 3, seed=1)
assert spiky["p90"] < 16200 + 9 * 16000, spiky

# Σταθερά έξοδα: ΕΦΚΑ σε κάθε μήνα → σταθερό, flat· τα πάγια/αποσβέσεις δεν μπαίνουν στις ροές.
def doc(date, cls, itype="1.1"):
    return {"issue_date": date, "invoice_type": itype, "cls_json": __import__("json").dumps(cls)}


docs = [doc(f"2026-0{m}-10", [{"type": "E3_585_007", "category": "category2_5", "amount": 160}]) for m in (1, 2, 3, 4)]
docs += [doc("2026-02-05", [{"type": "E3_585_016", "category": "category2_5", "amount": 80}]),
         doc("2026-03-01", [{"type": "E3_882_001", "category": "category2_7", "amount": 900},
                            {"type": "VAT_361", "amount": 216}]),
         doc("2026-03-02", [{"type": "E3_587", "category": "category2_5", "amount": 900}]),
         doc("2026-04-03", [{"type": "E3_882_001", "category": "category2_7", "amount": 100}], itype="5.1")]
by_type, assets = A._expense_lines(docs)
assert assets == [900, -100], assets  # πιστωτικό → αρνητικό
assert all("E3_587" not in m and "E3_882_001" not in m for m in by_type)
assert A._fixed_types(by_type, 4, 0) == {"E3_585_007": 160.0}  # το E3_585_016 μόνο σε 1 μήνα
assert A._fixed_types(by_type, 2, 0) == {"E3_585_007": 160.0}  # γνωστός κωδικός και με λίγους μήνες
assert A._fixed_types(by_type, 0, 0) == {}

# Αποσβέσεις: < 1.500 → 100% φέτος· ≥ 1.500 → 20% για 5 χρόνια· νέες αγορές με τον ίδιο κανόνα.
lines = {2021: [5000.0], 2022: [5000.0], 2026: [1000.0, 5000.0]}
d = A._depreciation(lines, 2026, 0)
assert (d["cur"], d["older"], d["total"]) == (2000.0, 1000.0, 3000.0), d  # 2021: 6ο έτος → 0
assert A._depreciation({}, 2026, small=1200)["total"] == 1200.0  # νέα < 1.500: 100%
assert A._depreciation({}, 2026, large=2000)["total"] == 400.0   # νέα ≥ 1.500: 20%
assert A._depreciation(lines, 2026, 300, 1500)["total"] == 3000 + 300 + 300
assert A._depreciation({}, 2026, large=6000, months=4)["total"] == 400.0  # αγορά Σεπτέμβριο: 20% × 4/12

# Γράφημα: 12 στήλες, βεντάλια μόνο με σενάρια, κόστος παγίων στον Δεκέμβριο.
ch = A._forecast_chart(cur[:3] + [120.0] * 9, out[:3] + [60.0] * 9, [40.0] * 12, [40.0] * 12, s, 3, dep=500)
assert len(ch["months"]) == 12 and set(ch["fans"]) == {"in", "out", "res"}
assert ch["months"][3]["forecast"] and not ch["months"][2]["forecast"] and ch["months"][2]["in_lo"] is None
assert ch["months"][11]["dep"] == 500 and ch["months"][10]["dep"] == 0
assert ch["months"][11]["out_hi"] == s["out"]["hi"][-1]  # εύρος συνόλου εξόδων (με αποσβέσεις), όπως η γραμμή
assert A._forecast_chart([0.0] * 12, [0.0] * 12, [0.0] * 12, [0.0] * 12, None, 0)["fans"] == {}
# Λιγότεροι μήνες (συνοπτικό βιβλίο τρέχοντος έτους): στήλες = μήνες, τέλος γραμμών στον τελευταίο.
ch3 = A._forecast_chart([100.0, 50.0, 20.0], [30.0] * 3, [70.0, 20.0, -10.0], [10.0] * 12, None, 3, res_label="Υπόλοιπο")
assert len(ch3["months"]) == 3 and ch3["end_x"] == ch3["months"][-1]["cx"] and ch3["months"][2]["prev"] == 30.0
assert [e["label"] for e in ch3["ends"] if e["key"] == "res"] == ["Υπόλοιπο"] and ch3["months"][2]["res"] == 80.0

# Σενάρια: οι σταθερές εκροές μετατοπίζουν όλη την κατανομή.
s2 = A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=2026, fixed=[0.0] * 11 + [100.0])
assert abs(s2["p50"] - (s["p50"] - 100)) < 0.01, (s, s2)

# Προγραμματισμένα: κατανομή αναλογικά των βαρών, ακριβές άθροισμα· γνωστά ποσά → μόνο προς το χειρότερο.
assert A._spread(10000, [1, 3]) == [2500.0, 7500.0]
assert A._spread(100, [0, 0, 0]) == [33.33, 33.33, 33.34]
assert A._spread(0, [5, 5]) == [0.0, 0.0] and A._spread(10, []) == []
# Καταχωρημένα + πρόγραμμα: το πρόγραμμα μοιράζεται κατά την εποχικότητα, επιπλέον των καταχωρημένων.
assert A._plan(1000, [100, 300], [400, 0]) == [650.0, 750.0]
k_in, k_out = [100.0] * 12, [40.0] * 12
s3 = A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=1, known_in=k_in, known_out=k_out)
plan_res = round(sum(cur[:3]) - sum(out[:3]) + 9 * (100 - 40), 2)
assert s3["p10"] < s3["p90"] <= plan_res, (s3, plan_res)  # ποτέ καλύτερο από το πρόγραμμα, αλλά με κίνδυνο
assert s3 == A._scenarios(cur, out, prev, [60.0] * 12, 3, seed=1, known_in=k_in, known_out=k_out)

# Σελίδα: τα πεδία νέων αγορών· «≥ 1.500» δεν δέχεται μικρότερο ποσό.
db.init_db()
tcid = db.add_company({"company_name": "Δοκιμή"})
db.set_active_company_id(tcid)
y0 = A.datetime.now(A.ATHENS).year - 1  # περσινά + προπέρσινα: γραμμές ιστορικού και περσινή αναφορά
for i, (kind, y, net, cls) in enumerate([
        ("income", y0, 1000, []), ("expense", y0, 300, [{"type": "E3_585_007", "category": "category2_5", "amount": 300}]),
        ("expense", y0, 2000, [{"type": "E3_882_001", "category": "category2_7", "amount": 2000}]),
        ("income", y0 - 1, 500, [])]):
    db.upsert_document(tcid, kind, {"mark": f"T{i}", "issue_date": f"{y}-06-15", "invoice_type": "1.1",
                                    "total_net": net, "total_vat": 0, "total_gross": net, "cls_info": cls}, "classified")
c = A.app.test_client()
for q, err in (("", False), ("?assets_small=800&assets_large=2000", False), ("?assets_small=1.200,50", False),
               ("?assets_large=1000", True), ("?assets_small=abc", True), ("?assets_large=nan", True),
               ("?planned_income=10000&planned_expense=0", False), ("?planned_income=-5", True),
               ("?planned_expense=abc", True), ("?vol_in=0&vol_out=200", False), ("?vol_in=abc&vol_out=999", False)):
    r = c.get("/reports/forecast" + q)
    assert r.status_code == 200 and "Πρόβλεψη έτους".encode() in r.data, (q, r.status_code)
    assert (b'class="flash error' in r.data) == err, q
r = c.get("/reports/forecast")
assert f"Σύνολο {y0}".encode() in r.data and f"Σύνολο {y0 - 1}".encode() in r.data
row = r.data.decode().split(f"Σύνολο {y0}</th>")[1].split("</tr>")[0]
cells = A.re.findall(r">([\d.,-]+)<", row)  # έσοδα, σταθερά, μεταβλητά, αποσβέσεις, σύνολο εξόδων, αποτέλεσμα
assert cells == ["1.000,00", "0,00", "300,00", "400,00", "700,00", "300,00"], cells  # πάγιο 2.000 → 20%
# Μετατόπιση: +20% έσοδα / −50% έξοδα αλλάζουν την πρόβλεψη (KPI έσοδα, έξοδα)· άκυρο → 0.
for i, kind in enumerate(("income", "expense")):  # περσινός Δεκέμβριος: πάντα μήνας πρόβλεψης
    db.upsert_document(tcid, kind, {"mark": f"N{i}", "issue_date": f"{y0}-12-15", "invoice_type": "1.1",
                                    "total_net": 800, "total_vat": 0, "total_gross": 800, "cls_info": []}, "classified")
kpis = lambda q: [float(v) for v in A.re.findall(r'data-num="([-\d.]+)"', c.get("/reports/forecast" + q).data.decode())[:2]]  # noqa: E731
(i0, e0), (i1, e1) = kpis(""), kpis("?shift_in=20&shift_out=-50")
assert i1 > i0 and e1 < e0, (i0, e0, i1, e1)
assert kpis("?shift_in=abc")[0] == i0 and kpis("?shift_in=-999")[0] < i0  # −999 → −100%: μόνο τα καταχωρημένα
assert kpis("?shift_in=999")[0] == kpis("?shift_in=100")[0] > kpis("?shift_in=50")[0]  # όριο +100%
with A.app.test_request_context("/?assets_small=1.200,50&assets_large=nan"):
    assert A._amount_arg("assets_small") == 1200.5 and A._amount_arg("x") is None
    try:
        A._amount_arg("assets_large")
        raise AssertionError("nan πρέπει να απορρίπτεται")
    except ValueError:
        pass
print("OK")
