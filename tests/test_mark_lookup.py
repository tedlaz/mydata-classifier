"""Έλεγχος: Αναζήτηση MARK (/mark) — κατάσταση και διασυνδέσεις για παραστατικό, χαρακτηρισμό, ακύρωση,
ίδιο UID, συσχετιζόμενα· λήψη από το myDATA για ό,τι λείπει. Εκτέλεση: python -m tests.test_mark_lookup"""
import os
import re
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402
from mydata_client import MyDataError, archive_records  # noqa: E402

INV = "http://www.aade.gr/myDATA/invoice/v1.0"
ECLS = "https://www.aade.gr/myDATA/expensesClassificaton/v1.0"


def inv(mark, uid=None, extra=""):
    return (f'<invoice><uid>{uid or "U" + str(mark)}</uid><mark>{mark}</mark><issuer><vatNumber>094025817</vatNumber></issuer>'
            f'<counterpart><vatNumber>999758350</vatNumber></counterpart><invoiceHeader><series>A</series><aa>{mark}</aa>'
            f'<issueDate>2026-04-20</issueDate><invoiceType>1.1</invoiceType>{extra}</invoiceHeader>'
            f'<invoiceSummary><totalNetValue>100</totalNetValue><totalVatAmount>24</totalVatAmount>'
            f'<totalGrossValue>124</totalGrossValue></invoiceSummary></invoice>')


def cls(mark, inv_mark, amount=100):
    return (f'<expensesInvoiceClassification xmlns="{ECLS}"><invoiceMark>{inv_mark}</invoiceMark>'
            f'<classificationMark>{mark}</classificationMark><invoicesExpensesClassificationDetails><lineNumber>1</lineNumber>'
            f'<expensesClassificationDetailData><classificationType>E3_102_001</classificationType>'
            f'<classificationCategory>category2_1</classificationCategory><amount>{amount}</amount>'
            f'</expensesClassificationDetailData></invoicesExpensesClassificationDetails></expensesInvoiceClassification>')


def cancel(mark, inv_mark):
    return (f"<cancelledInvoice><invoiceMark>{inv_mark}</invoiceMark><cancellationMark>{mark}</cancellationMark>"
            f"<cancellationDate>2026-04-25</cancellationDate></cancelledInvoice>")


def page(invoices="", classifications="", cancels=""):
    return (f'<RequestedDoc xmlns="{INV}"><invoicesDoc>{invoices}</invoicesDoc>'
            f'<expensesClassificationsDoc>{classifications}</expensesClassificationsDoc>'
            f'<cancelledInvoicesDoc>{cancels}</cancelledInvoicesDoc></RequestedDoc>').encode()


db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "999758350"})
db.set_active_company_id(cid)

# Βιβλία: 100 (ενεργό, χαρακτηρισμός 101 ισχύει, 99 προηγούμενος). Κουβάς: 200 ακυρωμένο από 201,
# 300 → 310 ίδιο UID (ισχύει το 310), 400 πιστωτικό που αναφέρει το 100 (correlatedInvoices).
xml100 = next(r["xml"] for r in archive_records(page(inv(100))))
db.upsert_document(cid, "expense", {"mark": "100", "issue_date": "2026-04-20", "invoice_type": "1.1", "total_net": 100,
                                    "classification_mark": "101", "raw_xml": xml100,
                                    "cls_info": [{"line": 1, "type": "E3_102_001", "category": "category2_1", "amount": 100}]},
                   "confirmed")
db.archive_sync(cid, archive_records(page(
    inv(200) + inv(300, "UDUP") + inv(310, "UDUP") + inv(400, extra="<correlatedInvoices>100</correlatedInvoices>"),
    cls(99, 100, 90) + cls(101, 100), cancel(201, 200)), "RequestDocs"))

import app as A  # noqa: E402

A.app.testing = True
web = A.app.test_client()


def look(m, **kw):
    r = web.get("/mark", query_string={"m": m, **kw})
    assert r.status_code in (200, 302), r.status_code
    return r.get_data(as_text=True)


def nodes(h):  # (MARK, ετικέτα κατάστασης) των κόμβων του χρονολογίου
    return re.findall(r'mk-rel-mark">(\d+)</span>\s*<span><span class="mk-pill" data-tone="\w+" title="[^"]*">([^<]+)<', h)


# Παραστατικό στα βιβλία: ενεργό, καρτέλα, χαρακτηρισμοί + ό,τι το αναφέρει.
h = look("100")
assert "Ενεργό στο βιβλίο εξόδων" in h and "Καρτέλα στα βιβλία" in h, h
n = dict(nodes(h))
assert n["101"] == "Ισχύει — στα βιβλία" and n["99"] == "Αντικαταστάθηκε" and n["400"] == "Εκτός βιβλίων", n

# Προηγούμενος χαρακτηρισμός: αντικαταστάθηκε, δείχνει το παραστατικό του.
h = look("99")
assert "Αντικαταστάθηκε" in h and "Αφορά το παραστατικό" in h and "Ενεργό στο βιβλίο εξόδων" in h and "90.00 €" in h, h

# Ακυρωμένο ↔ ακύρωση.
h = look("200")
assert ">Ακυρωμένο<" in h and dict(nodes(h))["201"] == "Πράξη ακύρωσης", h
h = look("201")
assert "Πράξη ακύρωσης" in h and "ακυρώθηκε το παραστατικό" in h and ">Ακυρωμένο<" in h, h

# Ίδιο UID: το παλιό αντικαταστάθηκε, ο κόμβος του έγκυρου στις διασυνδέσεις.
h = look("300")
assert "Ίδιο UID με το MARK 310" in h and "310" in dict(nodes(h)), h

# Συσχετιζόμενα: 400 → 100 (από το XML του).
assert "100" in dict(nodes(look("400")))

# Χωρίς εγγραφή: πρόταση λήψης· λήψη από το myDATA (fake) → κουβάς → σελίδα.
h = look("500")
assert "δεν υπάρχει στα δεδομένα" in h and "live=1" in h, h


class Fake:
    def request_mark(self, mark):
        if mark == "666":
            raise MyDataError("χωρίς κλειδιά")
        return archive_records(page(inv(500)), "RequestDocs") if mark == "500" else []


real, A.get_client = A.get_client, Fake
assert web.get("/mark?m=500&live=1").status_code == 302
assert "Εκτός βιβλίων" in look("500")
assert "Ούτε το myDATA" in look("501", live=1)
assert "Η λήψη από το myDATA απέτυχε" in look("666", live=1)
A.get_client = real
assert "είναι αριθμός" in look("abc")

# Βελάκι επιστροφής μόνο όταν την κάλεσε άλλη οθόνη· μένει ίδιο σε κόμβους/αναζήτηση· μόνο εσωτερικά URL.
assert "← Επιστροφή" not in look("100")
h = look("100", back="/document/100")
assert 'class="yr-hero-back" href="/document/100"' in h and 'name="back" value="/document/100"' in h, h
assert "/mark?back=/document/100&amp;m=101" in h, h  # κόμβος χαρακτηρισμού → ίδιο back
assert "← Επιστροφή" not in look("100", back="//evil.example")
assert web.get("/mark/100/xml").get_data(as_text=True).startswith("<invoice")

# Επωνυμία που λείπει από το XML: hint στο ΑΦΜ — εκδότης από τους συναλλασσόμενους, λήπτης = η ίδια η εταιρεία·
# με επωνυμία μέσα στο XML, κανένα hint.
db.upsert_supplier("094025817", "ΠΡΟΜΗΘΕΥΤΗΣ ΑΕ")
h = look("100")
assert 'title="ΠΡΟΜΗΘΕΥΤΗΣ ΑΕ (από τους συναλλασσόμενους' in h and 'title="Test (από τους συναλλασσόμενους' in h, h
db.archive_sync(cid, archive_records(page(inv(800).replace("</vatNumber></issuer>", "</vatNumber><name>ΑΠΟ XML</name></issuer>")),
                                     "RequestDocs"))
h = look("800")
assert "ΠΡΟΜΗΘΕΥΤΗΣ ΑΕ (από" not in h and 'title="Test (από' in h, h
print("OK")
