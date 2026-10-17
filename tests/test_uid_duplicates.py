"""Έλεγχος: ίδιο myDATA UID με πολλά MARK → στα βιβλία μόνο το νεότερο MARK, τα άλλα στον κουβά
(«myDATA λοιπά» → «Διπλές διαβιβάσεις»). Εκτέλεση: python -m tests.test_uid_duplicates"""
import os
import sqlite3
import tempfile
from unittest import mock

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402


def xml(mark, uid="U1"):
    return (f'<invoice xmlns="http://www.aade.gr/myDATA/invoice/v1.0"><uid>{uid}</uid><mark>{mark}</mark>'
            f'<invoiceHeader><issueDate>2024-01-02</issueDate><invoiceType>1.1</invoiceType></invoiceHeader>'
            f'<invoiceSummary><totalGrossValue>124</totalGrossValue></invoiceSummary></invoice>')


def doc(mark, uid="U1", **kw):
    return {"mark": mark, "issue_date": "2024-01-02", "invoice_type": "1.1", "total_net": 100, "raw_xml": xml(mark, uid), **kw}


def marks():
    return sorted(r["mark"] for r in db.get_conn().execute("SELECT mark FROM documents WHERE company_id = ?", (cid,)))


def archived():
    return sorted(r["mark"] for r in db.get_conn().execute(
        "SELECT mark FROM mydata_archive WHERE company_id = ? AND kind = 'invoice'", (cid,)))


db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "999758350"})
db.set_active_company_id(cid)

# 1. Παλιό MARK, μετά νεότερο με ίδιο UID: μένει το νεότερο, το παλιό στον κουβά ως διπλό.
db.upsert_document(cid, "expense", doc("10"), "unclassified")
db.upsert_document(cid, "expense", doc("20"), "unclassified")
assert marks() == ["20"] and archived() == ["10"], (marks(), archived())

# 2. Αντίστροφη σειρά: το παλαιότερο δεν μπαίνει (και ξανά το ίδιο: ποτέ IntegrityError).
db.upsert_document(cid, "expense", doc("5"), "unclassified")
db.upsert_document(cid, "expense", doc("20"), "unclassified")
assert marks() == ["20"], marks()

# 3. Τοπικός χαρακτηρισμός (classified) του παλιού περνά στο νέο MARK.
cls = [{"type": "E3_102_001", "category": "category2_1", "amount": 100}]
db.upsert_document(cid, "expense", doc("30", "U2", cls_info=cls), "classified")
db.upsert_document(cid, "expense", doc("31", "U2"), "unclassified")
r = db.get_document(cid, "31")
assert r["status"] == "classified" and "E3_102_001" in r["cls_json"] and not db.get_document(cid, "30"), dict(r)

# 4. «myDATA λοιπά»: κατηγορία duplicate με το MARK που μετρά.
import app  # noqa: E402

with app.app.test_request_context(), mock.patch.object(app, "get_active_company", return_value={"id": cid}):
    rows, _ = app._history_rows(cid)
dups = {r["mark"]: r["dup_of"] for r in rows if r["cat"] == "duplicate"}
assert dups == {"10": "20", "30": "31"}, dups  # το 5 δεν μπήκε ποτέ στα βιβλία, άρα ούτε στον κουβά

# Προβολή: έγκυρο (20) αριστερά, διπλό (10) δεξιά, με επισήμανση μόνο όσων διαφέρουν (το MARK).
with app.app.test_request_context("/?kind=invoice&mark=10&dup=1"), \
        mock.patch.object(app, "get_active_company", return_value={"id": cid}):
    html = app.reports_history_item()
assert html.index("Έγκυρο") < html.index(">20<") < html.index("Διπλό · 10"), html
assert html.count("is-diff") == 2, html.count("is-diff")

# 5. Μετάπτωση παλιάς βάσης: δύο γραμμές ίδιου UID χωρίς στήλη uid → init_db → μία, με unique index.
with db.get_conn() as conn:
    conn.execute("DROP INDEX ux_documents_uid")
    for m in ("40", "41"):
        conn.execute("INSERT INTO documents (company_id, kind, mark, status, raw_xml) VALUES (?, 'expense', ?, "
                     "'unclassified', ?)", (cid, m, xml(m, "U3")))
db.init_db()
assert "40" not in marks() and "41" in marks(), marks()
try:
    with db.get_conn() as conn:
        conn.execute("UPDATE documents SET uid = 'U1' WHERE mark = '41'")
    raise AssertionError("χωρίς unique index")
except sqlite3.IntegrityError:
    pass
print("OK")
