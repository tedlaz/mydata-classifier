"""Ειδικοί κωδικοί Μητρώου ΑΑΔΕ ανά εταιρεία + fallback VIES → GSIS για ελληνικά ΑΦΜ.
Εκτέλεση: python -m tests.test_gsis_fallback"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402
import gsis  # noqa: E402
import vies  # noqa: E402

os.environ["GSIS_USERNAME"] = os.environ["GSIS_PASSWORD"] = "env"  # αγνοούνται: μόνο ανά εταιρεία

A.app.testing = True
db.init_db()
cid = db.add_company({"company_name": "Δοκιμή ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k"})
db.set_active_company_id(cid)
c = A.app.test_client()
co = lambda: db.list_companies()[0]  # noqa: E731
form = {"company_name": "Δοκιμή ΑΕ", "aade_user_id": "u"}

calls = []
vies.check_vat = lambda country, afm: {"valid": False, "name": None, "address": None}
gsis.lookup = lambda afm, creds: calls.append((afm, creds)) or {
    "name": "ΜΗΤΡΩΟ ΑΕ", "street": "ΑΙΟΛΟΥ", "number": "86", "postal_code": "10559", "city": "ΑΘΗΝΑ"}

# Χωρίς κωδικούς: μόνο VIES.
assert not c.get("/check_vat/EL/094019245").json["valid"] and not calls
assert "Μητρώο ΑΑΔΕ" in c.get("/companies").get_data(as_text=True)

# Με κωδικούς: fallback στο GSIS (μόνο για ελληνικά ΑΦΜ).
c.post("/companies/update/0", data=form | {"gsis_username": "gu", "gsis_password": "gp"})
assert (co()["GSIS_USERNAME"], co()["GSIS_PASSWORD"]) == ("gu", "gp")
r = c.get("/check_vat/EL/094019245").json
assert r["valid"] and r["legal_name"] == "ΜΗΤΡΩΟ ΑΕ" and r["postal_code"] == "10559", r
assert calls == [("094019245", ("gu", "gp"))]
assert not c.get("/check_vat/DE/123456789").json["valid"] and len(calls) == 1

# Κενός κωδικός = κράτα τον αποθηκευμένο· toggles δεν τους σβήνουν· κενό username = απενεργοποίηση.
c.post("/companies/update/0", data=form | {"gsis_username": "gu2"})
assert (co()["GSIS_USERNAME"], co()["GSIS_PASSWORD"]) == ("gu2", "gp")
c.post("/companies/toggle/0/allow_send")
assert co()["GSIS_PASSWORD"] == "gp"
c.post("/companies/update/0", data=form)
assert not gsis.credentials(co())
print("OK")
