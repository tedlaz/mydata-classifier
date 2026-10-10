"""«Νέα από MARK» για έσοδα + έξοδα = 2 κλήσεις στο myDATA (RequestDocs + RequestTransmittedDocs):
οι σελίδες του new_since ξαναχρησιμοποιούνται από την ανάκτηση (cache του client, ένας client ανά request).
Εκτέλεση: python -m tests.test_mark_calls"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
from mydata_client import MyDataClient  # noqa: E402

c = MyDataClient("u", "k", "dev", own_vat="999758350")
calls = []
c.session.get = lambda url, params, **k: calls.append((url.rsplit("/", 1)[1], dict(params))) or NS(
    status_code=200, content=b"<RequestedDoc/>")
doc = dict(issue_date="2026-10-09", is_movement_doc=False, is_cancelled=False, has_embedded_classification=True,
           has_income_line_classification=False, lines=[])
c._parse_requested_doc = lambda root: [NS(mark="7", issuer_vat="800000000", invoice_type="1.1", **doc),
                                       NS(mark="8", issuer_vat="999758350", invoice_type="2.1", **doc)]
c._parse_expenses_classifications = c._parse_income_classifications = lambda root: {}
c._parse_cancelled_invoices = lambda root: set()

marks = {"RequestDocs": 100, "RequestTransmittedDocs": 100}
for ep in marks:
    c.new_since(ep, 100)
assert len(calls) == 2 and all(p == {"mark": "100"} for _, p in calls), calls
c.request_unclassified_expenses(None, None, marks)
c.request_classified_expenses(None, None, marks)
unc, _, _ = c.request_income(None, None, 100)
assert len(calls) == 2, calls  # όλα από την cache
assert [i.mark for i in unc] == ["8"], unc

# Νέα αχαρακτήριστα έξοδα: έλεγχος χαρακτηρισμών της πύλης για το διάστημα έκδοσής τους.
c2 = MyDataClient("u", "k", "dev", own_vat="999758350")
c2.session.get, c2._parse_requested_doc = c.session.get, c._parse_requested_doc
c2._parse_expenses_classifications = c2._parse_income_classifications = lambda root: {}
c2._parse_cancelled_invoices = lambda root: set()
doc["has_embedded_classification"] = False
portal = []
c2.request_portal_classifications = lambda df, dt: portal.append((df, dt)) or {}
c2.request_unclassified_expenses(None, None, marks)
assert portal == [("09/10/2026", "09/10/2026")], portal

# Αρχική ανάκτηση με αχαρακτήριστα: RequestE3Info/RequestVatInfo μία φορά (όχι μία ανά λίστα).
c3 = MyDataClient("u", "k", "dev", own_vat="999758350")
c3.session.get, c3._parse_requested_doc = c.session.get, c._parse_requested_doc
c3._parse_expenses_classifications = lambda root: {}
c3._parse_cancelled_invoices = lambda root: set()
info = []
c3._request_info_uncached = lambda ep, df, dt: info.append(ep) or []
c3.request_unclassified_expenses("01/10/2026", "10/10/2026")
c3.request_classified_expenses("01/10/2026", "10/10/2026")
assert info == ["RequestE3Info", "RequestVatInfo"], info

# Ένας client ανά request.
A._make_client = lambda: MyDataClient("u", "k", "dev")
with A.app.test_request_context():
    assert A.get_client() is A.get_client()
with A.app.test_request_context():
    first = A.get_client()
with A.app.test_request_context():
    assert A.get_client() is not first
print("OK")
