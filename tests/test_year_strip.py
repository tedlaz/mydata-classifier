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
# Βιβλία: πλοηγός περιόδου (Όλα › Έτος › Τρίμηνο › Μήνας › Εβδομάδα › Ημέρα) από το f_date, πλήθος της καρτέλας.
strip = A._month_strip(cid, ("expense",))
nav = lambda fd, view="classified": A._period_nav(strip, fd, view, cid, "expense")  # noqa: E731
f = lambda n: [k["f"] for k in n["kids"]]  # noqa: E731
n = nav("")
assert f(n)[0] == "2021" and n["crumbs"] == [("Όλα", None, 2)] and n["kids"][0]["n"] == 1 and n["kids"][0]["dot"], n
assert f(nav("2021")) == ["2021-01:2021-03", "2021-04:2021-06", "2021-07:2021-09", "2021-10:2021-12"]
n = nav("2021")
assert [k["n"] for k in n["kids"]] == [1, 0, 0, 0] and n["prev"] is None and n["next"] == "2022"
n = nav("2021-01:2021-03")
assert f(n) == ["2021-01", "2021-02", "2021-03"] and n["crumbs"][-1] == ("Α΄ τρίμ.", "2021-01:2021-03", 1)
assert n["next"] == "2021-04:2021-06" and not n["range"]
# Μήνας → εβδομάδες Δευτέρα–Κυριακή μέσα στον μήνα (1/3/2021 = Δευτέρα).
n = nav("2021-03")
assert f(n) == ["2021-03-01:2021-03-07", "2021-03-08:2021-03-14", "2021-03-15:2021-03-21", "2021-03-22:2021-03-28",
                "2021-03-29:2021-03-31"], f(n)
assert [k["n"] for k in n["kids"]] == [1, 0, 0, 0, 0] and n["kids"][0]["label"] == "Εβδ 1" and n["kids"][4]["wd"] == "29–31"
assert n["prev"] == "2021-02"
# Εβδομάδα → οι 7 ημέρες της (και οι κενές)· ‹ › γειτονικές εβδομάδες, και στον επόμενο μήνα (1/4/2021 = Πέμπτη).
n = nav("2021-03-15:2021-03-21", "unclassified")
assert f(n) == [f"2021-03-{d}" for d in range(15, 22)] and [k["n"] for k in n["kids"]][5] == 1 and not n["range"]
assert n["crumbs"][-1] == ("Εβδ 3", "2021-03-15:2021-03-21", 1) and n["prev"] == "2021-03-08:2021-03-14"
assert nav("2021-03-29:2021-03-31")["next"] == "2021-04-01:2021-04-04"
n = nav("2021-03-05", "unclassified")
assert f(n) == [f"2021-03-0{d}" for d in range(1, 8)] and [k["on"] for k in n["kids"]][4] and sum(k["on"] for k in n["kids"]) == 1
assert n["prev"] is None and n["next"] == "2021-03-20" and n["crumbs"] == [
    ("Όλα", None, 1), ("2021", "2021", 1), ("Α΄ τρίμ.", "2021-01:2021-03", 1), ("Μαρ", "2021-03", 1),
    ("Εβδ 1", "2021-03-01:2021-03-07", 0), ("5", "2021-03-05", 0)]
assert nav("2021-03-10", "unclassified")["next"] == "2021-03-20"  # ημέρα χωρίς παραστατικά: ‹ › στις γειτονικές με παραστατικά
n = nav("2021-02:2021-05")
assert n["range"] and [k["f"] for k in n["kids"] if k["on"]] == ["2021-02", "2021-03", "2021-04", "2021-05"]
n = nav("2021-03-01:2021-03-10")
assert n["range"] and [k["on"] for k in n["kids"]] == [True, False] and n["crumbs"][-1][2] == 1
assert nav("2021-03-06:2021-03-31", "unclassified")["crumbs"][-1][2] == 1 and nav("x")["crumbs"][-1][2] is None
assert all(k["on"] for k in nav("2021-03:2022-02")["kids"] if k["f"] in ("2021", "2022"))
# HTML: chips με data-ym/data-ymd, επιλεγμένο chip ξανά = ένα επίπεδο πάνω, ✕ μόνο με φίλτρο.
html = t.get("/invoices?view=classified&f_date=2021-03-05").data.decode().replace("%3A", ":")
assert 'class="pn-c is-on" style="--i:4"' in html and 'data-ymd="2021-03-05"' in html and 'title="Πα 05/03/2021: 1"' in html
assert re.search(r'href="[^"]*f_date=2021-03-01:2021-03-07(&|")[^>]*data-ymd="2021-03-05"', html), "ξανά κλικ → εβδομάδα"
assert 'class="pn-x"' in html and 'class="pn-x"' not in t.get("/invoices?view=classified").data.decode()
assert 'data-ym="2021-03"' in t.get("/income?f_date=2021-01:2021-03").data.decode().replace("%3A", ":")

# Παράμετρος: άκυρη τιμή δεν αλλάζει τη ρύθμιση· έγκυρη αποθηκεύεται.
t.post("/parameters/automation", data={"strip_from_year": "1990"})
assert db.get_setting("strip_from_year") == "2021"
t.post("/parameters/automation", data={"strip_from_year": "2023"})
assert db.get_setting("strip_from_year") == "2023"
print("ok")
