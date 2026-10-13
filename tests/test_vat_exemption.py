"""Κατηγορία ΦΠΑ 7 (0%) θέλει κατηγορία εξαίρεσης ΦΠΑ (python -m tests.test_vat_exemption)."""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
from mydata_client import build_self_expense_invoice_xml  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583",
                     "allow_send": True, "allow_classify": True, "allow_new": True})
db.set_active_company_id(cid)
c = A.app.test_client()
form = {"invoice_type": "13.1", "series": "X", "aa": "1", "issue_date": "2026-10-01", "payment_method": "3",
        "line_amount": "50", "line_category": "category2_4", "line_type": "E3_585_016",
        "line_vat_category": "7", "line_vat_amount": "0", "line_vat_type": "VAT_361", "line_vat_exemption": ""}

# 1) 0% χωρίς εξαίρεση → απορρίπτεται, η φόρμα ξαναδείχνεται με το πεδίο υποχρεωτικό.
r = c.post("/new_expense", data=form)
page = r.get_data(as_text=True)
assert r.status_code == 422 and "κατηγορία εξαίρεσης ΦΠΑ" in page
assert 'name="line_vat_exemption" required' in page

# 2) Με εξαίρεση → αποθηκεύεται στο draft· σε άλλη κατηγορία ΦΠΑ η εξαίρεση σβήνει.
c.post("/new_expense", data=dict(form, line_vat_exemption="22"))
(d,) = db.get_documents(cid, "expense")
assert json.loads(d["draft_json"])["lines"][0]["vat_exemption"] == "22"
c.post("/new_expense", data=dict(form, aa="2", line_vat_category="1", line_vat_amount="12", line_vat_exemption="22"))
d2 = next(x for x in db.get_documents(cid, "expense") if x["aa"] == "2")
assert json.loads(d2["draft_json"])["lines"][0]["vat_exemption"] == ""

# 3) XML: vatExemptionCategory αμέσως μετά το vatAmount (σειρά σχήματος), μόνο στην κατηγορία 7.
line = {"amount": 50.0, "classification_category": "category2_4", "classification_type": "E3_585_016",
        "vat_category": "7", "vat_amount": 0.0, "vat_type": "", "vat_exemption": "22"}
x = build_self_expense_invoice_xml("13.1", "X", "1", "2026-10-01", [line])
assert "<vatAmount>0.00</vatAmount><vatExemptionCategory>22</vatExemptionCategory>" in x, x
# Σφάλμα 230: και με ΦΠΑ 0 θέλει χαρακτηρισμό ΦΠΑ (γραμμή + invoiceSummary).
assert x.count("VAT_361</") == 2, x
assert json.loads(d["draft_json"])["lines"][0]["vat_type"] == "VAT_361"
x = build_self_expense_invoice_xml("13.1", "X", "1", "2026-10-01", [dict(line, vat_category="8")])
assert "vatExemptionCategory" not in x
print("ok")

# 4) Η σελίδα της τοπικής εγγραφής (χωρίς XML) δείχνει την εξαίρεση από το draft.
assert "Εξαίρεση 22 ·" in c.get(f"/document/{d['mark']}").get_data(as_text=True)
print("ok page")
