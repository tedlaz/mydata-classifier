"""Εναλλαγή εταιρείας από το chip: επιστροφή στην ίδια σελίδα όταν επιτρέπεται, έλεγχος company_id.
Εκτέλεση: python -m tests.test_company_switch"""
import os
import tempfile
from urllib.parse import urlparse

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
db.init_db()
a = db.add_company({"company_name": "Α ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k"})
b = db.add_company({"company_name": "Β ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "secret-key-b",
                    "allow_new": True})
c = A.app.test_client()
ids = [x["id"] for x in db.list_companies()]
ia, ib = ids.index(a), ids.index(b)


def switch(idx, cid, back=None):
    db.set_active_company_id(b if cid == a else a)  # ξεκινά πάντα από την «άλλη»
    data = {"company_id": str(cid)} if cid is not None else {}
    if back is not None:
        data["back"] = back
    r = c.post(f"/companies/select/{idx}", data=data)
    assert r.status_code == 302, r.status_code
    return r.headers["Location"]


def ends(loc, path):
    u = urlparse(loc)
    return u.path == path and not u.query


# Σελίδες χωρίς παραμέτρους → ίδια διαδρομή, χωρίς query.
assert ends(switch(ia, a, "/invoices?view=classified"), "/invoices")
assert db.get_active_company_id() == a
assert ends(switch(ia, a, "/reports/vat"), "/reports/vat")

# Εκτός εφαρμογής / σελίδες της παλιάς εταιρείας / άγνωστες / «Εταιρείες» → Πίνακας ελέγχου.
for back in ("//evil.com/x", "https://evil.com", "/\\evil", "/\t/x", "/document/123", "/classify/123",
             "/suppliers/999/documents", "/nonexistent", "/companies", "/static/app.css"):
    assert ends(switch(ia, a, back), "/dashboard"), back
assert "μεταφέρθηκες στον Πίνακα ελέγχου" in c.get("/dashboard").get_data(as_text=True)

# Σελίδα απενεργοποιημένη για τη νέα εταιρεία (Α: allow_new off) → Πίνακας ελέγχου· για τη Β επιτρέπεται.
assert ends(switch(ia, a, "/entry?edit=123"), "/dashboard")
assert ends(switch(ib, b, "/entry?edit=123"), "/entry")

# Χωρίς back (φόρμα σελίδας «Εταιρείες», παλιά tests) → Πίνακας ελέγχου όπως πριν· χωρίς company_id επιτρέπεται.
assert ends(switch(ia, None), "/dashboard") and db.get_active_company_id() == a

# Λάθος company_id για τη θέση (παλιά καρτέλα μετά από διαγραφή) → καμία αλλαγή.
db.set_active_company_id(b)
r = c.post(f"/companies/select/{ia}", data={"company_id": str(b), "back": "/invoices"})
assert r.status_code == 302 and ends(r.headers["Location"], "/companies")
assert db.get_active_company_id() == b
assert "Η λίστα εταιρειών άλλαξε" in c.get("/companies").get_data(as_text=True)

# Ίδιος έλεγχος στις παράμετροι: λάθος company_id δεν αλλάζει άλλη εταιρεία.
r = c.post(f"/companies/update/{ia}", data={"company_id": str(b), "company_name": "ΧΑΚ", "aade_user_id": "x"})
assert ends(r.headers["Location"], "/companies")
assert db.list_companies()[ia]["company_name"] == "Α ΑΕ"

# Παλιό _cid δεν μπλοκάρει την αλλαγή εταιρείας.
db.set_active_company_id(b)
c.post(f"/companies/select/{ia}", data={"company_id": str(a), "_cid": str(b), "back": "/invoices"})
assert db.get_active_company_id() == a

# Η λίστα: μία, με φόρμα για την άλλη εταιρεία, χωρίς credentials.
html = c.get("/dashboard").get_data(as_text=True)
assert html.count('id="co-switch"') == 1
assert f'action="/companies/select/{ib}"' in html and 'aria-controls="co-switch"' in html
assert "secret-key-b" not in html.split('id="co-switch"', 1)[1].split("</div>", 1)[0]

# Μία εταιρεία → χωρίς λίστα, το chip ανοίγει τις παραμέτρους όπως πριν.
c.post(f"/companies/delete/{ib}", data={"confirm": "Β ΑΕ"})
assert len(db.list_companies()) == 1
assert 'id="co-switch"' not in c.get("/dashboard").get_data(as_text=True)
print("OK")
