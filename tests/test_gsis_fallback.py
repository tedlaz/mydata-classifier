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
    "name": "ΜΗΤΡΩΟ ΑΕ", "street": "ΑΙΟΛΟΥ", "number": "86", "postal_code": "10559", "city": "ΑΘΗΝΑ",
    "details": [("Επωνυμία", "ΜΗΤΡΩΟ ΑΕ"), ("ΔΟΥ", "ΦΑΕ ΑΘΗΝΩΝ"), ("Τ.Κ.", "10559")],
    "activities": [{"firm_act_code": "62010000", "firm_act_descr": "ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ", "firm_act_kind": "1",
                    "firm_act_kind_descr": "ΚΥΡΙΑ"}]}

# Χωρίς κωδικούς: μόνο VIES.
assert not c.get("/check_vat/EL/094019245").json["valid"] and not calls
assert "Μητρώο ΑΑΔΕ" in c.get("/companies").get_data(as_text=True)
# Σελίδα «Αναζήτηση ΑΦΜ»: χωρίς κωδικούς το tab GSIS είναι ανενεργό και ?tab=gsis πέφτει στο VIES.
html = c.get("/vat_lookup?tab=gsis&afm=094019245").get_data(as_text=True)
assert 'class="tab" aria-disabled="true"' in html and "δεν βρέθηκε ενεργό στο VIES" in html and not calls
assert '<option value="GR" selected>' in html and '<option value="DE"' in html

# Με κωδικούς: fallback στο GSIS (μόνο για ελληνικά ΑΦΜ).
c.post("/companies/update/0", data=form | {"gsis_username": "gu", "gsis_password": "gp"})
assert (co()["GSIS_USERNAME"], co()["GSIS_PASSWORD"]) == ("gu", "gp")
r = c.get("/check_vat/EL/094019245").json
assert r["valid"] and r["legal_name"] == "ΜΗΤΡΩΟ ΑΕ" and r["postal_code"] == "10559", r
assert calls == [("094019245", ("gu", "gp"))]
assert not c.get("/check_vat/DE/123456789").json["valid"] and len(calls) == 1
html = c.get("/vat_lookup?tab=gsis&afm=094019245").get_data(as_text=True)
assert 'class="tab" aria-disabled' not in html and "ΜΗΤΡΩΟ ΑΕ" in html and "10559" in html and len(calls) == 2
assert "ΦΑΕ ΑΘΗΝΩΝ" in html and "62010000" in html and "ΚΥΡΙΑ" in html
assert "9 ψηφία" in c.get("/vat_lookup?tab=gsis&afm=12").get_data(as_text=True) and len(calls) == 2
vies.check_vat = lambda country, afm: {"valid": True, "name": "ΟΡΓΑΝΙΣΜΟΣ ΑΕ||ΟΤΕ", "address": "ΚΗΦΙΣΙΑΣ 99        15124 - ΜΑΡΟΥΣΙ"}
html = c.get("/vat_lookup?country=GR&afm=094019245").get_data(as_text=True)
assert "ΟΡΓΑΝΙΣΜΟΣ ΑΕ" in html and "ΟΤΕ" in html and "15124" in html and len(calls) == 2
vies.check_vat = lambda country, afm: {"valid": False, "name": None, "address": None}

# Κενός κωδικός = κράτα τον αποθηκευμένο· toggles δεν τους σβήνουν· κενό username = απενεργοποίηση.
c.post("/companies/update/0", data=form | {"gsis_username": "gu2"})
assert (co()["GSIS_USERNAME"], co()["GSIS_PASSWORD"]) == ("gu2", "gp")
c.post("/companies/toggle/0/allow_send")
assert co()["GSIS_PASSWORD"] == "gp"
c.post("/companies/update/0", data=form)
assert not gsis.credentials(co())
print("OK")
