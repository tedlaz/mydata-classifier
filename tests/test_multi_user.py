"""Πολλοί browsers με τον ίδιο λογαριασμό: δική τους ενεργή εταιρεία, login/logout δεν επηρεάζει τους άλλους.
Εκτέλεση: python -m tests.test_multi_user"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402

db.init_db()
a = db.add_company({"company_name": "Α ΑΕ"})
b = db.add_company({"company_name": "Β ΑΕ"})
ids = [x["id"] for x in db.list_companies()]
x, y = A.app.test_client(), A.app.test_client()
login = {"username": "ted", "password": "pw"}
x.post("/setup", data={**login, "password2": "pw"})
y.post("/login", data=login)


def active(c):
    with c:
        assert c.get("/companies").status_code == 200  # συνδεδεμένος
        return A._active_company_id()


x.post(f"/companies/select/{ids.index(a)}")
y.post(f"/companies/select/{ids.index(b)}")
assert active(x) == a and active(y) == b

# Νέο login (τρίτος browser) δεν βγάζει τους άλλους.
z = A.app.test_client()
z.post("/login", data=login)
assert active(x) == a and active(y) == b and active(z) == b  # νέος → τελευταία επιλογή

# Logout του x δεν βγάζει τον y.
x.post("/logout")
assert x.get("/companies").headers["Location"].endswith("/login")
assert auth.unlocked() and active(y) == b

# Αλλαγή κωδικού από τον y βγάζει τους άλλους (z), όχι τον ίδιο.
y.post("/password", data={"username": "ted", "old_password": "pw", "password": "pw2", "password2": "pw2"})
assert active(y) == b
assert z.get("/companies").headers["Location"].endswith("/login")
print("OK")
