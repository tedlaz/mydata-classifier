"""Φόρος εισοδήματος χρήσης: κλίμακες, νέοι, προκαταβολή, παράμετροι, σελίδες (python -m tests.test_tax)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
r20, r26 = A._tax_regime(2025), A._tax_regime(2026)
assert r20["from_year"] == 2020 and r26["from_year"] == 2026 and A._tax_regime(2031)["from_year"] == 2026
assert A._tax_regime(2010)["from_year"] == 2020  # πριν από όλα: το παλαιότερο

legal, sole = {"entity_type": "legal"}, {"entity_type": "sole"}
assert A._income_tax(10000, 2026, legal, r26) == 2200.0
assert A._income_tax(30000, 2025, sole, r20) == 5900.0  # 900 + 2.200 + 2.800
assert A._income_tax(30000, 2026, sole, r26) == 5500.0  # 900 + 2.000 + 2.600
assert A._income_tax(70000, 2026, sole, r26) == 21100.0  # … + 3.400 + 7.800 + 4.400
assert A._income_tax(30000, 2026, dict(sole, birth_year=2002), r26) == 2600.0  # 24 ετών: 0% έως 20.000
assert A._income_tax(30000, 2026, dict(sole, birth_year=1998), r26) == 4400.0  # 28 ετών: 9% στο 10–20 χιλ.
assert A._income_tax(30000, 2026, dict(sole, birth_year=1995), r26) == 5500.0  # 31: γενική
assert A._income_tax(30000, 2025, dict(sole, birth_year=2002), r20) == 5900.0  # 2025: χωρίς ομάδες νέων
assert A._income_tax(-500, 2026, sole, r26) == 0.0 and A._income_tax(0, 2026, legal, r26) == 0.0

t = A._tax_due(10000, 2026, legal, withheld=500, prev_prepay=0)
assert (t["tax"], t["prepay"], t["due"]) == (2200.0, 1260.0, 2960.0), t  # 80% × 2.200 − 500
t = A._tax_due(10000, 2026, legal, withheld=2000, prev_prepay=1000)
assert (t["prepay"], t["due"]) == (0.0, -800.0), t  # προκαταβολή ποτέ αρνητική· επιστροφή
t = A._tax_due(30000, 2026, sole, withheld=0, prev_prepay=0)
assert t["prepay"] == 3025.0 and t["prepay_rate"] == 55 and t["sole"]

# Εταιρεία: ατομική χωρίς έτος γέννησης απορρίπτεται· με έτος αποθηκεύεται.
c = A.app.test_client()
form = {"company_name": "Ατομική", "use_accountant": "1", "entity_type": "sole"}
assert b"flash error" in c.post("/companies/add", data=form, follow_redirects=True).data and not db.list_companies()
c.post("/companies/add", data=dict(form, birth_year="1998"))
co = db.list_companies()[0]
assert (co["entity_type"], co["birth_year"]) == ("sole", 1998), co
db.set_active_company_id(co["id"])
assert db.birth_year("abc") is None and db.birth_year("1850") is None

# Παράμετροι: αποθήκευση από τη φόρμα, λάθη, νέα κλίμακα, διαγραφή, επαναφορά.
assert "Φορολογία".encode() in c.get("/parameters?tab=tax").data
base = {"count": "1", "s0_from_year": "2020", "s0_rows": "3", "s0_ycols": "1", "s0_lim_0": "20.000", "s0_rate_0": "10",
        "s0_rate_1": "30", "s0_lim_1": "999", "s0_yage_0": "30", "s0_y0_0": "0",
        "s0_legal_rate": "25", "s0_prepay_sole": "50", "s0_prepay_legal": "100"}
c.post("/parameters/tax", data=base)
s = A._tax_scales()
assert s == [{"from_year": 2020, "limits": [20000.0], "rates": [10.0, 30.0], "young": [{"max_age": 30, "rates": [0.0, 30.0]}],
              "legal_rate": 25.0, "prepay_sole": 50.0, "prepay_legal": 100.0}], s
assert A._income_tax(30000, 2026, legal, s[0]) == 7500.0
for bad in ({"s0_rate_0": "101"}, {"s0_lim_0": ""}, {"s0_legal_rate": ""}, {"s0_rate_0": "", "s0_rate_1": ""}):
    r = c.post("/parameters/tax", data=dict(base, **bad), follow_redirects=True)
    assert b"flash error" in r.data and A._tax_scales() == s, bad
c.post("/parameters/tax", data={"action": "add"})
assert [x["from_year"] for x in A._tax_scales()] == [2020, A.datetime.now(A.ATHENS).year]
c.post("/parameters/tax", data={"delete": "1"})
assert A._tax_scales() == s
c.post("/parameters/tax", data={"delete": "0"})  # η μοναδική δεν σβήνεται
assert A._tax_scales() == s
c.post("/parameters/tax", data={"action": "reset"})
assert A._tax_scales() == A._DEFAULT_TAX_SCALES

# Σελίδες: έσοδα 40.000 με παρακράτηση 800 → μπλοκ φόρου στο «Καθαρό κέρδος» και στην Πρόβλεψη.
y = A.datetime.now(A.ATHENS).year
db.upsert_document(co["id"], "income", {"mark": "I1", "issue_date": f"{y}-01-15", "invoice_type": "2.1",
                                        "total_net": 40000, "total_vat": 0, "total_gross": 39200,
                                        "extra_totals": {"total_withheld": 800},
                                        "cls_info": [{"type": "E3_561_001", "category": "category1_3", "amount": 40000}]},
                   "classified")
assert A._year_profit(co["id"], str(y)) == (40000.0, 800.0)
html = c.get(f"/reports/yearly/e3?year={y}").data.decode()
assert "Φόρος εισοδήματος" in html and A.format_el_amount(A._year_tax(co["id"], y, 40000, 800)["tax"]) in html
fc = c.get("/reports/forecast").data.decode()
assert "Φόρος εισοδήματος" in fc
done = A.datetime.now(A.ATHENS).month - 1
if done:  # παρακράτηση 2% και στα έσοδα της πρόβλεψης (μέσος όρος 40.000 / done ανά υπόλοιπο μήνα)
    future = round(0.02 * 40000 / done * (12 - done), 2)
    assert f"2,00% × {A.format_el_amount(round(40000 / done * (12 - done), 2))} €" in fc
    assert f"− {A.format_el_amount(future)} €" in fc and "− 800,00 €" in fc, future  # χωριστές γραμμές
db.update_company(co["id"], dict(co, entity_type="legal"))  # νομικό πρόσωπο: χωρίς παρακράτηση στην πρόβλεψη
assert "στα έσοδα της πρόβλεψης" not in c.get("/reports/forecast").data.decode()
print("OK")
