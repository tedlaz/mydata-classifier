"""Λωρίδα ετών/μηνών: έτη από την παράμετρο «strip_from_year» (προεπιλογή 2024), σύνολα ανά έτος,
καθρέπτης στις σελίδες (python -m tests.test_year_strip)."""
import os
import re
import tempfile
from datetime import datetime

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
this_year = datetime.now(A.ATHENS).year


def doc(kind, mark, issue, status):
    db.upsert_document(cid, kind, {
        "mark": mark, "issue_date": issue, "issuer_vat": "111", "invoice_type": "1.1",
        "total_net": 10.0, "total_vat": 2.4, "total_gross": 12.4,
        "lines": [{"line_number": 1, "net_value": 10.0, "vat_amount": 2.4, "vat_category": "1"}],
    }, status)


doc("expense", "1", "2021-03-05", "classified")
doc("expense", "2", "2021-03-20", "unclassified")
doc("income", "3", "2021-07-01", "classified")
doc("expense", "4", f"{this_year}-01-10", "classified")

# Προεπιλογή: από το 2024 → το 2021 δεν φαίνεται.
s = A._month_strip(cid, ("income", "expense"))
assert [y["y"] for y in s["years"]] == [str(y) for y in range(2024, this_year + 1)], s["years"]
assert all(len(y["months"]) == 12 for y in s["years"])

# Από το 2021: σύνολα ετών, αχαρακτήριστα ανά πλευρά.
db.set_setting("strip_from_year", "2021")
s = A._month_strip(cid, ("income", "expense"))
y21 = s["years"][0]
assert y21["y"] == "2021" and y21["tot"] == {"income": 1, "expense": 1, "in_pending": 0, "ex_pending": 1, "total": 3,
                                       "unclassified": 1, "classified": 2, "sent": 0, "confirmed": 0}, y21["tot"]
assert dict(y21["months"])["2021-03"]["total"] == 2

# Σελίδες: ένα έτος ανοικτό (το τρέχον), κελί έτους με preset στην Ανάκτηση.
t = A.app.test_client()
html = t.get("/sync").data.decode()
assert html.count('name="sy-yr"') == this_year - 2020, html.count('name="sy-yr"')
assert f'id="sy-yr-{this_year}" checked' in html and 'id="sy-yr-2021" checked' not in html
assert 'data-preset="year-of" data-y="2021"' in html
assert 'data-preset="quarter-of" data-y="2021" data-q="1"' in html
# Βιβλίο με φίλτρο μήνα 2021 → ανοικτό το 2021.
html = t.get("/invoices?f_date=2021-03").data.decode()
assert 'id="sy-yr-2021" checked' in html and f'id="sy-yr-{this_year}" checked' not in html
# Βιβλίο εξόδων: γραμμές ανά κατάσταση (2021: 1 χαρακτηρισμένο, 1 αχαρακτήριστο).
assert all(t in html for t in ('title="Αχαρακτήριστα">1</b>', 'title="Χαρακτηρισμένα">1</b>', 'title="Απεσταλμένα"></b>',
                               'title="Ολοκληρωμένα"></b>')), "γραμμές καταστάσεων"
assert 'title="Απεσταλμένα"' not in t.get("/income").data.decode()
# Τρίμηνο: σύνδεσμος με φίλτρο διαστήματος· με ενεργό φίλτρο → is-on και ανοικτό το έτος του.
assert "f_date=2021-01:2021-03" in html.replace("%3A", ":")
html = t.get("/invoices?f_date=2021-01:2021-03").data.decode()
assert 'class="sy-qh is-on"' in html and 'id="sy-yr-2021" checked' in html
# Οι μήνες του επιλεγμένου τριμήνου (ή διαστήματος, π.χ. από Shift+κλικ) επιλέγονται κι αυτοί.
on = lambda h: sorted(set(re.findall(r'class="sy-mo[^"]*is-on" data-ym="([\d-]+)"', h)))  # noqa: E731
assert on(html) == ["2021-01", "2021-02", "2021-03"], on(html)
assert on(t.get("/invoices?f_date=2021-02:2021-05").data.decode()) == ["2021-02", "2021-03", "2021-04", "2021-05"]

# Παράμετρος: άκυρη τιμή δεν αλλάζει τη ρύθμιση· έγκυρη αποθηκεύεται.
t.post("/parameters/automation", data={"strip_from_year": "1990"})
assert db.get_setting("strip_from_year") == "2021"
t.post("/parameters/automation", data={"strip_from_year": "2023"})
assert db.get_setting("strip_from_year") == "2023"
print("ok")
