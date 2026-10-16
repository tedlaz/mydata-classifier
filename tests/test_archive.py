"""Έλεγχος: κουβάς myDATA — ό,τι δεν είναι στο documents, ποτέ διπλό.
Εκτέλεση: python -m tests.test_archive"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402
from mydata_client import archive_records  # noqa: E402

PAGE = b"""<RequestedDoc xmlns="http://www.aade.gr/myDATA/invoice/v1.0">
<invoicesDoc>
  <invoice><mark>1</mark><issuer><vatNumber>999758350</vatNumber></issuer>
    <counterpart><vatNumber>099634359</vatNumber></counterpart>
    <invoiceHeader><issueDate>2026-04-20</issueDate><invoiceType>13.1</invoiceType></invoiceHeader></invoice>
  <invoice><mark>2</mark><invoiceHeader><issueDate>2026-04-21</issueDate><invoiceType>1.1</invoiceType></invoiceHeader></invoice>
</invoicesDoc>
<expensesClassificationsDoc>
  <expensesInvoiceClassification xmlns="https://www.aade.gr/myDATA/expensesClassificaton/v1.0">
    <invoiceMark>2</invoiceMark><classificationMark>10</classificationMark></expensesInvoiceClassification>
</expensesClassificationsDoc>
<cancelledInvoicesDoc>
  <cancelledInvoice><invoiceMark>3</invoiceMark><cancellationMark>11</cancellationMark></cancelledInvoice>
</cancelledInvoicesDoc>
</RequestedDoc>"""

rows = archive_records(PAGE, "RequestDocs")
assert [(r["kind"], r["mark"], r["invoice_mark"]) for r in rows] == [
    ("invoice", "1", None), ("invoice", "2", None), ("expenses_cls", "10", "2"), ("cancel", "11", "3")], rows
assert rows[0]["counterpart_vat"] == "099634359" and rows[0]["invoice_type"] == "13.1"

db.init_db()
cid = db.add_company({"company_name": "Test"})
archived = lambda: sorted(r["mark"] for r in db.get_conn().execute(  # noqa: E731
    "SELECT mark FROM mydata_archive WHERE company_id = ?", (cid,)))

db.upsert_document(cid, "expense", {"mark": "2", "issue_date": "2026-04-21", "invoice_type": "1.1"}, "unclassified")
db.archive_sync(cid, rows)
assert archived() == ["1", "11"]  # το 2 και ο χαρακτηρισμός του (10) είναι στο documents
first = db.get_conn().execute("SELECT first_seen FROM mydata_archive WHERE mark = '1'").fetchone()[0]

db.delete_document(cid, "2")  # π.χ. ακυρώθηκε → περνά στον κουβά
db.archive_sync(cid, rows)
assert archived() == ["1", "10", "11", "2"]

db.upsert_document(cid, "expense", {"mark": "2", "issue_date": "2026-04-21", "invoice_type": "1.1"}, "unclassified")
db.archive_sync(cid, rows)
assert archived() == ["1", "11"]
assert db.get_conn().execute("SELECT first_seen FROM mydata_archive WHERE mark = '1'").fetchone()[0] == first
assert db.get_conn().execute("SELECT count(*) FROM mydata_all WHERE company_id = ?", (cid,)).fetchone()[0] == 3
print("OK")
