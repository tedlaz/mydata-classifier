"""Γενική φόρμα καταχώρησης: XML για όλους τους τύπους, πρόχειρο εξόδου, έλεγχοι (python -m tests.test_entry_form)."""
import json
import os
import tempfile
import xml.etree.ElementTree as ET

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402
import mydata_client as M  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583",
                      "allow_send": True, "allow_classify": True, "allow_new": True})
db.set_active_company_id(cid)
c = A.app.test_client()
NS = {"i": M.INV_NS, "ic": M.ICLS_NS, "ec": M.ECLS_NS}


def line(net, cls, **kw):
    return {"net": net, "vat_category": "1", "vat_amount": round(net * 0.24, 2), "cls": cls, **kw}


# 1) 1.1 έσοδο: 2 γραμμές, 2 χαρακτηρισμοί σε μία, παρακράτηση, 2 τρόποι πληρωμής.
sale = {
    "invoice_type": "1.1", "series": "A", "aa": "7", "issue_date": "2026-10-16",
    "counterpart": {"country": "GR", "vat": "123456789"},
    "lines": [line(100, [{"category": "category1_1", "type": "E3_561_001", "amount": 60},
                         {"category": "category1_1", "type": "E3_561_002", "amount": 40}]),
              line(50, [{"category": "category1_3", "type": "E3_561_001", "amount": 50}])],
    "taxes": [{"type": "1", "category": "2", "base": 150, "amount": 30}],
    "payments": [{"type": "3", "amount": 100}, {"type": "5", "amount": 56}],  # 150 + 36 − 30 = 156
}
with A.app.test_request_context():
    d, errors = A._entry_clean(sale)
assert not errors, errors
assert d["issuer"]["vat"] == "046949583"  # έσοδο: εκδότης η εταιρεία
root = ET.fromstring(M.build_invoice_xml(d))
inv = root.find("i:invoice", NS)
order = [ch.tag.split("}")[1] for ch in inv]
assert order == ["issuer", "counterpart", "invoiceHeader", "paymentMethods", "invoiceDetails", "invoiceDetails",
                 "taxesTotals", "invoiceSummary"], order
assert len(inv.findall("i:invoiceDetails/i:incomeClassification", NS)) == 3
assert not inv.findall(".//i:expensesClassification", NS)
s = inv.find("i:invoiceSummary", NS)
assert s.findtext("i:totalWithheldAmount", namespaces=NS) == "30.00"
assert s.findtext("i:totalGrossValue", namespaces=NS) == "156.00"
agg = {(e.findtext("ic:classificationType", namespaces=NS), e.findtext("ic:amount", namespaces=NS))
       for e in s.findall("i:incomeClassification", NS)}
assert agg == {("E3_561_001", "60.00"), ("E3_561_002", "40.00"), ("E3_561_001", "50.00")}, agg

# 1β) Ίδιο με φόρους σε επίπεδο γραμμής: φόροι μέσα στο invoiceDetails, χωρίς taxesTotals.
line_level = dict(sale, tax_level="line", taxes=[])
line_level["lines"] = [dict(sale["lines"][0], withheld_cat="2", withheld=20), dict(sale["lines"][1], withheld_cat="2", withheld=10)]
with A.app.test_request_context():
    d2, errors = A._entry_clean(line_level)
assert not errors, errors
inv2 = ET.fromstring(M.build_invoice_xml(d2)).find("i:invoice", NS)
assert inv2.find("i:taxesTotals", NS) is None
assert [e.text for e in inv2.findall("i:invoiceDetails/i:withheldAmount", NS)] == ["20.00", "10.00"]
assert inv2.findtext("i:invoiceSummary/i:totalGrossValue", namespaces=NS) == "156.00"

# 1γ) Νόμισμα εκτός EUR: υποχρεωτική ισοτιμία, που μπαίνει αμέσως μετά το currency (σειρά σχήματος).
with A.app.test_request_context():
    _, errors = A._entry_clean(dict(sale, currency="USD"))
    assert any("ισοτιμία" in e for e in errors), errors
    d3, errors = A._entry_clean(dict(sale, currency="USD", exchange_rate="1,0832"))
assert not errors, errors
hdr = [ch.tag.split("}")[1] for ch in ET.fromstring(M.build_invoice_xml(d3)).find("i:invoice/i:invoiceHeader", NS)]
assert hdr[hdr.index("currency") + 1] == "exchangeRate", hdr
assert "<exchangeRate>" not in M.build_invoice_xml(d)  # EUR: χωρίς ισοτιμία

# 1δ) Αρνητικό πρόσημο (recType 7) μόνο στα 17.3–17.6 (ΑΑΔΕ §8.16).
with A.app.test_request_context():
    _, errors = A._entry_clean(dict(sale, lines=[dict(sale["lines"][1], rec_type="7")], taxes=[], payments=[]))
    assert any("Αντίθετο πρόσημο" in e and "17.3" in e for e in errors), errors

# 1ε) Πεδία που δεν ισχύουν για τον τύπο (η φόρμα τα κρύβει) αγνοούνται και από τον server.
noisy = dict(efka := {"invoice_type": "14.5", "series": "EF", "aa": "1", "issue_date": "2026-10-16",
                      "issuer": {"country": "GR", "vat": "997072577"},
                      "lines": [{"net": 300, "vat_category": "8", "vat_amount": 0,
                                 "cls": [{"category": "category2_5", "type": "E3_585_007", "amount": 300}]}]},
             payments=[{"type": "3", "amount": 300}], self_pricing=True, vat_suspension=True, correlated="400001111111111",
             dispatch=True, vehicle="ΙΚΑ1234", lines=[dict(efka["lines"][0], vat_category="1", vat_amount=72,
                                                          not_vat195=True, quantity="2")])
with A.app.test_request_context():
    dn, errors = A._entry_clean(noisy)
assert not errors, errors
assert (dn["payments"], dn["self_pricing"], dn["vat_suspension"], dn["correlated"], dn["vehicle"]) == ([], False, False, "", "")
assert (dn["lines"][0]["vat_category"], dn["lines"][0]["vat_amount"], dn["lines"][0]["not_vat195"], dn["lines"][0]["quantity"]) == ("8", 0.0, False, "")

# 2) 14.5 έξοδο (ΕΦΚΑ) από τη φόρμα → τοπικό πρόχειρο· το /send καλεί send_invoice.
efka = {"invoice_type": "14.5", "series": "EF", "aa": "1", "issue_date": "2026-10-16",
        "issuer": {"country": "GR", "vat": "997072577"},
        "lines": [{"net": 300, "vat_category": "8", "vat_amount": 0,
                   "cls": [{"category": "category2_5", "type": "E3_585_007", "amount": 300}]}]}
r = c.post("/entry", data={"draft": json.dumps(efka)})
assert r.status_code == 302, r.get_data(as_text=True)[:500]
(doc,) = [x for x in db.get_documents(cid, "expense") if x["invoice_type"] == "14.5"]
assert doc["status"] == "classified" and doc["local_action"] == "create" and doc["total_net"] == 300
assert json.loads(doc["draft_json"])["counterpart"]["vat"] == "046949583"  # αντισυμβαλλόμενος = εμείς

sent = []


class FakeClient:
    def send_invoice(self, xml):
        sent.append(xml)
        return {"status": "Success", "invoice_mark": "400000000000123", "errors": []}


orig = A.get_client
A.get_client = lambda send="": FakeClient()
try:
    c.post("/send", data={"marks": [doc["mark"]]})
finally:
    A.get_client = orig
assert sent and "expensesClassification" in sent[0] and "<invoiceType>14.5</invoiceType>" in sent[0], sent
assert db.get_document(cid, "400000000000123")["status"] == "sent"

# 3) Χαρακτηρισμοί που δεν αθροίζουν στην καθαρή αξία → 422, τίποτα δεν αποθηκεύεται.
bad = dict(efka, aa="2", lines=[dict(efka["lines"][0], cls=[{"category": "category2_5", "type": "E3_585_007", "amount": 200}])])
assert c.post("/entry", data={"draft": json.dumps(bad)}).status_code == 422
assert len([x for x in db.get_documents(cid, "expense") if x["invoice_type"] == "14.5"]) == 1

# 3β) Ό,τι περνά τους ελέγχους της φόρμας αλλά όχι το XSD της ΑΑΔΕ δεν αποθηκεύεται (εδώ: σειρά > 50 χαρακτήρες).
bad_xsd = dict(efka, aa="3", series="Σ" * 60)
r = c.post("/entry", data={"draft": json.dumps(bad_xsd)})
assert r.status_code == 422 and "Έλεγχος XSD" in r.get_data(as_text=True)
assert len([x for x in db.get_documents(cid, "expense") if x["invoice_type"] == "14.5"]) == 1
# … ούτε φεύγει: η κεντρική αποστολή ελέγχει το XSD πριν από κάθε POST.
try:
    M.MyDataClient("u", "k", "dev").send_invoice(M.build_invoice_xml(A._entry_clean(bad_xsd)[0]))
    raise AssertionError("έπρεπε να μπλοκαριστεί")
except M.MyDataError as e:
    assert "XSD" in str(e), e

# 3γ) Χώρα ανά τύπο (14.1 → μόνο ΕΕ) και αποθήκευση νέου συναλλασσόμενου με χώρα/διεύθυνση.
intra = {"invoice_type": "14.1", "series": "I", "aa": "1", "issue_date": "2026-10-16",
         "issuer": {"country": "FR", "vat": "FR40303265045", "name": "ACME SARL", "street": "RUE DE LA PAIX", "number": "12",
                    "postal_code": "75002", "city": "PARIS"},
         "lines": [{"net": 100, "vat_category": "1", "vat_amount": 24,
                    "cls": [{"category": "category2_1", "type": "E3_102_004", "amount": 100}]}]}
r = c.post("/entry", data={"draft": json.dumps(dict(intra, issuer=dict(intra["issuer"], country="US")))})
assert r.status_code == 422 and "χώρα της ΕΕ" in r.get_data(as_text=True)
assert c.post("/entry", data={"draft": json.dumps(intra)}).status_code == 302
assert db.get_supplier("FR40303265045") == {"vat": "FR40303265045", "name": "ACME SARL", "country": "FR",
                                            "street": "RUE DE LA PAIX 12", "postal_code": "75002", "city": "PARIS"}
with A.app.test_request_context():
    d_gr, _ = A._entry_clean(dict(efka, issuer={"country": "DE", "vat": "997072577"}))
assert d_gr["issuer"]["country"] == "GR"  # 14.5: ο εκδότης κλειδώνει σε Ελλάδα (ΑΑΔΕ)

# 3δ) Έσοδο που στέλνεται αμέσως (1.2, πελάτης στη Γερμανία — το VIES δεν δίνει στοιχεία, συμπληρώνονται με το χέρι):
#     ο νέος πελάτης φαίνεται αμέσως στους «Συναλλασσόμενους» της εταιρείας, χωρίς να περιμένει την «Ανάκτηση».
sale_de = {"invoice_type": "1.2", "series": "E", "aa": "1", "issue_date": "2026-10-16",
           "counterpart": {"country": "DE", "vat": "129484218", "name": "Muster GmbH", "street": "Hauptstr.", "number": "5",
                           "postal_code": "10115", "city": "Berlin"},
           "lines": [{"net": 500, "vat_category": "7", "vat_amount": 0, "vat_exemption": "14",
                      "cls": [{"category": "category1_1", "type": "E3_561_005", "amount": 500}]}],
           "payments": [{"type": "5", "amount": 500}]}
orig = A.get_client
A.get_client = lambda send="": FakeClient()
try:
    r = c.post("/entry", data={"draft": json.dumps(sale_de)})
finally:
    A.get_client = orig
assert r.status_code == 302 and r.headers["Location"].endswith("/income"), r.get_data(as_text=True)[:400]
assert "129484218" in {s["vat"] for s in db.list_suppliers(cid)}
assert "Muster GmbH" in c.get("/suppliers").get_data(as_text=True)
other = db.add_company({"company_name": "Άλλη", "AADE_VAT_NUMBER": "099999999"})
assert "129484218" not in {s["vat"] for s in db.list_suppliers(other)}  # μόνο στην εταιρεία που τον δήλωσε

# 4) Η σελίδα φορτώνει.
assert "Γενική φόρμα καταχώρησης" in c.get("/entry").get_data(as_text=True)
print("OK")
