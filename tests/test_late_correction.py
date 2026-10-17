"""Ανάκτηση ΠΑΛΙΑΣ περιόδου εσόδων: χαρακτηρισμός με μεταγενέστερο MARK (υποβλήθηκε μετά το «Έως») πρέπει
να υπολογίζεται → η κλήση πάει έως σήμερα και τα παραστατικά φιλτράρονται στην περίοδο
(python -m tests.test_late_correction)."""
from types import SimpleNamespace as NS

import mydata_client as M
from mydata_client import MyDataClient

c = MyDataClient("u", "k", "dev", own_vat="1")
calls = []
c.session.get = lambda url, params, **k: calls.append(dict(params)) or NS(status_code=200, content=b"<x/>")
doc = lambda mark, d: NS(mark=mark, issuer_vat="1", invoice_type="2.1", is_off_books=False,  # noqa: E731
                         is_cancelled=False, issue_date=d, has_income_line_classification=False)
c._parse_requested_doc = lambda root: [doc("10", "2026-01-15"), doc("20", "2026-05-01")]
c._parse_income_classifications = lambda root: {"10": {"entries": [{"type": "E3_561_001"}], "cls_mark": "11"}}  # χαρακτηρισμός του Μαΐου
c._parse_cancelled_invoices = lambda root: set()

unc, cls, _ = c.request_income("01/01/2026", "31/01/2026")
assert calls[0]["dateFrom"] == "01/01/2026" and calls[0]["dateTo"] == M._today_gr(), calls
assert [i.mark for i in cls] == ["10"] and unc == [], (cls, unc)  # το 20 είναι εκτός περιόδου

# Με mark: ΚΑΜΙΑ αλλαγή (χωρίς ημερομηνίες, χωρίς φίλτρο).
calls.clear()
unc, cls, _ = c.request_income(None, None, 5)
assert calls == [{"mark": "5"}] and {i.mark for i in unc + cls} == {"10", "20"}, (calls, unc, cls)
print("ok")
