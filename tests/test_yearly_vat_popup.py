"""Ετήσια σύνοψη: popup ΦΠΑ όπως η Φ2 + «εκτός ΦΠΑ» (python -m tests.test_yearly_vat_popup)."""
import os
import re
import tempfile
os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A
A.app.testing = True
import db
db.init_db()
c = A.app.test_client()
c.post("/companies/add", data={"company_name": "X", "use_accountant": "1", "entity_type": "legal"})
cid = db.list_companies()[0]["id"]; db.set_active_company_id(cid)
def doc(kind, mark, date, lines, cls):
    net = sum(l["net_value"] for l in lines); vat = sum(l["vat_amount"] for l in lines)
    db.upsert_document(cid, kind, {"mark": mark, "issue_date": date, "invoice_type": "2.1" if kind == "income" else "1.1",
        "total_net": net, "total_vat": vat, "total_gross": net + vat, "lines": lines, "cls_info": cls}, "classified")
# Έσοδο: 100 με 24% + 50 στο 0% (εκτός)
doc("income", "1", "2025-01-10", [{"line_number": 1, "net_value": 100, "vat_amount": 24, "vat_category": "1"},
                                 {"line_number": 2, "net_value": 50, "vat_amount": 0, "vat_category": "7"}],
    [{"type": "E3_561_001", "category": "category1_1", "amount": 150}])
# Έξοδο με εκπιπτόμενο ΦΠΑ: 10 / 2,40
doc("expense", "2", "2025-01-12", [{"line_number": 1, "net_value": 10, "vat_amount": 2.4, "vat_category": "1"}],
    [{"type": "E3_102_001", "category": "category2_1", "amount": 10, "line": 1}, {"type": "VAT_361", "amount": 10, "line": 1}])
# Έξοδο χωρίς χαρακτηρισμό ΦΠΑ (μη εκπιπτόμενο): εκτός
doc("expense", "3", "2025-02-12", [{"line_number": 1, "net_value": 40, "vat_amount": 9.6, "vat_category": "1"}],
    [{"type": "E3_585_016", "category": "category2_4", "amount": 40, "line": 1}])
h = c.get("/reports/yearly?year=2025").data.decode()
jan = re.search(r'<tbody[^>]*data-month="Ιαν\. 2025"[^>]*', h).group(0)
for k, v in (("m-nout", "100,00"), ("m-out", "24,00"), ("m-nin", "10,00"), ("m-in", "2,40"), ("m-diff", "21,60"),
             ("q-nin", "10,00"), ("q-in", "2,40"), ("q-nout", "100,00")):
    assert f'data-{k}="{v}"' in jan, (k, jan)
feb = re.search(r'<tbody[^>]*data-month="Φεβ\. 2025"[^>]*', h).group(0)
assert 'data-m-nin="0,00"' in feb and 'data-m-in="0,00"' in feb, feb
hq = c.get("/reports/yearly?year=2025&per=q").data.decode()
assert 'data-q-nout="100,00"' in hq and 'data-q-in="2,40"' in hq and 'data-q-label="Α΄ τρίμηνο 2025"' in hq
# Διάγραμμα σωρευτικής πορείας: tooltip με μηνιαία (data-in/out) και σωρευτικά (data-cin/cout/cres) Φεβρουαρίου.
fcol = re.search(r'<g class="yr-col fc-col" data-month="Φεβ\. 2025"[^>]*', h).group(0)
for k, v in (("in", "0,00"), ("out", "40,00"), ("cin", "150,00"), ("cout", "50,00"), ("cres", "100,00")):
    assert f'data-{k}="{v}"' in fcol, (k, fcol)
assert h.count('class="yr-col fc-col"') == 12 and 'class="fc-line fc-line--res"' in h
print("ok")
# Εκτός ΦΠΑ: έσοδο 50 (0%), έξοδο 40 χωρίς χαρ. ΦΠΑ (Φεβ.)
for k, v in (("m-xout", "50,00"), ("m-xin", "0,00"), ("m-xdiff", "50,00"), ("q-xout", "50,00"), ("q-xin", "40,00"), ("q-xdiff", "10,00")):
    assert f'data-{k}="{v}"' in jan, (k, jan)
assert 'data-m-xin="40,00"' in feb, feb
for k, v in (("m-tout", "150,00"), ("m-tin", "10,00"), ("m-tdiff", "140,00"), ("q-tin", "50,00"), ("q-tdiff", "100,00")):
    assert f'data-{k}="{v}"' in jan, (k, jan)
print("ok3")
