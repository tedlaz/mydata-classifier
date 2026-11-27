"""Φόρμα καταχώρησης: XML για όλους τους τύπους, πρόχειρο εξόδου, έλεγχοι (python -m tests.test_entry_form)."""
import json
import re
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
assert "Φόρμα καταχώρησης" in c.get("/entry").get_data(as_text=True)

# 5) Έτοιμα πρότυπα («Γρήγορη έναρξη»): με ποσά και συναλλασσόμενο περνούν τους ελέγχους και το XSD της ΑΑΔΕ,
#    και ο χαρακτηρισμός Ε3 τους είναι στους συνδυασμούς της ΑΑΔΕ για τον τύπο.
fill = {"rent": ([500], [{"base": 500, "amount": 18}]), "payroll": ([1000, 220], [{"base": 1000, "amount": 50}, {"amount": 140}]),
        "stock": ([800, 650], []), "art46": ([500], [])}  # art46: πληρωμή 620 € = 500 + ΦΠΑ 120
PARTY = {"gr": {"country": "GR", "vat": "094019245", "street": "ΠΑΝΟΡΜΟΥ", "number": "1", "postal_code": "11523", "city": "ΑΘΗΝΑ"},
         "any": {"country": "GR", "vat": "094019245"},
         "eu": {"country": "DE", "vat": "DE123456789", "name": "X", "postal_code": "10115", "city": "Berlin"},
         "third": {"country": "US", "vat": "US123", "name": "X", "postal_code": "10001", "city": "NY"}}
page = c.get("/entry").get_data(as_text=True)
assert page.index("preset=art46") < page.index("preset=efka")  # το άρθρο 46 πρώτο
for key, (name, preset) in A._ENTRY_PRESETS.items():
    assert f"preset={key}" in page, key
    nets, tax_vals = fill.get(key, ([100] * len(preset["lines"]), []))
    p = json.loads(json.dumps(preset))
    p.update(series="P" + key, aa="1", issue_date="2026-10-16")
    t, roles = p["invoice_type"], M.entry_roles(p["invoice_type"], p.get("self_pricing"), p.get("omission"))
    for party in ("issuer", "counterpart"):
        if roles[party] == "other" and party not in p:
            p[party] = PARTY[M.entry_country_rule(party, t)]
        elif roles[party] == "own" and t in M.ENTRY_RULES["counterpart_address"]:  # 14.4: η διεύθυνσή μας
            p[party] = {"street": "ΑΙΟΛΟΥ", "number": "1", "postal_code": "10559", "city": "ΑΘΗΝΑ"}
    for ln, net in zip(p["lines"], nets):
        ln["net"] = net
        ln["vat_amount"] = round(net * 0.24, 2) if ln["vat_category"] == "1" else 0.0
        ln["cls"] = [dict(x, amount=net) for x in ln["cls"]]
        for x in ln["cls"]:
            if x["type"].startswith("E3_"):
                assert x["type"] in db.combos_for_type(t).get(x["category"], []), (key, x)
    for tx, v in zip(p.get("taxes", []), tax_vals):
        tx.update(v)
    gross = round(sum(ln["net"] + ln["vat_amount"] for ln in p["lines"]) - sum(v["amount"] for v in tax_vals if t == "17.1"), 2)
    if "payments" not in M.ENTRY_HIDDEN.get(t, []):  # στη φόρμα: μία πληρωμή = η συνολική αξία
        p["payments"] = [dict(pm, amount=gross) for pm in p.get("payments") or [{"type": "3"}]]
    with A.app.test_request_context():
        d, errors = A._entry_clean(p)
    assert not errors, (key, errors)
    assert not A.xsd_errors(M.draft_xml(d)), (key, A.xsd_errors(M.draft_xml(d)))
    if key == "art46":  # όπως έγινε δεκτό στο dev: εκδότης ο προμηθευτής, εμείς αντισυμβαλλόμενος, 2.95 χωρίς Ε3 + VAT_361
        inv = ET.fromstring(M.draft_xml(d)).find("i:invoice", NS)
        assert inv.findtext("i:issuer/i:vatNumber", namespaces=NS) == "094019245"
        assert inv.findtext("i:counterpart/i:vatNumber", namespaces=NS) == "046949583"
        assert inv.findtext("i:invoiceSummary/i:totalVatAmount", namespaces=NS) == "120.00"
        got = [(e.findtext("ec:classificationCategory", namespaces=NS), e.findtext("ec:classificationType", namespaces=NS))
               for e in inv.findall("i:invoiceDetails/i:expensesClassification", NS)]
        assert got == [("category2_95", None), (None, "VAT_361")], got

# 5β) Πληροφοριακά (category2_95, π.χ. πληρωμή προμηθευτή άρθρου 46): εκτός οικονομικών αναφορών (δεν είναι έξοδο —
#     το έξοδο είναι το τιμολόγιο του προμηθευτή), μέσα στη δήλωση ΦΠΑ (361/381 του Φ2, τον μήνα της πληρωμής).
import vat_return  # noqa: E402

info = json.loads(json.dumps(A._ENTRY_PRESETS["art46"][1]))
info.update(series="INFO", aa="1", issue_date="2026-09-10", issuer={"country": "GR", "vat": "094019245"})
info["lines"][0].update(net=500, vat_amount=120)
info["lines"][0]["cls"] = [dict(x, amount=500) for x in info["lines"][0]["cls"]]
with A.app.test_request_context():
    A._store_entry(cid, A._entry_clean(info)[0])
with db.get_conn() as conn:  # σαν να στάλθηκε: η δήλωση ΦΠΑ μετρά μόνο διαβιβασμένα (MARK)
    conn.execute("UPDATE documents SET mark = '400000000000950', status = 'confirmed' WHERE series = 'INFO'")
assert db.is_informational(json.dumps([{"category": "category2_95", "amount": 1}, {"type": "VAT_361", "amount": 1}]))
assert not db.is_informational(json.dumps([{"category": "category2_1", "type": "E3_102_001", "amount": 1}]))
assert not db.is_informational("[]")  # αχαρακτήριστο: δεν ξέρουμε ακόμα
st = ["classified", "sent", "confirmed"]
assert not [d for d in db.yearly_documents(cid, "expense", st, "2026") if d["issue_date"] == "2026-09-10"]
assert not [d for d in db.month_documents(cid, "expense", st, "2026-09")]
assert not [d for d in db.olap_documents(cid, "expense", st) if d["series"] == "INFO"]
sep = db.period_documents(cid, "expense", "2026-09-01", "2026-09-30")
assert [d["series"] for d in sep] == ["INFO"]
codes = vat_return.compute([], sep)["codes"]
assert (codes["361"], codes["381"]) == (500, 120), (codes["361"], codes["381"])
# Το τιμολόγιο του ίδιου προμηθευτή (1.240 €, ίδιος μήνας): χωρίς τη σημαία «άρθρο 46» μετρά στο Φ2 (κανονικό
# καθεστώς)· με τη σημαία όχι — ο ΦΠΑ του μπαίνει μόνο με την πληρωμή (2_95), αλλιώς θα μετρούσε δύο φορές.
# Η σημαία είναι ανά παραστατικό (ο προμηθευτής μπορεί να βγει/ξαναμπεί στο καθεστώς).
inv39 = {"invoice_type": "1.1", "omission": True, "series": "INV39", "aa": "1", "issue_date": "2026-09-05",
         "issuer": {"country": "GR", "vat": "094019245"}, "payments": [{"type": "3", "amount": 1240}],
         "lines": [line(1000, [{"category": "category2_1", "type": "E3_102_001", "amount": 1000}])]}
with A.app.test_request_context():
    A._store_entry(cid, A._entry_clean(inv39)[0])
with db.get_conn() as conn:
    conn.execute("UPDATE documents SET mark = '400000000000951', status = 'confirmed' WHERE series = 'INV39'")
f2 = lambda: vat_return.compute([], db.period_documents(cid, "expense", "2026-09-01", "2026-09-30"))["codes"]  # noqa: E731
assert (f2()["361"], f2()["381"]) == (1500, 360), f2()
checked = lambda: re.search(r'name="on" value="(\d)" checked', c.get("/document/400000000000951").get_data(as_text=True))[1]  # noqa: E731
assert checked() == "0"  # «Τον μήνα του τιμολογίου»
r = c.post("/document/400000000000951/vat_cash", data={"on": "1"})
assert r.status_code == 302 and db.get_document(cid, "400000000000951")["vat_cash"] == 1
assert checked() == "1" and "🕒 Άρθρο 46 (39β)</span>" in c.get("/document/400000000000951").get_data(as_text=True)
assert (f2()["361"], f2()["381"]) == (500, 120), f2()  # μόνο η πληρωμή
# Συμφωνία Φ2: το τιμολόγιο σε «Άρθρο 46» (αξία + ΦΠΑ που εκκρεμεί), η πληρωμή σε δική της γραμμή 2.95 → 361/381.
m = vat_return.reconcile([], db.period_documents(cid, "expense", "2026-09-01", "2026-09-30"))["expense"]
assert m["ok"] and (m["totals"]["a46"], m["totals"]["a46v"], m["totals"]["361"], m["totals"]["381"]) == (1000, 240, 500, 120), m
assert {r["e3"]: r["vals"] for r in m["rows"]}["2.95"]["361"] == 500 and "nd" not in m["totals"]
page = c.get("/reports/vat/reconcile?year=2026&period=m9").get_data(as_text=True)
assert "Πληρωμές άρθρου 46" in page and "ΦΠΑ άρθρου 46 · με την πληρωμή" in page
# Εκκαθάριση άρθρου 46: καρτέλα ανά προμηθευτή κατά ημερομηνία — τιμολόγιο 5/9 (αξία 1.000, ΦΠΑ 240), μετά πληρωμή
# 10/9 (500, ΦΠΑ 120) → υπόλοιπο αξία 500, ΦΠΑ 120.
(s39,) = vat_return.art46_ledger(db.art46_documents(cid), "2026-09-30")
assert s39["vat_number"] == "094019245" and s39["invoices"] == 1
assert [(r["series"], r["payment"], r["rest"], r["rest_vat"]) for r in s39["rows"]] == [
    ("INV39", False, 1000, 240), ("INFO", True, 500, 120)], s39["rows"]
t = s39["totals"]
assert (t["net"], t["vat"], t["paid"], t["vat_deducted"], t["rest"], t["rest_vat"]) == (1000, 240, 500, 120, 500, 120), t
assert not vat_return.art46_ledger(db.art46_documents(cid), "2026-09-01")
page = c.get("/reports/vat/art46?as_of=2026-09-30").get_data(as_text=True)
assert "INV39" in page and "INFO" in page and "Υπόλοιπο 500,00 + ΦΠΑ 120,00" in page and "💳 Πληρωμή (2.95)" in page
assert page.index("INV39") < page.index("INFO")  # ημερολογιακά
# Πληρωμή με αρνητικά ποσά (κόκκινα): αξία −500, ΦΠΑ −120, σύνολο −620· υπόλοιπο σύνολο 620.
assert '<td class="neg">-500,00</td><td class="neg">-120,00</td>' in page and '<td class="neg">-620,00</td>' in page
assert '<td class="rest">620,00</td>' in page
# Δύο τιμολόγια και μία πληρωμή ανάμεσα: τρέχον υπόλοιπο μετά από κάθε κίνηση.
led = vat_return.art46_ledger([
    {"mark": "1", "issue_date": "2026-01-10", "invoice_type": "1.1", "counterparty_vat": "9", "vat_cash": 1,
     "total_net": 100, "total_vat": 24, "total_gross": 124, "cls_json": "[]"},
    {"mark": "3", "issue_date": "2026-02-01", "invoice_type": "14.30", "counterparty_vat": "9", "vat_cash": 0,
     "total_net": 150, "total_vat": 36, "total_gross": 186, "cls_json": json.dumps([{"category": "category2_95", "amount": 150}])},
    {"mark": "2", "issue_date": "2026-02-10", "invoice_type": "1.1", "counterparty_vat": "9", "vat_cash": 1,
     "total_net": 200, "total_vat": 48, "total_gross": 248, "cls_json": "[]"}])
assert [(r["mark"], r["rest"], r["rest_vat"]) for r in led[0]["rows"]] == [("1", 100, 24), ("3", -50, -12), ("2", 150, 36)], led
assert [d["series"] for d in db.financial(db.period_documents(cid, "expense", "2026-09-01", "2026-09-30"))] == ["INV39"]
assert "vat_cash" not in (db.get_supplier("094019245") or {})  # οι συναλλασσόμενοι όπως πριν
c.post("/document/400000000000951/vat_cash", data={"on": "0"})
assert db.get_document(cid, "400000000000951")["vat_cash"] == 0 and f2()["361"] == 1500

# 6) Μισθοδοσία 17.1: παρακράτηση + κρατήσεις όχι πάνω από τις αποδοχές· παρακράτηση με υποκείμενη αξία.
pay = {"invoice_type": "17.1", "series": "M", "aa": "1", "issue_date": "2026-10-16",
       "lines": [{"net": 1000, "vat_category": "8", "cls": [{"category": "category2_6", "type": "E3_581_001", "amount": 1000}]}],
       "payments": [{"type": "1", "amount": 1000}]}
with A.app.test_request_context():
    _, e1 = A._entry_clean(dict(pay, taxes=[{"type": "1", "category": "11", "base": 1000, "amount": 600},
                                            {"type": "5", "amount": 500}]))
    _, e2 = A._entry_clean(dict(pay, taxes=[{"type": "1", "category": "11", "amount": 50}]))
assert any("σύνολο αποδοχών" in e for e in e1), e1
assert any("υποκείμενη αξία" in e for e in e2), e2
# Τρόπος πληρωμής υποχρεωτικός στο 17.1 (ΑΑΔΕ)· το έτοιμο πρότυπο βάζει «Επαγ. Λογαριασμός Πληρωμών Ημεδαπής».
with A.app.test_request_context():
    _, e3 = A._entry_clean(dict(pay, payments=[]))
assert e3 == ["Ο τύπος 17.1 θέλει τρόπο εξόφλησης."], e3
assert A._ENTRY_PRESETS["payroll"][1]["payments"] == [{"type": "1"}]

# 7) Παλιό πρότυπο της «Νέας εγγραφής» (17.1 με παρακράτηση/κρατήσεις) → draft Φόρμας καταχώρησης, έγκυρο XSD.
old = {"invoice_type": "17.1", "series": "MIS", "aa": "1", "issue_date": "2026-10-31", "payment_method": "3",
       "withheld_amount": 50.0, "withheld_base": 1000.0, "deductions_amount": 140.0,
       "lines": [{"amount": 1000.0, "classification_category": "category2_6", "classification_type": "E3_581_001",
                  "vat_category": "8", "vat_amount": 0.0, "vat_type": ""}, None]}
with A.app.test_request_context():
    d, errors = A._entry_clean(A._as_entry(old))
assert not errors, errors
assert not A.xsd_errors(M.draft_xml(d))
assert {(t["type"], t["amount"]) for t in d["taxes"]} == {("1", 50.0), ("5", 140.0)}
assert M.entry_totals(d)["gross"] == 810.0 and d["payments"] == [{"type": "3", "amount": 810.0, "info": ""}]

# 8) «Ως πρότυπο» από τη Φόρμα καταχώρησης → φόρτωση με σημερινή ημ/νία και επόμενο Α/Α της σειράς.
r = c.post("/entry/template", data={"draft": json.dumps(dict(pay, series="TPL")), "template_name": "Μισθοδοσία"})
tid = int(r.headers["Location"].rsplit("=", 1)[1])
assert "issue_date" not in db.get_template(cid, tid)["draft"]
c.post("/entry", data={"draft": json.dumps(dict(pay, series="TPL", aa="4")), "template_id": str(tid)})
page = c.get(f"/entry?template={tid}").get_data(as_text=True)
assert '"aa": "5"' in page and '"series": "TPL"' in page
assert c.post("/entry/template", data={"draft": json.dumps(dict(pay, series="TPL")), "template_name": "Άλλο"}).status_code == 422
assert c.post("/entry/template", data={"draft": json.dumps(sale), "template_name": "Πώληση"}).status_code == 422  # όχι έσοδα
c.post(f"/entry/template/{tid}/delete")
assert db.get_template(cid, tid) is None

# 9) Πρόχειρο που δεν έχει σταλεί (Φόρμα καταχώρησης ή παλιό της «Νέας εγγραφής»): η σελίδα δείχνει το XML που θα σταλεί.
c.post("/entry", data={"draft": json.dumps(dict(pay, series="XMLV", aa="1"))})
mark = next(x["mark"] for x in db.get_documents(cid, "expense") if x["series"] == "XMLV")
page = c.get(f"/document/{mark}").get_data(as_text=True)
assert "αυτό είναι το XML που θα σταλεί" in page and "17.1 ·" in page, page[-3000:]
xml = c.get(f"/document/{mark}/xml").get_data(as_text=True)
assert "<invoiceType>17.1</invoiceType>" in xml and "InvoicesDoc" in xml
db.create_local_expense(cid, {"issue_date": "2026-10-31", "invoice_type": "17.1", "series": "OLD", "aa": "1"},
                         dict(old, lines=old["lines"][:1]), [])  # αποθηκευμένο draft: χωρίς κενές γραμμές
mark = next(x["mark"] for x in db.get_documents(cid, "expense") if x["series"] == "OLD")
assert "αυτό είναι το XML που θα σταλεί" in c.get(f"/document/{mark}").get_data(as_text=True)

# 10) 14.30 (Παραστατικά Οντότητας), όπως απάντησε το dev της ΑΑΔΕ: εκδότης ο τρίτος, αντισυμβαλλόμενος εμείς (204),
#     χωρίς τρόπο πληρωμής (205), με χαρακτηρισμό ΦΠΑ VAT_xxx (230), χωρίς ΦΠΑ μέσα στον χαρακτηρισμό (337).
#     Η ειδική κατηγορία 4 είναι Tax Free και απαγορεύεται (205) — δεν προσφέρεται.
own_doc = {"invoice_type": "14.30", "series": "ΤΑΚΤ", "aa": "1", "issue_date": "2026-11-30",
           "issuer": {"country": "GR", "vat": "094019245"}, "payments": [{"type": "3", "amount": 18168.48}],
           "lines": [{"net": 14652, "vat_category": "1", "vat_amount": 3516.48,
                      "cls": [{"category": "category2_95", "amount": 14652}]}]}
assert M.entry_roles("14.30") == {"side": "expense", "issuer": "other", "counterpart": "own"}
assert M.entry_roles("13.30")["issuer"] == M.entry_roles("13.3")["issuer"] == "none"
assert "4" not in M.ENTRY_OPTIONS["special"]
with A.app.test_request_context():
    d, errors = A._entry_clean(own_doc)
    assert any("4" in e for e in A._entry_clean(dict(own_doc, special_category="4"))[1])
assert not errors, errors
assert d["issuer"]["vat"] == "094019245" and d["counterpart"]["vat"] == "046949583" and not d["payments"]
xml = M.build_invoice_xml(d)
assert not A.xsd_errors(xml), A.xsd_errors(xml)
inv = ET.fromstring(xml).find("i:invoice", NS)
assert [ch.tag.split("}")[1] for ch in inv] == ["issuer", "counterpart", "invoiceHeader", "invoiceDetails", "invoiceSummary"]
assert {e.findtext("ec:classificationType", namespaces=NS) for e in inv.findall("i:invoiceDetails/i:expensesClassification", NS)} == {None, "VAT_361"}
assert not inv.findall(".//ec:vatAmount", NS)

# 11) Προεπισκόπηση XML (/entry/xml): ίδιο XML με την αποθήκευση, μορφοποιημένο· λάθη επιστρέφονται χωρίς αποθήκευση.
r = c.post("/entry/xml", data={"draft": json.dumps(own_doc)}).get_json()
assert not r["errors"], r["errors"]
assert "<invoiceType>14.30</invoiceType>" in r["xml"] and "\n  <invoice>" in r["xml"]
r = c.post("/entry/xml", data={"draft": json.dumps(dict(own_doc, series=""))}).get_json()
assert any("σειρά" in e for e in r["errors"]) and r["xml"]
assert c.post("/entry/xml", data={"draft": "{}"}).get_json() == {"xml": "", "errors": ["Επίλεξε τύπο παραστατικού."]}

# 12) Τύποι «ημεδαπής / αλλοδαπής» (13.1, 13.2, 14.31): εκδότης από Ελλάδα ή εξωτερικό· 14.5 κλειδωμένος GR.
assert M.entry_country_rule("issuer", "14.31") == "any" and M.entry_country_rule("issuer", "14.5") == "gr"
cn = {"invoice_type": "14.31", "series": "Π", "aa": "1", "issue_date": "2026-11-20",
      "issuer": {"country": "US", "vat": "123", "name": "ACME", "postal_code": "10001", "city": "NY"},
      "lines": [{"net": 10, "vat_category": "1", "vat_amount": 2.4,
                 "cls": [{"category": "category2_1", "type": "E3_102_001", "amount": 10}]}]}
with A.app.test_request_context():
    d, errors = A._entry_clean(cn)
    assert d["issuer"]["country"] == "US", d["issuer"]
    d_gr, _ = A._entry_clean(dict(cn, issuer={"vat": "123456789"}))
    assert d_gr["issuer"]["country"] == "GR"  # χωρίς χώρα → Ελλάδα

# 13) Απαντήσεις dev ΑΑΔΕ.
# 14.31: διεύθυνση της εταιρείας ως αντισυμβαλλόμενου υποχρεωτική (204) — μόνο διεύθυνση, χωρίς επωνυμία.
with A.app.test_request_context():
    errors = A._entry_clean(cn)[1]
    assert any("διεύθυνση της εταιρείας" in e for e in errors), errors
    d, errors = A._entry_clean(dict(cn, counterpart={"street": "ΑΙΟΛΟΥ", "number": "10", "postal_code": "10559",
                                                     "city": "ΑΘΗΝΑ", "name": "ΑΓΝΟΕΙΤΑΙ"}))
    assert not errors, errors
    A._save_entry_parties(cid, d)
xml = M.build_invoice_xml(d)
assert not A.xsd_errors(xml), A.xsd_errors(xml)
cp = ET.fromstring(xml).find("i:invoice/i:counterpart", NS)
assert cp.findtext("i:vatNumber", namespaces=NS) == "046949583" and cp.find("i:name", NS) is None
assert cp.findtext("i:address/i:postalCode", namespaces=NS) == "10559"
assert db.get_supplier("046949583")["city"] == "ΑΘΗΝΑ"  # προσυμπληρώνεται την επόμενη φορά
# 3.1: χωρίς ΑΦΜ αντισυμβαλλόμενου → απορρίπτεται (204 «Counterpart is mandatory»).
t31 = {"invoice_type": "3.1", "series": "Τ", "aa": "1", "issue_date": "2026-11-20", "counterpart": {"vat": ""},
       "payments": [{"type": "3", "amount": 200}],
       "lines": [{"net": 200, "vat_category": "8", "cls": [{"category": "category2_1", "type": "E3_102_002", "amount": 200}]}]}
with A.app.test_request_context():
    assert "Συμπλήρωσε το ΑΦΜ του αντισυμβαλλόμενου." in A._entry_clean(t31)[1]
    # 3.1 / 1.4: και διεύθυνση του (ελληνικού) αντισυμβαλλόμενου (204 «Counterpart address is mandatory»).
    t31["counterpart"] = {"vat": "094019245"}
    assert any("διεύθυνση του αντισυμβαλλόμενου" in e for e in A._entry_clean(t31)[1])
    t31["counterpart"] = {"vat": "094019245", "street": "ΠΑΝΟΣ", "number": "1", "postal_code": "10555", "city": "ΑΘΗΝΑ"}
    d, errors = A._entry_clean(t31)
    assert not errors, errors
    cp = ET.fromstring(M.build_invoice_xml(d)).find("i:invoice/i:counterpart", NS)
    assert cp.findtext("i:address/i:city", namespaces=NS) == "ΑΘΗΝΑ" and cp.find("i:name", NS) is None
    assert not A.xsd_errors(M.build_invoice_xml(d))
    # 1.6 / 2.4: συσχετιζόμενο MARK υποχρεωτικό (204 «CorrelatedInvoices is mandatory»).
    t16 = dict(sale, invoice_type="1.6", self_pricing=True,
               lines=[line(100, [{"category": "category2_1", "type": "E3_102_001", "amount": 100}])],
               taxes=[], payments=[{"type": "3", "amount": 124}])
    assert any("MARK του παραστατικού" in e for e in A._entry_clean(t16)[1])
    assert not A._entry_clean(dict(t16, correlated="400001977207897"))[1]
    # 1.5: «Είδος γραμμής» σε κάθε γραμμή, τουλάχιστον μία εκκαθάριση και μία αμοιβή (246).
    cls15 = [{"category": "category1_7", "type": "E3_881_001", "amount": 100}]
    t15 = dict(sale, invoice_type="1.5", taxes=[], payments=[{"type": "3", "amount": 248}],
               lines=[line(100, cls15, detail_type="1"), line(100, cls15)])
    assert any("σε κάθε γραμμή" in e for e in A._entry_clean(t15)[1])
    t15["lines"][1]["detail_type"] = "1"
    assert any("μία «Αμοιβή" in e for e in A._entry_clean(t15)[1])
    t15["lines"][1]["detail_type"] = "2"
    d, errors = A._entry_clean(t15)
    assert not errors, errors
    xml = M.build_invoice_xml(d)
    assert not A.xsd_errors(xml), A.xsd_errors(xml)
    assert [e.text for e in ET.fromstring(xml).iter(f"{{{M.INV_NS}}}invoiceDetailType")] == ["1", "2"]
    assert all(ln["detail_type"] == "" for ln in A._entry_clean(dict(t15, invoice_type="1.1"))[0]["lines"])
    # 1.5 με αυτοτιμολόγηση: χαρακτηρισμοί εσόδων (dev: 230/231)· το 1.1 με αυτοτιμολόγηση μένει έξοδο.
    assert M.entry_roles("1.5", True)["side"] == "income" and M.entry_roles("1.1", True)["side"] == "expense"
    # 8.2: καθαρή αξία 0, το τέλος ως λοιπός φόρος γραμμής με κατηγορία (dev: 213/204).
    cls82 = [{"category": "category2_5", "type": "E3_585_005", "amount": 0}]
    t82 = {"invoice_type": "8.2", "omission": True, "series": "Κ", "aa": "1", "issue_date": "2026-10-15",
           "issuer": {"vat": "094019245"}, "tax_level": "line", "payments": [{"type": "3", "amount": 1.5}],
           "lines": [{"net": 0, "vat_category": "8", "cls": cls82, "other_cat": "20", "other": 1.5}]}
    d, errors = A._entry_clean(t82)
    assert not errors, errors
    xml = M.build_invoice_xml(d)
    assert not A.xsd_errors(xml), A.xsd_errors(xml)
    assert ET.fromstring(xml).findtext(".//i:otherTaxesPercentCategory", namespaces=NS) == "20"
    bad = dict(t82, lines=[dict(t82["lines"][0], net=100, cls=[dict(cls82[0], amount=100)])])
    assert any("καθαρή αξία 0" in e for e in A._entry_clean(bad)[1])
    assert any("σε κάθε γραμμή" in e for e in A._entry_clean(dict(t82, tax_level="doc"))[1])
# 17.6: οι κανόνες του xlsx χωρίς κωδικούς E3 («Δεν ενημερώνει E3») διαβάζονται (αλλιώς 331).
assert db.combos_for_type("17.6") == {"category2_12": ["-"], "category2_95": ["-"]}, db.combos_for_type("17.6")
# 14.1/14.3: χαρακτηρισμός ΦΠΑ μόνο VAT_364/VAT_365 (αυτόματα)· αλλού όχι αυτοί.
eu = {"invoice_type": "14.1", "series": "ΕΕ", "aa": "1", "issue_date": "2026-11-20",
      "issuer": {"country": "DE", "vat": "DE1", "name": "X", "postal_code": "1", "city": "B"},
      "lines": [{"net": 100, "vat_category": "1", "vat_amount": 24,
                 "cls": [{"category": "category2_1", "type": "E3_102_004", "amount": 100}]}]}
with A.app.test_request_context():
    d, errors = A._entry_clean(eu)
    assert not errors and [c["type"] for c in d["lines"][0]["cls"]] == ["E3_102_004", "VAT_364"], (errors, d["lines"])
    bad = dict(eu, lines=[dict(eu["lines"][0], cls=eu["lines"][0]["cls"] + [{"type": "VAT_361", "amount": 100}])])
    assert any("μόνο VAT_364" in e for e in A._entry_clean(bad)[1])
    bad = dict(cn, invoice_type="13.1", lines=[dict(cn["lines"][0], cls=cn["lines"][0]["cls"] + [{"type": "VAT_365", "amount": 10}])])
    assert any("VAT_365" in e for e in A._entry_clean(bad)[1])
# Ανάκτηση: τίτλος κτήσης 3.x και αυτοτιμολόγηση που διαβιβάσαμε εμείς είναι ΕΞΟΔΑ, όχι έσοδα (dev: X11SP/X32
# βρέθηκαν στο βιβλίο εσόδων). Ένα κανονικό 1.1 μένει έσοδο.
class FakeClient(M.MyDataClient):
    def _pages(self, endpoint, params):
        return [ET.fromstring(docs_xml)]
with A.app.test_request_context():
    sp = A._entry_clean(dict(sale, self_pricing=True, taxes=[], payments=[{"type": "3", "amount": 186}],
                             lines=[line(150, [{"category": "category2_1", "type": "E3_102_001", "amount": 150}])]))[0]
    t32 = A._entry_clean(dict(t31, invoice_type="3.2"))[0]
    plain = A._entry_clean(dict(sale, taxes=[], payments=[{"type": "3", "amount": 186}],
                                lines=[line(150, [{"category": "category1_1", "type": "E3_561_001", "amount": 150}])]))[0]
invs = []
for i, dd in enumerate((sp, t32, plain)):
    x = ET.fromstring(M.build_invoice_xml(dd)).find("i:invoice", NS)
    ET.SubElement(x, f"{{{M.INV_NS}}}mark").text = str(400000000000001 + i)
    invs.append(ET.tostring(x, encoding="unicode"))
docs_xml = f'<RequestedDoc xmlns="{M.INV_NS}"><invoicesDoc>{"".join(invs)}</invoicesDoc></RequestedDoc>'
fc = FakeClient("u", "k", "dev", own_vat="046949583")
own_exp, _, _ = fc.request_transmitted(None, None, mark="0")
assert sorted(i.invoice_type for i in own_exp) == ["1.1", "3.2"] and all(i.is_self_issued for i in own_exp), own_exp
unc, cl, _ = fc.request_income(None, None, mark="0")
assert [(i.invoice_type, i.self_pricing) for i in unc + cl] == [("1.1", False)], unc + cl
# «Οι παραπάνω» επεκτείνεται ανά πλευρά: σε κατηγορία εξόδων ποτέ Ε3 εσόδων (8.1: E3_561_001 → XSD).
for t in ("1.5", "8.1"):
    for cat, es in db.combos_for_type(t).items():
        assert not any(e.startswith("E3_5") and e[3:6] in ("561", "562", "563", "564", "570", "595", "596", "597")
                       for e in es if cat.startswith("category2_")), (t, cat, es)

# Ελεύθερη φόρμα: χωρίς κανόνες ανά τύπο — ό,τι έχει τιμή μπαίνει στο XML όπως δόθηκε (εδώ: το αρχικό παράδειγμα,
# 14.30 με εκδότη εμάς, πληρωμή, χωρίς αντισυμβαλλόμενο, χωρίς VAT_xxx — η κανονική φόρμα τα αλλάζει/απορρίπτει).
free = {"invoice_type": "14.30", "series": "ΕΛ", "aa": "1", "issue_date": "2026-11-27", "free": True, "side": "expense",
        "issuer": {"country": "GR", "vat": "046949583"}, "payments": [{"type": "3", "amount": 124}],
        "lines": [{"net": 100, "vat_category": "1", "vat_amount": 24, "cls": [{"category": "category2_95", "amount": 100}]}]}
with A.app.test_request_context():
    d_norm, err_norm = A._entry_clean(dict(free, free=False))
    d, errors = A._entry_clean(free)
assert not errors, errors
assert not d_norm["payments"] and d_norm["counterpart"]["vat"] == "046949583"  # κανονική: χωρίς πληρωμή, εμείς αντισυμβαλλόμενος
assert d["free"] and d["issuer"]["vat"] == "046949583" and d["payments"] and not d["counterpart"]["vat"]
xml = M.build_invoice_xml(d)
assert not A.xsd_errors(xml), A.xsd_errors(xml)
inv = ET.fromstring(xml).find("i:invoice", NS)
assert [ch.tag.split("}")[1] for ch in inv] == ["issuer", "invoiceHeader", "paymentMethods", "invoiceDetails", "invoiceSummary"]
assert not [e for e in inv.iter() if (e.text or "").startswith("VAT_")]  # κανένας αυτόματος χαρακτηρισμός ΦΠΑ
# Πλευρά από την επιλογή: έσοδο → incomeClassification.
with A.app.test_request_context():
    d, errors = A._entry_clean(dict(free, side="income", lines=[dict(free["lines"][0], cls=[{"category": "category1_95", "amount": 100}])]))
assert not errors and inv is not None and "incomeClassification" in M.build_invoice_xml(d)
# Προεπισκόπηση: το XSD ελέγχεται και στην ελεύθερη (γραμμή χωρίς κατηγορία ΦΠΑ → λάθος XSD).
r = c.post("/entry/xml", data={"draft": json.dumps(dict(free, lines=[dict(free["lines"][0], vat_category="")]))}).get_json()
assert any("XSD" in e for e in r["errors"]) and r["xml"], r
# Αποθήκευση εξόδου ως πρόχειρο και ξανάνοιγμα ξεκλείδωτο.
assert c.post("/entry", data={"draft": json.dumps(free)}).status_code == 302
mark = next(x["mark"] for x in db.get_documents(cid, "expense") if x["series"] == "ΕΛ")
html = c.get(f"/entry?edit={mark}").get_data(as_text=True)
assert '"free": true' in html and 'id="free_mode"' in html

print("OK")
