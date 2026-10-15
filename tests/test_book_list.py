"""Φίλτρα/ταξινόμηση λίστας βιβλίου (κοινά με προηγ./επόμενο της αναλυτικής προβολής).
Εκτέλεση: python -m tests.test_book_list"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ.setdefault("MYDATA_DATA_DIR", tempfile.mkdtemp())
from app import _book_list  # noqa: E402

docs = [NS(mark=str(n), issue_date=d, issuer_name="", issuer_vat="", invoice_type="1.1", total_gross=0.0)
        for n, d in enumerate(["2026-01-05", "2026-03-02", "2026-03-20", "2026-04-01", "2026-03-11"])]

# Περίοδος + desc → μόνο τα 3 του Μαρτίου, νεότερο πρώτο.
items, *_ = _book_list(docs, {"f_date": "2026-03", "dir": "desc"}, "unclassified")
assert [i.mark for i in items] == ["2", "4", "1"], items

# Χωρίς dir: προεπιλογή desc για classified, asc αλλιώς.
assert [i.mark for i in _book_list(docs, {}, "classified")[0]][0] == "3"
assert [i.mark for i in _book_list(docs, {"f_date": "2026-03:2026-04"}, "unclassified")[0]] == ["1", "4", "2", "3"]
print("ok")

# Route: τα βελάκια του παραστατικού ακολουθούν τα φίλτρα/ταξινόμηση του back.
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)
for m, d in (("901", "2026-01-05"), ("902", "2026-03-02"), ("903", "2026-03-20"), ("904", "2026-04-01")):
    db.upsert_document(cid, "expense", {"mark": m, "issue_date": d, "issuer_vat": "123456789", "invoice_type": "1.1",
                                        "total_net": 1.0, "total_vat": 0.0, "total_gross": 1.0, "lines": []}, "unclassified")
c = A.app.test_client()
page = c.get("/document/903", query_string={"back": "/invoices?view=unclassified&f_date=2026-03&dir=desc"}).get_data(as_text=True)
assert 'data-doc-nav="prev"' in page and "/document/902" in page and "/document/904" not in page, page
assert '1<span class="of"> από </span><span class="sl">/</span>2' in page
print("ok route")
