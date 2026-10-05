"""Συμφωνία Φ2: σελίδα, μενού, αθροίσματα (python -m tests.test_vat_reconcile)."""
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login
import db  # noqa: E402
import vat_return  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)
sale = {"mark": "5001", "issue_date": "2026-03-10", "issuer_vat": "1", "invoice_type": "2.1", "total_net": 150.0,
        "total_vat": 24.0, "total_gross": 174.0,
        "lines": [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"},
                  {"line_number": 2, "net_value": 50.0, "vat_amount": 0.0, "vat_category": "7"}]}
db.upsert_document(cid, "income", sale, "classified")
buy = {"mark": "4001", "issue_date": "2026-03-11", "issuer_vat": "2", "invoice_type": "1.1", "total_net": 80.0,
       "total_vat": 12.0, "total_gross": 92.0,
       "lines": [{"line_number": 1, "net_value": 80.0, "vat_amount": 12.0, "vat_category": "1"}],
       "cls_info": [{"type": "E3_102_001", "category": "category2_1", "amount": 50.0, "line": 1},
                    {"type": "VAT_361", "amount": 50.0, "line": 1},
                    {"type": "E3_585_016", "category": "category2_4", "amount": 30.0, "line": 1}]}
db.upsert_document(cid, "expense", buy, "confirmed")
c = A.app.test_client()

page = c.get("/reports/vat/reconcile?year=2026&period=m3").get_data(as_text=True)
assert page.count("✔ Συμφωνεί") == 2, page
assert "<b>303</b>" in page and "<b>361</b>" in page and "ΦΠΑ μη εκπιπτόμενος" in page and "E3_585_016" in page
# Ίδια ποσά με τη Δήλωση ΦΠΑ.
docs = [db.period_documents(cid, k, "2026-03-01", "2026-03-31") for k in ("income", "expense")]
codes, r = vat_return.compute(*docs)["codes"], vat_return.reconcile(*docs)
assert all(abs(v - codes[k]) < 0.01 for part in ("income", "expense") for k, v in r[part]["totals"].items() if k not in ("out", "nd"))
# Έξοδα: ο E3 με χαρακτηρισμό ΦΠΑ στο 361, ο άλλος εκτός· ο ΦΠΑ που δεν εκπίπτει στη στήλη nd.
rows = {x["e3"]: x["vals"] for x in r["expense"]["rows"]}
assert rows["E3_102_001"]["361"] == 50 and rows["E3_585_016"]["out"] == 30 and "nd" not in r["expense"]["totals"], rows
# Με γραμμές: ο ΦΠΑ της γραμμής χωρίς χαρακτηρισμό ΦΠΑ (30 € · 7,20 €) → «ΦΠΑ μη εκπιπτόμενος» στον λογαριασμό της.
two = dict(buy, mark="4002", total_vat=19.2, lines=[
    {"line_number": 1, "net_value": 50.0, "vat_amount": 12.0, "vat_category": "1"},
    {"line_number": 2, "net_value": 30.0, "vat_amount": 7.2, "vat_category": "1"}],
    cls_info=[{"type": "E3_102_001", "category": "category2_1", "amount": 50.0, "line": 1},
              {"type": "VAT_361", "amount": 50.0, "line": 1},
              {"type": "E3_585_016", "category": "category2_4", "amount": 30.0, "line": 2}])
r = vat_return.reconcile([], [{"mark": "4002", "invoice_type": "1.1", "total_net": 80.0, "total_vat": 19.2,
                               "lines_json": json.dumps(two["lines"]), "cls_json": json.dumps(two["cls_info"])}])
rows = {x["e3"]: x["vals"] for x in r["expense"]["rows"]}
assert rows["E3_102_001"]["381"] == 12 and rows["E3_585_016"]["nd"] == 7.2 and r["expense"]["ok"], rows
assert (r["expense"]["net"], r["expense"]["total"]) == (80, 80)
# Στο μενού, κάτω από τη Δήλωση ΦΠΑ· σύνδεσμος από τη Δήλωση.
nav = page[page.index('<nav class="nav">'):]
assert nav.index('href="/reports/vat"') < nav.index('href="/reports/vat/reconcile"') < nav.index('href="/reports"')
assert "/reports/vat/reconcile?year=2026&amp;period=m3" in c.get("/reports/vat?year=2026&period=m3").get_data(as_text=True)
print("ok")
