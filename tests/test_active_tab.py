"""Δύο καρτέλες, άλλη ενεργή εταιρεία: POST με παλιό _cid δεν εκτελείται.
Εκτέλεση: python -m tests.test_active_tab"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
db.init_db()
a = db.add_company({"company_name": "Α ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k", "allow_send": True})
b = db.add_company({"company_name": "Β ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k", "allow_send": True})
db.set_active_company_id(b)
c = A.app.test_client()

assert f'name="active-company" content="{b}"'.encode() in c.get("/companies").data

# Καρτέλα της Α, ενώ ενεργή είναι η Β → απορρίπτεται, πίσω στη σελίδα.
r = c.post("/send", data={"_cid": str(a)}, headers={"Referer": "http://localhost/invoices?view=classified"})
assert r.status_code == 302 and r.headers["Location"].endswith("/invoices?view=classified")
assert "άλλη καρτέλα" in c.get("/invoices?view=classified").get_data(as_text=True)

# Σωστό _cid ή χωρίς _cid → κανονική ροή (εδώ: τίποτα για αποστολή).
for data in ({"_cid": str(b)}, {}):
    r = c.post("/send", data=data)
    assert r.status_code == 302 and "view=classified" in r.headers["Location"]
    assert "άλλη καρτέλα" not in c.get("/invoices?view=classified").get_data(as_text=True)

# Εταιρείες: η αλλαγή εταιρείας από παλιά καρτέλα επιτρέπεται.
idx_a = [x["id"] for x in db.list_companies()].index(a)
c.post(f"/companies/select/{idx_a}", data={"_cid": str(b)})
assert db.get_active_company_id() == a
print("OK")
