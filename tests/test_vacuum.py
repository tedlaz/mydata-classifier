"""Έλεγχος: Παράμετροι → Αντίγραφα ασφαλείας → «Συμπίεση βάσης» μικραίνει το αρχείο μετά από διαγραφές.
Εκτέλεση: python -m tests.test_vacuum"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
db.set_active_company_id(cid)
with db.get_conn() as conn:
    conn.executemany("INSERT INTO mydata_archive (company_id, kind, mark, xml) VALUES (?, 'invoice', ?, ?)",
                     [(cid, str(i), "x" * 4000) for i in range(2000)])
with db.get_conn() as conn:
    conn.execute("DELETE FROM mydata_archive")
import app as A  # noqa: E402
import sqlite3  # noqa: E402

# Λήψη αντιγράφου: συμπιεσμένο, χωρίς να αλλάζει η ζωντανή βάση, και ανοίγει κανονικά.
live = A._backup_info()["size"]
data = db.backup_bytes()
assert len(data) < live / 5 and A._backup_info()["size"] == live, (len(data), live)
copy = sqlite3.connect(":memory:")
copy.deserialize(data)
assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert copy.execute("SELECT count(*) FROM companies").fetchone()[0] == 1

A.app.testing = True
size = lambda: A._backup_info()["size"]  # noqa: E731
before = size()
r = A.app.test_client().post("/backup/vacuum", follow_redirects=True).get_data(as_text=True)
assert "✔ Η βάση συμπιέστηκε" in r, r
assert size() < before / 5, (before, size())
print("OK")
