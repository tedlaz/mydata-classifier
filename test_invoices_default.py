"""Έλεγχος: τα βιβλία εξόδων/εσόδων ανοίγουν στο πρώτο tab με εγγραφές, αλλιώς στην Ανάκτηση.
Εκτέλεση: python test_invoices_default.py"""
import os
import re
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
c = A.app.test_client()


def add(mark, status, kind="expense"):
    db.upsert_document(cid, kind, {
        "mark": mark, "issue_date": "2026-01-10", "issuer_vat": "111", "invoice_type": "1.1",
        "total_net": 100.0, "total_vat": 24.0, "total_gross": 124.0,
        "lines": [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"}],
    }, status)


def active_tab(url):
    r = c.get(url)
    assert r.status_code == 200, r.status_code
    return re.search(r'lh-tile--(\w+) is-on', r.get_data(as_text=True)).group(1)


# 1) Καμία εγγραφή → Ανάκτηση / Διαγραφή.
r = c.get("/invoices")
assert r.status_code == 302 and r.location.endswith("/invoices/sync")

# 2) Μόνο απεσταλμένα → tab sent.
add("2001", "sent")
assert active_tab("/invoices") == "sent"

# 3) Αχαρακτήριστα + απεσταλμένα → το πρώτο με τη σειρά (unclassified).
add("2002", "unclassified")
assert active_tab("/invoices") == "unclassified"

# 4) Ρητό tab μένει ως έχει, ακόμα κι άδειο.
assert active_tab("/invoices?view=confirmed") == "confirmed"

# 5) Βιβλίο εσόδων: ίδιος κανόνας.
r = c.get("/income")
assert r.status_code == 302 and r.location.endswith("/income/sync")
add("3001", "classified", "income")
assert active_tab("/income") == "classified"
add("3002", "unclassified", "income")
assert active_tab("/income") == "unclassified"
assert active_tab("/income?view=classified") == "classified"

print("OK")
