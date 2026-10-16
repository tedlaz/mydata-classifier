"""Έλεγχος: myDATA = documents + κουβάς — κάθε εγγραφή (kind, mark) ακριβώς μία φορά, μετά από κάθε βήμα.
Εκτέλεση: python -m tests.test_archive"""
import os
import tempfile
import xml.etree.ElementTree as ET

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402
from mydata_client import MyDataClient, archive_records  # noqa: E402

INV = "http://www.aade.gr/myDATA/invoice/v1.0"
ECLS = "https://www.aade.gr/myDATA/expensesClassificaton/v1.0"


def inv(mark, extra=""):
    return (f'<invoice><mark>{mark}</mark><issuer><vatNumber>999758350</vatNumber></issuer>'
            f'<counterpart><vatNumber>099634359</vatNumber></counterpart><invoiceHeader><issueDate>2026-04-20'
            f'</issueDate><invoiceType>1.1</invoiceType></invoiceHeader>{extra}'
            f'<invoiceSummary><totalGrossValue>124</totalGrossValue></invoiceSummary></invoice>')


def cls(mark, inv_mark):
    return (f'<expensesInvoiceClassification xmlns="{ECLS}"><invoiceMark>{inv_mark}</invoiceMark>'
            f'<classificationMark>{mark}</classificationMark><invoicesExpensesClassificationDetails>'
            f'<expensesClassificationDetailData><classificationType>E3_102_001</classificationType><amount>100</amount></expensesClassificationDetailData>'
            f'<expensesClassificationDetailData><classificationType>E3_102_001</classificationType><amount>24</amount></expensesClassificationDetailData>'
            f'</invoicesExpensesClassificationDetails></expensesInvoiceClassification>')


def page(invoices="", classifications="", cancels=""):
    return (f'<RequestedDoc xmlns="{INV}"><invoicesDoc>{invoices}</invoicesDoc>'
            f'<expensesClassificationsDoc>{classifications}</expensesClassificationsDoc>'
            f'<cancelledInvoicesDoc>{cancels}</cancelledInvoicesDoc></RequestedDoc>').encode()


db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "999758350"})
db.set_active_company_id(cid)
mydata: set = set()  # ό,τι έχει δώσει ποτέ το myDATA (δεν σβήνει εγγραφές)


def fetch(content):
    rows = archive_records(content, "RequestDocs")
    mydata.update((r["kind"], r["mark"]) for r in rows)
    return rows


def check():
    got = [(r["kind"], r["mark"]) for r in db.get_conn().execute(
        "SELECT kind, mark FROM mydata_all WHERE company_id = ?", (cid,))]
    assert len(got) == len(set(got)), f"διπλές εγγραφές: {sorted(got)}"
    assert set(got) == mydata, f"myDATA ≠ documents + κουβάς: λείπουν {mydata - set(got)}, περισσεύουν {set(got) - mydata}"


def archived():
    return sorted(r["mark"] for r in db.get_conn().execute("SELECT mark FROM mydata_archive WHERE company_id = ?", (cid,)))


def doc(mark, cls_mark):
    xml = next(r["xml"] for r in archive_records(page(inv(mark))))
    return {"mark": mark, "issue_date": "2026-04-20", "invoice_type": "1.1", "classification_mark": cls_mark, "raw_xml": xml}


# 1. Ανάκτηση: το 2 στο βιβλίο με ενεργό χαρακτηρισμό 10 (ο 9 είναι παλιός)· 1 εκτός βιβλίου· 3 ακυρωμένο.
rows = fetch(page(inv(1) + inv(2) + inv(3, "<cancelledByMark>11</cancelledByMark>"), cls(9, 2) + cls(10, 2),
                  "<cancelledInvoice><invoiceMark>3</invoiceMark><cancellationMark>11</cancellationMark>"
                  "<cancellationDate>2026-04-22</cancellationDate></cancelledInvoice>"))
r9 = next(r for r in rows if r["mark"] == "9")
assert r9["amount"] == 124 and r9["invoice_mark"] == "2"
assert next(r for r in rows if r["kind"] == "cancel")["event_date"] == "2026-04-22"
assert next(r for r in rows if r["mark"] == "1")["amount"] == 124
db.upsert_document(cid, "expense", doc("2", "10"), "confirmed")
db.archive_sync(cid, rows)
check()
assert archived() == ["1", "11", "3", "9"]
assert "classificationMark>10<" in db.get_document(cid, "2")["cls_xml"]

# 2. Νέα αποστολή χαρακτηρισμού (MARK 12): ο 10 παύει να είναι ενεργός → κουβάς.
d2 = db.get_document(cid, "2")
db.mark_sent(d2["id"], "12")
db.archive_sync(cid, [])
check()
assert archived() == ["1", "10", "11", "3", "9"] and not db.get_document(cid, "2")["cls_xml"]
db.archive_sync(cid, fetch(page(classifications=cls(12, 2))))  # έρχεται από το myDATA → ενεργός στο documents
check()
assert "classificationMark>12<" in db.get_document(cid, "2")["cls_xml"]

# 3. Διαγραφή από το βιβλίο (π.χ. ακύρωση): παραστατικό + ενεργός χαρακτηρισμός → κουβάς.
db.delete_document(cid, "2")
check()
assert archived() == ["1", "10", "11", "12", "2", "3", "9"]

# 4. Επαναφορά στο βιβλίο: φεύγουν από τον κουβά.
db.upsert_document(cid, "expense", doc("2", "12"), "confirmed")
db.archive_sync(cid, [])
check()
assert archived() == ["1", "10", "11", "3", "9"]

# 5. Οι άλλες δύο διαγραφές.
db.delete_document_by_id(db.get_document(cid, "2")["id"])
check()
db.upsert_document(cid, "expense", doc("2", "12"), "confirmed")
db.archive_sync(cid, [])
check()

# 5β. «Διαγραφή» της Ανάκτησης (διάστημα / όλα): σβήνει το τοπικό αντίγραφο — documents ΚΑΙ κουβά του
# διαστήματος, χωρίς μεταφορά στον κουβά. Το διάστημα απλώς παύει να έχει ανακτηθεί.
db.get_conn().execute("UPDATE mydata_archive SET event_date = '2026-05-02' WHERE mark = '1'").connection.commit()
assert db.delete_documents_range(cid, "expense", "2026-04-01", "2026-04-30") == 1
assert db.get_document(cid, "2") is None
assert archived() == ["1"], archived()  # το 1 είναι εκτός διαστήματος· οι 9/10 (του 2) έφυγαν μαζί του
mydata.clear()
mydata.update({("invoice", "1")})
check()
assert db.delete_documents_range(cid, "expense", "", "9999-12-31") == 0 and archived() == []
mydata.clear()
check()

# 6. Έσοδα: ο χαρακτηρισμός φέρνει το MARK του (ενεργό MARK στο documents).
icls = ET.fromstring(
    f'<RequestedDoc xmlns="{INV}"><incomeClassificationsDoc><incomeInvoiceClassification '
    f'xmlns="https://www.aade.gr/myDATA/incomeClassificaton/v1.0"><invoiceMark>5</invoiceMark>'
    f'<classificationMark>50</classificationMark></incomeInvoiceClassification></incomeClassificationsDoc></RequestedDoc>')
assert MyDataClient._parse_income_classifications(icls)["5"]["cls_mark"] == "50"

# 7. Αναφορά «Ιστορικό κινήσεων»: κατηγορίες και drill down (κατηγορία → εγγραφές → στοιχεία).
import app as A  # noqa: E402

mydata.update((r["kind"], r["mark"]) for r in rows)
db.archive_sync(cid, rows + fetch(page(classifications=cls(12, 2))))  # ξανά ανάκτηση: ο κουβάς γεμίζει πάλι
check()

cats = {r["mark"]: A._history_category(r, "999758350", {"3"}) for r in db.archive_rows(cid)}
assert cats == {"1": "foreign", "2": "foreign", "3": "cancelled", "9": "expenses_cls", "10": "expenses_cls",
                "11": "cancel", "12": "expenses_cls"}, cats
assert A._history_category({"kind": "invoice", "mark": "7", "invoice_type": "9.3"}, "999758350", set()) == "movement"
assert A._history_category({"kind": "invoice", "mark": "8", "invoice_type": "1.1"}, "999758350", set()) == "other"
assert A._history_category({"kind": "expenses_cls", "mark": "9", "has_transaction_mode": 1}, "", set()) == "rejection"
assert A._history_category({"kind": "income_cls", "mark": "9"}, "", set()) == "income_cls"
A.app.testing = True
web = A.app.test_client()
page_html = web.get("/reports/history").get_data(as_text=True)
assert "ΙΣΤΟΡΙΚΟ ΚΙΝΗΣΕΩΝ" in page_html and "/reports/history/docs?cat=foreign" in page_html
docs_html = web.get("/reports/history/docs?cat=expenses_cls").get_data(as_text=True)
assert docs_html.count("/history/cls/2") == 1 and "<td class=\"num\">3</td>" in docs_html, docs_html  # 9/10/12 → 1 γραμμή
item = web.get("/reports/history/item?kind=expenses_cls&mark=12")
assert item.status_code == 200 and "xml-tree" in item.get_data(as_text=True)
assert web.get("/reports/history/docs?cat=nope").status_code == 400
assert web.get("/reports/history/item?kind=invoice&mark=404").status_code == 404


# 9. Ιστορικό χαρακτηρισμών: από τον ισχύοντα προς τον παλαιότερο· κουμπί στην καρτέλα όταν υπάρχουν αλλαγές.
from mydata_client import cls_record  # noqa: E402

h = web.get("/history/cls/2").get_data(as_text=True)
assert h.index('<span class="mono">12</span>') < h.index('<span class="mono">10</span>') < h.index('<span class="mono">9</span>') and "ισχύει" in h, h
rej = cls(13, 2).replace("<invoicesExpensesClassificationDetails>", "<transactionMode>1</transactionMode>"
                         "<invoicesExpensesClassificationDetails>")
assert cls_record(rej)["transaction_mode"] == "1" and len(cls_record(rej)["entries"]) == 2
normal = rej.replace("<transactionMode>1<", "<transactionMode>0<")  # 0 = κανονικός χαρακτηρισμός, όχι απόκλιση
assert cls_record(normal)["transaction_mode"] is None
db.archive_sync(cid, fetch(page(classifications=normal.replace(">13<", ">14<").replace("<invoiceMark>2<", "<invoiceMark>5<"))))
cat14 = next(r for r in db.archive_rows(cid) if r["mark"] == "14")
assert A._history_category(cat14, "999758350", set()) == "expenses_cls"
db.upsert_document(cid, "expense", doc("2", "12"), "confirmed")
db.archive_sync(cid, [])
assert [(r["mark"], r["active"]) for r in db.cls_history(cid, "2")] == [("12", 1), ("10", 0), ("9", 0)]
assert "Ιστορικό χαρακτηρισμών (3)" in web.get("/document/2").get_data(as_text=True)
h = web.get("/history/cls/2").get_data(as_text=True)
assert "ισχύει" in h and h.index('<span class="mono">12</span>') < h.index('<span class="mono">9</span>')
assert "Απόκλιση" not in h and "(εκτίμηση)" not in h and 'class="chip new' not in h[h.index("<tbody>"):], h  # 9/10/12 ίδιοι: καμία διαφορά

# Διαφορές: ο 31 άλλαξε το ποσό της 2ης εγγραφής (24 → 20) σε σχέση με τον 30.
db.archive_sync(cid, fetch(page(classifications=cls(30, 6) + cls(31, 6).replace("<amount>24<", "<amount>20<"))))
h = web.get("/history/cls/6").get_data(as_text=True)
h = h[h.index("<thead>"):]  # χωρίς το υπόμνημα
new_at, gone_at = h.index('class="chip new'), h.index('class="chip  gone')
assert h.index('<span class="mono">31</span>') < new_at and "20.00" in h[new_at:new_at + 400], h  # νέο στον 31
assert gone_at > h.index('<span class="mono">30</span>') and "24.00" in h[gone_at:gone_at + 400], h  # άλλαξε μετά τον 30
assert h.count('class="chip new') == 1 and h.count('class="chip  gone') == 1, h
check()

# ΦΠΑ: ίδιος τύπος/ποσό με διαφορετική (άνευ ουσίας) κατηγορία = καμία αλλαγή.
vat = lambda m, extra: (f'<expensesInvoiceClassification xmlns="{ECLS}"><invoiceMark>7</invoiceMark>'
                        f'<classificationMark>{m}</classificationMark><invoicesExpensesClassificationDetails>'
                        f'<lineNumber>1</lineNumber><expensesClassificationDetailData><classificationType>VAT_361'
                        f'</classificationType>{extra}<amount>10</amount></expensesClassificationDetailData>'
                        f'</invoicesExpensesClassificationDetails></expensesInvoiceClassification>')
db.archive_sync(cid, fetch(page(classifications=vat(40, "") + vat(41, "<classificationCategory>category2_1</classificationCategory>"))))
h = web.get("/history/cls/7").get_data(as_text=True)
h = h[h.index("<thead>"):]
assert 'class="chip new' not in h and 'gone' not in h, h

# Νεότερη υποβολή στο myDATA από αυτή του βιβλίου (ήρθε χωρίς ανάκτηση της περιόδου): ισχύει η νεότερη.
db.archive_sync(cid, fetch(page(classifications=cls(15, 2))))
h = web.get("/history/cls/2").get_data(as_text=True)
assert "Το βιβλίο δείχνει ακόμα τον χαρακτηρισμό" in h
t = h[h.index("<thead>"):]
assert t.index('<span class="mono">15</span>') < t.index('<span class="pill">ισχύει</span>') < t.index('<span class="mono">12</span>') < t.index("στο βιβλίο"), t
check()
print("OK")
