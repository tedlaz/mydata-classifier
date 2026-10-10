"""Log συνδέσεων με το myDATA: ένα αρχείο ανά ενέργεια, μόνο τα τελευταία 10, εμφάνιση στην καρτέλα της Ανάκτησης.
Εκτέλεση: python -m tests.test_mydata_log"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Δοκιμή ΑΕ", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)


def resp(status, path, text="", host="mydatapi.aade.gr"):
    return NS(ok=status < 400, status_code=status, text=text, content=text.encode(), url=f"https://{host}{path}",
              elapsed=timedelta(seconds=1.25), request=NS(method="GET", path_url=path))


status = 200


def request_income(df, dt, *_):
    A._log_mydata(resp(status, "/RequestTransmittedDocs?mark=0", "<error>λάθος κλειδί</error>"))
    # Επωνυμίες: οι κλήσεις VIES τρέχουν σε thread pool (χωρίς g) → μέσω _log_hooks.
    hooks = A._log_hooks()
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda v: hooks["response"](resp(200, f"/vies/rest-api/ms/EL/vat/{v}", host="ec.europa.eu")),
                      ["094025817"]))
    return [], [], set()


A.get_client = lambda: NS(request_income=request_income)
c = A.app.test_client()
form = {"scope": "income", "action": "fetch", "date_from": "2026-10-01", "date_to": "2026-10-31"}

c.get("/sync")  # χωρίς κλήση στο myDATA → κανένα log
assert not os.path.isdir(A._LOG_DIR) or not os.listdir(A._LOG_DIR)

for _ in range(11):
    c.post("/sync", data=form)
status = 401
c.post("/sync", data=form)
assert len(os.listdir(A._LOG_DIR)) == A.MYDATA_LOG_KEEP == 10, os.listdir(A._LOG_DIR)

logs = A.mydata_logs()
assert logs[0]["failed"] and not logs[1]["failed"], logs[:2]
assert "POST /sync · Δοκιμή ΑΕ" in logs[0]["title"] and "✗ 401" in logs[0]["body"], logs[0]
assert "ec.europa.eu/vies/rest-api/ms/EL/vat/094025817  →  ✓ 200" in logs[0]["body"], logs[0]["body"]
assert "mydatapi.aade.gr/RequestTransmittedDocs" in logs[0]["body"], logs[0]["body"]
assert "λάθος κλειδί" in logs[0]["body"] and "action=fetch" in logs[0]["body"], logs[0]["body"]

html = c.get("/sync?tab=log").get_data(as_text=True)
assert "Ιστορικό συνδέσεων" in html and "λάθος κλειδί" in html
print("OK")
