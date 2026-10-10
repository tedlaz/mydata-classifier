"""Σελίδα «Ανάκτηση»: έσοδα/έξοδα/και τα δύο × ανάκτηση/διαγραφή (python -m tests.test_sync)."""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login
import db  # noqa: E402
from mydata_client import ExpenseInvoice, MyDataError  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)


def inv(mark, **kw):
    return ExpenseInvoice(mark=mark, uid=None, issuer_vat="123456789", issuer_name="X", invoice_type="1.1",
                          series="A", aa=mark, issue_date="2026-10-01", total_net=10.0, total_vat=2.4,
                          total_gross=12.4, **kw)


fail_income = False


def request_income(df, dt, *_):
    if fail_income:
        raise MyDataError("έσοδα: κάτι πήγε στραβά")
    return [inv("5001", counterpart_vat="987654321")], [], set()


fake = NS(request_unclassified_expenses=lambda df, dt, *_: [inv("4001")], request_classified_expenses=lambda df, dt, *_: [],
          last_cancelled=set(), request_income=request_income)
A.get_client = lambda *a, **k: fake
A.enrich_issuer_names = A.enrich_counterpart_names = lambda invs: invs  # χωρίς VIES/GSIS στα tests

c = A.app.test_client()
form = {"date_from": "2026-10-01", "date_to": "2026-10-31"}

# (α) Έσοδα & Έξοδα: και τα δύο βιβλία, και τα δύο «τελευταία διαστήματα»· μένεις στη σελίδα Ανάκτησης.
r = c.post("/sync", data=dict(form, scope="both", action="fetch"))
assert r.status_code == 302 and r.headers["Location"].endswith("/sync?scope=both"), r.headers.get("Location")
assert db.get_document(cid, "5001")["kind"] == "income" and db.get_document(cid, "4001")["kind"] == "expense"
assert db.get_setting(A._range_key("income")) == db.get_setting(A._range_key("expense")) == "01/10/2026 – 31/10/2026"

# (β) Σφάλμα στα έσοδα: τα έξοδα αποθηκεύονται κανονικά, μήνυμα σφάλματος για τα έσοδα.
fail_income = True
fake.request_unclassified_expenses = lambda df, dt, *_: [inv("4002")]
with c:
    c.post("/sync", data=dict(form, scope="both", action="fetch"))
    flashes = A.session.get("_flashes", [])
assert db.get_document(cid, "4002") is not None
assert ("error", "Έσοδα: έσοδα: κάτι πήγε στραβά") in flashes and any(k == "ok" and m.startswith("Έξοδα:") for k, m in flashes), flashes
fail_income = False

# (γ) Μόνο ένα βιβλίο: πας στο βιβλίο του.
assert c.post("/sync", data=dict(form, scope="income")).headers["Location"].endswith("/income")
assert c.post("/sync", data=dict(form, scope="expense")).headers["Location"].endswith("/invoices")

# (δ) Διαγραφή μόνο εσόδων: τα έξοδα μένουν· καμία ανάκτηση.
fake.request_income = lambda df, dt, *_: (_ for _ in ()).throw(AssertionError("δεν έπρεπε να γίνει ανάκτηση"))
c.post("/sync", data=dict(form, scope="income", action="delete"))
assert db.get_document(cid, "5001") is None and db.get_document(cid, "4001") is not None

# (ε) Διαγραφή και μετά ανάκτηση εξόδων: σβήνει ό,τι δεν ξαναέρχεται (4001) και φέρνει το τρέχον (4002).
assert "tab=delete" in c.post("/sync", data=dict(form, scope="expense", action="delete", tab="delete")).headers["Location"]
c.post("/sync", data=dict(form, scope="expense", action="fetch"))
assert db.get_document(cid, "4001") is None and db.get_document(cid, "4002") is not None

# (στ) Λάθος διάστημα: πίσω στη σελίδα με τις ίδιες επιλογές.
r = c.post("/sync", data={"date_from": "2026-10-31", "date_to": "2026-10-01", "scope": "income"})
assert "/sync?scope=income" in r.headers["Location"], r.headers["Location"]

# (ζ) Η σελίδα: radio βιβλίων, «Ανάκτηση» πρώτη στο μενού «Βιβλία», οι παλιές σελίδες δεν υπάρχουν.
page = c.get("/sync?scope=expense").get_data(as_text=True)
assert 'value="expense" checked' in page and "Έσοδα &amp; Έξοδα" in page
nav = page[page.index('<nav class="nav">'):]
assert nav.index('href="/sync"') < nav.index('href="/income"') < nav.index('href="/invoices"'), nav[:2000]
assert c.get("/invoices/sync").status_code == 404 and c.post("/fetch", data=form).status_code == 404

# (η) Βιβλία: η λωρίδα μηνών μόνο με τα δικά τους παραστατικά· κλικ = φίλτρο στον μήνα (ξανά κλικ = χωρίς φίλτρο).
import re  # noqa: E402

fake.request_income = request_income
c.post("/sync", data=dict(form, scope="income"))  # ξανά έσοδα στο βιβλίο (τα έσβησε το (δ))
page = c.get("/invoices?view=unclassified&f_date=2026-10").get_data(as_text=True).replace("&amp;", "&")
strip = page[page.index('class="lh-sums'):page.index('<div class="card ledger-card"')]
assert "έσοδα</span>" not in strip and "έξοδα</span>" in strip and "Καθαρή αξία<b>" not in page
on = re.findall(r'<a class="[^"]*is-on[^"]*"[^>]*href="([^"]+)"', strip)
assert len(on) == 1 and "f_date" not in on[0] and "view=unclassified" in on[0], on  # επιλεγμένος μήνας → χωρίς φίλτρο
assert re.search(r'href="/invoices\?[^"]*f_date=2026-09', strip)
page = c.get("/income").get_data(as_text=True)
strip = page[page.index('class="lh-sums'):page.index('<div class="card ledger-card"')]
assert "έσοδα</span>" in strip and "έξοδα</span>" not in strip

# (θ) Τελευταία ανάκτηση + MARK ανά βιβλίο: στη σελίδα Ανάκτησης, όχι στα βιβλία.
page = c.get("/sync").get_data(as_text=True)
last = page[page.index('class="sy-hero-last"'):page.index('</header>', page.index('class="sy-hero-last"'))]
assert last.count("Περίοδος 01/10/2026 – 31/10/2026") == 2 and "5001" in last and "4002" in last, last
assert re.search(r"\d\d/\d\d/\d{4} \d\d:\d\d", last), last  # ημερομηνία & ώρα ανάκτησης
# «Νέα από MARK» (αρχή ήδη ορισμένη): ο τύπος αλλάζει, η περίοδος δεν φαίνεται πια ως τελευταία στα έσοδα.
db.set_setting(A._start_key("income", cid), "2026-10-01")
db.set_setting(A._wm_key("income", "RequestTransmittedDocs", cid), 5001)
assert "MARK myDATA" not in last and "MARK τελευταίου παραστατικού" in last, last
fake.new_since = lambda ep, mark: (9000, [], set())  # π.χ. μόνο χαρακτηρισμός: W > MARK παραστατικού
c.post("/sync", data=dict(form, scope="income", action="new"))
page = c.get("/sync").get_data(as_text=True)
last = page[page.index('class="sy-hero-last"'):page.index('</header>', page.index('class="sy-hero-last"'))]
assert "Νέα από MARK" in last and last.count("Περίοδος 01/10/2026 – 31/10/2026") == 1, last
assert last.count("MARK myDATA") == 1 and '<span class="mono">9000</span>' in last, last
A._mark_reset(cid, "income")
assert "Τελευταία ανάκτηση" not in c.get("/invoices?view=unclassified").get_data(as_text=True)

# (ι) Καρτέλες. Τρόπος MARK (προεπιλογή), χωρίς αρχή: αρχική ανάκτηση «Από» → σήμερα (action=new)· και οι 3 καρτέλες.
page = c.get("/sync").get_data(as_text=True)
assert 'value="new"' in page and 'value="fetch"' not in page and "tab=past" in page and "tab=delete" in page
assert 'value="refetch"' not in page
# Μόνο με ημερομηνίες: διάστημα + «Ανάκτηση», χωρίς MARK και χωρίς καρτέλα προηγούμενων περιόδων.
c.post("/parameters/automation", data={"sync_mode": "dates"})
page = c.get("/sync").get_data(as_text=True)
assert 'value="fetch"' in page and "Νέα παραστατικά από MARK" not in page and "tab=past" not in page
assert 'value="delete"' in c.get("/sync?tab=delete").get_data(as_text=True)
# Διαγραφή «Όλα τα δεδομένα»: άδεια βιβλία → μόνο «Ανάκτηση» (+ ιστορικό συνδέσεων), από 1/1 του έτους.
c.post("/sync", data={"scope": "both", "action": "delete", "all": "1"})
assert not db.count_by_status(cid, "income") and not db.count_by_status(cid, "expense")
assert db.get_setting(A._range_key("expense")) is None
page = c.get("/sync?tab=delete").get_data(as_text=True)
year = A.datetime.now(A.ATHENS).year
assert 'Διαγραφή</a>' not in page and 'περιόδων</a>' not in page and 'value="delete"' not in page and f'value="{year}-01-01"' in page
print("ok")
