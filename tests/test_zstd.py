"""Έλεγχος: τα XML μένουν zlib μέχρι 1000· τότε εκπαιδεύεται λεξικό zstd (στη βάση), όλα ξανασυμπιέζονται,
διαβάζονται ίδια (και από SQL unz) και τα νέα γράφονται zstd.
Εκτέλεση: python -m tests.test_zstd"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test"})
xml = lambda i: (f"<InvoicesDoc><invoice><issuer><vatNumber>099999999</vatNumber></issuer><mark>{i}</mark>"  # noqa: E731
                 f"<invoiceSummary><totalNetValue>{i * 7 % 1000}.50</totalNetValue></invoiceSummary></invoice></InvoicesDoc>")
rows = lambda r: [{"kind": "invoice", "mark": str(i), "invoice_mark": None, "endpoint": None,  # noqa: E731
                   "issue_date": None, "invoice_type": "1.1", "issuer_vat": None, "counterpart_vat": None,
                   "event_date": None, "amount": None, "xml": xml(i)} for i in r]
blobs = lambda: [r[0] for r in db.sqlite3.connect(db.DB_PATH).execute("SELECT xml FROM mydata_archive")]  # noqa: E731

with db.get_conn() as conn:
    db._archive_upsert(conn, cid, rows(range(999)), "now")
db.init_db()
assert db._ACTIVE is None and all(b[:4] != db._ZSTD_MAGIC for b in blobs())  # λίγα → zlib

with db.get_conn() as conn:
    db._archive_upsert(conn, cid, rows(range(999, 1500)), "now")
db.init_db()
assert db._ACTIVE is not None and all(b[:4] == db._ZSTD_MAGIC for b in blobs())  # λεξικό + ξανασυμπίεση
with db.get_conn() as conn:
    assert [r["xml"] for r in conn.execute("SELECT xml FROM mydata_archive ORDER BY CAST(mark AS INT)")] == \
        [xml(i) for i in range(1500)]
    assert conn.execute("SELECT count(*) FROM mydata_archive WHERE instr(unz(xml), '<mark>1234<')").fetchone()[0] == 1
    db._archive_upsert(conn, cid, rows([2000]), "now")
assert all(b[:4] == db._ZSTD_MAGIC for b in blobs())

# Νέα «εκκίνηση»: τα λεξικά διαβάζονται από τη βάση.
db._DICTS.clear()
db._ACTIVE = None
db.init_db()
with db.get_conn() as conn:
    assert conn.execute("SELECT xml FROM mydata_archive WHERE mark = '2000'").fetchone()["xml"] == xml(2000)
print("OK")
