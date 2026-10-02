"""Μηνιαίες εγγραφές από πρότυπα: μία ανά μήνα (python -m tests.test_recurring)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)
line = {"amount": 500.0, "classification_category": "category2_4", "classification_type": "E3_585_016",
        "vat_category": "8", "vat_amount": 0.0, "vat_type": ""}
tid = db.save_template(cid, "Ενοίκιο", {"invoice_type": "16.1", "series": "ENO", "issuer_vat": "123456789",
                                        "issuer_country": "GR", "payment_method": "3", "lines": [line, None]})
c = A.app.test_client()
docs = lambda: [d for d in db.get_documents(cid, "expense") if d["template_id"] == tid]  # noqa: E731

# 1) Μη μηνιαίο → δεν δημιουργείται τίποτα.
c.post("/recurring/create", data={"period": "2026-02", "template_ids": [str(tid)]})
assert docs() == []

# 2) Ημέρα 31 → τελευταία του Φεβρουαρίου· Α/Α 1.
c.post("/recurring/days", data={"period": "2026-02", f"day_{tid}": "31"})
c.post("/recurring/create", data={"period": "2026-02", "template_ids": [str(tid)]})
(d,) = docs()
assert (d["issue_date"], d["aa"], d["status"], d["period"]) == ("2026-02-28", "1", "classified", "2026-02"), d
assert d["total_net"] == 500.0

# 3) Ξανά ίδιος μήνας → τίποτα· και απευθείας στη βάση → το unique index το απορρίπτει.
c.post("/recurring/create", data={"period": "2026-02", "template_ids": [str(tid)]})
assert len(docs()) == 1
draft = A._draft_for_month(cid, db.get_template(cid, tid), "2026-02")
assert A._store_self_expense(cid, draft, (tid, "2026-02")) is None and len(docs()) == 1

# 4) Χειροκίνητη φόρμα με ίδιο πρότυπο/μήνα → απορρίπτεται· άλλος μήνας → περνά.
form = {"invoice_type": "16.1", "series": "ENO", "aa": "9", "issue_date": "2026-02-10", "issuer_vat": "123456789",
        "issuer_country": "GR", "payment_method": "3", "line_amount": "500", "line_category": "category2_4",
        "line_type": "E3_585_016", "line_vat_category": "8", "line_vat_amount": "", "line_vat_type": "",
        "template_id": str(tid)}
assert c.post("/new_expense", data=form).status_code == 422 and len(docs()) == 1
c.post("/new_expense", data=dict(form, issue_date="2026-03-10"))
assert len(docs()) == 2

# 5) Επεξεργασία κρατά το πρότυπο· διαγραφή ελευθερώνει τον μήνα.
mark = next(x["mark"] for x in docs() if x["period"] == "2026-03")
c.post("/new_expense", data=dict(form, issue_date="2026-03-15", template_id="", edit_mark=mark, line_amount="620"))
(m3,) = [x for x in docs() if x["period"] == "2026-03"]
assert m3["total_net"] == 620.0 and m3["issue_date"] == "2026-03-15"
c.post(f"/draft/{m3['mark']}/delete")
c.post("/recurring/create", data={"period": "2026-03", "template_ids": [str(tid)]})
assert [x["period"] for x in docs()].count("2026-03") == 1

assert c.get("/recurring?period=2026-02").status_code == 200
assert c.get("/dashboard").status_code == 200
print("OK")

# 6) «Επαναφορά σε αχαρακτήριστα» σε δική μας εγγραφή → διαγράφεται.
(m3,) = [x for x in docs() if x["period"] == "2026-03"]
c.post("/unclassify", data={"marks": [m3["mark"]]})
assert db.get_document(cid, m3["mark"]) is None
print("OK unclassify")
