"""Προτάσεις συναλλασσόμενου: κλειδιά τύπος/ΦΠΑ/εξαίρεση + φίλτρα ΑΑΔΕ (python -m tests.test_rule_limits)."""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True

# 1) Αναζήτηση: από το πιο ειδικό στο πιο γενικό.
P = {k: {"category": k, "type": "E3"} for k in ("2.1|1", "2.1|*", "1", "7:16", "7")}
base = {"category": "*", "type": "E3"}
pick = lambda *a: A._pick_rule(P, base, *a)[0]  # noqa: E731
assert pick("2.1", "1") == "2.1|1"
assert pick("2.1", "2") == "2.1|*"          # τύπος πριν από «όλοι οι τύποι»
assert pick("1.1", "1") == "1"
assert pick("1.1", "2") == "*"
assert pick("1.1", "7", "16") == "7:16" and pick("1.1", "7", "27") == "7" and pick("1.1", "7") == "7"
assert A._split_rule_key("2.1|7:16") == ("2.1", "7", "16") and A._split_rule_key("*") == ("", "*", "")

# 2) Όρια: συγκεκριμένος τύπος, «Όλοι» = τομή, χωρίς κινήσεις = ένωση.
L = A._rule_limits(["1.1", "2.1"])
t11, t21 = L["types"]["1.1"]["combos"], L["types"]["2.1"]["combos"]
assert "E3_102_001" in t11["category2_1"] and "E3_585_016" not in t11["category2_1"]
allr = A._row_limits(L, "", set())
assert set(allr["combos"]["category2_7"]) == set(t11["category2_7"]) & set(t21["category2_7"])
assert A._row_limits(L, "", {"1.1"})["combos"] == t21           # το 1.1 έχει δική του βασική
assert "VAT_364" not in allr["vat"] and L["types"].keys() == {"1.1", "2.1"}
assert A._type_limits("14.1")["vat"] == ["VAT_364"]
assert "VAT_364" in A._row_limits(A._rule_limits([]), "", set())["vat"]

# 3) Χαρακτηρισμός ΦΠΑ: 0%/χωρίς ΦΠΑ/2.5 χωρίς, αλλιώς υποχρεωτικός· 362 μόνο με πάγια.
vt, ok = allr["vat"], A._rule_vat_ok
assert ok("1", "category2_1", "VAT_361", vt) and not ok("1", "category2_1", "", vt)  # σφάλμα 230
assert ok("7", "category2_1", "", vt) and not ok("7", "category2_1", "VAT_361", vt)  # 0% (και 7:16)
assert ok("8", "category2_1", "", vt) and ok("1", "category2_5", "", vt)
assert ok("1", "category2_7", "VAT_362", vt) and not ok("1", "category2_7", "VAT_361", vt)
assert not ok("1", "category2_1", "VAT_362", vt) and not ok("*", "category2_1", "VAT_364", vt)
assert ok("*", "category2_1", "", vt)  # βασική «χωρίς» = αυτόματα VAT_361

# 4) Σελίδα: αποθήκευση/απόρριψη ανά τύπο, κάλυψη, και εφαρμογή στο παραστατικό.
lines = lambda *vcs: json.dumps([{"line_number": i + 1, "net_value": 100, "vat_amount": 0 if vc == "7" else 24,  # noqa: E731
                                   "vat_category": vc} for i, vc in enumerate(vcs)])
with db.get_conn() as c:
    cid = c.execute("INSERT INTO companies (company_name) VALUES ('Α')").lastrowid
    for mark, t, ln in (("m1", "1.1", lines("1", "7")), ("m2", "2.1", lines("1"))):
        c.execute("INSERT INTO documents (company_id, kind, mark, counterparty_vat, invoice_type, lines_json, status) "
                  "VALUES (?, 'expense', ?, '111', ?, ?, 'unclassified')", (cid, mark, t, ln))
db.set_setting("active_company_id", str(cid))
cl = A.app.test_client()
form = lambda *rows: {k: [r[i] for r in rows] for i, k in enumerate(("itype", "vat_category", "category", "type", "vat_type"))}  # noqa: E731
BASE = ("", "*", "category2_1", "E3_102_001", "VAT_361")
ZERO = ("", "7", "category2_5", "E3_585_016", "")
SERV = ("2.1", "*", "category2_3", "E3_585_005", "VAT_361")

r = cl.post("/suppliers/111/rules", data=form(BASE, ("2.1", "*", "category2_1", "E3_585_016", "VAT_361")))
assert r.status_code == 400 and "στον τύπο 2.1" in r.get_data(as_text=True)
r = cl.post("/suppliers/111/rules", data=form(("", "*", "", "", "VAT_361")))
assert r.status_code == 400  # η βασική είναι υποχρεωτική
r = cl.post("/suppliers/111/rules", data=form(BASE, ZERO, SERV))
assert r.status_code == 302, r.get_data(as_text=True)
assert set(db.load_all_rule_patterns(cid, "111")) == {"*", "7", "2.1|*"}

page = cl.get("/suppliers/111/rules").get_data(as_text=True)
i = page.index("Πώς θα χαρακτηριστούν")
cov = page[i:page.index("</table>", i)]
assert "2.1 · όλα τα υπόλοιπα" in cov and "⚠" not in cov, cov[:3000]

inv = A._row_to_invoice(db.get_document(cid, "m2"))
rule = db.load_rules(cid)["111"]
rows = A._line_classification_rows(inv, rule, {}, A._patterns_for("111"))
assert rows[0]["rows"][0]["category"] == "category2_3"  # 2.1 → υπηρεσίες
inv = A._row_to_invoice(db.get_document(cid, "m1"))
rows = A._line_classification_rows(inv, rule, {}, A._patterns_for("111"))
assert [g["rows"][0]["category"] for g in rows] == ["category2_1", "category2_5"]
print("OK")
