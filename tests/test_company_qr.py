"""Έλεγχος QR εταιρείας για το myDATAview: σωστά στοιχεία, μόνο με POST, ποτέ κλειδί λογιστή.
Εκτέλεση: python -m tests.test_company_qr"""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
db.init_db()
db.save_accountant({"user_id": "ACCUSER", "subscription_key": "ACCKEY"})

own = db.add_company({"company_name": "Ελληνική Δοκιμή ΑΕ", "AADE_VAT_NUMBER": "123456789", "AADE_USER_ID": "OWNUSER",
                      "AADE_SUBSCRIPTION_KEY": "OWNKEY", "MYDATA_ENV": "dev", "vat_period": "q", "use_accountant": True})
none = db.add_company({"company_name": "Χωρίς κλειδιά", "AADE_VAT_NUMBER": "987654321"})
shared = db.add_company({"company_name": "Κλειδιά λογιστή", "AADE_VAT_NUMBER": "111222333", "AADE_USER_ID": "ACCUSER",
                         "AADE_SUBSCRIPTION_KEY": "ACCKEY"})
db.set_active_company_id(own)
c = A.app.test_client()

# 1) Το QR έχει όλα τα στοιχεία, σε καθαρό ASCII (η βιβλιοθήκη QR γράφει μόνο 8-bit χαρακτήρες).
r = c.post(f"/companies/qr/{own}")
assert r.status_code == 200 and "no-store" in r.headers["Cache-Control"]
text = r.get_json()["text"]
assert text.isascii()
assert json.loads(text) == {"format": "mydataview-company", "v": 1, "name": "Ελληνική Δοκιμή ΑΕ", "vat": "123456789",
                            "env": "dev", "vat_period": "q", "user": "OWNUSER", "key": "OWNKEY"}

# 2) Μόνο POST: τα κλειδιά δεν έρχονται με σύνδεσμο, ούτε μπαίνουν στη σελίδα της εταιρείας.
assert c.get(f"/companies/qr/{own}").status_code == 405
page = c.get("/companies").get_data(as_text=True)
assert "OWNKEY" not in page
assert f"/companies/qr/{own}" in page

# 3) Κλειδιά λογιστή: ποτέ στο QR, ούτε ως εναλλακτική, ούτε όταν είναι γραμμένα στην εταιρεία.
for cid in (none, shared):
    r = c.post(f"/companies/qr/{cid}")
    assert r.status_code == 400, cid
    assert "ACCKEY" not in r.get_data(as_text=True)
    assert "error" in r.get_json()

# 4) Άγνωστη εταιρεία.
assert c.post("/companies/qr/9999").status_code == 404

# 5) Από ξένη σελίδα απορρίπτεται (CSRF).
assert c.post(f"/companies/qr/{own}", headers={"Origin": "https://evil.example"}).status_code == 403

print("ok")
