"""Αυτόματος κύκλος: ανάκτηση → χαρακτηρισμός με πρόταση → αποστολή, σε άλλη εταιρεία από την ενεργή· log, διακοπή.
Εκτέλεση: python -m tests.test_auto_cycle"""
import json
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
os.environ["MYDATA_NO_AUTO_CYCLE"] = "1"
import app as A  # noqa: E402

real_get_client = A.get_client

A.app.testing = True
import db  # noqa: E402

db.init_db()
flags = {"allow_send": True, "allow_classify": True}
a = db.add_company({"company_name": "Ενεργή ΑΕ", **flags})
b = db.add_company({"company_name": "Κύκλος ΑΕ", "AADE_VAT_NUMBER": "123456789", **flags})
c2 = db.add_company({"company_name": "Χωρίς αρχή ΑΕ", **flags})
db.set_active_company_id(a)
for cid in (a, b):  # «Νέα από MARK» έτοιμο (Αρχή + W)
    db.set_setting(A._start_key("expense", cid), "2026-01-01")
    for ep in A._MARK_ENDPOINTS["expense"]:
        db.set_setting(A._wm_key("expense", ep, cid), "1000")
db.save_rule(b, "111", "E3_102_001", "category2_1", "VAT_361")
for mark, vat in (("2001", "111"), ("2002", "999")):
    db.upsert_document(b, "expense", {
        "mark": mark, "issue_date": "2026-01-10", "issuer_vat": vat, "invoice_type": "1.1",
        "total_net": 100.0, "total_vat": 24.0, "total_gross": 124.0,
        "lines": [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"}],
    }, "unclassified")

sent, seen = [], []


def new_since(ep, mark):
    seen.append(A._active_company_id())
    A._log_mydata(NS(ok=True, status_code=200, text="", content=b"", url=f"https://mydatapi.aade.gr/{ep}?mark={mark}",
                     elapsed=A.timedelta(seconds=0.5), request=NS(method="GET", path_url=f"/{ep}?mark={mark}")))
    return mark, [], set()


def send_cls(mark, cls, post_per_invoice=False):
    sent.append((A._active_company_id(), mark))
    return {"status": "Success", "classification_mark": "900" + mark, "errors": []}


A.get_client = lambda send="": NS(new_since=new_since, send_expenses_classification=send_cls)
A.enrich_names = lambda *a, **k: None
status = lambda m: db.get_document(b, m)["status"]  # noqa: E731

# 1) Κύκλος στη Β (και στη Γ χωρίς αρχική ανάκτηση) ενώ ενεργή είναι η Α.
db.set_setting("auto_cycle_companies", json.dumps({b: "send", c2: "send", 999: "fetch"}))
tot = A._run_cycle("δοκιμή")
assert db.get_active_company_id() == a and set(seen) == {b}, seen
assert status("2001") == "sent" and status("2002") == "unclassified"
assert sent == [(b, "2001")], sent
assert tot["classified"] == tot["sent"] == tot["no_rule"] == tot["skipped"] == 1 and tot["failed"] == 0, tot
log = A.mydata_logs(None, A._CYCLE_DIR)[0]
assert not log["failed"] and "στάλθηκαν 1" in log["title"], log["title"]
body = log["body"]
for s in ("Κύκλος ΑΕ", "mydatapi.aade.gr/RequestDocs", "✓ 2001", "category2_1/E3_102_001 100.00", "MARK χαρακτηρισμού 9002001",
          "Χωρίς πρόταση", "2002 · 2026-01-10 · 999", "Χωρίς αρχή ΑΕ", "αρχική ανάκτηση", "id 999"):
    assert s in body, (s, body)

# 2) Απρόσμενο σφάλμα → ✗ + traceback στο log, ο κύκλος τελειώνει κανονικά.
db.set_setting("auto_cycle_companies", json.dumps({b: "send"}))
orig, A.db.load_names = A.db.load_names, lambda: 1 / 0
tot = A._run_cycle("δοκιμή")
A.db.load_names = orig
log = A.mydata_logs(None, A._CYCLE_DIR)[0]
assert tot["failed"] == 1 and log["failed"] and "ZeroDivisionError" in log["body"] and "Traceback" in log["body"], log

# 3) Διακοπή μετά την ανάκτηση της Β → τίποτα δεν χαρακτηρίζεται, η Α δεν εκτελείται.
db.upsert_document(b, "expense", {"mark": "2003", "issue_date": "2026-01-11", "issuer_vat": "111", "invoice_type": "1.1",
                                  "total_net": 10.0, "total_vat": 0.0, "total_gross": 10.0,
                                  "lines": [{"line_number": 1, "net_value": 10.0, "vat_amount": 0.0, "vat_category": "7"}]},
                   "unclassified")
db.set_setting("auto_cycle_companies", json.dumps({b: "send", a: "send"}))
seen.clear()
A.get_client = lambda send="": NS(new_since=lambda *x: (A._cycle_stop.set(), new_since(*x))[1], send_expenses_classification=send_cls)
A._run_cycle("δοκιμή")
log = A.mydata_logs(None, A._CYCLE_DIR)[0]
assert status("2003") == "unclassified" and set(seen) == {b}, seen
assert "Διακόπηκε" in log["body"] and "Δεν εκτελέστηκαν: Ενεργή ΑΕ" in log["body"] and "διακόπηκε" in log["title"], log

# 4) Σελίδα, κατάσταση (JSON με νέες γραμμές), ρυθμίσεις, λήψη log.
c = A.app.test_client()
st = c.get("/auto-cycle/status").get_json()
assert st["phase"] in ("locked", "disabled") and not st["running"] and st["lines"] and st["summary"]["stopped"], st
assert c.get(f"/auto-cycle/status?since={st['lines'][-1]['n']}").get_json()["lines"] == []
assert "Κύκλος ΑΕ" in c.get("/auto-cycle").get_data(as_text=True)
c.post("/auto-cycle/settings", data={"on": "1", "minutes": "15", f"level_{b}": "send"})
assert A._cycle_settings() == (True, 15, {b: "send"})
c.post("/auto-cycle/settings", data={"minutes": "2"})  # εκτός ορίων → καμία αλλαγή
assert A._cycle_settings() == (True, 15, {b: "send"})
assert c.get(f"/auto-cycle/log/{log['name']}").status_code == 200
assert c.get("/auto-cycle/log/..%2Fx.log").status_code == 404
print("OK")

# 5) Παύση / συνέχιση: η παύση σβήνει τον χρονοπρογραμματισμό και διακόπτει κύκλο που τρέχει.
A._cycle["running"] = True
assert c.post("/auto-cycle/toggle").get_json() == {"ok": True, "on": False}
assert not A._cycle_settings()[0] and A._cycle_stop.is_set()
A._cycle["running"] = False
A._cycle_stop.clear()
assert c.post("/auto-cycle/toggle").get_json()["on"] and A._cycle_settings()[0]
assert (A._cycle_due() - A.datetime.now(A.ATHENS)).total_seconds() > 14 * 60  # μετρά από τώρα
print("OK toggle")

# 6) ΑΣΦΑΛΕΙΑ — ενέργειες ανά εταιρεία, ποτέ πάνω από τις άδειές της.
import requests  # noqa: E402


def set_flags(cid, **kw):
    db.update_company(cid, {**next(x for x in db.list_companies() if x["id"] == cid), **kw})


def add_doc(mark):
    db.upsert_document(b, "expense", {
        "mark": mark, "issue_date": "2026-01-12", "issuer_vat": "111", "invoice_type": "1.1",
        "total_net": 50.0, "total_vat": 12.0, "total_gross": 62.0,
        "lines": [{"line_number": 1, "net_value": 50.0, "vat_amount": 12.0, "vat_category": "1"}]}, "unclassified")


A.get_client = lambda send="": NS(new_since=new_since, send_expenses_classification=send_cls)
A._cycle_stop.clear()
sent.clear()
db.delete_setting("auto_cycle_last")

# 6α) «Μόνο ανάκτηση»: ούτε χαρακτηρισμός ούτε αποστολή, ακόμα κι αν η εταιρεία τα επιτρέπει.
add_doc("2005")
db.set_setting("auto_cycle_companies", json.dumps({b: "fetch"}))
A._run_cycle("δοκιμή")
body = A.mydata_logs(None, A._CYCLE_DIR)[0]["body"]
assert status("2005") == "unclassified" and sent == [], (status("2005"), sent)
assert "Ενέργειες: Μόνο ανάκτηση" in body and "Χαρακτηρισμός: εκτός ενεργειών" in body, body

# 6β) Επιλογή «αποστολή» αλλά η εταιρεία δεν επιτρέπει αποστολή → μόνο τοπικός χαρακτηρισμός.
set_flags(b, allow_send=False)
db.set_setting("auto_cycle_companies", json.dumps({b: "send"}))
A._run_cycle("δοκιμή")
body = A.mydata_logs(None, A._CYCLE_DIR)[0]["body"]
assert status("2005") == "classified" and sent == [], (status("2005"), sent)
assert "δεν το επιτρέπουν" in body and "Αποστολή: εκτός ενεργειών" in body, body
db.unclassify(b, ["2005"])

# 6γ) Χωρίς άδεια χαρακτηρισμού → μόνο ανάκτηση (η αποστολή χαρακτηρισμών θέλει και αυτήν).
set_flags(b, allow_send=True, allow_classify=False)
A._run_cycle("δοκιμή")
assert status("2005") == "unclassified" and sent == []

# 6δ) Άδειες που αφαιρούνται ΣΤΗ ΜΕΣΗ του κύκλου (εδώ: κατά την ανάκτηση) → δεν στέλνεται τίποτα.
set_flags(b, allow_send=True, allow_classify=True)
A.get_client = lambda send="": NS(new_since=lambda *x: (set_flags(b, allow_send=False), new_since(*x))[1],
                                  send_expenses_classification=send_cls)
A._run_cycle("δοκιμή")
assert status("2005") == "classified" and sent == [], (status("2005"), sent)
set_flags(b, allow_send=True)

# 6ε) Πύλη στον client: εκτός βήματος αποστολής, μόνο GET· καμία αποστολή.
A._make_client = lambda send="": NS(entity_vat=None, session=requests.Session())
for level in ("fetch", "classify"):
    with A._cycle_ctx(b, level=level):
        cl = real_get_client()
        for call in (lambda: cl.session.post("http://127.0.0.1:9/SendExpensesClassification"),
                     lambda: cl.session.request("PUT", "http://127.0.0.1:9/x"),
                     lambda: real_get_client(send="classify")):
            try:
                call()
                raise AssertionError(f"δεν μπλοκαρίστηκε ({level})")
            except PermissionError:
                pass
with A._cycle_ctx(b, level="send"):  # στο βήμα αποστολής ο client δεν πειράζεται
    assert "guarded" not in getattr(real_get_client(send="classify").session.request, "__name__", "")

# 6στ) Ρυθμίσεις: επίπεδο πάνω από τις άδειες κόβεται στην αποθήκευση· χαλασμένη/παλιά ρύθμιση = μόνο ανάκτηση.
set_flags(b, allow_send=False)
c.post("/auto-cycle/settings", data={"minutes": "15", f"level_{b}": "send", f"level_{a}": "fetch", f"level_{c2}": ""})
assert A._cycle_settings()[2] == {b: "classify", a: "fetch"}, A._cycle_settings()
db.set_setting("auto_cycle_companies", json.dumps([b]))
assert A._cycle_settings()[2] == {b: "fetch"}
db.set_setting("auto_cycle_companies", json.dumps({b: "admin"}))
assert A._cycle_settings()[2] == {b: "fetch"}
print("OK security")

# 7) Δομημένο log: κάρτα κύκλου → υποκάρτα ανά εταιρεία (σύνολα, κατάσταση, γραμμές μόνο της εταιρείας).
set_flags(b, allow_send=True, allow_classify=True)
A.get_client = lambda send="": NS(new_since=new_since, send_expenses_classification=send_cls)
db.unclassify(b, ["2005"])
db.set_setting("auto_cycle_companies", json.dumps({b: "send", c2: "fetch"}))
A._run_cycle("δοκιμή")
cy = A._cycle_history()[0]
assert cy["ended"] and not cy["stopped"] and cy["totals"]["sent"] == 1 and cy["name"].endswith(".log"), cy
co_b, co_c = cy["companies"]
assert (co_b["name"], co_b["status"], co_b["level"]) == ("Κύκλος ΑΕ", "ok", "send"), co_b
assert co_b["counts"]["sent"] == 1 and co_b["counts"]["no_rule"] == 1, co_b["counts"]
assert any("✓ 2005" in ln["t"] for ln in co_b["lines"]) and not any("Χωρίς αρχή" in ln["t"] for ln in co_b["lines"])
assert co_c["status"] == "skipped" and any("αρχική ανάκτηση" in ln["t"] for ln in co_c["lines"]), co_c
assert not any(ln["k"] == "sep" for co in cy["companies"] for ln in co["lines"])
st = c.get("/auto-cycle/status").get_json()
assert [x["status"] for x in st["companies"]] == ["ok", "skipped"] and {ln["c"] for ln in st["lines"]} <= {0, 1, 2}
page = c.get("/auto-cycle").get_data(as_text=True)
assert 'class="cy"' in page and 'class="co"' in page and "Χωρίς αρχή ΑΕ" in page and "στάλθηκαν" in page
print("OK cards")

# 8) Παλαιότεροι κύκλοι χωρίς .json: ανασύνθεση από το .log σε κάρτα ανά εταιρεία, ίδια με την αρχική.
for cy in A._cycle_history(100):
    path = os.path.join(A._CYCLE_DIR, cy["name"][:-4] + ".json")
    os.rename(path, path + ".bak")
    old = A._cycle_history(100)[[x["name"] for x in A._cycle_history(100)].index(cy["name"])]
    os.rename(path + ".bak", path)
    assert [x["name"] for x in old["companies"]] == [x["name"] for x in cy["companies"]], (old, cy)
    for o, n in zip(old["companies"], cy["companies"]):
        assert (o["status"], o["counts"], o["level"], o["vat"]) == (n["status"], n["counts"], n["level"], n["vat"]), (o, n)
        assert [ln["t"] for ln in o["lines"]] == [ln["t"] for ln in n["lines"]], (o["lines"], n["lines"])
    assert (old["stopped"], old["totals"], old["trigger"]) == (cy["stopped"], cy["totals"], cy["trigger"])
print("OK legacy")
