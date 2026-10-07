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

# Χωρίς credentials: δεν κλείνει ο λογιστής.
db.update_company(cid, co() | {"AADE_USER_ID": "", "use_accountant": True})
c.post("/companies/toggle/0/use_accountant")
assert co()["use_accountant"]

# Κλειδωμένη: οι άδειες δεν ανοίγουν.
db.lock_company(cid)
c.post("/companies/toggle/0/allow_new")
assert not co()["allow_new"]
print("OK")
