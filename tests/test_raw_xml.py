"""Όλα τα στοιχεία του παραστατικού: XML από την ανάκτηση → βάση → δέντρο/λήψη (python -m tests.test_raw_xml)."""
import os
import tempfile
import xml.etree.ElementTree as ET

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login
import db  # noqa: E402
from mydata_client import MyDataClient  # noqa: E402

I = "http://www.aade.gr/myDATA/invoice/v1.0"
xml = (f'<RequestedDoc xmlns="{I}"><invoicesDoc><invoice><mark>4001</mark>'
       f'<issuer><vatNumber>123456789</vatNumber><country>GR</country><branch>0</branch></issuer>'
       f'<invoiceHeader><series>A</series><aa>7</aa><issueDate>2026-10-01</issueDate><invoiceType>1.1</invoiceType></invoiceHeader>'
       f'<paymentMethods><paymentMethodDetails><type>3</type><amount>124.00</amount></paymentMethodDetails></paymentMethods>'
       f'<invoiceDetails><lineNumber>1</lineNumber><quantity>2</quantity><netValue>100.00</netValue>'
       f'<vatCategory>1</vatCategory><vatAmount>24.00</vatAmount><lineComments>Δοκιμή</lineComments></invoiceDetails>'
       f'<invoiceSummary><totalNetValue>100.00</totalNetValue><totalVatAmount>24.00</totalVatAmount>'
       f'<totalGrossValue>124.00</totalGrossValue></invoiceSummary></invoice></invoicesDoc></RequestedDoc>')

# (α) Η ανάκτηση κρατά ολόκληρο το <invoice>.
inv = MyDataClient("u", "k")._parse_requested_doc(ET.fromstring(xml))[0]
assert inv.raw_xml.startswith(f'<invoice xmlns="{I}">') and "<lineComments>Δοκιμή</lineComments>" in inv.raw_xml, inv.raw_xml

# (β) Αποθήκευση· δεύτερο upsert χωρίς XML δεν το σβήνει.
db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)
doc = A._invoice_to_doc(inv)
db.upsert_document(cid, "expense", doc, "unclassified")
db.upsert_document(cid, "expense", dict(doc, raw_xml=""), "unclassified")
assert db.get_document(cid, "4001")["raw_xml"] == inv.raw_xml

# (γ) Δέντρο με ελληνικές ετικέτες· κωδικοί με περιγραφή· άγνωστο πεδίο ως έχει.
tree = A._xml_tree(inv.raw_xml.replace("</invoice>", "<fooBar>x</fooBar></invoice>"))
flat = {}


def walk(nodes):
    for n in nodes:
        flat.setdefault(n["label"], n["value"])
        walk(n["children"])


walk(tree)
assert flat["ΑΦΜ"] == "123456789" and flat["Σχόλια γραμμής"] == "Δοκιμή" and flat["fooBar"] == "x", flat
assert flat["Κατηγορία ΦΠΑ"].startswith("1 · 24") and flat["Τύπος"].startswith("3 · Μετρητά"), flat
cls = ('<invoice><expensesClassification><classificationType>E3_585_016</classificationType>'
       '<classificationCategory>category2_4</classificationCategory></expensesClassification>'
       '<taxesTotals><taxes><taxType>1</taxType><taxCategory>11</taxCategory></taxes></taxesTotals>'
       '<invoiceDetails><vatExemptionCategory>16</vatExemptionCategory></invoiceDetails>'
       '<invoiceHeader><currency>EUR</currency><movePurpose>1</movePurpose></invoiceHeader>'
       '<issuer><country>CY</country></issuer></invoice>')
flat = {}
walk(A._xml_tree(cls))
assert all(" · " in flat[k] for k in ("Τύπος χαρακτηρισμού", "Κατηγορία χαρακτηρισμού", "Τύπος φόρου",
                                       "Κατηγορία φόρου", "Κατηγορία εξαίρεσης ΦΠΑ", "Νόμισμα",
                                       "Σκοπός διακίνησης")), flat
assert flat["Χώρα"] == "CY · Κύπρος", flat

# (δ) Σελίδα παραστατικού και λήψη XML.
c = A.app.test_client()
page = c.get("/document/4001").get_data(as_text=True)
assert "Όλα τα στοιχεία" in page and "Σχόλια γραμμής" in page
r = c.get("/document/4001/xml")
assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"] and r.get_data(as_text=True) == inv.raw_xml

# (ε) URL στο XML → σύνδεσμος· άλλο σχήμα (javascript:) όχι.
xml2 = inv.raw_xml.replace("</invoice>", "<downloadingInvoiceUrl>https://mydata.aade.gr/x?a=1&amp;b=2</downloadingInvoiceUrl>"
                           "<qrCodeUrl>javascript:alert(1)</qrCodeUrl></invoice>")
db.upsert_document(cid, "expense", dict(doc, raw_xml=xml2), "unclassified")
page = c.get("/document/4001").get_data(as_text=True)
assert 'href="https://mydata.aade.gr/x?a=1&amp;b=2" target="_blank"' in page, page[-3000:]
assert 'href="javascript' not in page
print("ok")
