"""Ενδοκοινοτική 14.1: επωνυμία/διεύθυνση εκδότη εκτός GR, ΦΠΑ ≠ 8 (python -m tests.test_intra_eu)."""
from mydata_client import SELF_TYPES_WITH_VAT, build_self_expense_invoice_xml

assert {"14.1", "14.3"} <= SELF_TYPES_WITH_VAT
line = {"amount": 100.0, "classification_category": "category2_1", "classification_type": "E3_102_001",
        "vat_category": "1", "vat_amount": 24.0, "vat_type": "VAT_364"}
x = build_self_expense_invoice_xml("14.1", "A", "1", "2026-10-01", [line], issuer_vat="123456789",
                                   issuer_country="DE", issuer_name="Muster GmbH",
                                   issuer_address={"street": "Hauptstr. 1", "postal_code": "10115", "city": "Berlin"},
                                   counterpart_vat="046949583", include_payment=False)
assert "<country>DE</country><branch>0</branch><name>Muster GmbH</name><address><street>Hauptstr. 1</street>" \
       "<postalCode>10115</postalCode><city>Berlin</city></address>" in x, x
assert "<vatCategory>1</vatCategory><vatAmount>24.00</vatAmount>" in x
assert "VAT_364</" in x

# Εκδότης στην Ελλάδα: επωνυμία/διεύθυνση απαγορεύονται.
x = build_self_expense_invoice_xml("14.5", "A", "1", "2026-10-01", [dict(line, vat_category="8", vat_amount=0)],
                                   issuer_vat="997072577", issuer_name="ΕΦΚΑ",
                                   issuer_address={"postal_code": "1", "city": "x"})
assert "<name>" not in x and "<address>" not in x

# Ο συναλλασσόμενος κρατά χώρα και διεύθυνση (για τη λίστα της «Νέας εγγραφής»).
import os  # noqa: E402
import tempfile  # noqa: E402

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

db.init_db()
db.save_supplier_address("CY12345678X", "ARGOTEST", "CY", "ANAFIS 34", "1143", "NICOSIA")
db.save_supplier_address("CY12345678X", None, "CY", "", "1144", "NICOSIA")  # χωρίς επωνυμία: μένει η παλιά
s = {r["vat"]: r for r in db.list_suppliers()}["CY12345678X"]
assert (s["name"], s["country"], s["street"], s["postal_code"]) == ("ARGOTEST", "CY", None, "1144"), s
print("ok")
