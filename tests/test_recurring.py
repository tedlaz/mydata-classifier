"""Μηνιαίες εγγραφές από πρότυπα (και παλιά της «Νέας εγγραφής»): μία ανά μήνα (python -m tests.test_recurring)."""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583",
                     "allow_send": True, "allow_classify": True, "allow_new": True})
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
with A.app.test_request_context():
    draft, errors = A._entry_clean(A._draft_for_month(cid, db.get_template(cid, tid), "2026-02"))
assert not errors, errors
assert A._store_entry(cid, draft, (tid, "2026-02")) is None and len(docs()) == 1

# 4) Φόρμα καταχώρησης με ίδιο πρότυπο/μήνα → απορρίπτεται· άλλος μήνας → περνά.
def entry(**kw):
    d = {"invoice_type": "16.1", "series": "ENO", "aa": "9", "issue_date": "2026-02-10",
         "issuer": {"country": "GR", "vat": "123456789"},
         "lines": [{"net": 500, "vat_category": "8", "cls": [{"category": "category2_5", "type": "E3_585_014", "amount": 500}]}]}
    return json.dumps({**d, **kw})


form = {"draft": entry(), "template_id": str(tid)}
page = c.get("/entry", query_string={"template": tid}).get_data(as_text=True)
assert f'name="template_id" value="{tid}"' in page and '"aa": "2"' in page  # Α/Α: συνέχεια της σειράς
assert c.post("/entry", data=form).status_code == 422 and len(docs()) == 1
c.post("/entry", data=dict(form, draft=entry(issue_date="2026-03-10")))
assert len(docs()) == 2

# 5) Επεξεργασία κρατά το πρότυπο· διαγραφή ελευθερώνει τον μήνα.
mark = next(x["mark"] for x in docs() if x["period"] == "2026-03")
back = "/invoices?view=classified&sort=name&dir=desc"
page = c.get("/entry", query_string={"edit": mark, "back": back}).get_data(as_text=True).replace("&amp;", "&")
assert f'name="back" value="{back}"' in page  # η επεξεργασία κρατά τη λίστα (ταξινόμηση) για την επιστροφή
lines = [{"net": 620, "vat_category": "8", "cls": [{"category": "category2_5", "type": "E3_585_014", "amount": 620}]}]
r = c.post("/entry", data={"draft": entry(issue_date="2026-03-15", lines=lines), "edit_mark": mark, "back": back})
assert r.headers["Location"].endswith(back), r.headers["Location"]
(m3,) = [x for x in docs() if x["period"] == "2026-03"]
assert m3["total_net"] == 620.0 and m3["issue_date"] == "2026-03-15"
c.post(f"/draft/{m3['mark']}/delete")
c.post("/recurring/create", data={"period": "2026-03", "template_ids": [str(tid)]})
assert [x["period"] for x in docs()].count("2026-03") == 1

assert c.get("/recurring?period=2026-02").status_code == 200

# 5β) Τριμηνιαίο από Μάρτιο (Μάρ/Ιούν/Σεπ/Δεκ) και ετήσιο τον Δεκέμβριο (απογραφή λήξης).
stock = {"invoice_type": "17.5", "series": "APO", "lines": [dict(line, classification_category="category2_14",
                                                                  classification_type="E3_104")]}
q = db.save_template(cid, "Συνδρομή", dict(stock, series="SYN"))
yr = db.save_template(cid, "Απογραφή λήξης", stock)
c.post("/recurring/days", data={"period": "2026-02", f"day_{tid}": "31", f"day_{q}": "15", f"every_{q}": "3", f"start_{q}": "3",
                                f"day_{yr}": "31", f"every_{yr}": "12", f"start_{yr}": "12"})
assert (db.get_template(cid, q)["every_months"], db.get_template(cid, q)["start_month"]) == (3, 3)
assert [A._is_due(db.get_template(cid, q), f"2026-{m:02d}") for m in range(1, 13)].count(True) == 4
assert A._is_due(db.get_template(cid, q), "2026-09") and not A._is_due(db.get_template(cid, q), "2026-10")
assert A._next_due(db.get_template(cid, yr), "2026-02") == "2026-12"
assert A._next_due(db.get_template(cid, yr), "2026-12") == "2027-12"
with A.app.test_request_context():
    assert {t["name"] for t in A._recurring_pending(cid, "2026-06")} == {"Ενοίκιο", "Συνδρομή"}
    assert {t["name"] for t in A._recurring_pending(cid, "2026-12")} == {"Ενοίκιο", "Συνδρομή", "Απογραφή λήξης"}
page = c.get("/recurring?period=2026-02").get_data(as_text=True)
assert "⏭ Μάρτιος" in page and "⏭ Δεκέμβριος" in page and "Τριμηνιαία" in page
c.post("/recurring/create", data={"period": "2026-02", "template_ids": [str(q), str(yr)]})  # όχι αυτόν τον μήνα
assert not [d for d in db.get_documents(cid, "expense") if d["template_id"] in (q, yr)]
c.post("/recurring/create", data={"period": "2026-12", "template_ids": [str(q), str(yr)]})
made = {d["template_id"]: d for d in db.get_documents(cid, "expense") if d["template_id"] in (q, yr)}
assert made[q]["issue_date"] == "2026-12-15" and made[yr]["issue_date"] == "2026-12-31", made
assert "Προγραμματισμένες εγγραφές" in c.get("/recurring?period=2026-12").get_data(as_text=True)
assert c.get("/dashboard").status_code == 200
print("OK")

# 6) «Ακύρωση τελευταίου χαρακτηρισμού» σε δική μας εγγραφή → ΔΕΝ διαγράφεται (μένει πρόχειρο).
(m3,) = [x for x in docs() if x["period"] == "2026-03"]
c.post("/unclassify", data={"marks": [m3["mark"]]})
assert db.get_document(cid, m3["mark"])["local_action"] == "create"
print("OK unclassify")

# 7) Διαβιβασμένη εγγραφή = κανονικό παραστατικό: «Χαρακτηρισμός», όχι διαγραφή/επεξεργασία/επαναφορά ως πρόχειρο.
(d,) = [x for x in docs() if x["period"] == "2026-02"]
db.finalize_created(d["id"], "400000777")
assert db.get_document(cid, "400000777")["local_action"] == "classify"
assert "✏️ Αλλαγή χαρακτηρισμού" in c.get("/document/400000777").get_data(as_text=True)
c.post("/draft/400000777/delete")
c.post("/unclassify", data={"marks": ["400000777"]})
assert db.get_document(cid, "400000777")["status"] == "sent"
# Μετάπτωση: παλιά απεσταλμένη που έμεινε 'create' → 'classify' στην εκκίνηση.
with db.get_conn() as conn:
    conn.execute("UPDATE documents SET local_action = 'create' WHERE mark = '400000777'")
db.init_db()
assert db.get_document(cid, "400000777")["local_action"] == "classify"
print("OK sent")
