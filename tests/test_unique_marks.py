"""Έλεγχος: το ίδιο MARK δύο φορές από την ΑΑΔΕ (π.χ. επικάλυψη σελίδων) → μία φορά σε κάθε λίστα.
Εκτέλεση: python -m tests.test_unique_marks"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
from mydata_client import MyDataClient  # noqa: E402

doc = dict(invoice_type="1.1", issue_date="2025-01-23", is_off_books=False, is_cancelled=False,
           has_embedded_classification=True, has_income_line_classification=True, income_classifications=[],
           counterpart_vat="", lines=[], issuer_vat="")
c = MyDataClient("u", "k", "dev")
c._pages = lambda ep, params: ["p1", "p2"]  # δύο σελίδες με το ίδιο παραστατικό
c._parse_requested_doc = lambda root: [NS(mark="7", **doc), NS(mark="8", **doc)]
c._parse_expenses_classifications = c._parse_income_classifications = lambda root: {}
c._parse_cancelled_invoices = lambda root: set()
c._recipient_transmitted = lambda inv: False

assert [i.mark for i in c.request_docs("01/01/2025", "31/01/2025")[0]] == ["7", "8"]
assert [i.mark for i in c.request_income("01/01/2025", "31/01/2025")[1]] == ["7", "8"]
doc["invoice_type"] = "17.5"  # αυτοτιμολογούμενο έξοδο
assert [i.mark for i in c.request_transmitted("01/01/2025", "31/01/2025")[0]] == ["7", "8"]
print("OK")
