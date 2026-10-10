"""«Νέα παραστατικά από MARK»: ανάκτηση με MARK χωρίς ημερομηνίες, πιάνει τα εκπρόθεσμα
(python -m tests.test_new_since)."""
import json
import os
import tempfile
from datetime import datetime, timedelta
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

# (β) App: πρώτο πάτημα από το «Από» έως σήμερα → Αρχή + W· μετά εκπρόθεσμα, κενά από διαγραφές, αλλαγή αρχής.
db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
now = datetime.now(A.ATHENS)
today = now.strftime("%Y-%m-%d")
yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
start = now.strftime("%Y-%m-01")
gr = lambda iso: "/".join(reversed(iso.split("-")))  # noqa: E731
W = lambda kind="expense", ep="RequestDocs": db.get_setting(A._wm_key(kind, ep, cid))  # noqa: E731
gaps = lambda: json.loads(db.get_setting(A._gaps_key("expense", cid)) or "[]")  # noqa: E731


def inv(mark, issue):
    return ExpenseInvoice(mark=mark, uid=None, issuer_vat="123456789", issuer_name="X", invoice_type="1.1",
                          series="A", aa=mark, issue_date=issue, total_net=10.0, total_vat=2.4, total_gross=12.4)


def post(**kw):
    with t:
        t.post(kw.pop("url", "/sync"), data=dict({"date_from": today, "date_to": today, "scope": "expense"}, **kw))
        return A.session.get("_flashes", [])


book = [inv("400", today)]
ranges = []
fake = NS(request_unclassified_expenses=lambda df, dt: ranges.append((df, dt)) or book, last_cancelled=set(),
          request_classified_expenses=lambda df, dt: [], request_income=lambda df, dt: ([], [], set()),
          new_since=lambda ep, mark: (_ for _ in ()).throw(AssertionError("όχι new_since την πρώτη φορά")))
A.get_client = lambda *a, **k: fake
A.enrich_issuer_names = A.enrich_counterpart_names = lambda invs: invs
t = A.app.test_client()

# Απλή «Ανάκτηση» (Έως χθες): ελεύθερο διάστημα, κανένα MARK.
post(action="fetch", date_from=yesterday, date_to=yesterday)
assert ranges[-1] == (gr(yesterday), gr(yesterday)) and W() is None, ranges
page = t.get("/sync?scope=expense").get_data(as_text=True)
assert "Αρχική ανάκτηση: από την ημερομηνία" in page and 'value="new"' in page and "Νέα παραστατικά από MARK</b>" not in page

# Πρώτο πάτημα: από το «Από» (1η του μήνα) έως σήμερα, όποιο κι αν είναι το «Έως».
post(action="new", date_from=start, date_to=yesterday)
assert ranges[-1] == (gr(start), gr(today)), ranges
assert W() == W("expense", "RequestTransmittedDocs") == "400" and db.get_setting(A._start_key("expense", cid)) == start
assert "αρχή <span class=\"mono\">" + gr(start) in t.get("/sync?scope=expense").get_data(as_text=True)

# Άδειο βιβλίο εσόδων: W από το μέγιστο MARK της εταιρείας (κοινή αρίθμηση ΑΑΔΕ).
post(action="new", scope="income", date_from=start)
assert W("income", "RequestTransmittedDocs") == "400"

# Ο προμηθευτής ανεβάζει σήμερα τιμολόγιο του 2025 (MARK 450).
fake.new_since = lambda ep, mark: (450, [("450", "2025-03-15")], set()) if ep == "RequestDocs" else (mark, [], set())
book = [inv("450", "2025-03-15")]
flashes = post(action="new")
assert ranges[-1] == ("15/03/2025", "15/03/2025"), ranges
assert db.get_document(cid, "450")["kind"] == "expense"
assert any("1 αχαρακτήριστα εκπρόθεσμα" in m and "450 (2025-03-15)" in m for _, m in flashes), flashes
assert W() == "450" and W("expense", "RequestTransmittedDocs") == "400"
assert db.get_document(cid, "450")["late_since"]
assert t.get("/invoices?view=unclassified").get_data(as_text=True).count("⏰ εκπρόθεσμο") == 1

# Εκπρόθεσμο που έρχεται ήδη χαρακτηρισμένο: καμία αναφορά στο μήνυμα.
fake.new_since = lambda ep, mark: (460, [("460", "2025-05-01")], set()) if ep == "RequestDocs" else (mark, [], set())
book = []
fake.request_classified_expenses = lambda df, dt: [inv("460", "2025-05-01")]
flashes = post(action="new")
assert db.get_document(cid, "460")["status"] == "confirmed"
assert any("MARK: 1" in m for _, m in flashes) and not any("εκπρόθεσμα" in m for _, m in flashes), flashes

# «Ανάκτηση» με Έως χθες: το W δεν αλλάζει.
post(action="fetch", date_from=yesterday, date_to=yesterday)
assert W() == "460"

# Διαγραφή μετά την Αρχή → κενό· πριν από την Αρχή → τίποτα. Το επόμενο πάτημα ξαναφέρνει το κενό.
fake.new_since = lambda ep, mark: (mark, [], set())
post(action="delete", date_from=start, date_to=today)
post(action="delete", date_from="2020-01-01", date_to="2020-01-31")
assert gaps() == [[start, today]], gaps()
flashes = post(action="new")
assert (gr(start), gr(today)) in ranges[-2:] and gaps() == [], (ranges, gaps())
assert any("ξαναήρθαν 1 διαστήματα" in m for _, m in flashes), flashes

# (γ) Αποτυχία: W και κενά μένουν ως είχαν (αλλιώς τα παραστατικά θα χάνονταν).
post(action="delete", date_from=today, date_to=today)
fake.new_since = lambda ep, mark: (500, [("500", "2025-04-01")], set())
fake.request_unclassified_expenses = lambda df, dt: (_ for _ in ()).throw(MyDataError("δίκτυο"))
post(action="new")
assert W() == "460" and gaps() == [[today, today]], (W(), gaps())

# «Αλλαγή αρχής»: το επόμενο πάτημα ξεκινά από το νέο «Από».
fake.request_unclassified_expenses = lambda df, dt: ranges.append((df, dt)) or []
post(url="/sync/mark-reset")
assert W() is None and db.get_setting(A._start_key("expense", cid)) is None and gaps() == []
post(action="new", date_from=yesterday)
assert ranges[-1] == (gr(yesterday), gr(today)) and db.get_setting(A._start_key("expense", cid)) == yesterday
print("OK")
