"""Πίνακας ελέγχου: cache μεταφοράς ΦΠΑ (ακυρώνεται με κάθε αλλαγή παραστατικού) και κάρτα «Πάγια» με καθυστερημένη
φόρτωση (python -m tests.test_dashboard_lazy)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
c = A.app.test_client()
c.post("/companies/add", data={"company_name": "Δοκιμή", "use_accountant": "1", "entity_type": "legal"})
cid = db.list_companies()[0]["id"]
db.set_active_company_id(cid)


def doc(mark, kind, date, net, vat, e3=("E3_585_009", "category2_5")):
    db.upsert_document(cid, kind, {"mark": mark, "issue_date": date, "invoice_type": "1.1", "issuer_vat": "999",
                                   "total_net": net, "total_vat": vat, "total_gross": net + vat,
                                   "cls_info": [{"type": e3[0], "category": e3[1], "amount": net},
                                                {"type": "VAT_361", "category": "", "amount": net}]}, "classified")


calls = []
chain = A._vat_carry_chain
A._vat_carry_chain = lambda *a: calls.append(a) or chain(*a)
full = lambda: chain(cid, 2025, "m", 3, "2025-03-01")  # noqa: E731  (χωρίς cache)
doc("101", "expense", "2025-01-10", 1000, 240)  # πιστωτικό Ιανουαρίου (εκπίπτει: VAT_361) → μεταφέρεται
first = A._vat_auto_carry(cid, 2025, "m", 3)
assert first[0] > 0 and A._vat_auto_carry(cid, 2025, "m", 3) == first == full() and len(calls) == 1  # 2η φορά από cache
doc("102", "expense", "2025-02-10", 500, 120)  # αλλαγή → ξαναϋπολογισμός (πιστωτικό 240 + 120)
second = A._vat_auto_carry(cid, 2025, "m", 3)
assert len(calls) == 2 and second == full() == (360.0, 0.0), (first, second)
with db.get_conn() as conn:
    conn.execute("DELETE FROM documents WHERE mark = '102'")
assert A._vat_auto_carry(cid, 2025, "m", 3) == first and len(calls) == 3
print("ok carry cache")

y = A.datetime.now(A.ATHENS).year
doc("A1", "expense", f"{y}-01-05", 2000, 0, e3=("E3_882_001", "category2_7"))
page = c.get("/dashboard").data.decode()
assert 'id="db-assets"' in page and "Φόρτωση…" in page
card = c.get("/dashboard/assets").data.decode()
assert 'id="db-assets"' in card and "2.000,00 €" in card, card
print("ok assets lazy")
