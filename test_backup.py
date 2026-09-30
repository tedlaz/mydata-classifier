"""Backup / restore ολόκληρης της βάσης (python test_backup.py)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Πριν"})
c = A.app.test_client()

# 1) Λήψη αντιγράφου.
r = c.get("/backup")
assert r.status_code == 200 and r.data[:16] == b"SQLite format 3\x00"
backup = r.data

# 2) Αλλαγή δεδομένων → επαναφορά → επανέρχονται· κρατιέται η προηγούμενη βάση.
db.delete_company(cid)
db.add_company({"company_name": "Μετά"})
safety = db.restore_bytes(backup)
assert [x["company_name"] for x in db.list_companies()] == ["Πριν"]
assert os.path.exists(os.path.join(os.environ["MYDATA_DATA_DIR"], safety))

# 3) Άκυρο αρχείο → σφάλμα, βάση ανέπαφη.
for bad in (b"garbage", b"SQLite format 3\x00" + b"\x00" * 100):
    try:
        db.restore_bytes(bad)
        raise AssertionError("έπρεπε να απορριφθεί")
    except ValueError:
        pass
r = c.post("/restore", data={"file": (__import__("io").BytesIO(b"x"), "x.db")})
assert r.status_code == 302
assert [x["company_name"] for x in db.list_companies()] == ["Πριν"]

names = lambda: [b["name"] for b in db.list_safety_backups()]  # noqa: E731

# 4) Αναίρεση: η νεότερη safety βάση είναι η «Μετά».
before_undo = names()[0]
db.restore_bytes(backup)  # ξανά «Πριν» → νέα safety με «Πριν»· η «Μετά» είναι 2η
assert c.post("/restore/safety", data={"name": names()[1]}).status_code == 302
assert [x["company_name"] for x in db.list_companies()] == ["Μετά"]
assert c.get("/parameters?tab=backup").status_code == 200

# 5) Όριο: προεπιλογή 2· ρύθμιση 3 → 3· ρύθμιση 1 → αμέσως 1.
for _ in range(4):
    db.restore_bytes(backup)
assert len(names()) == 2
c.post("/parameters/backup", data={"keep_safety_backups": "3"})  # ρύθμιση στη βάση «Πριν»
for _ in range(3):
    db.restore_bytes(db.backup_bytes())  # restore τρέχουσας → κρατά τη ρύθμιση
assert len(names()) == 3, names()
c.post("/parameters/backup", data={"keep_safety_backups": "1"})
assert len(names()) == 1
c.post("/parameters/backup", data={"keep_safety_backups": "0"})
assert db.keep_safety() == 1

# 6) Μόνο αρχεία της λίστας· λήψη / διαγραφή.
try:
    db.safety_path("../mydata.db")
    raise AssertionError("path traversal")
except ValueError:
    pass
assert c.get("/backup/safety/..%2Fmydata.db").status_code == 404
(n,) = names()
r = c.get(f"/backup/safety/{n}")
assert r.status_code == 200 and r.data[:16] == b"SQLite format 3\x00"
r.close()
c.post("/backup/safety/delete", data={"name": n})
assert names() == []

print("OK")
