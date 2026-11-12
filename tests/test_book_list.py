"""Φίλτρα/ταξινόμηση/σελίδα λίστας βιβλίου στη βάση (κοινά με προηγ./επόμενο της αναλυτικής προβολής).
Εκτέλεση: python -m tests.test_book_list"""
import os
import tempfile

os.environ.setdefault("MYDATA_DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)


def add(mark, date, gross=0.0, status="unclassified", vat="", itype="1.1", series="", aa=""):
    db.upsert_document(cid, "expense", {"mark": mark, "issue_date": date, "issuer_vat": vat, "invoice_type": itype,
                                        "series": series, "aa": aa, "total_net": gross, "total_vat": 0.0,
                                        "total_gross": gross, "lines": []}, status)


def marks(args, view="unclassified"):
    with A.app.test_request_context():
        return [i.mark for i in A._book_page(cid, "expense", view, args)["invoices"]]


for n, (d, g) in enumerate(zip(["2026-01-05", "2026-03-02", "2026-03-20", "2026-04-01", "2026-03-11"],
                               (0.0, 50.0, 100.0, 500.0, 1234.5))):
    add(str(n), d, g)
    add(f"c{n}", d, g, status="classified")

# Περίοδος + desc → μόνο τα 3 του Μαρτίου, νεότερο πρώτο.
assert marks({"f_date": "2026-03", "dir": "desc"}) == ["2", "4", "1"]
# Χωρίς dir: προεπιλογή desc για classified, asc αλλιώς.
assert marks({}, "classified")[0] == "c3"
assert marks({"f_date": "2026-03:2026-04"}) == ["1", "4", "2", "3"]

# Ποσά: substring ή τελεστές.
amt = lambda f: marks({"f_total": f})  # noqa: E731
assert amt("=0") == ["0"]
assert amt(">100") == ["4", "3"]
assert amt(">=100 <1000") == ["2", "3"]
assert amt("!=0") == ["1", "4", "2", "3"]
assert amt("> 1234,5") == []
assert amt("1234") == ["4"]
assert amt(">abc") == []

# Επωνυμία: πεζά/κεφαλαία ελληνικά (η SQLite μόνη της κάνει lower() μόνο σε λατινικά) + ταξινόμηση.
db.upsert_supplier("111111111", "ΑΛΦΑ ΕΠΕ")
db.upsert_supplier("222222222", "βήτα ΑΕ")
add("n1", "2026-05-01", vat="222222222")
add("n2", "2026-05-02", vat="111111111")
assert marks({"f_issuer": "άλφα"}) == [] and marks({"f_issuer": "αλφα"}) == ["n2"]
assert marks({"f_issuer": "ΒΉΤΑ"}) == ["n1"] and marks({"f_issuer": "2222"}) == ["n1"]
assert marks({"f_date": "2026-05", "sort": "name"}) == ["n2", "n1"]
# Τύπος: αριθμητική σειρά (1.2 < 1.10)· σειρά/ΑΑ: ΑΑ αριθμητικά.
add("t1", "2026-06-01", itype="1.10", series="Α", aa="10")
add("t2", "2026-06-02", itype="1.2", series="Α", aa="9")
assert marks({"f_date": "2026-06", "sort": "type"}) == ["t2", "t1"]
assert marks({"f_date": "2026-06", "sort": "series"}) == ["t2", "t1"]
assert marks({"f_type": "1.10"}) == ["t1"]

# Σελίδα: per_page() ανά σελίδα (προεπιλογή 50), μετά την ταξινόμηση· σελίδα εκτός ορίων → η τελευταία.
assert A.per_page() == A.PER_PAGE == 50
for n in range(A.PER_PAGE + 5):
    add(f"p{n:03d}", "2027-01-01")
with A.app.test_request_context():
    bp = A._book_page(cid, "expense", "unclassified", {"f_date": "2027", "page": "9"})
assert (bp["page"], bp["total_pages"], bp["total"]) == (2, 2, A.PER_PAGE + 5)
assert [i.mark for i in bp["invoices"]] == [f"p{n:03d}" for n in range(A.PER_PAGE, A.PER_PAGE + 5)]

# Παράμετρος «Εγγραφές ανά σελίδα»: αποθήκευση με έλεγχο ορίων· η βάση φέρνει μόνο τόσες.
c = A.app.test_client()
c.post("/parameters/automation", data={"per_page": "5"})  # εκτός ορίων → δεν αλλάζει
assert A.per_page() == 50
c.post("/parameters/automation", data={"per_page": "20"})
assert A.per_page() == 20
with A.app.test_request_context():
    bp = A._book_page(cid, "expense", "unclassified", {"f_date": "2027", "page": "3"})
assert (bp["page"], bp["total_pages"], len(bp["invoices"])) == (3, 3, 15)
db.set_setting("per_page", "")
print("ok")

# Route: τα βελάκια του παραστατικού ακολουθούν τα φίλτρα/ταξινόμηση του back.
for m, d in (("901", "2028-01-05"), ("902", "2028-03-02"), ("903", "2028-03-20"), ("904", "2028-04-01")):
    add(m, d, 1.0, vat="123456789")
c = A.app.test_client()
page = c.get("/document/903", query_string={"back": "/invoices?view=unclassified&f_date=2028-03&dir=desc"}).get_data(as_text=True)
assert 'data-doc-nav="prev"' in page and "/document/902" in page and "/document/904" not in page, page
assert '1<span class="of"> από </span><span class="sl">/</span>2' in page
print("ok route")
