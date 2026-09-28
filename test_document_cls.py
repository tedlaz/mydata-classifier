"""Έλεγχος κατανομής χαρακτηρισμών σε γραμμές / επίπεδο παραστατικού.
Εκτέλεση: python test_document_cls.py"""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ.setdefault("MYDATA_DATA_DIR", tempfile.mkdtemp())
from app import _cls_list_rows, _copy_lines, _document_cls_rows  # noqa: E402


def inv(lines, cls_info=()):
    return NS(lines=[NS(line_number=n, net_value=0.0, vat_amount=0.0, classifications=c) for n, c in lines],
              cls_info=list(cls_info))


def row(cat, typ, vat, amt):
    return {"category": cat, "type": typ, "vat_type": vat, "amount": amt}


# 1) Τοπικός: split E3 στη γραμμή 1 + ΦΠΑ ζευγαρωμένος ανά ποσό· γραμμή 9 δεν υπάρχει → παραστατικό.
local = [
    {"line_number": 1, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 60.0},
    {"line_number": 1, "classification_type": "VAT_361", "classification_category": "", "amount": 60.0},
    {"line_number": 1, "classification_type": "E3_581_001", "classification_category": "category2_6", "amount": 40.0},
    {"line_number": 9, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 5.0},
]
lines, doc = _document_cls_rows(local, inv([(1, [])]))
assert lines == {1: [row("category2_4", "E3_585_016", "VAT_361", 60.0), row("category2_6", "E3_581_001", "", 40.0)]}, lines
assert doc == [row("category2_4", "E3_585_016", "", 5.0)], doc

# 2) Ενσωματωμένος στις γραμμές· ο ασύζευκτος ΦΠΑ δεν χάνεται.
lines, doc = _document_cls_rows([], inv([
    (1, [{"type": "E3_882_001", "category": "category2_7", "amount": 10.0}, {"type": "VAT_362", "category": "", "amount": 10.0}]),
    (2, [{"type": "VAT_361", "category": "", "amount": 3.0}]),
]))
assert lines == {1: [row("category2_7", "E3_882_001", "VAT_362", 10.0)], 2: [row("", "", "VAT_361", 3.0)]}, lines
assert doc == []

# 3) Συγκεντρωτικός χωρίς γραμμές → όλα σε επίπεδο παραστατικού· οι σημαίες εξαιρούνται.
lines, doc = _document_cls_rows([], inv([(1, [])], [
    {"type": "E3_585_016", "category": "category2_4", "amount": 7.0},
    {"transaction_mode": "2"},
]))
assert lines == {} and doc == [row("category2_4", "E3_585_016", "", 7.0)], (lines, doc)

# 4) Ξεχωριστή υποβολή myDATA (με «line»): υπερισχύει του ενσωματωμένου.
lines, doc = _document_cls_rows([], inv(
    [(1, [{"type": "E3_OLD", "category": "category2_1", "amount": 1.0}])],
    [{"line": 1, "type": "E3_NEW", "category": "category2_4", "amount": 1.0}, {"line": None, "type": "E3_X", "category": "category2_5", "amount": 2.0}],
))
assert lines == {1: [row("category2_4", "E3_NEW", "", 1.0)]} and doc == [row("category2_5", "E3_X", "", 2.0)], (lines, doc)

# 5) Λίστα: ζευγάρωμα ΦΠΑ ανά γραμμή (ίδιο ποσό σε 2 γραμμές, άλλος ΦΠΑ)· σημαίες εκτός.
assert _cls_list_rows([
    {"line": 1, "type": "E3_A", "category": "category2_1", "amount": 5.0},
    {"line": 2, "type": "E3_B", "category": "category2_4", "amount": 5.0},
    {"line": 2, "type": "VAT_362", "category": "", "amount": 5.0},
    {"line": 1, "type": "VAT_361", "category": "", "amount": 5.0},
    {"transaction_mode": "2"},
]) == [row("category2_1", "E3_A", "VAT_361", 5.0), row("category2_4", "E3_B", "VAT_362", 5.0)]


# 6) Αντιγραφή: split E3 → 2 γραμμές φόρμας, ΦΠΑ αναλογικά με υπόλοιπο στην τελευταία.
split = NS(lines=[NS(line_number=1, net_value=100.0, vat_amount=24.01, vat_category=1, classifications=[
    {"type": "E3_585_016", "category": "category2_4", "amount": 70.0},
    {"type": "E3_581_001", "category": "category2_6", "amount": 30.0},
])], cls_info=[])
got = _copy_lines(split, [])
assert [(g["amount"], g["vat_amount"], g["vat_category"], g["classification_type"]) for g in got] == [
    (70.0, 16.81, "1", "E3_585_016"), (30.0, 7.2, "1", "E3_581_001")], got

# 7) Χώρα εκδότη ανά τύπο εγγραφής.
from classifications import country_allowed  # noqa: E402

assert country_allowed("14.1", "DE") and not country_allowed("14.1", "GR") and not country_allowed("14.3", "US")
assert country_allowed("14.2", "US") and not country_allowed("14.4", "FR") and not country_allowed("14.2", "GR")
assert country_allowed("13.1", "US") and country_allowed("13.4", "DE") and not country_allowed("13.1", "G1")
for t in ("13.3", "14.5", "15.1", "16.1"):  # μόνο Ελλάδα
    assert country_allowed(t, "GR") and not country_allowed(t, "DE") and not country_allowed(t, "US"), t

# 8) Pager: πρώτες/τελευταίες 2 πάντα, ±1 γύρω από την τρέχουσα, None = «…».
from app import page_numbers  # noqa: E402

assert page_numbers(8, 20) == [1, 2, None, 7, 8, 9, None, 19, 20]
assert page_numbers(1, 20) == [1, 2, None, 19, 20]
assert page_numbers(4, 20) == [1, 2, 3, 4, 5, None, 19, 20]  # μονό κενό (3) γεμίζει
assert page_numbers(20, 20) == [1, 2, None, 19, 20]
assert page_numbers(2, 5) == [1, 2, 3, 4, 5]
assert page_numbers(1, 1) == [1]

# 9) Φόρμα χαρακτηρισμού: γραμμή χωρίς ΦΠΑ → «Χωρίς χαρακτηρισμό ΦΠΑ» ακόμη κι αν ο κανόνας λέει VAT_361·
#    ο υπάρχων χαρακτηρισμός (ανά γραμμή) υπερισχύει του κανόνα.
from app import _line_classification_rows  # noqa: E402

two = NS(lines=[NS(line_number=1, net_value=100.0, vat_amount=24.0, vat_category=1, classifications=[]),
                NS(line_number=2, net_value=50.0, vat_amount=0.0, vat_category=7, classifications=[])],
         cls_info=[], total_net=150.0, total_vat=24.0)
rule = {"category": "category2_4", "type": "E3_585_016", "vat_type": "VAT_361"}
g = _line_classification_rows(two, rule)
assert [x["rows"][0]["vat_type"] for x in g] == ["VAT_361", ""], g
assert [x["vat_category"] for x in g] == ["1", "7"]
g = _line_classification_rows(two, rule, {2: [row("category2_5", "E3_585_009", "", 50.0)]})
assert g[1]["rows"] == [row("category2_5", "E3_585_009", "", 50.0)] and g[0]["rows"][0]["type"] == "E3_585_016"

# 10) Συγκεντρωτικά (ανά παραστατικό): ομάδες ΦΠΑ, κανόνες ΑΑΔΕ, XML.
from app import _per_invoice_entries, _per_invoice_errors, _vat_groups  # noqa: E402
from mydata_client import build_expenses_classification_xml  # noqa: E402

pi = NS(lines=[NS(line_number=1, net_value=100.0, vat_amount=24.0, vat_category="1", classifications=[]),
               NS(line_number=2, net_value=50.0, vat_amount=6.5, vat_category="2", classifications=[]),
               NS(line_number=3, net_value=30.0, vat_amount=7.2, vat_category="1", classifications=[]),
               NS(line_number=4, net_value=20.0, vat_amount=0.0, vat_category="8", classifications=[])],
        cls_info=[])
# 24% (2 γραμμές), 13%, χωρίς ΦΠΑ → 3 συγκεντρωτικές γραμμές
assert _vat_groups(pi) == [
    {"vat_category": "1", "net": 130.0, "vat": 31.2, "lines": 2, "classify_vat": True},
    {"vat_category": "2", "net": 50.0, "vat": 6.5, "lines": 1, "classify_vat": True},
    {"vat_category": "8", "net": 20.0, "vat": 0.0, "lines": 1, "classify_vat": False},
]
E = lambda amount, cat="category2_4": {"category": cat, "type": "E3_585_016", "amount": amount}  # noqa: E731
ok = {"1": [E(130.0)], "2": [E(50.0)], "8": [E(20.0)]}
both = {"1": "VAT_361", "2": "VAT_361"}
assert _per_invoice_errors(pi, ok, both) == []                                   # Ε3 = καθαρή κάθε ομάδας
assert _per_invoice_errors(pi, ok, {"1": "VAT_361", "2": ""})                    # λείπει ο ΦΠΑ της 13% (6,50)
assert _per_invoice_errors(pi, dict(ok, **{"2": [E(56.5)]}), {"1": "VAT_361", "2": ""}) == []
assert _per_invoice_errors(pi, {"1": [E(161.2, "category2_5")], "2": [E(56.5, "category2_5")], "8": [E(20.0, "category2_5")]},
                           {"1": "", "2": ""}) == []                             # 2.5: ο ΦΠΑ μέσα στο Ε3
assert _per_invoice_errors(pi, dict(ok, **{"8": []}), both)                      # ομάδα χωρίς Ε3
assert _per_invoice_errors(pi, {"1": [E(130.0), E(0.0)], "2": [E(50.0)], "8": [E(20.0)]}, both) == []  # split Ε3
assert _per_invoice_errors(pi, dict(ok, **{"1": [E(130.0, "category2_9")]}), both)  # 2.9: ένα Ε3, χωρίς ΦΠΑ
assert _per_invoice_errors(pi, {}, both)
ent = _per_invoice_entries(pi, ok, {"1": "VAT_361", "2": ""})
assert ent == [
    {"line_number": None, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 130.0, "vat_category": 1},
    {"line_number": None, "classification_type": "VAT_361", "classification_category": "", "amount": 130.0, "vat_category": 1, "vat_amount": 31.2},
    {"line_number": None, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 50.0, "vat_category": 2},
    {"line_number": None, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 20.0, "vat_category": 8},
], ent
x = build_expenses_classification_xml("400", ent, post_per_invoice=True)
assert x.count("<lineNumber>1</lineNumber>") == 1 and "classificationPostMode" not in x
assert x.count("<vatCategory>") == 1  # πεδία ΦΠΑ μόνο στον χαρακτηρισμό ΦΠΑ, όχι στα Ε3
assert "<amount>130.00</amount><vatAmount>31.20</vatAmount><vatCategory>1</vatCategory>" in x
y = build_expenses_classification_xml("400", [dict(ent[1], line_number=2)], post_per_invoice=False)
assert "vatAmount" not in y and "<lineNumber>2</lineNumber>" in y                # ανά γραμμή: χωρίς πεδία ΦΠΑ (337)

# 11) «Νέα εγγραφή» (ΑΑΔΕ, επιβεβαιωμένο στο dev): ΦΠΑ μόνο 13.1/13.2/13.31, με χαρακτηρισμό ΦΠΑ
#     στη γραμμή και στη σύνοψη (σφάλμα 230)· 13.3/13.4 χωρίς εκδότη (σφάλμα 205).
from mydata_client import SELF_TYPES_NO_ISSUER, SELF_TYPES_WITH_VAT, build_self_expense_invoice_xml  # noqa: E402

assert SELF_TYPES_WITH_VAT == {"13.1", "13.2", "13.31"} and SELF_TYPES_NO_ISSUER == {"13.3", "13.4"}
from mydata_client import SELF_TYPES_ISSUER_OPTIONAL  # noqa: E402

assert SELF_TYPES_ISSUER_OPTIONAL == {"13.1", "13.2", "13.31"}  # λιανικές: ΑΦΜ πωλητή προαιρετικό (dev ΑΑΔΕ)
v = build_self_expense_invoice_xml("13.1", "A", "1", "2026-09-25",
                                   [{"amount": 100.0, "classification_category": "category2_4",
                                     "classification_type": "E3_585_016", "vat_category": "1", "vat_amount": 24.0}],
                                   issuer_vat="090000045", counterpart_vat="123456789", include_payment=False)
assert v.count("VAT_361") == 2 and "paymentMethods" not in v and "<vatAmount>24.00</vatAmount>" in v
n = build_self_expense_invoice_xml("13.4", "A", "1", "2026-09-25",
                                   [{"amount": 20.0, "classification_category": "category2_5",
                                     "classification_type": "E3_585_016"}], issuer_vat="", counterpart_vat="123456789")
assert "<issuer>" not in n and "VAT_361" not in n

# 12) Αναγνώριση χαρακτηρισμού που έγινε συγκεντρωτικά στο myDATA (συμβολικές «γραμμές»).
from app import _looks_per_invoice, _remote_per_invoice  # noqa: E402

L = lambda n, net, vat, cat: NS(line_number=n, net_value=net, vat_amount=vat, vat_category=cat, classifications=[])  # noqa: E731
deh = NS(lines=[L(1, 3.55, 0.21, "3"), L(2, 84.93, 0.0, "7"), L(3, 176.03, 10.56, "3")], cls_info=[
    {"line": 1, "type": "E3_585_011", "category": "category2_4", "amount": 179.58},
    {"line": 1, "type": "VAT_361", "amount": 179.58},
    {"line": 2, "type": "E3_585_009", "category": "category2_5", "amount": 84.95},
])
assert _looks_per_invoice(deh)
assert _remote_per_invoice(deh) == (
    {"3": [{"category": "category2_4", "type": "E3_585_011", "amount": 179.58}],
     "7": [{"category": "category2_5", "type": "E3_585_009", "amount": 84.95}]},
    {"3": "VAT_361"},
)
lines, doc = _document_cls_rows([], deh)
assert lines == {} and len(doc) == 2  # εμφανίζεται σε επίπεδο παραστατικού
# Κανονικός χαρακτηρισμός ανά γραμμή → όχι συγκεντρωτικός.
per_line = NS(lines=[L(1, 100.0, 24.0, "1"), L(2, 50.0, 0.0, "7")], cls_info=[
    {"line": 1, "type": "E3_585_016", "category": "category2_4", "amount": 100.0},
    {"line": 1, "type": "VAT_361", "amount": 100.0},
    {"line": 2, "type": "E3_585_016", "category": "category2_4", "amount": 50.0},
])
assert not _looks_per_invoice(per_line) and _remote_per_invoice(per_line) is None
assert not _looks_per_invoice(NS(lines=deh.lines, cls_info=[]))

# 13) Μαζικός χαρακτηρισμός: ΦΠΑ «αυτόματα» / «χωρίς» / συγκεκριμένος, και 2.5 πάντα χωρίς ΦΠΑ.
from app import _bulk_line_entries  # noqa: E402

ln_vat, ln_zero = L(1, 100.0, 24.0, "1"), L(2, 50.0, 0.0, "7")
kinds = lambda es: [(e["classification_type"], e["amount"]) for e in es]  # noqa: E731
assert kinds(_bulk_line_entries(ln_vat, "category2_4", "E3_585_016", "auto")) == [("E3_585_016", 100.0), ("VAT_361", 100.0)]
assert kinds(_bulk_line_entries(ln_vat, "category2_4", "E3_585_016", "VAT_362")) == [("E3_585_016", 100.0), ("VAT_362", 100.0)]
assert kinds(_bulk_line_entries(ln_vat, "category2_4", "E3_585_016", "none")) == [("E3_585_016", 124.0)]
assert kinds(_bulk_line_entries(ln_vat, "category2_5", "E3_585_016", "auto")) == [("E3_585_016", 124.0)]  # 2.5 υπερισχύει
assert kinds(_bulk_line_entries(ln_zero, "category2_4", "E3_585_016", "auto")) == [("E3_585_016", 50.0)]

# 14) Προτάσεις ανά κατηγορία ΦΠΑ: μαθαίνονται από τον χαρακτηρισμό (ανά γραμμή ή συγκεντρωτικά)
#     και εφαρμόζονται ανά γραμμή/ομάδα στο επόμενο παραστατικό.
from app import _learn_patterns  # noqa: E402

inv3 = NS(lines=[L(1, 100.0, 24.0, "1"), L(2, 30.0, 0.0, "7"), L(3, 5.0, 1.2, "1")], cls_info=[])
per_line_entries = [
    {"line_number": 1, "classification_type": "E3_585_011", "classification_category": "category2_4", "amount": 100.0},
    {"line_number": 1, "classification_type": "VAT_361", "classification_category": "", "amount": 100.0},
    {"line_number": 2, "classification_type": "E3_585_009", "classification_category": "category2_5", "amount": 30.0},
    {"line_number": 3, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 5.0},
]
learned = {"1": {"category": "category2_4", "type": "E3_585_011", "vat_type": "VAT_361"},  # μεγαλύτερο ποσό κερδίζει
           "7": {"category": "category2_5", "type": "E3_585_009", "vat_type": ""}}
assert _learn_patterns(inv3, per_line_entries) == learned, _learn_patterns(inv3, per_line_entries)
pi_entries = [
    {"line_number": None, "classification_type": "E3_585_011", "classification_category": "category2_4", "amount": 105.0, "vat_category": 1},
    {"line_number": None, "classification_type": "VAT_361", "classification_category": "", "amount": 105.0, "vat_category": 1, "vat_amount": 25.2},
    {"line_number": None, "classification_type": "E3_585_009", "classification_category": "category2_5", "amount": 30.0, "vat_category": 7},
]
assert _learn_patterns(inv3, pi_entries) == learned
# Εφαρμογή: κάθε γραμμή παίρνει την πρόταση της κατηγορίας ΦΠΑ της· άγνωστη κατηγορία → βασική.
base = {"category": "category2_3", "type": "E3_585_001", "vat_type": ""}
nxt = NS(lines=[L(1, 50.0, 12.0, "1"), L(2, 20.0, 0.0, "7"), L(3, 10.0, 1.3, "2")], cls_info=[])
got = [(g["rows"][0]["category"], g["rows"][0]["type"], g["rows"][0]["vat_type"], g["rows"][0]["amount"])
       for g in _line_classification_rows(nxt, base, None, learned)]
assert got == [("category2_4", "E3_585_011", "VAT_361", 50.0),
               ("category2_5", "E3_585_009", "", 20.0),
               ("category2_3", "E3_585_001", "VAT_361", 10.0)], got

# 15) «Ορισμός ως πρόταση προμηθευτή»: προτάσεις από χαρακτηρισμό myDATA (ανά γραμμή ή συγκεντρωτικό).
import json  # noqa: E402

from app import _doc_patterns  # noqa: E402


def doc_row(src):
    lines = [{"line_number": ln.line_number, "net_value": ln.net_value, "vat_amount": ln.vat_amount,
              "vat_category": ln.vat_category} for ln in src.lines]
    return {"id": -1, "mark": "1", "lines_json": json.dumps(lines), "cls_json": json.dumps(src.cls_info)}


assert _doc_patterns(doc_row(per_line)) == {
    "1": {"category": "category2_4", "type": "E3_585_016", "vat_type": "VAT_361"},
    "7": {"category": "category2_4", "type": "E3_585_016", "vat_type": ""}}
assert _doc_patterns(doc_row(deh)) == {
    "3": {"category": "category2_4", "type": "E3_585_011", "vat_type": "VAT_361"},
    "7": {"category": "category2_5", "type": "E3_585_009", "vat_type": ""}}
assert _doc_patterns(doc_row(NS(lines=deh.lines, cls_info=[]))) == {}

# 16) Χαρακτηρισμοί της πύλης myDATA (RequestE3Info + RequestVatInfo): Ε3 ανά παραστατικό,
#     ΦΠΑ μόνο VAT_36x (τα Vat38x είναι ποσά ΦΠΑ), χωρίς αριθμό γραμμής.
from mydata_client import MyDataClient  # noqa: E402

NSX = 'xmlns="http://www.aade.gr/myDATA/invoice/v1.0"'
REPLIES = {
    "RequestE3Info": f"<RequestedE3Info {NSX}><E3Info><V_Mark>4001</V_Mark><V_Class_Category>category2_4</V_Class_Category>"
                     f"<V_Class_Type>E3_585_016</V_Class_Type><V_Class_Value>72.12</V_Class_Value></E3Info>"
                     f"<E3Info><V_Mark>4001</V_Mark><V_Class_Category>category2_7</V_Class_Category>"
                     f"<V_Class_Type>E3_882_001</V_Class_Type><V_Class_Value>314.97</V_Class_Value></E3Info></RequestedE3Info>",
    "RequestVatInfo": f"<RequestedVatInfo {NSX}><VatInfo><Mark>4001</Mark><Vat361>72.12</Vat361><Vat362>314.97</Vat362>"
                      f"<Vat381>17.31</Vat381></VatInfo><VatInfo><Mark>4999</Mark><Vat361>1</Vat361></VatInfo></RequestedVatInfo>",
}
fake = MyDataClient("u", "k")
fake.session = NS(get=lambda url, params, timeout: NS(status_code=200, content=REPLIES[url.rsplit("/", 1)[-1]].encode(), text=""))
assert fake.request_portal_classifications("01/01/2025", "31/12/2025") == {"4001": {"cls_mark": "", "entries": [
    {"line": None, "type": "E3_585_016", "category": "category2_4", "amount": 72.12},
    {"line": None, "type": "E3_882_001", "category": "category2_7", "amount": 314.97},
    {"line": None, "type": "VAT_361", "amount": 72.12},
    {"line": None, "type": "VAT_362", "amount": 314.97},
]}}

# 17) Ετήσια σύνοψη: άθροιση ανά μήνα, πιστωτικό (5.1) αφαιρείται, «τρίτων» = κατηγορία x_9.
from app import _yearly_totals  # noqa: E402

doc = lambda date_, typ, net, cls=(), **kw: dict(  # noqa: E731
    {"issue_date": date_, "invoice_type": typ, "total_net": net, "total_vat": net * 0.24, "cls_json": json.dumps(list(cls)),
     "total_withheld": 0, "total_other_taxes": 0, "total_stamp_duty": None, "total_fees": 0, "total_deductions": 0}, **kw)
y = _yearly_totals([doc("2026-03-05", "1.1", 100.0, total_withheld=20.0),
                    doc("2026-03-20", "5.1", 30.0),
                    doc("2026-03-21", "1.1", 50.0, [{"category": "category1_9", "amount": 50.0}]),
                    doc("bad", "1.1", 999.0)])
assert y[3]["net"] == 120.0 and y[3]["vat"] == 28.8 and y[3]["withheld"] == 20.0 and y[3]["third_party"] == 50.0, y[3]
assert y[4]["net"] == 0.0 and y[3]["stamp_duty"] == 0.0
# Αγορές παγίων (2.7, χωρίς τον ΦΠΑ τους) και αποσβέσεις (E3_587) — για το πραγματικό καθαρό κέρδος.
y = _yearly_totals([doc("2025-11-15", "1.1", 1047.58, [{"type": "E3_882_001", "category": "category2_7", "amount": 1047.58},
                                                       {"type": "VAT_362", "amount": 1047.58}]),
                    doc("2025-12-31", "17.5", 200.0, [{"type": "E3_587_001", "category": "category2_6", "amount": 200.0}])])
assert y[11]["assets"] == 1047.58 and y[12]["depreciation"] == 200.0 and y[12]["assets"] == 0.0, (y[11], y[12])
# Διάγραμμα: η μπάρα εξόδων χωρίζεται σε έξοδα χωρίς πάγια + αγορές παγίων (μόνο όπου υπάρχουν).
from app import _yearly_chart  # noqa: E402

row = lambda inc, exp, assets: {"label": "Νοέ. 2025", "balance": inc - exp,  # noqa: E731
                                "income": {"net": inc}, "expense": {"net": exp, "assets": assets}}
ch = _yearly_chart([row(1693.54, 1591.46, 1362.55), row(120.97, 1563.67, 0.0)])
nov, dec = ch["months"]
assert nov["op"] == 228.91 and nov["profit"] == 1464.63 and nov["op_path"] and nov["asset_path"], nov
assert dec["op"] == 1563.67 and dec["asset_path"] == "" and ch["has_assets"], dec

print("OK")
