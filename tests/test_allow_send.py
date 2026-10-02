"""Έλεγχος: η αποστολή στο myDATA είναι κλειστή εξ ορισμού (και σε παλιές βάσεις) και
μπλοκάρει και τις ακυρώσεις/απορρίψεις. Εκτέλεση: python -m tests.test_allow_send"""
import os
import sqlite3
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

# 1) Παλιά βάση χωρίς allow_send, με ενεργές ακυρώσεις → μετά τη μετάπτωση: μόνο ανάγνωση.
with sqlite3.connect(db.DB_PATH) as c:
    c.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL,
            aade_user_id TEXT, aade_subscription_key TEXT, aade_vat_number TEXT,
            mydata_env TEXT DEFAULT 'prod', use_accountant INTEGER DEFAULT 0, allow_cancel INTEGER DEFAULT 0);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO companies (company_name, allow_cancel) VALUES ('Α', 1);
        INSERT INTO settings VALUES ('active_company_id', '1');
    """)
db.init_db()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
comp = db.list_companies()[0]
assert comp["allow_send"] is False and comp["allow_cancel"] is True
with A.app.test_request_context():
    assert not A.send_allowed()
    assert not A.cancel_allowed()  # οι ακυρώσεις θέλουν και αποστολή


def no_client():
    raise AssertionError("δεν πρέπει να γίνει κλήση στο myDATA")


A.get_client = no_client
db.upsert_document(1, "expense", {
    "mark": "1001", "issue_date": "2026-01-10", "issuer_vat": "111", "invoice_type": "1.1",
    "total_net": 100.0, "total_vat": 24.0, "total_gross": 124.0,
    "lines": [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"}],
}, "classified")
c = A.app.test_client()

# 2) POST /send χωρίς άδεια → redirect με μήνυμα, καμία κλήση στο myDATA.
r = c.post("/send", follow_redirects=True)
assert "Η αποστολή στο myDATA είναι απενεργοποιημένη".encode() in r.data
assert "🔒 Αποστολή στο myDATA".encode() in c.get("/invoices?view=classified").data
assert b'name="allow_send"' in c.get("/companies").data

# 3) Ενεργοποίηση → επιτρέπονται αποστολές και ακυρώσεις.
db.update_company(1, {**comp, "allow_send": True})
with A.app.test_request_context():
    assert A.send_allowed() and A.cancel_allowed()
assert "🔒 Αποστολή στο myDATA".encode() not in c.get("/invoices?view=classified").data

# 4) Νέα εταιρεία: κλειστή εξ ορισμού.
cid = db.add_company({"company_name": "Β"})
assert next(x for x in db.list_companies() if x["id"] == cid)["allow_send"] is False
print("ok")

# 5) Χαρακτηρισμοί / «Νέα εγγραφή»: κλειστά εξ ορισμού, ανοίγουν από τις Παραμέτρους.
assert comp["allow_classify"] is False and comp["allow_new"] is False
for url in ("/classify/1001", "/new_expense", "/recurring", "/invoices/auto-classify"):
    r = c.get(url)
    assert r.status_code == 302 and r.headers["Location"].endswith("/invoices"), url
assert c.post("/bulk_classify").status_code == 302
page = c.get("/invoices?view=unclassified").data.decode()
assert 'href="/new_expense"' not in page and "Μαζικός χαρακτηρισμός" not in page
db.update_company(1, {**comp, "allow_send": True, "allow_classify": True, "allow_new": True})
assert c.get("/new_expense").status_code == 200
assert c.get("/classify/1001").status_code == 200
print("ok gates")
