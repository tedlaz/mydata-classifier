"""Έλεγχος «αρχείου πελάτη»: κρυπτογράφηση με κωδικό, ποτέ κλειδί λογιστή, μόνιμο κλείδωμα.
Εκτέλεση: python -m tests.test_share"""
import io
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
db.init_db()

# 1) seal/unseal: ίδιο αντικείμενο· λάθος κωδικός → ValueError.
box = auth.seal({"a": "Ελλάδα"}, "pass-1234")
assert auth.unseal(box, "pass-1234") == {"a": "Ελλάδα"}
for bad in ("wrong-pass", ""):
    try:
        auth.unseal(box, bad)
        raise AssertionError("δέχτηκε λάθος κωδικό")
    except ValueError:
        pass
assert b"\xce\x95" not in box  # ούτε το περιεχόμενο φαίνεται ακρυπτογράφητο

# 2) Εταιρεία λογιστή με όλες τις άδειες· το αρχείο δεν έχει τίποτα από τον λογιστή.
db.save_accountant({"user_id": "ACCUSER", "subscription_key": "ACCKEY"})
cid = db.add_company({"company_name": "Πελάτης ΑΕ", "AADE_VAT_NUMBER": "123456789", "AADE_USER_ID": "u1",
                      "AADE_SUBSCRIPTION_KEY": "OWNKEY", "use_accountant": True, "allow_send": True,
                      "allow_classify": True, "allow_new": True, "allow_cancel": True})
db.set_active_company_id(cid)
c = A.app.test_client()
assert c.post("/companies/share/0", data={"password": "short", "password2": "short"}).status_code == 302
r = c.post("/companies/share/0", data={"password": "pass-1234", "password2": "pass-1234"})
assert r.headers["Content-Disposition"].endswith(".my")
plain = json.dumps(auth.unseal(r.data, "pass-1234"), ensure_ascii=False)
assert "ACCKEY" not in plain and "ACCUSER" not in plain
assert "OWNKEY" in plain and '"use_accountant": false' in plain
assert b"OWNKEY" not in r.data

# 3) Εισαγωγή (εδώ ίδια επωνυμία → ενημέρωση): κλειδωμένη, όλες οι άδειες κλειστές.
bad = c.post("/companies/import-share", data={"password": "wrong-pass", "file": (io.BytesIO(r.data), "x.my")},
             follow_redirects=True)
assert "Λάθος κωδικός".encode() in bad.data and not db.list_companies()[0]["locked"]
c.post("/companies/import-share", data={"password": "pass-1234", "file": (io.BytesIO(r.data), "x.my")})
comp = db.list_companies()[0]
assert comp["locked"] and comp["AADE_SUBSCRIPTION_KEY"] == "OWNKEY" and not comp["use_accountant"]
perms = ("allow_send", "allow_classify", "allow_new", "allow_cancel")
assert not any(comp[p] for p in perms)

# 4) Δεν ξανανοίγουν: ούτε από τη φόρμα, ούτε από εισαγωγή companies.json.
db.update_company(cid, {**comp, **dict.fromkeys(perms, True)})
c.post("/companies/update/0", data={"company_name": "Πελάτης ΑΕ", "aade_user_id": "u1", **dict.fromkeys(perms, "1")})
c.post("/companies/import", data={"file": (io.BytesIO(json.dumps({"companies": [
    {"company_name": "Πελάτης ΑΕ", "AADE_USER_ID": "u1", "AADE_SUBSCRIPTION_KEY": "k"}]}).encode()), "c.json")})
comp = db.list_companies()[0]
assert comp["locked"] and not any(comp[p] for p in perms)
assert c.get("/new_expense").status_code == 302
assert b"disabled" in c.get("/companies").data
print("ok")
