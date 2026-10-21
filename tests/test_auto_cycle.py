"""Αυτόματος κύκλος: ανάκτηση → χαρακτηρισμός με πρόταση → αποστολή, σε άλλη εταιρεία από την ενεργή· log, διακοπή.
Εκτέλεση: python -m tests.test_auto_cycle"""
import base64
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
assert "άλλαξαν οι άδειες" in body and "Αποστολή: εκτός ενεργειών" in body, body
assert A._cycle_settings()[2] == {b: "classify"}  # η ρύθμιση κατέβηκε μόνιμα
db.unclassify(b, ["2005"])

# 6γ) Χωρίς άδεια χαρακτηρισμού → μόνο ανάκτηση (η αποστολή χαρακτηρισμών θέλει και αυτήν).
set_flags(b, allow_send=True, allow_classify=False)
db.set_setting("auto_cycle_companies", json.dumps({b: "send"}))
A._run_cycle("δοκιμή")
assert status("2005") == "unclassified" and sent == []

# 6δ) Άδειες που αφαιρούνται ΣΤΗ ΜΕΣΗ του κύκλου (εδώ: κατά την ανάκτηση) → δεν στέλνεται τίποτα.
set_flags(b, allow_send=True, allow_classify=True)
db.set_setting("auto_cycle_companies", json.dumps({b: "send"}))
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

# 9) Αλλαγή αδειών από τις Εταιρείες → οι επιλογές του κύκλου κατεβαίνουν αμέσως (ποτέ δεν ανεβαίνουν), με μήνυμα.
A.get_client = lambda send="": NS(new_since=new_since, send_expenses_classification=send_cls)
set_flags(b, allow_send=True, allow_classify=True)
db.set_setting("auto_cycle_companies", json.dumps({b: "send", a: "classify", 999: "fetch"}))
orig_unlocked, A.auth.unlocked = A.auth.unlocked, lambda: True
with c.session_transaction() as sess:
    sess.pop("_flashes", None)
set_flags(b, allow_send=False)  # (η Β δεν έχει κλειδιά ΑΑΔΕ, οπότε το tag θα αρνιόταν· η άδεια αλλάζει απευθείας)
c.post("/companies/toggle/999/allow_send")  # οποιοδήποτε POST των Εταιρειών ενεργοποιεί την ενημέρωση
with c.session_transaction() as sess:
    msgs = [m for _, m in sess.get("_flashes", [])]
assert A._cycle_settings()[2] == {b: "classify", a: "classify"}, A._cycle_settings()  # η 999 (διαγραμμένη) έφυγε
assert any("Κύκλος ΑΕ" in m and "άλλαξαν οι άδειες" in m for m in msgs), msgs
set_flags(b, allow_send=True)
c.post("/companies/toggle/999/allow_send")  # ξανανοίγει: η επιλογή ΔΕΝ ανεβαίνει μόνη της
assert A._cycle_settings()[2][b] == "classify"
A.auth.unlocked = orig_unlocked
print("OK sync")

# 10) Η σελίδα μαθαίνει την αλλαγή από το /auto-cycle/status: μήνυμα + νέα υπογραφή ρυθμίσεων (→ ξαναφορτώνει τα select).
A.auth.unlocked = lambda: True
set_flags(b, allow_send=True)
db.set_setting("auto_cycle_companies", json.dumps({b: "send"}))
st1 = c.get("/auto-cycle/status").get_json()
assert st1["notes"] == [] and st1["settings_sig"] == c.get("/auto-cycle/status").get_json()["settings_sig"]
set_flags(b, allow_send=False)  # π.χ. σε άλλη καρτέλα
st2 = c.get("/auto-cycle/status").get_json()
assert st2["settings_sig"] != st1["settings_sig"] and any("άλλαξαν οι άδειες" in n for n in st2["notes"]), st2["notes"]
assert A._cycle_settings()[2] == {b: "classify"}
page = c.get("/auto-cycle").get_data(as_text=True)
assert 'autocomplete="off"' in page and f'name="level_{b}"' in page
A.auth.unlocked = orig_unlocked
print("OK live settings")

# 11) «Επιβεβαίωση πριν την αποστολή»: ο κύκλος χαρακτηρίζει αλλά δεν στέλνει παραστατικά του προμηθευτή.
set_flags(b, allow_send=True, allow_classify=True)
A.get_client = lambda send="": NS(new_since=new_since, send_expenses_classification=send_cls)
db.set_setting("auto_cycle_companies", json.dumps({b: "send"}))
db.set_review_vat(b, "111", True)
add_doc("2006")
sent.clear()
A._run_cycle("δοκιμή")
body = A.mydata_logs(None, A._CYCLE_DIR)[0]["body"]
assert status("2006") == "classified" and sent == [], (status("2006"), sent)
assert "⏸ Αναμένουν επιβεβαίωση (1)" in body and "2006" in body, body
assert A._cycle_rec["totals"]["held"] == 1 and A._cycle_rec["companies"][0]["counts"]["held"] == 1, A._cycle_rec
assert A._cycle_from_log(A.mydata_logs(None, A._CYCLE_DIR)[0])["companies"][0]["counts"]["held"] == 1
db.set_review_vat(b, "111", False)
assert db.review_vats(b) == set()
print("OK review")

# 12) Λίστα «Χαρακτηρισμένα»: σήμανση ⏸ και φίλτρο «μόνο αυτά».
db.set_review_vat(b, "111", True)
add_doc("2007")
db.save_local_classification(b, "2007", [{"line_number": 1, "classification_type": "E3_102_001",
                                          "classification_category": "category2_1", "amount": 50.0}])
db.set_active_company_id(b)
A.auth.unlocked = lambda: True
page = c.get("/invoices?view=classified").get_data(as_text=True)
assert "⏸ αναμένει επιβεβαίωση" in page and "μόνο αυτά" in page, page
page = c.get("/invoices?view=classified&review=1").get_data(as_text=True)
assert "εμφάνιση όλων" in page and "2006" in page, page
db.set_review_vat(b, "111", False)
assert "⏸" not in c.get("/invoices?view=classified").get_data(as_text=True)
A.auth.unlocked = orig_unlocked
db.set_active_company_id(a)
print("OK review list")

# 13) Ειδοποίηση ntfy: μόνο με κανάλι και μόνο όταν ο κύκλος έκανε κάτι· χωρίς ονόματα εταιρειών.
posts = []  # (url, headers, κείμενο)


def fake_put(url, data, headers, **kw):
    posts.append((url, headers, data.decode()))
    return NS(ok=True)


orig_put = A.requests.put
A.requests.put = fake_put
add_doc("2008")
A._run_cycle("δοκιμή")  # χωρίς κανάλι → τίποτα
assert posts == [], posts
c.post("/auto-cycle/ntfy/new")
topic = db.get_setting("ntfy_topic")
assert topic.startswith("mydata-") and f"ntfy://ntfy.sh/{topic}?display=myDATA" in c.get("/auto-cycle").get_data(as_text=True)
add_doc("2009")
A._run_cycle("δοκιμή")
url, headers, text = posts[0]
assert base64.b64decode(headers["Title"][10:-2]).decode().startswith("myDATA · ") and headers["Tags"] == "white_check_mark", headers
assert len(posts) == 1 and url == "https://ntfy.sh/" + topic, posts
assert text == "1️⃣  1🏷️  1📤\n\n✋\n\n1️⃣  1❓", text  # μόνο Α/Α και «αριθμός+εικονίδιο», χωρίς ονόματα
A._run_cycle("δοκιμή")  # τίποτα νέο → καμία ειδοποίηση
assert len(posts) == 1, posts
A._run_cycle("χειροκίνητα")  # «Έναρξη τώρα» → ειδοποίηση και χωρίς τίποτα νέο
assert len(posts) == 2 and posts[1][2] == "—\n\n✋\n\n1️⃣  1❓", posts  # «—» = τίποτα έγινε

# Επιβεβαίωση: το «απεσταλμένο» 2009 βρίσκεται χαρακτηρισμένο στην ανάκτηση του κύκλου → μετρά confirmed.
A.get_client = lambda send="": NS(send_expenses_classification=send_cls, new_since=lambda *x: (
    db.mark_confirmed(db.get_document(b, "2009")["id"]), new_since(*x))[1])
tot = A._run_cycle("χειροκίνητα")
assert tot["confirmed"] == 1 and A._cycle_rec["companies"][0]["counts"]["confirmed"] == 1, tot
assert A.mydata_logs(None, A._CYCLE_DIR) and A._cycle_from_log(A.mydata_logs(None, A._CYCLE_DIR)[0])[
    "companies"][0]["counts"]["confirmed"] == 1
A.get_client = lambda send="": NS(new_since=new_since, send_expenses_classification=send_cls)

assert A._cycle_text([{"counts": {}}] * 9 + [{"counts": {"new": 1}}, {"counts": {"new": 2}}]) == "🔟  1📥\n\n11.  2📥"
# Σύνοψη: χωρίς μηδενικά· εταιρεία χωρίς τίποτα παραλείπεται (ο Α/Α μένει)· κάτω όσες θέλουν ενέργεια.
assert A._cycle_text([{"counts": {"new": 2, "failed": 0}}, {"counts": {}}, {"counts": {"failed": 1, "no_rule": 3}},
                      {"counts": {"held": 1}}]) == "1️⃣  2📥\n\n3️⃣  1❌\n\n✋\n\n3️⃣  3❓\n\n4️⃣  1⏸️"
c.post("/auto-cycle/ntfy/off")
assert A._ntfy("x") is False and len(posts) == 3
A.requests.put = orig_put
print("OK ntfy")
