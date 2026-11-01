"""Tags-διακόπτες στη σελίδα Εταιρείες: POST /companies/toggle/<idx>/<flag>.
Εκτέλεση: python -m tests.test_company_toggle"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
db.init_db()
cid = db.add_company({"company_name": "Δοκιμή ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k"})
c = A.app.test_client()
co = lambda: db.list_companies()[0]  # noqa: E731

c.post("/companies/toggle/0/allow_send")
assert co()["allow_send"] and co()["AADE_SUBSCRIPTION_KEY"] == "k"  # τα υπόλοιπα μένουν
c.post("/companies/toggle/0/allow_send")
assert not co()["allow_send"]
c.post("/companies/toggle/0/MYDATA_ENV")
assert co()["MYDATA_ENV"] == "dev"
c.post("/companies/toggle/0/locked")  # άγνωστο flag → τίποτα
assert not co()["locked"]
assert b"companies/toggle/0/allow_new" in c.get("/companies").data

# Χωρίς υποχρεωτικό λογιστή: η «Χρήση credentials λογιστή» δεν ανοίγει (ούτε από τη βάση, ούτε από τη φόρμα).
c.post("/companies/toggle/0/use_accountant")
assert not co()["use_accountant"]
db.update_company(cid, co() | {"use_accountant": True, "allow_send": True})
assert not co()["use_accountant"]
c.post("/companies/update/0", data={"company_name": "Δοκιμή ΑΕ", "aade_user_id": "u", "use_accountant": "1",
                                    "allow_send": "1", "entity_type": "legal"})
assert not co()["use_accountant"]
assert 'title="Λογιστής: OFF · μόνο με υποχρεωτικό λογιστή"' in c.get("/companies").get_data(as_text=True)

# Κλειδωμένη: οι άδειες δεν ανοίγουν.
db.lock_company(cid)
c.post("/companies/toggle/0/allow_new")
assert not co()["allow_new"]
print("OK")

# Διαγραφή: θέλει το ΑΦΜ για επιβεβαίωση.
db.add_company({"company_name": "Β ΑΕ", "AADE_VAT_NUMBER": "123456789", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k"})
i = [x["company_name"] for x in db.list_companies()].index("Β ΑΕ")
c.post(f"/companies/delete/{i}", data={"confirm": "999"})
assert len(db.list_companies()) == 2
c.post(f"/companies/delete/{i}", data={"confirm": "123456789"})
assert [x["company_name"] for x in db.list_companies()] == ["Δοκιμή ΑΕ"]
print("OK delete")

# Ακυρώσεις/απορρίψεις μόνο με ενεργή αποστολή: δεν ανοίγουν χωρίς αυτήν, κλείνουν μαζί της.
co = lambda: db.list_companies()[0]  # noqa: E731
with db.get_conn() as conn:  # κλειδώθηκε παραπάνω
    conn.execute("UPDATE companies SET locked = 0")
db.update_company(co()["id"], co() | {"AADE_USER_ID": "u", "allow_send": False, "allow_cancel": True})
assert not co()["allow_cancel"]  # και απευθείας στη βάση
c.post("/companies/toggle/0/allow_cancel")
assert not co()["allow_cancel"]
c.post("/companies/toggle/0/allow_send")
c.post("/companies/toggle/0/allow_cancel")
assert co()["allow_send"] and co()["allow_cancel"]
c.post("/companies/toggle/0/allow_send")
assert not co()["allow_send"] and not co()["allow_cancel"]
print("OK cancel needs send")

# Badges του chip στο πλαϊνό μενού: και τα 5 εμφανίζονται, clickable, και το back γυρνά στη σελίδα.
h = c.get("/companies").get_data(as_text=True)
for f in ("allow_send", "allow_classify", "allow_new"):
    assert f'data-co-toggle="/companies/toggle/0/{f}"' in h, f
assert 'aria-label="Ακυρώσεις &amp; απορρίψεις"' in h  # αχνό/disabled όσο δεν υπάρχει αποστολή
r = c.post("/companies/toggle/0/allow_new", data={"back": "/parameters"})
assert r.headers["Location"].endswith("/parameters")
