"""Log συνδέσεων με το myDATA: ένα αρχείο ανά ενέργεια, μόνο τα τελευταία 10, εμφάνιση στην καρτέλα της Ανάκτησης.
Εκτέλεση: python -m tests.test_mydata_log"""
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace as NS

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True
import db  # noqa: E402
import gsis  # noqa: E402
import vies  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Δοκιμή ΑΕ", "AADE_VAT_NUMBER": "046949583"})
db.set_active_company_id(cid)


def resp(status, path, text="", host="mydatapi.aade.gr"):
    return NS(ok=status < 400, status_code=status, text=text, content=text.encode(), url=f"https://{host}{path}",
              elapsed=timedelta(seconds=1.25), request=NS(method="GET", path_url=path))


status = 200


def request_income(df, dt, *_):
    A._log_mydata(resp(status, "/RequestTransmittedDocs?mark=0", "<error>λάθος κλειδί</error>"))
    # Επωνυμίες: μία συγκεντρωτική γραμμή, όχι μία ανά ΑΦΜ.
    A.enrich_counterpart_names([NS(counterpart_vat=v, counterpart_name=None) for v in ("094025817", "111111111")])
    return [], [], set()


vies.lookup_name = lambda v: "ΒΡΕΘΗΚΕ" if v == "094025817" else None
gsis.gsis_available = lambda: False
A.save_names = lambda names: None


A.get_client = lambda: NS(request_income=request_income)
c = A.app.test_client()
form = {"scope": "income", "action": "fetch", "date_from": "2026-10-01", "date_to": "2026-10-31"}

c.get("/sync")  # χωρίς κλήση στο myDATA → κανένα log
assert not os.path.isdir(A._LOG_DIR) or not os.listdir(A._LOG_DIR)
A.load_names = lambda: {}

for _ in range(11):
    c.post("/sync", data=form)
status = 401
c.post("/sync", data=form)
assert len(os.listdir(A._log_dir(cid))) == A.MYDATA_LOG_KEEP == 10, os.listdir(A._log_dir(cid))

logs = A.mydata_logs(cid)
assert logs[0]["failed"] and not logs[1]["failed"], logs[:2]
assert "POST /sync · Δοκιμή ΑΕ" in logs[0]["title"] and "✗ 401" in logs[0]["body"], logs[0]
assert "Επωνυμίες πελατών (VIES/ΑΑΔΕ): 2 ΑΦΜ ελέγχθηκαν → ✓ 1 επιβεβαιώθηκαν (VIES 1, ΑΑΔΕ 0), 1 χωρίς" in logs[0]["body"]
assert not logs[1]["failed"] and "ec.europa.eu" not in logs[0]["body"], logs[1]
assert "mydatapi.aade.gr/RequestTransmittedDocs" in logs[0]["body"], logs[0]["body"]
assert "λάθος κλειδί" in logs[0]["body"] and "action=fetch" in logs[0]["body"], logs[0]["body"]

html = c.get("/sync?tab=log").get_data(as_text=True)
assert "Ιστορικό συνδέσεων" in html and "λάθος κλειδί" in html

# Άλλη εταιρεία: δικό της ιστορικό.
db.set_active_company_id(db.add_company({"company_name": "Άλλη ΑΕ"}))
assert "λάθος κλειδί" not in c.get("/sync?tab=log").get_data(as_text=True)
c.post("/sync", data=form)
assert len(os.listdir(A._log_dir(db.get_active_company_id()))) == 1 and len(os.listdir(A._log_dir(cid))) == 10
print("OK")
