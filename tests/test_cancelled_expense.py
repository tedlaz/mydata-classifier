"""Έλεγχος: έξοδο με cancelledByMark στο ίδιο το παραστατικό εξαιρείται και δηλώνεται για αφαίρεση.
Εκτέλεση: python -m tests.test_cancelled_expense"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
from mydata_client import MyDataClient  # noqa: E402

c = MyDataClient("u", "k", "dev", own_vat="999758350")
doc = dict(issuer_vat="094025817", invoice_type="1.1", issue_date="2025-01-23", is_movement_doc=False,
           has_embedded_classification=False)
c.request_docs = lambda df, dt: ([NS(mark="259", is_cancelled=True, **doc), NS(mark="373", is_cancelled=False, **doc)],
                                 {}, set())
c.request_transmitted = lambda df, dt: ([], {}, set())
c.request_portal_classifications = lambda df, dt: {}

assert [i.mark for i in c.request_unclassified_expenses("23/01/2025", "23/01/2025")] == ["373"]
assert c.last_cancelled == {"259"}, c.last_cancelled
print("OK")
