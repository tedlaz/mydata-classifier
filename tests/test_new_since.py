"""«Νέα από την τελευταία φορά»: ανάκτηση με MARK χωρίς ημερομηνίες, πιάνει τα εκπρόθεσμα
(python -m tests.test_new_since)."""
import os
import tempfile
from datetime import datetime
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402
from mydata_client import ExpenseInvoice, MyDataClient, MyDataError  # noqa: E402

# (α) Client: mark=wm, ΧΩΡΙΣ dateFrom/dateTo· top = μέγιστο κάθε είδους MARK (και ακύρωσης).
c = MyDataClient("u", "k", "dev")
calls = []
xml = (b'<RequestedDoc><invoicesDoc><invoice><mark>105</mark></invoice></invoicesDoc>'
       b'<cancelledInvoicesDoc><cancelledInvoice><invoiceMark>50</invoiceMark>'
       b'<cancellationMark>110</cancellationMark></cancelledInvoice></cancelledInvoicesDoc></RequestedDoc>')
c.session.get = lambda url, params, **k: calls.append(dict(params)) or NS(status_code=200, content=xml)
c._parse_requested_doc = lambda root: [NS(mark="105", issue_date="2026-08-20")]
top, docs, refs = c.new_since("RequestDocs", 100)
assert calls == [{"mark": "100"}], calls
assert (top, docs, refs) == (110, [("105", "2026-08-20")], {"50"}), (top, docs, refs)

# (β) App: «Ανάκτηση» έως σήμερα → σημείο αναφοράς· «Νέα» φέρνει εκπρόθεσμο με παλιά ημερομηνία.
db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
today = datetime.now(A.ATHENS).strftime("%Y-%m-%d")


def inv(mark, issue):
    return ExpenseInvoice(mark=mark, uid=None, issuer_vat="123456789", issuer_name="X", invoice_type="1.1",
                          series="A", aa=mark, issue_date=issue, total_net=10.0, total_vat=2.4, total_gross=12.4)


book = [inv("400", today)]
ranges = []
fake = NS(request_unclassified_expenses=lambda df, dt: ranges.append((df, dt)) or book, last_cancelled=set(),
          request_classified_expenses=lambda df, dt: [], new_since=None)
A.get_client = lambda *a, **k: fake
A.enrich_issuer_names = lambda invs: invs
t = A.app.test_client()

# Χωρίς σημείο αναφοράς: μήνυμα σφάλματος, καμία κλήση.
with t:
    t.post("/sync", data={"date_from": today, "date_to": today, "scope": "expense", "action": "new"})
    assert any(k == "error" and "σημείο αναφοράς" in m for k, m in A.session.get("_flashes", [])), A.session
t.post("/sync", data={"date_from": today, "date_to": today, "scope": "expense", "action": "fetch"})
assert db.get_setting(A._wm_key("expense", "RequestDocs", cid)) == "400"

# Ο προμηθευτής ανεβάζει σήμερα τιμολόγιο του 2025 (MARK 450).
fake.new_since = lambda ep, mark: (450, [("450", "2025-03-15")], set()) if ep == "RequestDocs" else (mark, [], set())
book = [inv("450", "2025-03-15")]
with t:
    t.post("/sync", data={"date_from": today, "date_to": today, "scope": "expense", "action": "new"})
    flashes = A.session.get("_flashes", [])
assert ranges[-1] == ("15/03/2025", "15/03/2025"), ranges
assert db.get_document(cid, "450")["kind"] == "expense"
assert any("1 αχαρακτήριστα εκπρόθεσμα" in m and "450 (2025-03-15)" in m for _, m in flashes), flashes
assert db.get_setting(A._wm_key("expense", "RequestDocs", cid)) == "450"
assert db.get_setting(A._wm_key("expense", "RequestTransmittedDocs", cid)) == "400"
assert db.get_document(cid, "450")["late_since"]
page = t.get("/invoices?view=unclassified").get_data(as_text=True)
assert page.count("⏰ εκπρόθεσμο") == 1, page

# Εκπρόθεσμο που έρχεται ήδη χαρακτηρισμένο: καμία αναφορά στο μήνυμα.
fake.new_since = lambda ep, mark: (460, [("460", "2025-05-01")], set()) if ep == "RequestDocs" else (mark, [], set())
book = []
fake.request_classified_expenses = lambda df, dt: [inv("460", "2025-05-01")]
with t:
    t.post("/sync", data={"date_from": today, "date_to": today, "scope": "expense", "action": "new"})
    flashes = A.session.get("_flashes", [])
assert db.get_document(cid, "460")["status"] == "confirmed"
assert any("1 παραστατικά" in m for _, m in flashes) and not any("εκπρόθεσμα" in m for _, m in flashes), flashes

# (γ) Αποτυχία ανάκτησης: το MARK ΔΕΝ προχωρά (αλλιώς τα παραστατικά θα χάνονταν).
fake.new_since = lambda ep, mark: (500, [("500", "2025-04-01")], set())
fake.request_unclassified_expenses = lambda df, dt: (_ for _ in ()).throw(MyDataError("δίκτυο"))
t.post("/sync", data={"date_from": today, "date_to": today, "scope": "expense", "action": "new"})
assert db.get_setting(A._wm_key("expense", "RequestDocs", cid)) == "460"
print("OK")
