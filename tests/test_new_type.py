"""Νέος τύπος παραστατικού συναλλασσόμενου: όχι αυτόματος χαρακτηρισμός, ειδοποίηση
(python -m tests.test_new_type)."""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

A.app.testing = True
LINES = json.dumps([{"line_number": 1, "net_value": 100, "vat_amount": 24, "vat_category": "1"}])
cid = db.add_company({"company_name": "Α", "allow_send": True, "allow_classify": True, "allow_new": True})
with db.get_conn() as c:
    for mark, vat, t, st in (("m11", "111", "1.1", "confirmed"), ("u11", "111", "1.1", "unclassified"),
                             ("u21", "111", "2.1", "unclassified"), ("u52", "111", "5.2", "unclassified"),
                             ("x21", "222", "2.1", "unclassified")):  # 222: πρόταση χωρίς ιστορικό
        c.execute("INSERT INTO documents (company_id, kind, mark, counterparty_vat, invoice_type, lines_json, status, "
                  "total_net, total_vat, total_gross) VALUES (?, 'expense', ?, ?, ?, ?, ?, 100, 24, 124)",
                  (cid, mark, vat, t, LINES, st))
db.set_setting("active_company_id", str(cid))
GOODS = {"category": "category2_1", "type": "E3_102_001", "vat_type": "VAT_361"}
for vat in ("111", "222"):
    db.save_rule_patterns(cid, vat, {"*": GOODS})

# 1) Το 2.1 του 111 είναι νέος τύπος (γνωστός μόνο ο 1.1)· το πιστωτικό 5.2 ακολουθεί· ο 222 χωρίς ιστορικό: όλα.
cands, new = A._auto_classify_split(cid)
assert sorted(i.mark for i in cands) == ["u11", "u52", "x21"], [i.mark for i in cands]
assert [i.mark for i in new] == ["u21"] and new[0].known_types == ["1.1"]

cl = A.app.test_client()
# 2) Μαζικός «με βάση την πρόταση» (και ο αυτόματος κύκλος): το 2.1 δεν χαρακτηρίζεται, με μήνυμα.
cl.post("/bulk_classify", data={"marks": ["u21"], "action": "rules", "bulk_post_mode": "line"})
assert db.get_document(cid, "u21")["status"] == "unclassified"
page = cl.get("/invoices/auto-classify").get_data(as_text=True)
page = cl.get("/invoices/auto-classify").get_data(as_text=True)
assert "Νέος τύπος παραστατικού" in page and "u21" in page
assert "Νέοι τύποι παραστατικών" in cl.get("/dashboard").get_data(as_text=True)
rules_page = cl.get("/suppliers/111/rules").get_data(as_text=True)
assert "Νέος τύπος 2.1" in rules_page and "Ίδιες προτάσεις και για τον τύπο 2.1" in rules_page

# 3) Χειροκίνητος (ρητός) χαρακτηρισμός του 2.1 με υπηρεσίες: πρόταση «2.1|…», οι γενικές μένουν.
cl.post("/bulk_classify", data={"marks": ["u21"], "bulk_category": "category2_3", "bulk_type": "E3_585_005",
                                "bulk_vat_type": "auto", "bulk_post_mode": "line"})
assert db.get_document(cid, "u21")["status"] == "classified"
pats = db.load_all_rule_patterns(cid, "111")
assert pats["*"]["category"] == "category2_1" and "1" not in pats, pats
assert pats["2.1|1"]["category"] == pats["2.1|*"]["category"] == "category2_3", pats

# 4) Έγκριση τύπου: γίνεται γνωστός και χαρακτηρίζεται αυτόματα.
with db.get_conn() as c:
    c.execute("INSERT INTO documents (company_id, kind, mark, counterparty_vat, invoice_type, lines_json, status) "
              "VALUES (?, 'expense', 'u14', '111', '1.4', ?, 'unclassified')", (cid, LINES))
assert "u14" in [i.mark for i in A._auto_classify_split(cid)[1]]
cl.post("/suppliers/111/rules/approve-type", data={"invoice_type": "1.4"})
assert "u14" in [i.mark for i in A._auto_classify_split(cid)[0]]

# 5) Ειδοποίηση κύκλου: ο νέος μετρητής στο ntfy και στο parse του log.
assert "🆕" in A._cycle_text([{"counts": {"new_type": 2}}])
rec = A._cycle_from_log({"name": "20261101-120000-000000.log", "title": "x",
                         "body": "12:00:00  ━━ 1/1 · Α · ΑΦΜ 123 · prod\n"
                                 "12:00:01  🆕 Νέος τύπος παραστατικού — δεν χαρακτηρίστηκαν (3):"})
assert rec["companies"][0]["counts"]["new_type"] == 3
print("OK")
