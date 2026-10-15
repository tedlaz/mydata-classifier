"""«Ανάκτηση παραστατικών» στο «Τι εκκρεμεί» όταν είναι ενεργό το «Νέα από MARK»
(python -m tests.test_dashboard_fetch_todo)."""
import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
assert db.list_companies()[0]["mark_check_hours"] == 1  # default
c = A.app.test_client()
TODO = "<b>Ανάκτηση παραστατικών</b>"
shown = lambda: TODO in c.get("/dashboard").data.decode()  # noqa: E731


def last(hours_ago):
    at = datetime.now(A.ATHENS) - timedelta(hours=hours_ago)
    db.set_setting(A._last_key("expense", cid), json.dumps({"kind": "Νέα από MARK", "at": f"{at:%d/%m/%Y %H:%M}"}))


assert not shown()  # χωρίς «Νέα από MARK»
db.set_setting(A._start_key("expense", cid), "2026-01-01")
for ep in A._MARK_ENDPOINTS["expense"]:
    db.set_setting(A._wm_key("expense", ep, cid), 100)
assert shown()  # ενεργό, καμία ανάκτηση ακόμα
last(0)
assert not shown()
last(2)
assert shown() and "2ώ" in c.get("/dashboard").data.decode()
db.update_company(cid, {**db.list_companies()[0], "mark_check_hours": "3"})
assert db.list_companies()[0]["mark_check_hours"] == 3
assert not shown()
db.update_company(cid, {**db.list_companies()[0], "mark_check_hours": "x"})
assert db.list_companies()[0]["mark_check_hours"] == 1
assert [db.check_hours(v) for v in ("0", "-1", "nan", "inf", None)] == [1] * 5
db.update_company(cid, {**db.list_companies()[0], "mark_check_hours": "0,5"})  # δεκαδικό, και με κόμμα
assert db.list_companies()[0]["mark_check_hours"] == 0.5
last(0.25)
assert not shown()
import re  # noqa: E402 — αντίστροφη μέτρηση για ανανέωση του «Τι εκκρεμεί» (~15′ ως τη λήξη)
due_in = int(re.search(r'data-due-in="(\d+)"', c.get("/dashboard").data.decode())[1])
assert 840 <= due_in <= 901, due_in
last(0.75)
assert shown() and "45λ" in c.get("/dashboard").data.decode()
assert 'name="mark_check_hours" value="0.5"' in c.get("/companies").data.decode()
print("OK")

# Πάτημα από τον Πίνακα ελέγχου: POST στο /sync («Νέα από MARK») και επιστροφή στον Πίνακα ελέγχου.
h = c.get("/dashboard").data.decode()
assert 'action="/sync"' in h and 'name="back" value="dashboard"' in h and 'name="scope" value="expense"' in h
calls = []
A._fetch_new = lambda cid_, kind, start: calls.append(kind) or "✔ Νέα παραστατικά από MARK: 0."
form = {"scope": "expense", "action": "new", "back": "dashboard", "date_from": "2026-10-15", "date_to": "2026-10-15"}
r = c.post("/sync", data=form)
assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard"), r.headers["Location"]
assert calls == ["expense"]
h = c.get("/dashboard").data.decode()
assert TODO not in h and "Νέα παραστατικά από MARK: 0" in h  # ανανεωμένη σελίδα + μήνυμα
print("OK post")
