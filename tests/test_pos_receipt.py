"""Έλεγχος: απόδειξη POS (8.4/8.5) — εκτός εξόδων/εσόδων, στο «myData λοιπά» → «Αποδείξεις POS».
Εκτέλεση: python -m tests.test_pos_receipt"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
from app import _history_category  # noqa: E402
from mydata_client import MyDataClient  # noqa: E402

INV = "http://www.aade.gr/myDATA/invoice/v1.0"
page = (f'<RequestedDoc xmlns="{INV}"><invoicesDoc><invoice><mark>400012287139652</mark>'
        '<issuer><vatNumber>998255160</vatNumber></issuer><counterpart><vatNumber>059991307</vatNumber></counterpart>'
        '<invoiceHeader><issueDate>2026-01-16</issueDate><invoiceType>8.4</invoiceType></invoiceHeader>'
        '<invoiceSummary><totalGrossValue>20</totalGrossValue></invoiceSummary></invoice></invoicesDoc></RequestedDoc>')

c = MyDataClient("u", "k", "dev")
c._pages = lambda ep, params: [__import__("xml.etree.ElementTree").etree.ElementTree.fromstring(page)]
c._recipient_transmitted = lambda inv: False
assert c.request_unclassified_expenses("01/01/2026", "31/01/2026") == []
assert c.request_classified_expenses("01/01/2026", "31/01/2026") == []

row = {"kind": "invoice", "mark": "400012287139652", "invoice_type": "8.4"}
assert _history_category(row, "059991307", set()) == "pos"
assert _history_category({**row, "invoice_type": "8.5"}, "059991307", set()) == "pos"
print("OK")
