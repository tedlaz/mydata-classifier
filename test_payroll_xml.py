"""Μισθοδοσία 17.1: παρακράτηση φόρου + κρατήσεις ΕΦΚΑ στο XML (python test_payroll_xml.py)."""

import xml.etree.ElementTree as ET

from mydata_client import INV_NS, build_self_expense_invoice_xml

N = {"i": INV_NS}
xml = build_self_expense_invoice_xml(
    "17.1", "A", "1", "2026-09-28",
    [{"amount": 1000, "classification_type": "E3_581_001", "classification_category": "category2_6",
     },
     {"amount": 222.9, "classification_type": "E3_581_002", "classification_category": "category2_6"}],
    issuer_vat="046949583", withheld=45.3, deductions=138.7, withheld_base=861.3,
)
root = ET.fromstring(xml)
s = root.find(".//i:invoiceSummary", N)
assert s.findtext("i:totalWithheldAmount", namespaces=N) == "45.30"
assert s.findtext("i:totalDeductionsAmount", namespaces=N) == "138.70"
assert s.findtext("i:totalGrossValue", namespaces=N) == "1038.90"  # 1222.90 − 45.30 − 138.70
assert root.findtext(".//i:paymentMethodDetails/i:amount", namespaces=N) == "1038.90"
# Σε επίπεδο παραστατικού (taxesTotals), όχι στις γραμμές.
assert not any(d.find("i:withheldAmount", N) is not None for d in root.findall(".//i:invoiceDetails", N))
t1, t5 = root.findall(".//i:taxesTotals/i:taxes", N)
assert (t1.findtext("i:taxType", namespaces=N), t1.findtext("i:taxCategory", namespaces=N),
        t1.findtext("i:taxAmount", namespaces=N)) == ("1", "11", "45.30")
assert t1.findtext("i:underlyingValue", namespaces=N) == "861.30"  # φορολογητέο, όχι η καθαρή αξία
assert (t5.findtext("i:taxType", namespaces=N), t5.findtext("i:taxAmount", namespaces=N)) == ("5", "138.70")
assert t5.find("i:underlyingValue", N) is None  # οι κρατήσεις δεν έχουν ποσό υπολογισμού
print("OK")

# Ανάγνωση: taxesTotals → επίπεδο παραστατικού· φόροι γραμμής → μόνο στη γραμμή τους.
from mydata_client import _line_taxes, _parse_taxes  # noqa: E402

inv = root.find("i:invoice", N)
assert [(t["type"], t["base"], t["amount"]) for t in _parse_taxes(inv)] == [("1", 861.3, 45.3), ("5", None, 138.7)]
assert all(_line_taxes(d) == [] for d in inv.findall("i:invoiceDetails", N))
det = ET.fromstring(f'<invoiceDetails xmlns="{INV_NS}"><netValue>1000</netValue><withheldAmount>200</withheldAmount>'
                    '<withheldPercentCategory>3</withheldPercentCategory><stampDutyAmount>12</stampDutyAmount>'
                    '<stampDutyPercentCategory>1</stampDutyPercentCategory><deductionsAmount>5</deductionsAmount></invoiceDetails>')
assert _line_taxes(det) == [{"type": "1", "category": "3", "amount": 200.0},
                            {"type": "4", "category": "1", "amount": 12.0},
                            {"type": "5", "category": "", "amount": 5.0}]
print("OK parse")
