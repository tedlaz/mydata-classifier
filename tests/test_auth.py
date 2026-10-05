"""Έλεγχος: κεντρικός χρήστης, κρυπτογράφηση κλειδιών myDATA, αλλαγή κωδικού, reset."""

import os
import sqlite3
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402

c = A.app.test_client()


def raw():
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute("SELECT aade_user_id, aade_subscription_key FROM companies").fetchone()


# Υπάρχουσα εταιρεία πριν το setup: plaintext.
db.add_company({"company_name": "Α", "AADE_USER_ID": "u1", "AADE_SUBSCRIPTION_KEY": "SECRETKEY"})
db.save_accountant({"user_id": "acc", "subscription_key": "ACCKEY"})
assert raw() == ("u1", "SECRETKEY")

# 1) Χωρίς χρήστη → /setup· κακός κωδικός απορρίπτεται.
assert c.get("/").headers["Location"].endswith("/setup")
assert c.get("/setup").status_code == 200
c.post("/setup", data={"username": "ted", "password": "", "password2": ""})
assert db.get_auth() is None

# 2) Setup → κρυπτογράφηση των υπαρχόντων κλειδιών, είσοδος.
r = c.post("/setup", data={"username": "ted", "password": "correct horse", "password2": "correct horse"})
assert r.headers["Location"].endswith("/dashboard")
assert all(v.startswith("enc:") for v in raw())
for p in (db.DB_PATH, db.DB_PATH + "-wal"):  # ούτε σε ελεύθερες σελίδες / WAL
    assert not os.path.exists(p) or b"SECRETKEY" not in open(p, "rb").read()
assert db.list_companies()[0]["AADE_SUBSCRIPTION_KEY"] == "SECRETKEY"
assert db.get_accountant()["subscription_key"] == "ACCKEY"
assert c.get("/companies").status_code == 200
assert b"SECRETKEY" not in c.get("/companies").data  # το κλειδί δεν φτάνει στον browser
assert b"ACCKEY" not in c.get("/parameters").data

# 3) Κενό κλειδί στη φόρμα = κράτα το αποθηκευμένο.
c.post("/companies/update/0", data={"company_name": "Α2", "aade_user_id": "u1", "aade_subscription_key": ""})
assert db.list_companies()[0]["AADE_SUBSCRIPTION_KEY"] == "SECRETKEY"
c.post("/accountant", data={"user_id": "acc", "subscription_key": "", "env": "prod"})
assert db.get_accountant()["subscription_key"] == "ACCKEY"

# 4) Πλαστό session / restart → login.
c2 = A.app.test_client()
with c2.session_transaction() as s:
    s["auth"] = "forged"
assert c2.get("/").headers["Location"].endswith("/login")
c.post("/logout")
assert not auth.unlocked()
assert c.get("/").headers["Location"].endswith("/login")
assert c.get("/login").status_code == 200

# 5) Λάθος κωδικός / λάθος χρήστης απορρίπτονται· σωστός ξεκλειδώνει.
c.post("/login", data={"username": "ted", "password": "wrong password"})
assert not auth.unlocked()
c.post("/login", data={"username": "other", "password": "correct horse"})
assert not auth.unlocked()
c.post("/login", data={"username": "ted", "password": "correct horse"})
assert auth.unlocked() and c.get("/companies").status_code == 200

# 6) Αλλαγή κωδικού: τα κλειδιά μένουν ίδια, ο παλιός κωδικός δεν δουλεύει.
before = raw()
c.post("/password", data={"username": "ted", "old_password": "correct horse", "password": "new password", "password2": "new password"})
assert raw() == before
rec = db.get_auth()
assert not auth.unlock("ted", "correct horse", rec)
assert auth.unlock("ted", "new password", rec)
assert db.list_companies()[0]["AADE_SUBSCRIPTION_KEY"] == "SECRETKEY"

# 7) Reset: σβήνει χρήστη και κλειδιά, κρατά τις εταιρείες.
db.reset_auth()
assert db.get_auth() is None and raw() == ("", "")
assert db.list_companies()[0]["company_name"] == "Α2"

print("OK")
