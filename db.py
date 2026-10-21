"""
Μόνιμη αποθήκευση σε SQLite (βιβλίο εσόδων-εξόδων).

Αντικαθιστά τα προηγούμενα JSON stores (companies.json, supplier_names_*,
supplier_rules_*, pending_classifications_*) και το per-session pickle cache.

- Εταιρείες / λογιστής / ρυθμίσεις: πίνακες companies / accountant / settings.
- Συναλλασσόμενοι (ΑΦΜ → επωνυμία): πίνακας suppliers, κοινός για όλες τις εταιρείες.
- Προτάσεις χαρακτηρισμού: πίνακας supplier_rules, ανά εταιρεία.
- Παραστατικά (το ledger): πίνακας documents με στήλη status
  (unclassified → classified → sent → confirmed), scoped ανά company_id.
- Τοπικός χαρακτηρισμός ανά γραμμή: πίνακας classifications.

Η βάση ζει στο <MYDATA_DATA_DIR>/mydata.db (ίδια σύμβαση με τα υπόλοιπα stores).
"""

from contextlib import closing
import json
import os
import re as _re
import sqlite3
import zlib
from compression import zstd
from datetime import UTC, datetime

import auth

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("MYDATA_DATA_DIR", _BASE_DIR)
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "mydata.db")


# Λεξικά zstd της βάσης (πίνακας zdicts, dict_id → λεξικό), φορτώνονται στο init_db. Εκπαιδεύονται από τα
# XML της ίδιας της βάσης (ΑΦΜ/επωνυμίες επαναλαμβάνονται) — γι' αυτό ζουν στη βάση, όχι στο git/installer.
_DICTS: dict[int, zstd.ZstdDict] = {}
_ACTIVE: zstd.ZstdDict | None = None  # το νεότερο· None = νέα βάση με λίγα XML → zlib
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ZSTD_OPTS = {zstd.CompressionParameter.compression_level: 9, zstd.CompressionParameter.checksum_flag: 1}


def _z(s: str) -> bytes:
    """XML (raw_xml / cls_xml / mydata_archive.xml) → zstd BLOB με λεξικό (~15x μικρότερο), ή zlib (~4x)
    όσο η βάση δεν έχει λεξικό."""
    if _ACTIVE is None:
        return zlib.compress(s.encode())
    return zstd.compress(s.encode(), options=_ZSTD_OPTS, zstd_dict=_ACTIVE)


# ponytail: κάθε BLOB θεωρείται συμπιεσμένο XML (εκτός zdicts, που διαβάζεται χωρίς row_factory)· αν προστεθεί άλλη, ανά στήλη.
def _unz(v):
    if not isinstance(v, bytes):
        return v
    if v[:4] != _ZSTD_MAGIC:
        return zlib.decompress(v).decode()
    did = zstd.get_frame_info(v).dictionary_id
    return zstd.decompress(v, zstd_dict=_DICTS[did] if did else None).decode()


def _load_dicts() -> None:
    global _ACTIVE
    raw = sqlite3.connect(DB_PATH)
    try:
        rows = raw.execute("SELECT dict FROM zdicts ORDER BY rowid").fetchall()
    finally:
        raw.close()
    _DICTS.clear()
    _DICTS.update((d.dict_id, d) for d in (zstd.ZstdDict(r[0]) for r in rows))
    _ACTIVE = zstd.ZstdDict(rows[-1][0]) if rows else None


_XML_COLS = (("documents", "raw_xml"), ("documents", "cls_xml"), ("mydata_archive", "xml"))


# ponytail: εκπαίδευση μία φορά (≥1000 XML)· νέο λεξικό (π.χ. αν αλλάξει πολύ το περιεχόμενο) = νέα εγγραφή
# zdicts — ποτέ αλλαγή/διαγραφή παλιάς, τα BLOB της τη χρειάζονται.
def _zstd_migrate(conn) -> int:
    """Αν η βάση δεν έχει λεξικό και έχει αρκετά XML: εκπαίδευση από δείγμα 2000. Με λεξικό: ξανασυμπίεση
    όσων BLOB είναι ακόμα zlib (όλα την πρώτη φορά· μετά, μόνο αν διακόπηκε). Επιστρέφει πόσα άλλαξαν."""
    if _ACTIVE is None:
        union = " UNION ALL ".join(f"SELECT {c} AS x FROM {t} WHERE typeof({c}) = 'blob'" for t, c in _XML_COLS)
        sample = [r["x"].encode() for r in conn.execute(f"SELECT x FROM ({union}) ORDER BY random() LIMIT 2000")]
        if len(sample) < 1000:
            return 0
        d = zstd.train_dict(sample, 112_640)
        conn.execute("INSERT INTO zdicts (id, dict) VALUES (?, ?)", (d.dict_id, d.dict_content))
        conn.commit()
        _load_dicts()
    return sum(conn.execute(f"UPDATE {t} SET {c} = z(unz({c})) WHERE typeof({c}) = 'blob' "
                            f"AND substr({c}, 1, 4) <> ?", (_ZSTD_MAGIC,)).rowcount for t, c in _XML_COLS)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.create_function("z", 1, lambda s: _z(s) if isinstance(s, str) else s, deterministic=True)
    conn.create_function("unz", 1, _unz, deterministic=True)
    # Τα συμπιεσμένα XML διαβάζονται πάντα ως str: οι αναγνώστες δεν ξέρουν τη συμπίεση.
    conn.row_factory = lambda cur, row: sqlite3.Row(cur, tuple(map(_unz, row)))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def backup_bytes() -> bytes:
    """Συνεπές στιγμιότυπο όλης της βάσης (online backup API· ασφαλές με WAL), συμπιεσμένο (VACUUM στο
    αντίγραφο της μνήμης — όχι στη ζωντανή βάση): το αρχείο λήψης χωρίς τον κενό χώρο των διαγραφών."""
    src, mem = get_conn(), sqlite3.connect(":memory:")
    try:
        src.backup(mem)
        mem.execute("VACUUM")
        return mem.serialize()
    finally:
        src.close()
        mem.close()


def vacuum() -> None:
    """Συμπίεση της βάσης: το SQLite δεν μικραίνει το αρχείο μόνο του μετά από διαγραφές. Πρώτα αδειάζει
    το WAL στο κύριο αρχείο, μετά VACUUM (ξαναγράφει τη βάση χωρίς κενές σελίδες) και ξανά άδειασμα του WAL."""
    conn = get_conn()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def restore_bytes(data: bytes, keep: bool = True) -> str | None:
    """Αντικαθιστά τη βάση με το backup `data`. Πρώτα κρατά αντίγραφο της τρέχουσας
    (mydata-before-restore-*.db στον φάκελο δεδομένων) και επιστρέφει το όνομά του.
    keep=False (πρώτη εκκίνηση, άδεια βάση): χωρίς αντίγραφο."""
    if data[:16] != b"SQLite format 3\x00":
        raise ValueError("Το αρχείο δεν είναι βάση SQLite.")
    mem = sqlite3.connect(":memory:")
    try:
        # Bytes 18-19 = 2 (WAL): η in-memory βάση δεν ανοίγει σε WAL → rollback journal.
        mem.deserialize(data[:18] + b"\x01\x01" + data[20:])
        try:
            ok = mem.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            has_companies = mem.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies'"
            ).fetchone()
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            raise ValueError("Το αρχείο είναι κατεστραμμένο.")
        if not has_companies:
            raise ValueError("Το αρχείο δεν είναι backup της εφαρμογής.")

        safety = f"{_SAFETY_PREFIX}{datetime.now():%Y%m%d-%H%M%S-%f}.db" if keep else None
        live = get_conn()
        try:
            if safety:
                with closing(sqlite3.connect(os.path.join(_DATA_DIR, safety))) as dst:
                    live.backup(dst)
            mem.backup(live)
        finally:
            live.close()
    finally:
        mem.close()
    global _restores
    _restores += 1  # άκυρα τα cache με documents_rev
    init_db()  # παλαιότερο backup → τρέχουσες μεταπτώσεις schema
    prune_safety_backups()
    auth.lock()  # το backup έχει δικό του χρήστη/κλειδί → νέο login με τον κωδικό του backup
    return safety


_SAFETY_PREFIX = "mydata-before-restore-"


def keep_safety() -> int:
    """Πόσα αυτόματα αντίγραφα πριν από επαναφορά κρατούνται (ρύθμιση, προεπιλογή 2)."""
    try:
        return max(1, int(get_setting("keep_safety_backups") or 2))
    except ValueError:
        return 2


def list_safety_backups() -> list[dict]:
    """Τα αντίγραφα πριν από επαναφορά, νεότερο πρώτα (το όνομα περιέχει timestamp)."""
    names = sorted(
        (n for n in os.listdir(_DATA_DIR) if n.startswith(_SAFETY_PREFIX) and n.endswith(".db")),
        reverse=True,
    )
    out = []
    for n in names:
        st = os.stat(os.path.join(_DATA_DIR, n))
        out.append({"name": n, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime)})
    return out


def safety_path(name: str) -> str:
    """Path ενός αντιγράφου μόνο αν είναι στη λίστα (όχι αυθαίρετα paths από τη φόρμα)."""
    if name not in {b["name"] for b in list_safety_backups()}:
        raise ValueError("Το αντίγραφο δεν βρέθηκε.")
    return os.path.join(_DATA_DIR, name)


def prune_safety_backups() -> None:
    for b in list_safety_backups()[keep_safety():]:
        try:
            os.remove(os.path.join(_DATA_DIR, b["name"]))
        except OSError:
            pass  # κλειδωμένο (Windows)· σβήνεται στο επόμενο prune


_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name           TEXT NOT NULL,
    aade_user_id           TEXT,
    aade_subscription_key  TEXT,
    aade_vat_number        TEXT,
    mydata_env             TEXT DEFAULT 'prod',
    use_accountant         INTEGER DEFAULT 0,
    allow_cancel           INTEGER DEFAULT 0,
    allow_send             INTEGER DEFAULT 0,  -- αποστολές στο myDATA (κλειστές εξ ορισμού)
    allow_classify         INTEGER DEFAULT 0,  -- χαρακτηρισμοί (κλειστοί εξ ορισμού)
    allow_new              INTEGER DEFAULT 0,  -- «Νέα εγγραφή» / μηνιαίες (κλειστές εξ ορισμού)
    locked                 INTEGER DEFAULT 0,  -- από «αρχείο πελάτη»: άδειες μόνιμα κλειστές
    vat_period             TEXT DEFAULT 'm',  -- περίοδος ΦΠΑ: 'm' = μηνιαία, 'q' = τριμηνιαία
    entity_type            TEXT DEFAULT 'legal',  -- 'sole' = ατομική, 'legal' = νομικό πρόσωπο
    birth_year             INTEGER,              -- ατομική: έτος γέννησης (μειωμένοι συντελεστές νέων)
    mark_check_hours       REAL DEFAULT 1        -- «Ανάκτηση παραστατικών» στο «Τι εκκρεμεί» κάθε Ν ώρες (δεκαδικό)
);

CREATE TABLE IF NOT EXISTS accountant (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    user_id           TEXT,
    subscription_key  TEXT,
    env               TEXT DEFAULT 'prod'
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Συναλλασσόμενοι: ΚΟΙΝΟΙ για όλες τις εταιρείες (ΑΦΜ → επωνυμία).
CREATE TABLE IF NOT EXISTS suppliers (
    vat              TEXT PRIMARY KEY,
    name             TEXT,
    -- Εκδότης εκτός Ελλάδας (π.χ. ενδοκοινοτικές 14.x): η ΑΑΔΕ θέλει χώρα και διεύθυνση.
    country          TEXT,
    street           TEXT,
    postal_code      TEXT,
    city             TEXT
);

-- Προτάσεις χαρακτηρισμού ΑΝΑ ΕΤΑΙΡΕΙΑ, συναλλασσόμενο ΚΑΙ κατηγορία ΦΠΑ γραμμής: ένα
-- παραστατικό μπορεί να έχει π.χ. γραμμές 24% (έξοδα με ΦΠΑ) και 0% (χωρίς δικαίωμα έκπτωσης)·
-- στο επόμενο παραστατικό κάθε γραμμή παίρνει την πρόταση της κατηγορίας ΦΠΑ της.
-- vat_category '*' = βασική πρόταση του συναλλασσόμενου (όταν δεν ταιριάζει κατηγορία ΦΠΑ).
CREATE TABLE IF NOT EXISTS supplier_rules (
    company_id     INTEGER NOT NULL,
    vat            TEXT NOT NULL,
    vat_category   TEXT NOT NULL,
    rule_category  TEXT,
    rule_type      TEXT,
    rule_vat_type  TEXT,
    rule_post_mode INTEGER DEFAULT 0,  -- 0 = ανά γραμμή, 1 = συγκεντρωτικά· ίδιο σε όλες τις γραμμές του ΑΦΜ
    updated_at     TEXT,
    PRIMARY KEY (company_id, vat, vat_category),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

-- Συναλλασσόμενοι που δήλωσε η ίδια η εταιρεία (Φόρμα καταχώρησης), ακόμα κι αν δεν υπάρχει ακόμα
-- παραστατικό τους στο τοπικό βιβλίο (π.χ. έσοδο που στάλθηκε αμέσως, πριν την επόμενη «Ανάκτηση»).
CREATE TABLE IF NOT EXISTS company_suppliers (
    company_id  INTEGER NOT NULL,
    vat         TEXT NOT NULL,
    PRIMARY KEY (company_id, vat),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'expense',
    mark                TEXT NOT NULL,
    issue_date          TEXT,
    counterparty_vat    TEXT,
    invoice_type        TEXT,
    series              TEXT,
    aa                  TEXT,
    total_net           REAL,
    total_vat           REAL,
    total_gross         REAL,
    is_self_issued      INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'unclassified',
    source              TEXT DEFAULT 'rest',
    classification_mark TEXT,
    lines_json          TEXT,
    cls_json            TEXT,
    local_action        TEXT DEFAULT 'classify',
    draft_json          TEXT,
    sent_at             TEXT,
    confirmed_at        TEXT,
    updated_at          TEXT,
    UNIQUE (company_id, mark),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

-- Μετρητής αλλαγών παραστατικών ανά εταιρεία (documents_rev): cache υπολογισμών όπως η μεταφορά ΦΠΑ.
CREATE TABLE IF NOT EXISTS doc_rev (company_id INTEGER PRIMARY KEY, rev INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS doc_rev_ins AFTER INSERT ON documents BEGIN
    INSERT INTO doc_rev VALUES (NEW.company_id, 1) ON CONFLICT(company_id) DO UPDATE SET rev = rev + 1;
END;
CREATE TRIGGER IF NOT EXISTS doc_rev_upd AFTER UPDATE ON documents BEGIN
    INSERT INTO doc_rev VALUES (NEW.company_id, 1) ON CONFLICT(company_id) DO UPDATE SET rev = rev + 1;
END;
CREATE TRIGGER IF NOT EXISTS doc_rev_del AFTER DELETE ON documents BEGIN
    INSERT INTO doc_rev VALUES (OLD.company_id, 1) ON CONFLICT(company_id) DO UPDATE SET rev = rev + 1;
END;

CREATE TABLE IF NOT EXISTS classifications (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id              INTEGER NOT NULL,
    line_number              INTEGER,
    classification_type      TEXT,
    classification_category  TEXT,
    vat_type                 TEXT,
    amount                   REAL,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

-- Πρότυπα «Νέας εγγραφής» ανά εταιρεία (π.χ. ΕΦΚΑ, ενοίκιο): η φόρμα χωρίς ημ/νία και Α/Α.
CREATE TABLE IF NOT EXISTS expense_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    draft_json  TEXT NOT NULL,
    UNIQUE (company_id, name),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

-- Επιτρεπόμενοι συνδυασμοί χαρακτηρισμού ανά τύπο παραστατικού (ΑΑΔΕ «Συνδυασμοί
-- χαρακτηρισμών»): invoice_type → category2_x → E3_xxx. Κενός = χωρίς περιορισμό.
CREATE TABLE IF NOT EXISTS classification_combos (
    invoice_type  TEXT NOT NULL,
    category      TEXT NOT NULL,
    e3_type       TEXT NOT NULL,
    PRIMARY KEY (invoice_type, category, e3_type)
);

-- Κουβάς: ό,τι επέστρεψε το myDATA (RequestDocs/RequestTransmittedDocs) και ΔΕΝ είναι στο documents
-- (ακυρωμένα, διακίνηση, παραστατικά άλλων λήπτων…) μαζί με τους χαρακτηρισμούς/ακυρώσεις ΤΟΥΣ.
-- Ένα παραστατικό (και ό,τι αναφέρεται σε αυτό) είναι ή εδώ ή στο documents. Όλο το myDATA: view mydata_all.
CREATE TABLE IF NOT EXISTS mydata_archive (
    company_id      INTEGER NOT NULL,
    kind            TEXT NOT NULL,   -- invoice | expenses_cls | income_cls | cancel
    mark            TEXT NOT NULL,   -- MARK παραστατικού / classificationMark / cancellationMark
    invoice_mark    TEXT,            -- παραστατικό στο οποίο αναφέρεται (cls/cancel)
    endpoint        TEXT,
    issue_date      TEXT,
    invoice_type    TEXT,
    issuer_vat      TEXT,
    counterpart_vat TEXT,
    xml             TEXT NOT NULL,
    first_seen      TEXT,
    last_seen       TEXT,
    PRIMARY KEY (company_id, kind, mark),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

-- Λεξικά συμπίεσης zstd των XML (id = dict_id μέσα σε κάθε BLOB). Μόνο προσθήκη, ποτέ αλλαγή/διαγραφή.
CREATE TABLE IF NOT EXISTS zdicts (id INTEGER PRIMARY KEY, dict BLOB NOT NULL);
"""


# Στήλες που ίσως λείπουν από παλιότερη βάση → προστίθενται με ALTER (idempotent).
_DOCUMENT_COLUMNS = {
    "local_action": "TEXT DEFAULT 'classify'",
    "draft_json": "TEXT",
    "source": "TEXT DEFAULT 'rest'",
    # Τρόπος χαρακτηρισμού: 0 = ανά γραμμή, 1 = ανά παραστατικό (postPerInvoice).
    "cls_post_mode": "INTEGER DEFAULT 0",
    # Λοιπά σύνολα της σύνοψης (invoiceSummary) — για την ετήσια σύνοψη των αναφορών.
    "total_withheld": "REAL",      # φόροι παρακράτησης
    "total_other_taxes": "REAL",   # λοιποί φόροι
    "total_stamp_duty": "REAL",    # χαρτόσημο / ψηφιακό τέλος συναλλαγής
    "total_fees": "REAL",          # τέλη
    "total_deductions": "REAL",    # κρατήσεις
    # Ανάλυση φόρων/κρατήσεων: [{"type","category","base","amount"}] (για εμφάνιση).
    "taxes_json": "TEXT",
    # Μηνιαία εγγραφή από πρότυπο: μία ανά (εταιρεία, πρότυπο, μήνας) — unique index στο init_db.
    "template_id": "INTEGER",
    "period": "TEXT",
    # Ολόκληρο το XML του παραστατικού από το myDATA (προβολή «Όλα τα στοιχεία»).
    "raw_xml": "TEXT",
    # Ημερομηνία που εντοπίστηκε ως εκπρόθεσμο («Νέα παραστατικά από MARK»).
    "late_since": "TEXT",
    # Απεσταλμένος/επιβεβαιωμένος χαρακτηρισμός πριν τον αλλάξουμε τοπικά — η «Επαναφορά» γυρνά σε αυτόν.
    "prev_state": "TEXT",
    # Η ΕΝΕΡΓΗ εγγραφή χαρακτηρισμού του myDATA (MARK = classification_mark), όπως ήρθε· οι υπόλοιπες
    # στον κουβά (mydata_archive) — βλ. archive_sync.
    "cls_xml": "TEXT",
    # myDATA UID: ένα παραστατικό μία φορά, ακόμα κι αν ξαναδιαβιβάστηκε με νέο MARK (ux_documents_uid).
    "uid": "TEXT",
}
# Στήλες του κουβά που προστέθηκαν μετά τη δημιουργία του.
_ARCHIVE_COLUMNS = {"event_date": "TEXT", "amount": "REAL", "uid": "TEXT"}
_UID_RE = _re.compile(r"<(?:\w+:)?uid>([^<]+)</")


def _uid_of(xml: str | None) -> str | None:
    m = _UID_RE.search(xml or "")
    return m and m.group(1).strip()


def _mark_no(mark: str | None) -> int:
    return int(mark) if (mark or "").isdigit() else 0


def _drop_uid_duplicates(conn) -> None:
    """Ίδιο UID με πολλά MARK (το myDATA δέχτηκε ξαναδιαβίβαση): στα βιβλία μένει το νεότερο MARK,
    τα υπόλοιπα στον κουβά (όπως τα ακυρωμένα) — «myDATA λοιπά» → «Διπλές διαβιβάσεις»."""
    for g in conn.execute("SELECT company_id, uid FROM documents WHERE uid IS NOT NULL "
                          "GROUP BY company_id, uid HAVING COUNT(*) > 1").fetchall():
        marks = [r["mark"] for r in conn.execute("SELECT mark FROM documents WHERE company_id = ? AND uid = ?",
                                                 (g["company_id"], g["uid"]))]
        where, params = "company_id = ? AND uid = ? AND mark <> ?", (g["company_id"], g["uid"], max(marks, key=_mark_no))
        _to_archive(conn, where, params)
        conn.execute(f"DELETE FROM documents WHERE {where}", params)
EXTRA_TOTALS = ("total_withheld", "total_other_taxes", "total_stamp_duty", "total_fees", "total_deductions")
# Ανά παραστατικό, ο χαρακτηρισμός ΦΠΑ κρατά κατηγορία ΦΠΑ + ποσό ΦΠΑ της ομάδας.
_CLASSIFICATION_COLUMNS = {
    "vat_category": "INTEGER",
    "vat_amount": "REAL",
}


def _copy_rules_per_company(conn: sqlite3.Connection, select_sql: str) -> None:
    """Αντιγράφει κοινές προτάσεις (vat, vat_category, category, type, vat_type) σε κάθε
    εταιρεία με παραστατικά του ΑΦΜ· όσες δεν αντιστοιχούν πάνε στην ενεργή (ή στην πρώτη)."""
    companies = [r["id"] for r in conn.execute("SELECT id FROM companies ORDER BY id")]
    if not companies:
        return
    active = conn.execute("SELECT value FROM settings WHERE key = 'active_company_id'").fetchone()
    fallback = int(active["value"]) if active and str(active["value"]).isdigit() else 0
    if fallback not in companies:
        fallback = companies[0]
    conn.execute(
        "INSERT OR IGNORE INTO supplier_rules (company_id, vat, vat_category, rule_category, "
        "rule_type, rule_vat_type, updated_at) "
        "SELECT COALESCE(d.company_id, ?), r.vat, r.vat_category, r.rule_category, r.rule_type, "
        f"r.rule_vat_type, ? FROM ({select_sql}) AS r "
        "LEFT JOIN (SELECT DISTINCT company_id, counterparty_vat FROM documents) AS d "
        "ON d.counterparty_vat = r.vat",
        (fallback, datetime.now(UTC).isoformat(timespec="seconds")),
    )


def init_db() -> None:
    """Δημιουργεί το schema αν δεν υπάρχει (καλείται στην εκκίνηση) και μεταπτώνει
    παλιότερη βάση (global suppliers, νέες στήλες documents)."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _load_dicts()  # και μετά από επαναφορά backup: τα λεξικά της νέας βάσης

        # Μετάβαση suppliers: από per-company (company_id) σε ΚΟΙΝΟ πίνακα ανά ΑΦΜ.
        sup_cols = {r["name"] for r in conn.execute("PRAGMA table_info(suppliers)")}
        if "company_id" in sup_cols:
            conn.executescript(
                "CREATE TABLE suppliers_new (vat TEXT PRIMARY KEY, name TEXT, "
                "rule_type TEXT, rule_category TEXT, rule_vat_type TEXT);"
                "INSERT INTO suppliers_new (vat, name, rule_type, rule_category, rule_vat_type) "
                "SELECT vat, MAX(name), MAX(rule_type), MAX(rule_category), MAX(rule_vat_type) "
                "FROM suppliers GROUP BY vat;"
                "DROP TABLE suppliers;"
                "ALTER TABLE suppliers_new RENAME TO suppliers;"
            )
            sup_cols = {r["name"] for r in conn.execute("PRAGMA table_info(suppliers)")}

        # Μετάβαση προτάσεων: από ΚΟΙΝΕΣ (ανά ΑΦΜ) σε ανά εταιρεία. Κάθε πρόταση πάει σε όσες
        # εταιρείες έχουν παραστατικά με το ΑΦΜ· αλλιώς στην ενεργή (ή στην πρώτη) εταιρεία.
        rule_cols = {r["name"] for r in conn.execute("PRAGMA table_info(supplier_rules)")}
        if "company_id" not in rule_cols:
            conn.execute("ALTER TABLE supplier_rules RENAME TO supplier_rules_old")
            conn.executescript(_SCHEMA)  # ξαναδημιουργεί τον supplier_rules με το νέο σχήμα
            _copy_rules_per_company(
                conn,
                "SELECT vat, vat_category, rule_category, rule_type, rule_vat_type "
                "FROM supplier_rules_old",
            )
            conn.execute("DROP TABLE supplier_rules_old")
        if "rule_type" in sup_cols:  # παλιά βασική πρόταση στον suppliers → γραμμή '*'
            _copy_rules_per_company(
                conn,
                "SELECT vat, '*' AS vat_category, rule_category, rule_type, rule_vat_type "
                "FROM suppliers WHERE rule_type IS NOT NULL",
            )
            for col in ("rule_type", "rule_category", "rule_vat_type"):
                conn.execute(f"ALTER TABLE suppliers DROP COLUMN {col}")

        # documents: νέες στήλες + κατάργηση counterparty_name (το όνομα βγαίνει από suppliers).
        doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        for col, decl in _DOCUMENT_COLUMNS.items():
            if col not in doc_cols:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")
        # suppliers: χώρα/διεύθυνση· συμπλήρωση από τις «Νέες εγγραφές» με εκδότη εκτός Ελλάδας.
        if "country" not in sup_cols:
            for col in SUPPLIER_ADDRESS:
                conn.execute(f"ALTER TABLE suppliers ADD COLUMN {col} TEXT")
            for r in conn.execute("SELECT draft_json FROM documents WHERE draft_json IS NOT NULL").fetchall():
                d = json.loads(r["draft_json"])
                if d.get("issuer_vat") and (d.get("issuer_country") or "GR") != "GR":
                    _save_supplier_address(conn, d["issuer_vat"], d.get("issuer_name"), d["issuer_country"],
                                           *(d.get("issuer_" + k) for k in SUPPLIER_ADDRESS[1:]))
        # companies: άδεια ακυρώσεων (επικίνδυνη ενέργεια — κλειστή εξ ορισμού).
        comp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
        if "allow_cancel" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN allow_cancel INTEGER DEFAULT 0")
        for col in ("allow_send", "allow_classify", "allow_new", "locked", "accountant_required"):  # και οι υπάρχουσες ξεκινούν κλειστές
            if col not in comp_cols:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} INTEGER DEFAULT 0")
        conn.execute("UPDATE companies SET allow_cancel = 0 WHERE allow_cancel AND NOT allow_send")  # ακυρώσεις μόνο με αποστολή
        if "vat_period" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN vat_period TEXT DEFAULT 'm'")
        if "entity_type" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN entity_type TEXT DEFAULT 'legal'")
        if "birth_year" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN birth_year INTEGER")
        if "mark_check_hours" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN mark_check_hours REAL DEFAULT 1")
        if "rule_post_mode" not in {r["name"] for r in conn.execute("PRAGMA table_info(supplier_rules)")}:
            conn.execute("ALTER TABLE supplier_rules ADD COLUMN rule_post_mode INTEGER DEFAULT 0")
        cls_cols = {r["name"] for r in conn.execute("PRAGMA table_info(classifications)")}
        for col, decl in _CLASSIFICATION_COLUMNS.items():
            if col not in cls_cols:
                conn.execute(f"ALTER TABLE classifications ADD COLUMN {col} {decl}")
        if "counterparty_name" in doc_cols:
            conn.execute("ALTER TABLE documents DROP COLUMN counterparty_name")
        # Ενσωματωμένος χαρακτηρισμός εξόδων: MARK χαρακτηρισμού = MARK παραστατικού (παλιές εγγραφές με κενό).
        if "classification_mark" in doc_cols:
            conn.execute(
                "UPDATE documents SET classification_mark = mark WHERE kind = 'expense' AND status = 'confirmed' "
                "AND COALESCE(classification_mark, '') = '' AND COALESCE(cls_json, '[]') NOT IN ('', '[]') "
                "AND (source = 'manual' OR lines_json LIKE '%\"has_expenses_classification\": true%')"
            )
        # Αναφορές/ΦΠΑ: παραστατικά εταιρείας + είδους σε διάστημα ημερομηνιών (αλλιώς σάρωση όλου του πίνακα).
        if "issue_date" in doc_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_documents_period ON documents(company_id, kind, issue_date)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_template_period "
            "ON documents(company_id, template_id, period) WHERE template_id IS NOT NULL"
        )
        tpl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(expense_templates)")}
        if "monthly_day" not in tpl_cols:
            conn.execute("ALTER TABLE expense_templates ADD COLUMN monthly_day INTEGER")
        # Προγραμματισμένες εγγραφές: κάθε πόσους μήνες (1, 2, 3, 6, 12) και από ποιον μήνα του έτους.
        if "every_months" not in tpl_cols:
            conn.execute("ALTER TABLE expense_templates ADD COLUMN every_months INTEGER NOT NULL DEFAULT 1")
            conn.execute("ALTER TABLE expense_templates ADD COLUMN start_month INTEGER NOT NULL DEFAULT 1")
        # Διαβιβασμένες «Νέες εγγραφές» που έμειναν σημειωμένες ως πρόχειρα (παλιό finalize_created).
        if "status" in doc_cols:
            conn.execute("UPDATE documents SET local_action = 'classify' "
                         "WHERE local_action = 'create' AND status IN ('sent', 'confirmed')")
        arc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mydata_archive)")}
        for col, decl in _ARCHIVE_COLUMNS.items():
            if col not in arc_cols:
                conn.execute(f"ALTER TABLE mydata_archive ADD COLUMN {col} {decl}")
        # UID από το XML όσων δεν το έχουν ακόμα (παλιές εγγραφές), μετά ένα UID = ένα παραστατικό στη βάση.
        for t, c, extra in (("documents", "raw_xml", ""), ("mydata_archive", "xml", " AND kind = 'invoice'")):
            rows = conn.execute(f"SELECT rowid AS r, {c} AS x FROM {t} WHERE uid IS NULL "
                                f"AND COALESCE({c}, '') <> ''{extra}").fetchall()
            if rows and t == "documents":  # η συμπλήρωση μπορεί να φέρει διπλό UID: index ξανά μετά το _drop_uid_duplicates
                conn.execute("DROP INDEX IF EXISTS ux_documents_uid")
            conn.executemany(f"UPDATE {t} SET uid = ? WHERE rowid = ?",
                             [(u, r["r"]) for r in rows if (u := _uid_of(r["x"]))])
        _drop_uid_duplicates(conn)
        # Αποδείξεις POS (8.4/8.5) εκτός βιβλίων: όσες είχαν ήδη μπει → κουβάς («myData λοιπά»).
        if "invoice_type" in {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}:
            _to_archive(conn, "invoice_type IN ('8.4', '8.5')", ())
            conn.execute("DELETE FROM documents WHERE invoice_type IN ('8.4', '8.5')")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_uid ON documents(company_id, uid) "
                     "WHERE uid IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_archive_uid ON mydata_archive(company_id, uid)")
        # myDATA = documents + κουβάς: κάθε εγγραφή (kind, mark) ακριβώς μία φορά. Μετά τα ALTER (raw_xml, cls_xml).
        conn.execute("DROP VIEW IF EXISTS mydata_all")
        conn.execute(
            "CREATE VIEW mydata_all AS "
            "SELECT company_id, 'documents' AS src, 'invoice' AS kind, mark, NULL AS invoice_mark, "
            "issue_date AS event_date, invoice_type, total_gross AS amount, raw_xml AS xml "
            "FROM documents WHERE COALESCE(raw_xml, '') <> '' "
            "UNION ALL SELECT company_id, 'documents', CASE WHEN unz(cls_xml) LIKE '%incomeInvoiceClassification%' "
            "THEN 'income_cls' ELSE 'expenses_cls' END, classification_mark, mark, NULL, NULL, NULL, cls_xml "
            "FROM documents WHERE COALESCE(cls_xml, '') <> '' "
            "UNION ALL SELECT company_id, 'archive', kind, mark, invoice_mark, event_date, invoice_type, amount, xml "
            "FROM mydata_archive"
        )
        # XML ως BLOB (μία φορά· όσα είναι ακόμα text), μετά λεξικό zstd όταν μαζευτούν αρκετά.
        squeezed = sum(conn.execute(
            f"UPDATE {t} SET {c} = z({c}) WHERE typeof({c}) = 'text' AND {c} <> ''").rowcount
            for t, c in _XML_COLS)
        squeezed += _zstd_migrate(conn)
    if squeezed:
        vacuum()  # αλλιώς το αρχείο δεν μικραίνει


_CLS_KINDS = "('expenses_cls', 'income_cls')"
_CLS_KIND_NAMES = ("expenses_cls", "income_cls")


def _archive_upsert(conn, company_id: int, rows: list[dict], now: str) -> None:
    conn.executemany(
        "INSERT INTO mydata_archive (company_id, kind, mark, invoice_mark, endpoint, issue_date, invoice_type, "
        "issuer_vat, counterpart_vat, event_date, amount, xml, uid, first_seen, last_seen) VALUES (:company_id, "
        ":kind, :mark, :invoice_mark, :endpoint, :issue_date, :invoice_type, :issuer_vat, :counterpart_vat, "
        ":event_date, :amount, :xml, :uid, :now, :now) ON CONFLICT (company_id, kind, mark) DO UPDATE SET "
        "invoice_mark = excluded.invoice_mark, endpoint = COALESCE(excluded.endpoint, endpoint), "
        "uid = COALESCE(excluded.uid, uid), "
        "issue_date = excluded.issue_date, invoice_type = excluded.invoice_type, "
        "issuer_vat = excluded.issuer_vat, counterpart_vat = excluded.counterpart_vat, "
        "event_date = excluded.event_date, amount = excluded.amount, "
        "xml = excluded.xml, last_seen = excluded.last_seen",
        [{"uid": None, **r, "xml": _z(r["xml"]), "company_id": company_id, "now": now} for r in rows],
    )


def _to_archive(conn, where: str, params: tuple) -> None:
    """Πριν από κάθε διαγραφή από το documents: το παραστατικό (raw_xml) και ο ενεργός χαρακτηρισμός
    (cls_xml) περνούν στον κουβά, ώστε myDATA = documents + κουβάς να ισχύει και μετά."""
    from mydata_client import archive_records

    now = datetime.now(UTC).isoformat(timespec="seconds")
    for r in conn.execute(f"SELECT company_id, raw_xml, cls_xml FROM documents WHERE {where}", params).fetchall():
        rows = [rec for x in (r["raw_xml"], r["cls_xml"]) if x for rec in archive_records(x.encode())]
        if rows:
            _archive_upsert(conn, r["company_id"], [{**rec, "endpoint": None} for rec in rows], now)


def archive_sync(company_id: int | None, rows: list[dict]) -> None:
    """Στο τέλος κάθε request (μετά την ενημέρωση του documents) — myDATA = documents + κουβάς,
    κάθε εγγραφή ακριβώς μία φορά:
    1. ό,τι ήρθε από το myDATA (mydata_client.archive_records) γράφεται στον κουβά·
    2. παλιός ενεργός χαρακτηρισμός (cls_xml χωρίς το τρέχον classification_mark) → κουβάς·
    3. ο ενεργός χαρακτηρισμός (MARK = classification_mark) → documents.cls_xml, έξω από τον κουβά·
    4. παραστατικά που είναι στο documents (με raw_xml) → έξω από τον κουβά."""
    from mydata_client import archive_records

    if not company_id:
        return
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_conn() as conn:
        if rows:
            _archive_upsert(conn, company_id, rows, now)
        stale = conn.execute(
            "SELECT id, cls_xml FROM documents WHERE company_id = ? AND COALESCE(cls_xml, '') <> '' "
            "AND (COALESCE(classification_mark, '') = '' OR instr(unz(cls_xml), 'classificationMark>' || classification_mark || '<') = 0)",
            (company_id,),
        ).fetchall()
        for r in stale:
            _archive_upsert(conn, company_id, [{**rec, "endpoint": None}
                                               for rec in archive_records(r["cls_xml"].encode())], now)
            conn.execute("UPDATE documents SET cls_xml = NULL WHERE id = ?", (r["id"],))
        active = (f"FROM mydata_archive a WHERE a.company_id = documents.company_id AND a.kind IN {_CLS_KINDS} "
                  "AND a.invoice_mark = documents.mark AND a.mark = documents.classification_mark")
        conn.execute(f"UPDATE documents SET cls_xml = (SELECT a.xml {active}) "
                     f"WHERE company_id = ? AND EXISTS (SELECT 1 {active})", (company_id,))
        conn.execute(
            f"DELETE FROM mydata_archive WHERE company_id = ? AND ("
            f"(kind = 'invoice' AND mark IN (SELECT mark FROM documents WHERE company_id = ? "
            f"AND COALESCE(raw_xml, '') <> '')) OR (kind IN {_CLS_KINDS} AND EXISTS (SELECT 1 FROM documents d "
            f"WHERE d.company_id = ? AND d.mark = mydata_archive.invoice_mark "
            f"AND d.classification_mark = mydata_archive.mark AND COALESCE(d.cls_xml, '') <> '')))",
            (company_id, company_id, company_id),
        )


def archive_rows(company_id: int | None, mark: str | None = None) -> list[dict]:
    """Όλος ο κουβάς της εταιρείας (χωρίς xml), για την αναφορά «myData λοιπά» — ή μόνο οι εγγραφές ενός MARK
    (Αναζήτηση MARK). Ο χαρακτηρισμός δεν έχει δική του ημερομηνία: παίρνει (date) του παραστατικού του."""
    if not company_id:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.kind, a.mark, a.invoice_mark, a.endpoint, a.invoice_type, a.issuer_vat, a.counterpart_vat, "
            "a.amount, COALESCE(a.event_date, d.issue_date, ai.event_date) AS date, "
            "COALESCE(d.counterparty_vat, ai.issuer_vat) AS ref_vat, COALESCE(d.invoice_type, ai.invoice_type) AS ref_type, "
            "instr(unz(a.xml), 'cancelledByMark') > 0 AS has_cancelled_by, "
            "(instr(unz(a.xml), 'transactionMode>1<') + instr(unz(a.xml), 'transactionMode>2<')) > 0 AS has_transaction_mode, "
            "d.id IS NOT NULL AS ref_in_books, ai.mark IS NOT NULL AS ref_in_archive, "
            # ίδιο UID με άλλο MARK: το παραστατικό των βιβλίων ή νεότερη διαβίβαση στον κουβά
            "COALESCE((SELECT u.mark FROM documents u WHERE u.company_id = a.company_id AND u.uid = a.uid "
            "AND u.mark <> a.mark), (SELECT MAX(CAST(n.mark AS INTEGER)) FROM mydata_archive n "
            "WHERE n.company_id = a.company_id AND n.kind = 'invoice' AND n.uid = a.uid "
            "AND CAST(n.mark AS INTEGER) > CAST(a.mark AS INTEGER))) AS dup_of, "
            # χαρακτηρισμός που αντικαταστάθηκε: ο ενεργός είναι στο documents ή υπάρχει νεότερος στον κουβά
            "(d.id IS NOT NULL OR EXISTS (SELECT 1 FROM mydata_archive n WHERE n.company_id = a.company_id "
            "AND n.kind = a.kind AND n.invoice_mark = a.invoice_mark "
            "AND CAST(n.mark AS INTEGER) > CAST(a.mark AS INTEGER))) AS superseded "
            "FROM mydata_archive a "
            "LEFT JOIN documents d ON d.company_id = a.company_id AND d.mark = a.invoice_mark "
            "LEFT JOIN mydata_archive ai ON ai.company_id = a.company_id AND ai.kind = 'invoice' "
            "AND ai.mark = a.invoice_mark "
            f"WHERE a.company_id = ?{' AND a.mark = ?' if mark else ''} ORDER BY date DESC, a.mark DESC",
            (company_id, mark) if mark else (company_id,))]


def mark_records(company_id: int | None, mark: str) -> list[dict]:
    """Ό,τι έχουμε από το myDATA με αυτό το MARK (mydata_all: documents + κουβάς) — παραστατικό, χαρακτηρισμός
    ή ακύρωση. Ο ενσωματωμένος χαρακτηρισμός έχει το MARK του παραστατικού: έρχεται και αυτός, μετά το παραστατικό.
    {src, kind, mark, invoice_mark, event_date, invoice_type, amount, xml}."""
    if not company_id:
        return []
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM mydata_all WHERE company_id = ? AND mark = ?",
                                              (company_id, mark))]
        if not any(r["kind"] in _CLS_KIND_NAMES for r in rows):  # χαρακτηρισμός των βιβλίων χωρίς XML (ενσωματωμένος / απεσταλμένος)
            rows += [{"src": "documents", "kind": "income_cls" if r["kind"] == "income" else "expenses_cls", "mark": mark,
                      "invoice_mark": r["mark"], "event_date": None, "invoice_type": None, "amount": None, "xml": None}
                     for r in conn.execute("SELECT mark, kind FROM documents WHERE company_id = ? AND classification_mark = ?",
                                           (company_id, mark))]
    return sorted(rows, key=lambda r: r["kind"] != "invoice")


def cancellations(company_id: int | None, invoice_mark: str) -> list[dict]:
    """Πράξεις ακύρωσης ενός παραστατικού (κουβάς): {mark, event_date}."""
    if not company_id:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT mark, event_date FROM mydata_archive WHERE company_id = ? AND kind = 'cancel' AND invoice_mark = ?",
            (company_id, invoice_mark))]


def uid_marks(company_id: int | None, uid: str | None) -> list[str]:
    """Όλα τα MARK (βιβλία + κουβάς) ενός UID, κατά αύξουσα σειρά — ισχύει το τελευταίο (myDATA API v1.0.9, σελ. 8)."""
    if not (company_id and uid):
        return []
    with get_conn() as conn:
        return sorted({r["mark"] for r in conn.execute(
            "SELECT mark FROM documents WHERE company_id = ? AND uid = ? UNION SELECT mark FROM mydata_archive "
            "WHERE company_id = ? AND kind = 'invoice' AND uid = ?", (company_id, uid, company_id, uid))}, key=_mark_no)


def mark_refs(company_id: int | None, mark: str) -> list[str]:
    """MARK παραστατικών που αναφέρουν το mark ως συσχετιζόμενο (correlatedInvoices / multipleConnectedMarks)."""
    if not company_id:
        return []
    with get_conn() as conn:
        return sorted({r["mark"] for r in conn.execute(
            "SELECT mark FROM mydata_all WHERE company_id = ? AND kind = 'invoice' AND mark <> ? AND "
            "(instr(unz(xml), 'correlatedInvoices>' || ? || '<') OR instr(unz(xml), 'multipleConnectedMarks>' || ? || '<'))",
            (company_id, mark, mark, mark))}, key=_mark_no)


def cls_history(company_id: int | None, invoice_mark: str) -> list[dict]:
    """Όλες οι εγγραφές χαρακτηρισμού ενός παραστατικού, νεότερη πρώτα: η ενεργή (documents.cls_xml,
    active=True) και οι προηγούμενες/απορρίψεις του κουβά. {kind, mark, xml, active}."""
    if not company_id:
        return []
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT kind, mark, xml, 0 AS active, NULL AS sent_at FROM mydata_archive "
            f"WHERE company_id = ? AND kind IN {_CLS_KINDS} "
            "AND invoice_mark = ? UNION ALL SELECT CASE WHEN unz(cls_xml) LIKE '%incomeInvoiceClassification%' "
            "THEN 'income_cls' ELSE 'expenses_cls' END, classification_mark, cls_xml, 1, sent_at FROM documents "
            "WHERE company_id = ? AND mark = ? AND COALESCE(cls_xml, '') <> ''",
            (company_id, invoice_mark, company_id, invoice_mark))]
    return sorted(rows, key=lambda r: int(r["mark"]) if (r["mark"] or "").isdigit() else 0, reverse=True)


def archive_item(company_id: int | None, kind: str, mark: str) -> dict | None:
    if not company_id:
        return None
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM mydata_archive WHERE company_id = ? AND kind = ? AND mark = ?",
                         (company_id, kind, mark)).fetchone()
    return dict(r) if r else None


def uid_current(company_id: int | None, uid: str | None, mark: str) -> dict | None:
    """Η έγκυρη διαβίβαση ενός UID που έχει και άλλο MARK (ίδια λογική με το dup_of του archive_rows):
    το παραστατικό των βιβλίων, αλλιώς η νεότερη του κουβά. {mark, xml, in_books}."""
    if not (company_id and uid):
        return None
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM (SELECT mark, raw_xml AS xml, 1 AS in_books FROM documents WHERE company_id = ? AND uid = ? "
            "AND mark <> ? UNION ALL SELECT mark, xml, 0 FROM mydata_archive WHERE company_id = ? AND kind = 'invoice' "
            "AND uid = ? AND CAST(mark AS INTEGER) > CAST(? AS INTEGER)) "
            "ORDER BY in_books DESC, CAST(mark AS INTEGER) DESC LIMIT 1",
            (company_id, uid, mark, company_id, uid, mark)).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------- #
# Ρυθμίσεις (settings)
# --------------------------------------------------------------------- #
def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def delete_setting(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def set_setting(key: str, value) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# --------------------------------------------------------------------- #
# Εταιρείες
# --------------------------------------------------------------------- #
def _company_row_to_dict(row: sqlite3.Row) -> dict:
    """Επιστρέφει dict με τα ίδια κλειδιά που χρησιμοποιούσε το companies.json,
    ώστε routes/templates να μην αλλάξουν, συν το εσωτερικό "id"."""
    return {
        "id": row["id"],
        "company_name": row["company_name"],
        "AADE_USER_ID": auth.dec(row["aade_user_id"]),
        "AADE_SUBSCRIPTION_KEY": auth.dec(row["aade_subscription_key"]),
        "AADE_VAT_NUMBER": row["aade_vat_number"] or "",
        "MYDATA_ENV": row["mydata_env"] or "prod",
        "use_accountant": bool(row["use_accountant"]),
        "accountant_required": bool(row["accountant_required"]),
        "allow_cancel": bool(row["allow_cancel"]),
        "allow_send": bool(row["allow_send"]),
        "allow_classify": bool(row["allow_classify"]),
        "allow_new": bool(row["allow_new"]),
        "locked": bool(row["locked"]),
        "vat_period": "q" if row["vat_period"] == "q" else "m",
        "entity_type": "sole" if row["entity_type"] == "sole" else "legal",
        "birth_year": row["birth_year"],
        "mark_check_hours": check_hours(row["mark_check_hours"]),
    }


def birth_year(value) -> int | None:
    try:
        y = int(value)
    except (TypeError, ValueError):
        return None
    return y if 1900 <= y <= datetime.now().year else None


def check_hours(value) -> float:
    """Διάστημα ελέγχου ανάκτησης σε ώρες, δεκαδικό (π.χ. 0,5 = μισή ώρα)· μη έγκυρο ή ≤ 0 → 1."""
    try:
        h = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 1.0
    return h if 0 < h < 10_000 else 1.0  # και nan/inf


def list_companies() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM companies ORDER BY id").fetchall()
    return [_company_row_to_dict(r) for r in rows]


def _use_accountant(data: dict) -> int:
    """Υποχρεωτικός λογιστής: «Χρήση credentials λογιστή» και «Αποστολή στο myDATA» ανοίγουν/κλείνουν μαζί."""
    return 1 if data.get("allow_send" if data.get("accountant_required") else "use_accountant") else 0


def add_company(data: dict) -> int | None:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO companies (company_name, aade_user_id, aade_subscription_key, "
            "aade_vat_number, mydata_env, use_accountant, allow_cancel, allow_send, allow_classify, allow_new, vat_period, entity_type, birth_year, "
            "accountant_required, mark_check_hours) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.get("company_name", ""),
                auth.enc(data.get("AADE_USER_ID") or ""),
                auth.enc(data.get("AADE_SUBSCRIPTION_KEY") or ""),
                data.get("AADE_VAT_NUMBER", ""),
                data.get("MYDATA_ENV", "prod"),
                _use_accountant(data),
                1 if data.get("allow_cancel") and data.get("allow_send") else 0,  # ακυρώσεις/απορρίψεις μόνο με αποστολή
                1 if data.get("allow_send") else 0,
                1 if data.get("allow_classify") else 0,
                1 if data.get("allow_new") else 0,
                "q" if data.get("vat_period") == "q" else "m",
                "sole" if data.get("entity_type") == "sole" else "legal",
                birth_year(data.get("birth_year")),
                1 if data.get("accountant_required") else 0,
                check_hours(data.get("mark_check_hours")),
            ),
        )
        return cur.lastrowid


def update_company(company_id: int, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET company_name = ?, aade_user_id = ?, "
            "aade_subscription_key = ?, aade_vat_number = ?, mydata_env = ?, "
            "vat_period = ?, entity_type = ?, birth_year = ?, accountant_required = ?, mark_check_hours = ?, "
            # υποχρεωτικός λογιστής σε κλειδωμένη: κλειστή η αποστολή → κλειστός και ο λογιστής
            "use_accountant = CASE WHEN locked AND ? THEN 0 ELSE ? END, "
            # κλειδωμένη εταιρεία (αρχείο πελάτη): οι άδειες μένουν κλειστές, ό,τι κι αν έρθει
            "allow_cancel = CASE WHEN locked THEN 0 ELSE ? END, allow_send = CASE WHEN locked THEN 0 ELSE ? END, "
            "allow_classify = CASE WHEN locked THEN 0 ELSE ? END, allow_new = CASE WHEN locked THEN 0 ELSE ? END "
            "WHERE id = ?",
            (
                data.get("company_name", ""),
                auth.enc(data.get("AADE_USER_ID") or ""),
                auth.enc(data.get("AADE_SUBSCRIPTION_KEY") or ""),
                data.get("AADE_VAT_NUMBER", ""),
                data.get("MYDATA_ENV", "prod"),
                "q" if data.get("vat_period") == "q" else "m",
                "sole" if data.get("entity_type") == "sole" else "legal",
                birth_year(data.get("birth_year")),
                1 if data.get("accountant_required") else 0,
                check_hours(data.get("mark_check_hours")),
                1 if data.get("accountant_required") else 0,
                _use_accountant(data),
                1 if data.get("allow_cancel") and data.get("allow_send") else 0,  # ακυρώσεις/απορρίψεις μόνο με αποστολή
                1 if data.get("allow_send") else 0,
                1 if data.get("allow_classify") else 0,
                1 if data.get("allow_new") else 0,
                company_id,
            ),
        )


def lock_company(company_id: int) -> None:
    """«Αρχείο πελάτη»: μόνο αναφορές — όλες οι άδειες κλειστές, και δεν ξανανοίγουν (βλ. update_company)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET locked = 1, allow_cancel = 0, allow_send = 0, allow_classify = 0, allow_new = 0 "
            "WHERE id = ?",
            (company_id,),
        )


def delete_company(company_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))


def get_active_company_id() -> int | None:
    val = get_setting("active_company_id")
    return int(val) if val is not None else None


def set_active_company_id(company_id: int) -> None:
    set_setting("active_company_id", int(company_id))


# --------------------------------------------------------------------- #
# Λογιστής
# --------------------------------------------------------------------- #
def get_accountant() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accountant WHERE id = 1").fetchone()
    if not row:
        return {}
    return {
        "user_id": auth.dec(row["user_id"]),
        "subscription_key": auth.dec(row["subscription_key"]),
        "env": row["env"] or "prod",
    }


def save_accountant(data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO accountant (id, user_id, subscription_key, env) "
            "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "user_id = excluded.user_id, subscription_key = excluded.subscription_key, "
            "env = excluded.env",
            (
                auth.enc(data.get("user_id") or ""),
                auth.enc(data.get("subscription_key") or ""),
                data.get("env", "prod"),
            ),
        )


# --------------------------------------------------------------------- #
# Κεντρικός χρήστης (βλ. auth.py): ρυθμίσεις auth_user / auth_salt / auth_dek
# --------------------------------------------------------------------- #
_AUTH_KEYS = ("auth_user", "auth_salt", "auth_dek")


def get_auth() -> dict | None:
    rec = {k: get_setting(k) for k in _AUTH_KEYS}
    return rec if all(rec.values()) else None


def save_auth(rec: dict) -> None:
    for k in _AUTH_KEYS:
        set_setting(k, rec[k])


def encrypt_credentials() -> None:
    """Μετά το setup/login: όσα plaintext κλειδιά υπάρχουν ξαναγράφονται κρυπτογραφημένα και
    γίνεται VACUUM, ώστε οι παλιές τιμές να μη μένουν σε ελεύθερες σελίδες ή στο WAL."""
    with get_conn() as conn:
        plain = conn.execute(
            "SELECT (SELECT COUNT(*) FROM companies WHERE aade_user_id NOT LIKE 'enc:%' AND aade_user_id <> '' "
            "OR aade_subscription_key NOT LIKE 'enc:%' AND aade_subscription_key <> '') + "
            "(SELECT COUNT(*) FROM accountant WHERE user_id NOT LIKE 'enc:%' AND user_id <> '' "
            "OR subscription_key NOT LIKE 'enc:%' AND subscription_key <> '')"
        ).fetchone()[0]
    if not plain:
        return
    for c in list_companies():
        update_company(c["id"], c)
    acc = get_accountant()
    if acc:
        save_accountant(acc)
    conn = get_conn()
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def reset_auth() -> None:
    """Ξέχασα τον κωδικό: σβήνει τον χρήστη ΚΑΙ τα κλειδιά myDATA (δεν αποκρυπτογραφούνται πια).
    Τα παραστατικά μένουν. Από τερματικό: python -c "import db; db.reset_auth()"."""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(_AUTH_KEYS))})", _AUTH_KEYS)
        conn.execute("UPDATE companies SET aade_user_id = '', aade_subscription_key = ''")
        conn.execute("UPDATE accountant SET user_id = '', subscription_key = ''")
    auth.lock()


# --------------------------------------------------------------------- #
# Προτάσεις χαρακτηρισμού: ΑΝΑ ΕΤΑΙΡΕΙΑ (πίνακας supplier_rules)
# Συναλλασσόμενοι (ΑΦΜ → επωνυμία): ΚΟΙΝΟΙ για όλες τις εταιρείες (πίνακας suppliers)
# --------------------------------------------------------------------- #
DEFAULT_RULE = "*"  # vat_category της βασικής πρότασης του συναλλασσόμενου


def _rule_dict(r: sqlite3.Row) -> dict:
    return {"category": r["rule_category"], "type": r["rule_type"], "vat_type": r["rule_vat_type"] or ""}


def _load_all_rules(company_id: int | None, vat: str | None = None) -> dict:
    """{vat: {vat_category: rule}} της εταιρείας, μαζί με τη βασική ('*')."""
    if not company_id:
        return {}
    q = "SELECT * FROM supplier_rules WHERE company_id = ?" + (" AND vat = ?" if vat else "")
    with get_conn() as conn:
        rows = conn.execute(q, (company_id, vat) if vat else (company_id,)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["vat"], {})[r["vat_category"]] = _rule_dict(r)
    return out


def load_rules(company_id: int | None) -> dict:
    """{vat: {"type", "category", "vat_type"}} — η βασική πρόταση ανά συναλλασσόμενο."""
    return {
        vat: pats[DEFAULT_RULE]
        for vat, pats in _load_all_rules(company_id).items()
        if DEFAULT_RULE in pats
    }


def save_rule(company_id: int | None, vat: str | None, ctype: str, ccat: str, vat_type: str = "") -> None:
    save_rule_patterns(
        company_id, vat, {DEFAULT_RULE: {"category": ccat, "type": ctype, "vat_type": vat_type}}
    )


def load_rule_patterns(company_id: int | None, vat: str | None = None) -> dict:
    """{vat: {vat_category: {"category", "type", "vat_type"}}} — προτάσεις ανά κατηγορία ΦΠΑ."""
    out = _load_all_rules(company_id, vat)
    for pats in out.values():
        pats.pop(DEFAULT_RULE, None)
    return {v: p for v, p in out.items() if p}


def save_rule_patterns(company_id: int | None, vat: str | None, patterns: dict) -> None:
    """Αποθηκεύει/ανανεώνει προτάσεις {vat_category: {category, type, vat_type}} της εταιρείας·
    οι κατηγορίες ΦΠΑ που δεν εμφανίζονται στο νέο παραστατικό μένουν ως είχαν."""
    if not company_id or not vat or not patterns:
        return
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO supplier_rules (company_id, vat, vat_category, rule_category, rule_type, "
            "rule_vat_type, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(company_id, vat, vat_category) DO UPDATE SET "
            "rule_category = excluded.rule_category, rule_type = excluded.rule_type, "
            "rule_vat_type = excluded.rule_vat_type, updated_at = excluded.updated_at",
            [
                (company_id, vat, cat, p["category"], p["type"], p.get("vat_type") or "", now)
                for cat, p in patterns.items()
            ],
        )


def replace_rule_patterns(company_id: int | None, vat: str | None, patterns: dict) -> None:
    """Αντικαθιστά ΟΛΕΣ τις προτάσεις του συναλλασσόμενου στην εταιρεία (και τη βασική '*')."""
    if not company_id or not vat:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM supplier_rules WHERE company_id = ? AND vat = ?", (company_id, vat))
    save_rule_patterns(company_id, vat, patterns)


def delete_rules(company_id: int | None, vats) -> int:
    """Σβήνει ΟΛΕΣ τις προτάσεις (και τη βασική) των ΑΦΜ στην εταιρεία· επιστρέφει πόσοι είχαν."""
    vats = list(vats)
    if not company_id or not vats:
        return 0
    with get_conn() as conn:
        had = conn.execute(
            f"SELECT COUNT(DISTINCT vat) FROM supplier_rules WHERE company_id = ? AND vat IN ({','.join('?' * len(vats))})",
            (company_id, *vats),
        ).fetchone()[0]
        conn.execute(
            f"DELETE FROM supplier_rules WHERE company_id = ? AND vat IN ({','.join('?' * len(vats))})",
            (company_id, *vats),
        )
    return had


def get_rule_post_mode(company_id: int | None, vat: str | None) -> int:
    """Τρόπος χαρακτηρισμού της πρότασης: 0 = ανά γραμμή, 1 = συγκεντρωτικά (postPerInvoice)."""
    if not company_id or not vat:
        return 0
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(rule_post_mode) AS m FROM supplier_rules WHERE company_id = ? AND vat = ?",
            (company_id, vat),
        ).fetchone()
    return int(row["m"] or 0)


def set_rule_post_mode(company_id: int | None, vat: str | None, post_mode: int) -> None:
    if not company_id or not vat:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE supplier_rules SET rule_post_mode = ? WHERE company_id = ? AND vat = ?",
            (1 if post_mode else 0, company_id, vat),
        )


def review_vats(company_id: int | None) -> set[str]:
    """ΑΦΜ της εταιρείας με «Επιβεβαίωση πριν την αποστολή»: ο αυτόματος κύκλος τα χαρακτηρίζει
    αλλά δεν τα στέλνει. Ρύθμιση (όχι στήλη του supplier_rules, που ξαναγράφεται με DELETE)."""
    return set(json.loads(get_setting(f"review_vats_{company_id}") or "[]")) if company_id else set()


def set_review_vat(company_id: int | None, vat: str | None, on: bool) -> None:
    if not company_id or not vat:
        return
    vats = review_vats(company_id)
    vats = vats | {vat} if on else vats - {vat}
    set_setting(f"review_vats_{company_id}", json.dumps(sorted(vats)))


def load_all_rule_patterns(company_id: int | None, vat: str) -> dict:
    """{vat_category: rule} του συναλλασσόμενου, μαζί με τη βασική ('*')."""
    return _load_all_rules(company_id, vat).get(vat, {})


def foreign_rule(company_id: int | None, vat: str | None) -> dict | None:
    """Πρόταση ΑΛΛΗΣ εταιρείας για το ΑΦΜ (η πιο πρόσφατα ενημερωμένη), για προσυμπλήρωση όταν
    η ενεργή εταιρεία δεν έχει δική της: {"company_name", "default", "patterns"} ή None."""
    if not vat:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.company_id, c.company_name FROM supplier_rules AS r "
            "JOIN companies AS c ON c.id = r.company_id "
            "WHERE r.vat = ? AND r.company_id != ? ORDER BY r.updated_at DESC LIMIT 1",
            (vat, company_id or 0),
        ).fetchone()
    if row is None:
        return None
    pats = _load_all_rules(row["company_id"], vat).get(vat, {})
    return {
        "company_name": row["company_name"],
        "default": pats.pop(DEFAULT_RULE, {}),
        "patterns": pats,
        "post_mode": get_rule_post_mode(row["company_id"], vat),
    }


def load_names() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT vat, name FROM suppliers WHERE name IS NOT NULL"
        ).fetchall()
    return {r["vat"]: r["name"] for r in rows}


def save_names(names: dict) -> None:
    if not names:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO suppliers (vat, name) VALUES (?, ?) "
            "ON CONFLICT(vat) DO UPDATE SET name = excluded.name",
            [(vat, name) for vat, name in names.items()],
        )


def get_supplier_name(vat: str | None) -> str | None:
    if not vat:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM suppliers WHERE vat = ?", (vat,)
        ).fetchone()
    return row["name"] if row else None


SUPPLIER_ADDRESS = ("country", "street", "postal_code", "city")


def _save_supplier_address(conn, vat, name, country, street, postal_code, city) -> None:
    conn.execute(
        "INSERT INTO suppliers (vat, name, country, street, postal_code, city) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(vat) DO UPDATE SET name = COALESCE(excluded.name, name), country = excluded.country, "
        "street = excluded.street, postal_code = excluded.postal_code, city = excluded.city",
        (vat, name or None, country, street or None, postal_code or None, city or None),
    )


def save_supplier_address(vat: str, name: str | None, country: str, street: str | None,
                          postal_code: str | None, city: str | None) -> None:
    """Επωνυμία + χώρα/διεύθυνση συναλλασσόμενου εκτός Ελλάδας (για τις επόμενες εγγραφές)."""
    with get_conn() as conn:
        _save_supplier_address(conn, vat, name, country, street, postal_code, city)


def get_supplier(vat: str | None) -> dict | None:
    """Ένας συναλλασσόμενος του κοινού καταλόγου (επωνυμία, χώρα, διεύθυνση) ή None."""
    if not vat:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT vat, name, country, street, postal_code, city FROM suppliers WHERE vat = ?",
                           (vat,)).fetchone()
    return dict(row) if row else None


def list_suppliers(company_id: int | None = None) -> list[dict]:
    """Ολόκληρος ο κοινός κατάλογος· με company_id μόνο οι συναλλασσόμενοι της εταιρείας
    (με παραστατικά, δική της πρόταση χαρακτηρισμού ή δηλωμένοι από τη Φόρμα καταχώρησης)."""
    q = "SELECT vat, name, country, street, postal_code, city FROM suppliers"
    params: tuple = ()
    if company_id is not None:
        q += (
            " WHERE vat IN (SELECT counterparty_vat FROM documents WHERE company_id = ?"
            " UNION SELECT vat FROM supplier_rules WHERE company_id = ?"
            " UNION SELECT vat FROM company_suppliers WHERE company_id = ?)"
        )
        params = (company_id, company_id, company_id)
    with get_conn() as conn:
        rows = conn.execute(q + " ORDER BY name IS NULL, name, vat", params).fetchall()
    return [dict(r) for r in rows]


def upsert_supplier(vat: str, name: str | None) -> None:
    """Χειροκίνητη προσθήκη/διόρθωση προμηθευτή (ΑΦΜ + επωνυμία)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO suppliers (vat, name) VALUES (?, ?) "
            "ON CONFLICT(vat) DO UPDATE SET name = excluded.name",
            (vat, name),
        )


def vats_with_documents() -> set[str]:
    """ΑΦΜ με παραστατικά σε οποιαδήποτε εταιρεία (ο κατάλογος συναλλασσόμενων είναι κοινός)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT counterparty_vat FROM documents WHERE counterparty_vat IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def delete_supplier(vat: str) -> bool:
    """Διαγραφή συναλλασσόμενου — μόνο αν δεν έχει κινήσεις (παραστατικά) σε καμία εταιρεία."""
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM documents WHERE counterparty_vat = ? LIMIT 1", (vat,)).fetchone():
            return False
        conn.execute("DELETE FROM suppliers WHERE vat = ?", (vat,))
        conn.execute("DELETE FROM supplier_rules WHERE vat = ?", (vat,))
        conn.execute("DELETE FROM company_suppliers WHERE vat = ?", (vat,))
    return True


def link_supplier(company_id: int, vat: str) -> None:
    """Ο συναλλασσόμενος ανήκει στην εταιρεία (εμφανίζεται στους «Συναλλασσόμενούς» της)."""
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO company_suppliers (company_id, vat) VALUES (?, ?)", (company_id, vat))


def import_suppliers_txt(text: str) -> int:
    """Εισαγωγή από .txt: μία γραμμή ανά προμηθευτή = «ΑΦΜ<κενό>Επωνυμία».
    Διαχωριστικό είναι το ΠΡΩΤΟ κενό μετά το ΑΦΜ. Επιστρέφει πλήθος εγγραφών."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)  # split στο πρώτο διάστημα/tab
        vat = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else ""
        if vat:
            rows.append((vat, name))
    if rows:
        with get_conn() as conn:
            conn.executemany(
                "INSERT INTO suppliers (vat, name) VALUES (?, ?) "
                "ON CONFLICT(vat) DO UPDATE SET name = excluded.name",
                rows,
            )
    return len(rows)


def export_suppliers_txt() -> str:
    """Εξαγωγή σε .txt: «ΑΦΜ<κενό>Επωνυμία» ανά γραμμή."""
    return "\n".join(
        f"{s['vat']} {s['name'] or ''}".rstrip() for s in list_suppliers()
    )


# --------------------------------------------------------------------- #
# Παραστατικά (documents) + τοπικοί χαρακτηρισμοί
# --------------------------------------------------------------------- #
# Ιεραρχία status: όσο μεγαλύτερος ο βαθμός, τόσο πιο «προχωρημένη» η κατάσταση.
# Ένα upsert από νέα ανάκτηση δεν πρέπει ΠΟΤΕ να χαμηλώνει την τοπική πρόοδο.
_STATUS_RANK = {"unclassified": 0, "classified": 1, "sent": 2, "confirmed": 3}


def upsert_document(
    company_id: int, kind: str, doc: dict, incoming_status: str
) -> None:
    """Εισάγει/ενημερώνει παραστατικό. Τα οικονομικά/μεταδεδομένα πεδία πάντα
    ανανεώνονται· το status ανανεώνεται ΜΟΝΟ αν το εισερχόμενο είναι ίσο ή
    «ανώτερο» (π.χ. myDATA confirmed) — δεν χαμηλώνει τοπικά classified/sent."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines_json = json.dumps(doc.get("lines") or [], ensure_ascii=False)
    cls_json = json.dumps(doc.get("cls_info") or [], ensure_ascii=False)
    uid = doc.get("uid") or _uid_of(doc.get("raw_xml"))
    with get_conn() as conn:
        # Ίδιο UID με άλλο MARK (ξαναδιαβίβαση): μετρά μόνο το νεότερο MARK — το άλλο στον κουβά (_drop_uid_duplicates).
        other = uid and conn.execute(
            "SELECT id, mark, status, cls_json FROM documents WHERE company_id = ? AND uid = ? AND mark <> ?",
            (company_id, uid, doc["mark"]),
        ).fetchone()
        if other:
            if _mark_no(other["mark"]) > _mark_no(doc["mark"]):
                return  # μένει στον κουβά (archive_sync)
            if other["status"] == "classified" and incoming_status == "unclassified":
                incoming_status, cls_json = "classified", other["cls_json"]  # τοπικός χαρακτηρισμός δεν χάνεται
            _to_archive(conn, "id = ?", (other["id"],))
            conn.execute("DELETE FROM documents WHERE id = ?", (other["id"],))
        existing = conn.execute(
            "SELECT id, status, cls_json FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, doc["mark"]),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO documents (company_id, kind, mark, issue_date, "
                "counterparty_vat, invoice_type, series, aa, "
                "total_net, total_vat, total_gross, is_self_issued, status, "
                "lines_json, cls_json, classification_mark, updated_at, uid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    kind,
                    doc["mark"],
                    doc.get("issue_date"),
                    doc.get("issuer_vat"),
                    doc.get("invoice_type"),
                    doc.get("series"),
                    doc.get("aa"),
                    doc.get("total_net"),
                    doc.get("total_vat"),
                    doc.get("total_gross"),
                    1 if doc.get("is_self_issued") else 0,
                    incoming_status,
                    lines_json,
                    cls_json,
                    doc.get("classification_mark") or "",
                    now,
                    uid,
                ),
            )
            _set_extra_totals(conn, company_id, doc)
            return
        # υπάρχει ήδη: μην χαμηλώσεις το status
        keep = _STATUS_RANK.get(existing["status"], 0) >= _STATUS_RANK.get(
            incoming_status, 0
        )
        new_status = existing["status"] if keep else incoming_status
        # Το cls_json από το myDATA το κρατάμε μόνο όταν όντως προχωράμε σε
        # confirmed (τοπικός χαρακτηρισμός έχει δικό του cls_json).
        # ...ή όταν ο αποθηκευμένος είναι κενός (π.χ. ενσωματωμένος χαρακτηρισμός εσόδων που
        # παλιότερη ανάκτηση δεν κρατούσε) — τοπικός χαρακτηρισμός δεν αντικαθίσταται ποτέ.
        fill_cls = keep and existing["cls_json"] in (None, "", "[]") and cls_json != "[]"
        # confirmed → confirmed: το myDATA είναι η αλήθεια· νεότερος χαρακτηρισμός (χωριστή υποβολή ή
        # πύλη πάνω σε ενσωματωμένο) αντικαθιστά cls_json ΚΑΙ MARK — κενό MARK = χαρακτηρισμός πύλης.
        refresh = existing["status"] == "confirmed" == incoming_status and cls_json != "[]"
        set_cls = ", cls_json = ?" if not keep or fill_cls or refresh else ""
        # Αλλιώς το MARK χαρακτηρισμού γράφεται όποτε έρχεται μη κενό — και σε keep·
        # κενή τιμή δεν σβήνει ό,τι έχουμε τοπικά.
        cls_mark = doc.get("classification_mark") or ""
        set_mark = ", classification_mark = ?" if cls_mark or refresh else ""
        params = [
            kind,  # διορθώνει παλιές εγγραφές σε λάθος βιβλίο (π.χ. διαβίβαση από λήπτη ως έσοδο)
            doc.get("issue_date"),
            doc.get("issuer_vat"),
            doc.get("invoice_type"),
            doc.get("series"),
            doc.get("aa"),
            doc.get("total_net"),
            doc.get("total_vat"),
            doc.get("total_gross"),
            1 if doc.get("is_self_issued") else 0,
            new_status,
            lines_json,
            now,
            uid,
        ]
        if set_cls:
            params.append(cls_json)
        if set_mark:
            params.append(cls_mark)
        params.append(existing["id"])
        conn.execute(
            "UPDATE documents SET kind = ?, issue_date = ?, counterparty_vat = ?, "
            "invoice_type = ?, series = ?, aa = ?, "
            "total_net = ?, total_vat = ?, total_gross = ?, is_self_issued = ?, "
            f"status = ?, lines_json = ?, updated_at = ?, uid = COALESCE(?, uid){set_cls}{set_mark} "
            "WHERE id = ?",
            params,
        )
        _set_extra_totals(conn, company_id, doc)


def _set_extra_totals(conn: sqlite3.Connection, company_id: int, doc: dict) -> None:
    """Λοιπά σύνολα σύνοψης (παρακρατήσεις, τέλη κ.λπ.), όταν τα έφερε το myDATA."""
    if doc.get("raw_xml"):  # κενό (τοπικό draft, παλιά ανάκτηση) δεν σβήνει ό,τι έχουμε
        conn.execute("UPDATE documents SET raw_xml = ? WHERE company_id = ? AND mark = ?",
                     (_z(doc["raw_xml"]), company_id, doc["mark"]))
    extra = doc.get("extra_totals")
    if extra:
        conn.execute(
            f"UPDATE documents SET {', '.join(c + ' = ?' for c in EXTRA_TOTALS)}, taxes_json = ? "
            "WHERE company_id = ? AND mark = ?",
            [extra.get(c) for c in EXTRA_TOTALS] + [extra.get("taxes_json"), company_id, doc["mark"]],
        )


def get_documents(
    company_id: int, kind: str, statuses: list[str] | None = None
) -> list[dict]:
    q = "SELECT * FROM documents WHERE company_id = ? AND kind = ?"
    params: list = [company_id, kind]
    if statuses:
        statuses_str = ",".join("?" * len(statuses))
        q += f" AND status IN ({statuses_str})"
        params.extend(statuses)
    q += " ORDER BY mark"  # η σειρά του UNIQUE (company_id, mark), όπως πριν από το ix_documents_period
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_document(company_id: int, mark: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, mark),
        ).fetchone()
    return dict(row) if row else None


def month_counts(company_id: int, kind: str, date_from: str) -> dict:
    """Πλήθος παραστατικών ανά μήνα έκδοσης (από date_from): {"yyyy-mm": {status: n}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT substr(issue_date, 1, 7) AS ym, status, COUNT(*) AS n FROM documents "
            "WHERE company_id = ? AND kind = ? AND issue_date >= ? GROUP BY ym, status",
            (company_id, kind, date_from),
        ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["ym"], {})[r["status"]] = r["n"]
    return out


def day_counts(company_id: int, kind: str, ym: str) -> dict:
    """Πλήθος παραστατικών ανά ημέρα έκδοσης του μήνα ym: {"yyyy-mm-dd": {status: n}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT substr(issue_date, 1, 10) AS d, status, COUNT(*) AS n FROM documents "
            "WHERE company_id = ? AND kind = ? AND issue_date LIKE ? GROUP BY d, status",
            (company_id, kind, ym + "%"),
        ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["d"], {})[r["status"]] = r["n"]
    return out


def count_by_status(company_id: int, kind: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents "
            "WHERE company_id = ? AND kind = ? GROUP BY status",
            (company_id, kind),
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def company_stats() -> dict:
    """Σύνοψη βιβλίου ανά εταιρεία: {company_id: {income, expense, pending, done, last}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT company_id, SUM(kind = 'income') AS income, SUM(kind = 'expense') AS expense, "
            "SUM(kind = 'expense' AND status = 'unclassified') AS pending, "
            "SUM(kind = 'expense' AND status = 'confirmed') AS done, MAX(issue_date) AS last "
            "FROM documents GROUP BY company_id"
        ).fetchall()
    return {r["company_id"]: dict(r) for r in rows}


def company_month_counts(date_from: str) -> dict:
    """Πλήθος παραστατικών ανά εταιρεία/μήνα (από date_from): {company_id: {"yyyy-mm": {"income": n, "expense": n}}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT company_id, kind, substr(issue_date, 1, 7) AS ym, COUNT(*) AS n FROM documents "
            "WHERE issue_date >= ? GROUP BY company_id, kind, ym",
            (date_from,),
        ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["company_id"], {}).setdefault(r["ym"], {})[r["kind"]] = r["n"]
    return out


def olap_documents(company_id: int | None, kind: str, statuses: list[str]) -> list[dict]:
    """Όλα τα παραστατικά ενός kind (στις δοσμένες καταστάσεις) με γραμμές, ισχύοντα χαρακτηρισμό
    και όλα τα σύνολα — η πρώτη ύλη του κύβου OLAP."""
    if not company_id:
        return []
    cols = ", ".join(f"d.{c}" for c in (
        "id", "mark", "issue_date", "invoice_type", "series", "aa", "counterparty_vat",
        "total_net", "total_vat", "total_gross", "lines_json", "cls_json") + EXTRA_TOTALS)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {cols}, s.name AS counterparty_name FROM documents AS d "
            "LEFT JOIN suppliers AS s ON s.vat = d.counterparty_vat "
            f"WHERE d.company_id = ? AND d.kind = ? AND d.status IN ({','.join('?' * len(statuses))}) ORDER BY d.mark",
            [company_id, kind, *statuses],
        ).fetchall()
    return [dict(r) for r in rows]


def save_local_classification(
    company_id: int | None,
    mark: str,
    entries: list[dict],
    manual: bool = False,
    post_mode: int = 0,
) -> bool:
    """Αποθηκεύει ΤΟΠΙΚΑ τον χαρακτηρισμό (post_mode 0 = ανά γραμμή, 1 = ανά παραστατικό).
    - manual=False → status='classified' (θα σταλεί στο myDATA με το «Αποστολή»).
    - manual=True → status='confirmed', source='manual': παραστατικό ήδη χαρακτηρισμένο
      χειροκίνητα στην πύλη myDATA, καταγράφεται μόνο τοπικά (ΔΕΝ στέλνεται).
    Επιστρέφει False αν το παραστατικό δεν βρέθηκε."""
    if not company_id:
        return False
    cls_info = [
        {
            "type": e.get("classification_type"),
            "category": e.get("classification_category") or "",
            "amount": e.get("amount"),
        }
        for e in entries
    ]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, mark),
        ).fetchone()
        if row is None:
            return False
        doc_id = row["id"]
        if not manual:
            _snapshot_sent(conn, doc_id)
        conn.execute("DELETE FROM classifications WHERE document_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO classifications (document_id, line_number, classification_type, "
            "classification_category, vat_type, amount, vat_category, vat_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    doc_id,
                    e.get("line_number"),
                    e.get("classification_type"),
                    e.get("classification_category") or "",
                    e.get("vat_type") or "",
                    e.get("amount"),
                    e.get("vat_category"),
                    e.get("vat_amount"),
                )
                for e in entries
            ],
        )
        conn.execute(
            "UPDATE documents SET cls_post_mode = ? WHERE id = ?", (post_mode, doc_id)
        )
        if manual:
            conn.execute(
                "UPDATE documents SET status = 'confirmed', source = 'manual', prev_state = NULL, "
                "local_action = 'classify', draft_json = NULL, cls_json = ?, "
                "confirmed_at = ? WHERE id = ?",
                (
                    json.dumps(cls_info, ensure_ascii=False),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    doc_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE documents SET status = 'classified', local_action = 'classify', "
                "draft_json = NULL, cls_json = ? WHERE id = ?",
                (json.dumps(cls_info, ensure_ascii=False), doc_id),
            )
    return True


def _snapshot_sent(conn: sqlite3.Connection, doc_id: int) -> None:
    """Πριν από τοπική αλλαγή σε ήδη απεσταλμένο/επιβεβαιωμένο: κράτα την κατάστασή του (μία φορά)."""
    d = conn.execute(
        "SELECT status, source, cls_json, cls_post_mode, prev_state FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if d["prev_state"] or d["status"] not in ("sent", "confirmed"):
        return
    rows = [dict(r) for r in conn.execute(
        "SELECT line_number, classification_type, classification_category, vat_type, amount, "
        "vat_category, vat_amount FROM classifications WHERE document_id = ? ORDER BY id", (doc_id,))]
    state = {k: d[k] for k in ("status", "source", "cls_json", "cls_post_mode")} | {"rows": rows}
    conn.execute("UPDATE documents SET prev_state = ? WHERE id = ?", (json.dumps(state, ensure_ascii=False), doc_id))


def unclassify(company_id: int | None, marks: list[str]) -> int:
    """Αναιρεί τον τοπικό χαρακτηρισμό ή τη στημένη απόρριψη/ακύρωση (μόνο status='classified',
    όχι πρόχειρα νέας εγγραφής): το παραστατικό επιστρέφει στα αχαρακτήριστα — ή, αν ήταν ήδη
    απεσταλμένο/επιβεβαιωμένο, στον χαρακτηρισμό που είχε (prev_state). Επιστρέφει πόσα άλλαξαν."""
    if not company_id or not marks:
        return 0
    q = ",".join("?" * len(marks))
    with get_conn() as conn:
        docs = conn.execute(
            f"SELECT id, prev_state FROM documents WHERE company_id = ? AND mark IN ({q}) "
            "AND status = 'classified' AND local_action IN ('classify', 'reject', 'cancel')",
            (company_id, *marks),
        ).fetchall()
        ids = [r["id"] for r in docs]
        for doc_id, prev in docs:
            conn.execute("DELETE FROM classifications WHERE document_id = ?", (doc_id,))
            if prev:
                p = json.loads(prev)
                conn.executemany(
                    "INSERT INTO classifications (document_id, line_number, classification_type, "
                    "classification_category, vat_type, amount, vat_category, vat_amount) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(doc_id, r["line_number"], r["classification_type"], r["classification_category"],
                      r["vat_type"], r["amount"], r["vat_category"], r["vat_amount"]) for r in p["rows"]],
                )
                conn.execute(
                    "UPDATE documents SET status = ?, source = ?, cls_json = ?, cls_post_mode = ?, "
                    "local_action = 'classify', prev_state = NULL WHERE id = ?",
                    (p["status"], p["source"], p["cls_json"], p["cls_post_mode"], doc_id),
                )
                continue
            conn.execute(
                "UPDATE documents SET status = 'unclassified', local_action = 'classify', "
                "cls_json = '[]', cls_post_mode = 0 "
                "WHERE id = ?", (doc_id,),
            )
    return len(ids)


def get_local_classification(doc_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT line_number, classification_type, classification_category, "
            "vat_type, amount, vat_category, vat_amount FROM classifications "
            "WHERE document_id = ? "
            "ORDER BY line_number, id",
            (doc_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_sent(doc_id: int, classification_mark: str | None) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET status = 'sent', classification_mark = ?, sent_at = ?, prev_state = NULL "
            "WHERE id = ?",
            (classification_mark or "", now, doc_id),
        )


def mark_late(company_id: int, marks) -> None:
    """Σήμανση εκπρόθεσμων: ανέβηκαν στο myDATA μετά τον μήνα έκδοσής τους."""
    with get_conn() as conn:
        conn.executemany(
            "UPDATE documents SET late_since = date('now') WHERE company_id = ? AND mark = ?",
            [(company_id, m) for m in marks],
        )


def mark_confirmed(doc_id: int, classification_mark: str | None = None) -> None:
    """Επιβεβαίωση από το myDATA. Αν δοθεί MARK χαρακτηρισμού (από την απάντηση
    του myDATA) συμπληρώνεται — χρήσιμο όταν λείπει από την τοπική αποστολή."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    set_mark = ", classification_mark = ?" if classification_mark else ""
    params: list = [now]
    if classification_mark:
        params.append(classification_mark)
    params.append(doc_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET status = 'confirmed', confirmed_at = ?, prev_state = NULL"
            f"{set_mark} WHERE id = ?",
            params,
        )


def delete_document(company_id: int | None, mark: str) -> None:
    if not company_id:
        return
    with get_conn() as conn:
        _to_archive(conn, "company_id = ? AND mark = ?", (company_id, mark))
        conn.execute(
            "DELETE FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, mark),
        )


def delete_documents_range(
    company_id: int | None, kind: str, date_from: str, date_to: str
) -> int:
    """Διαγράφει τα παραστατικά ενός βιβλίου και εύρους ημερομηνιών."""
    if not company_id or kind not in ("expense", "income"):
        return 0
    with get_conn() as conn:
        # Ρητή διαγραφή του τοπικού αντιγράφου: φεύγει ΚΑΙ ο κουβάς του διαστήματος (όχι μεταφορά σε αυτόν).
        # Ημερομηνία όπως στο archive_rows (ο χαρακτηρισμός παίρνει του παραστατικού του)· «όλα» = όλος ο κουβάς.
        # ponytail: ο κουβάς δεν χωρίζεται σε έσοδα/έξοδα — σβήνει και του άλλου βιβλίου στο διάστημα· τον
        # ξαναφέρνει η επόμενη ανάκτηση εκείνου.
        if not date_from:
            conn.execute("DELETE FROM mydata_archive WHERE company_id = ?", (company_id,))
        else:
            conn.execute(
                "DELETE FROM mydata_archive WHERE company_id = ? AND rowid IN (SELECT a.rowid FROM mydata_archive a "
                "LEFT JOIN documents d ON d.company_id = a.company_id AND d.mark = a.invoice_mark "
                "LEFT JOIN mydata_archive ai ON ai.company_id = a.company_id AND ai.kind = 'invoice' "
                "AND ai.mark = a.invoice_mark WHERE a.company_id = ? "
                "AND COALESCE(a.event_date, d.issue_date, ai.event_date) BETWEEN ? AND ?)",
                (company_id, company_id, date_from, date_to),
            )
        cursor = conn.execute(
            "DELETE FROM documents "
            "WHERE company_id = ? AND kind = ? AND issue_date BETWEEN ? AND ?",
            (company_id, kind, date_from, date_to),
        )
        return cursor.rowcount


def stage_action(company_id: int | None, mark: str, action: str) -> bool:
    """Στήνει τοπικά ενέργεια απόρριψης/ακύρωσης στο «Χαρακτηρισμένα» για μαζική
    αποστολή. action: 'reject' | 'cancel'. Επιστρέφει False αν δεν βρέθηκε."""
    if not company_id:
        return False
    marker = {
        "reject": [{"transaction_mode": "1"}],
        "cancel": [{"transaction_mode": "cancel"}],
    }.get(action, [])
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, mark),
        ).fetchone()
        if row is None:
            return False
        _snapshot_sent(conn, row["id"])
        conn.execute("DELETE FROM classifications WHERE document_id = ?", (row["id"],))
        conn.execute(
            "UPDATE documents SET status = 'classified', local_action = ?, "
            "draft_json = NULL, cls_json = ? WHERE id = ?",
            (action, json.dumps(marker, ensure_ascii=False), row["id"]),
        )
    return True


def create_local_expense(
    company_id: int, doc: dict, draft: dict, cls_info: list, origin: tuple | None = None
) -> int | None:
    """Δημιουργεί ΤΟΠΙΚΑ αυτοτιμολογούμενη εγγραφή εξόδου (Νέα εγγραφή), σε κατάσταση
    «Χαρακτηρισμένο» με local_action='create'. Διαβιβάζεται μαζικά μέσω /send.
    Το mark είναι προσωρινό μέχρι τη διαβίβαση. origin = (template_id, "YYYY-MM"): αν υπάρχει
    ήδη εγγραφή του προτύπου για τον μήνα, δεν δημιουργείται (επιστρέφει None)."""
    import uuid as _uuid

    now = datetime.now(UTC).isoformat(timespec="seconds")
    tmp_mark = "NEW-" + _uuid.uuid4().hex[:12]
    template_id, period = origin or (None, None)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO documents (company_id, kind, mark, issue_date, "
                "counterparty_vat, invoice_type, series, aa, "
                "total_net, total_vat, total_gross, is_self_issued, status, "
                "local_action, draft_json, lines_json, cls_json, updated_at, template_id, period) "
                "VALUES (?, 'expense', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'classified', "
                "'create', ?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    tmp_mark,
                    doc.get("issue_date"),
                    doc.get("issuer_vat"),
                    doc.get("invoice_type"),
                    doc.get("series"),
                    doc.get("aa"),
                    doc.get("total_net"),
                    doc.get("total_vat"),
                    doc.get("total_gross"),
                    json.dumps(draft, ensure_ascii=False),
                    json.dumps(doc.get("lines") or [], ensure_ascii=False),
                    json.dumps(cls_info, ensure_ascii=False),
                    now,
                    template_id,
                    period,
                ),
            )
            _set_extra_totals(conn, company_id, dict(doc, mark=tmp_mark))
            return cur.lastrowid
    except sqlite3.IntegrityError:  # υπάρχει ήδη εγγραφή του προτύπου για τον μήνα
        return None


def finalize_created(doc_id: int, new_mark: str | None) -> None:
    """Μετά από επιτυχή διαβίβαση Νέας εγγραφής: πραγματικό mark + «Απεσταλμένο»· από εδώ και πέρα κανονικό
    παραστατικό (όχι πρόχειρο): χαρακτηρίζεται, δεν διαγράφεται/επεξεργάζεται. Το draft_json μένει για «Αντιγραφή»."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET mark = ?, status = 'sent', local_action = 'classify', classification_mark = ?, "
            "sent_at = ? WHERE id = ?",
            (new_mark or ("SENT-" + str(doc_id)), new_mark or "", now, doc_id),
        )


def delete_document_by_id(doc_id: int) -> None:
    with get_conn() as conn:
        _to_archive(conn, "id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


# --------------------------------------------------------------------- #
# Επιτρεπόμενοι συνδυασμοί χαρακτηρισμού (ΑΑΔΕ «Συνδυασμοί χαρακτηρισμών»)
# --------------------------------------------------------------------- #
_RE_ITYPE = _re.compile(r"^\d{1,2}(\.\d{1,3})?$")
_RE_CAT = _re.compile(r"^category\d+_\d+$")
_RE_E3 = _re.compile(r"^E3_\w+$")


def import_combos_csv(text: str) -> int:
    """Εισαγωγή επιτρεπόμενων συνδυασμών από csv/tsv/txt. Ανιχνεύει σε κάθε γραμμή
    τα κελιά που είναι τύπος παραστατικού (π.χ. 1.1), κατηγορία (category2_x) και
    τύπος E3 (E3_xxx) — ανεξάρτητα από θέση/επικεφαλίδες. Αντικαθιστά τους παλιούς.
    Επιστρέφει το πλήθος συνδυασμών (invoice_type × category × e3)."""
    combos = set()
    for line in text.splitlines():
        cells = _re.split(r"[\t;,]", line)
        itypes, cats, e3s = [], [], []
        for raw in cells:
            v = raw.strip().strip('"').strip()
            if not v:
                continue
            if _RE_ITYPE.match(v):
                itypes.append(v)
            elif _RE_CAT.match(v):
                cats.append(v)
            elif _RE_E3.match(v):
                e3s.append(v)
        for t in itypes:
            for cat in cats:
                for e in e3s:
                    combos.add((t, cat, e))
    with get_conn() as conn:
        conn.execute("DELETE FROM classification_combos")
        if combos:
            conn.executemany(
                "INSERT OR IGNORE INTO classification_combos "
                "(invoice_type, category, e3_type) VALUES (?, ?, ?)",
                list(combos),
            )
    return len(combos)


def _col_num(ref: str | None) -> int:
    match = _re.match(r"[A-Z]+", ref or "")
    if match is None:
        raise ValueError(f"Invalid Excel cell reference: {ref!r}")
    letters = match.group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _read_xlsx(data: bytes) -> dict:
    """Επιστρέφει {sheet_name: [rows]} όπου κάθε row = {col_num: value}. Stdlib μόνο."""
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    z = zipfile.ZipFile(io.BytesIO(data))

    shared = []
    try:
        sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in sst.iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    except KeyError:
        pass

    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid2target = {r.get("Id"): r.get("Target") for r in rels}
    wb = ET.fromstring(z.read("xl/workbook.xml"))

    sheets = {}
    for s in wb.iter(ns + "sheet"):
        target = rid2target.get(s.get(rns + "id")) or ""
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        root = ET.fromstring(z.read(path))
        rows = []
        for row in root.iter(ns + "row"):
            cells = {}
            for c in row.findall(ns + "c"):
                t = c.get("t")
                v = c.find(ns + "v")
                istag = c.find(ns + "is")
                if t == "s" and v is not None:
                    if v.text is None:
                        raise ValueError("Missing shared-string index in Excel cell")
                    val = shared[int(v.text)]
                elif t == "inlineStr" and istag is not None:
                    val = "".join(x.text or "" for x in istag.iter(ns + "t"))
                elif v is not None:
                    val = v.text or ""
                else:
                    val = ""
                cells[_col_num(c.get("r"))] = (val or "").strip()
            if cells:
                rows.append(cells)
        sheets[s.get("name")] = rows
    return sheets


def import_combos_xlsx(data: bytes) -> int:
    """Εισαγωγή από το xlsx «Συνδυασμοί χαρακτηρισμών» της ΑΑΔΕ: ένα φύλλο ανά τύπο
    παραστατικού (όνομα φύλλου = invoice_type). Μέσα, μπλοκ στηλών «κατηγορία → E3»
    (Εσόδων category1_x και Εξόδων category2_x), όπου η κατηγορία γράφεται μία φορά
    και ακολουθούν οι E3 από κάτω (carry-forward). Αντικαθιστά τους παλιούς."""
    sheets = _read_xlsx(data)
    combos = set()
    for sheet_name, rows in sheets.items():
        itype = sheet_name.strip()
        if not _RE_ITYPE.match(itype):
            continue
        # Εντόπισε στήλες κατηγορίας και στήλες E3 (βάσει περιεχομένου).
        cat_cols, e3_cols = set(), set()
        for r in rows:
            for col, val in r.items():
                if _RE_CAT.match(val):
                    cat_cols.add(col)
                elif _RE_E3.match(val):
                    e3_cols.add(col)
        # Κάθε στήλη E3 ζευγαρώνει με την πλησιέστερη στήλη κατηγορίας αριστερά της.
        pairs = []
        for e3c in e3_cols:
            left = [c for c in cat_cols if c < e3c]
            if left:
                pairs.append((max(left), e3c))
        for cat_col, e3_col in pairs:
            current = None
            cat_e3: dict[str, set] = {}
            cat_text: dict[str, str] = {}
            for r in rows:
                cv = r.get(cat_col, "").strip()
                if _RE_CAT.match(cv):
                    current = cv
                    cat_e3.setdefault(current, set())
                    cat_text.setdefault(current, "")
                if not current:
                    continue
                ev = r.get(e3_col, "").strip()
                if not ev:
                    continue
                if _RE_E3.match(ev):
                    cat_e3[current].add(ev)
                else:
                    cat_text[current] += " " + ev.lower()
            # Κατηγορίες με συγκεκριμένα E3, ή markers: '*'=οποιοδήποτε («οι παραπάνω/
            # όλες»), '-'=χωρίς E3 («δεν ενημερώνει E3»).
            for cat, e3s in cat_e3.items():
                if e3s:
                    for e in e3s:
                        combos.add((itype, cat, e))
                    continue
                low = cat_text.get(cat, "")
                if "δεν ενημερ" in low or "μη διαθέσιμο" in low or (
                    "δεν" in low and "διαθέσιμο" in low
                ):
                    combos.add((itype, cat, "-"))
                elif low.strip():
                    combos.add((itype, cat, "*"))
    with get_conn() as conn:
        conn.execute("DELETE FROM classification_combos")
        if combos:
            conn.executemany(
                "INSERT OR IGNORE INTO classification_combos "
                "(invoice_type, category, e3_type) VALUES (?, ?, ?)",
                list(combos),
            )
    return len(combos)


def combos_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM classification_combos"
        ).fetchone()["n"]


def clear_combos() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM classification_combos")


def combos_invoice_types() -> set:
    """Τύποι παραστατικών που έχουν ορισμένους συνδυασμούς (άρα φιλτράρονται)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT invoice_type FROM classification_combos"
        ).fetchall()
    return {r["invoice_type"] for r in rows}


def combos_for_type(invoice_type: str | None) -> dict:
    """{category: [e3_type,...]} επιτρεπόμενα για τον τύπο. Κενό dict = χωρίς κανόνες
    (→ ο caller δείχνει τα πάντα)."""
    if not invoice_type:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, e3_type FROM classification_combos "
            "WHERE invoice_type = ? ORDER BY category, e3_type",
            (invoice_type,),
        ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["category"], []).append(r["e3_type"])
    # Επέκταση marker '*' («οι παραπάνω/όλες») στην ένωση των συγκεκριμένων E3 του
    # τύπου. Το '-' («δεν ενημερώνει E3») μένει ως έχει (κατηγορία χωρίς E3).
    union = sorted({e for lst in out.values() for e in lst if e not in ("*", "-")})
    for cat, lst in out.items():
        if "*" in lst:
            out[cat] = union if union else ["*"]
    return out


# --------------------------------------------------------------------- #
# Πρότυπα «Νέας εγγραφής» (ανά εταιρεία)
# --------------------------------------------------------------------- #
def list_templates(company_id: int | None) -> list[dict]:
    if not company_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, monthly_day, every_months, start_month FROM expense_templates WHERE company_id = ? ORDER BY name",
            (company_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_template(company_id: int | None, template_id) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, draft_json, monthly_day, every_months, start_month FROM expense_templates WHERE company_id = ? AND id = ?",
            (company_id or 0, template_id),
        ).fetchone()
    return {"id": row["id"], "name": row["name"], "draft": json.loads(row["draft_json"]),
            "monthly_day": row["monthly_day"], "every_months": row["every_months"], "start_month": row["start_month"]} if row else None


def set_template_monthly_day(company_id: int | None, template_id, day: int | None,
                             every_months: int = 1, start_month: int = 1) -> None:
    """Πρόγραμμα του προτύπου: ημέρα του μήνα (None = όχι προγραμματισμένο), κάθε πόσους μήνες
    και από ποιον μήνα του έτους (π.χ. 3 και 3 = Μάρτιος, Ιούνιος, Σεπτέμβριος, Δεκέμβριος)."""
    with get_conn() as conn:
        conn.execute("UPDATE expense_templates SET monthly_day = ?, every_months = ?, start_month = ? "
                     "WHERE company_id = ? AND id = ?", (day, every_months, start_month, company_id or 0, template_id))


def template_periods(company_id: int | None, period: str) -> dict[int, dict]:
    """template_id → εγγραφή (mark, status) όσων προτύπων έχουν ήδη εγγραφή τον μήνα."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT template_id, mark, status FROM documents WHERE company_id = ? AND period = ? "
            "AND template_id IS NOT NULL", (company_id or 0, period),
        ).fetchall()
    return {r["template_id"]: dict(r) for r in rows}


def save_template(company_id: int, name: str, draft: dict) -> int:
    """Νέο πρότυπο ή αντικατάσταση του ομώνυμου· επιστρέφει το id."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO expense_templates (company_id, name, draft_json) VALUES (?, ?, ?) "
            "ON CONFLICT(company_id, name) DO UPDATE SET draft_json = excluded.draft_json RETURNING id",
            (company_id, name, json.dumps(draft, ensure_ascii=False)),
        ).fetchone()[0]


def delete_template(company_id: int | None, template_id) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM expense_templates WHERE company_id = ? AND id = ?", (company_id or 0, template_id))


def next_aa(company_id: int | None, counterparty_vat: str, series: str) -> int:
    """Επόμενος Α/Α: ο Α/Α του τελευταίου (κατά ημ/νία έκδοσης) παραστατικού εξόδου με ίδιο
    συναλλασσόμενο και ίδια σειρά + 1. Μετράνε και τα τοπικά drafts, ώστε δύο εγγραφές του
    μήνα να μη συγκρούονται· μη αριθμητικοί Α/Α παραλείπονται."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT aa FROM documents WHERE company_id = ? AND kind = 'expense' "
            "AND COALESCE(counterparty_vat, '') = ? AND series = ? "
            "ORDER BY issue_date DESC, id DESC",
            (company_id or 0, counterparty_vat or "", series),
        ).fetchall()
    last = next((int(r["aa"]) for r in rows if (r["aa"] or "").strip().isdigit()), 0)
    return last + 1


def template_with_series(company_id: int | None, series: str, exclude_name: str) -> str | None:
    """Όνομα ΑΛΛΟΥ προτύπου της εταιρείας με την ίδια σειρά (κάθε πρότυπο έχει δική του)."""
    for r in list_templates(company_id):
        tp = get_template(company_id, r["id"])
        if tp["name"] != exclude_name and (tp["draft"].get("series") or "").strip() == series:
            return tp["name"]
    return None


def yearly_documents(company_id: int | None, kind: str, statuses: list[str], year: str) -> list[dict]:
    """Παραστατικά ενός έτους (κατά ημ/νία έκδοσης) με όλα τα σύνολα σύνοψης και τον συναλλασσόμενο,
    για την ετήσια σύνοψη και τη σύγκριση ετών."""
    if not company_id:
        return []
    cols = ", ".join(f"d.{c}" for c in ("issue_date", "invoice_type", "counterparty_vat", "total_net", "total_vat",
                                         "cls_json", "lines_json") + EXTRA_TOTALS)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {cols}, s.name AS counterparty_name FROM documents AS d "
            "LEFT JOIN suppliers AS s ON s.vat = d.counterparty_vat WHERE d.company_id = ? AND d.kind = ? "
            f"AND d.status IN ({','.join('?' * len(statuses))}) AND d.issue_date >= ? AND d.issue_date < ? || '~' ORDER BY d.mark",  # = έτος (πρόθεμα), με χρήση του index
            [company_id, kind, *statuses, year, year],
        ).fetchall()
    return [dict(r) for r in rows]


def period_documents(company_id: int | None, kind: str, date_from: str, date_to: str, xml: bool = True) -> list[dict]:
    """Όλα τα παραστατικά ενός διαστήματος (yyyy-mm-dd, κατά ημ/νία έκδοσης), κάθε κατάστασης,
    με γραμμές και χαρακτηρισμούς — για τη δήλωση ΦΠΑ. xml=False: χωρίς raw_xml (αποσυμπίεση ανά γραμμή)·
    το χρειάζονται μόνο οι κωδικοί εξαίρεσης ΦΠΑ (342–349/310), που δεν αλλάζουν πληρωτέο/πιστωτικό."""
    if not company_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.mark, d.issue_date, d.invoice_type, d.series, d.aa, d.counterparty_vat, d.status, "
            f"d.local_action, d.total_net, d.total_vat, d.lines_json, d.cls_json, {'d.raw_xml' if xml else 'NULL AS raw_xml'}, "
            "s.name AS counterparty_name "
            "FROM documents AS d LEFT JOIN suppliers AS s ON s.vat = d.counterparty_vat "
            "WHERE d.company_id = ? AND d.kind = ? AND d.issue_date BETWEEN ? AND ? "
            "ORDER BY d.issue_date, d.series, d.aa",
            (company_id, kind, date_from, date_to),
        ).fetchall()
    return [dict(r) for r in rows]


_restores = 0  # επαναφορές backup σε αυτή τη διεργασία: η βάση άλλαξε ολόκληρη (και ο μετρητής doc_rev)


def documents_rev(company_id: int | None) -> tuple[int, int]:
    """Έκδοση των παραστατικών μιας εταιρείας: αλλάζει με κάθε προσθήκη / αλλαγή / διαγραφή (triggers doc_rev)
    ή επαναφορά backup — για cache υπολογισμών που εξαρτώνται μόνο από αυτά (π.χ. μεταφορά ΦΠΑ)."""
    with get_conn() as conn:
        row = conn.execute("SELECT rev FROM doc_rev WHERE company_id = ?", (company_id or 0,)).fetchone()
    return _restores, row["rev"] if row else 0


def document_years(company_id: int | None) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(issue_date, 1, 4) AS y FROM documents "
            "WHERE company_id = ? AND issue_date IS NOT NULL ORDER BY y DESC",
            (company_id or 0,),
        ).fetchall()
    return [r["y"] for r in rows if r["y"]]


def month_documents(company_id: int | None, kind: str, statuses: list[str], year_month: str) -> list[dict]:
    """Παραστατικά ενός μήνα (yyyy-mm, κατά ημ/νία έκδοσης) με επωνυμία συναλλασσόμενου και όλα
    τα σύνολα σύνοψης — τα ίδια που αθροίζει η ετήσια σύνοψη για τη γραμμή του μήνα."""
    if not company_id:
        return []
    cols = ", ".join(f"d.{c}" for c in (
        "mark", "issue_date", "invoice_type", "series", "aa", "counterparty_vat", "status",
        "total_net", "total_vat", "total_gross", "cls_json") + EXTRA_TOTALS)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {cols}, s.name AS counterparty_name FROM documents AS d "
            "LEFT JOIN suppliers AS s ON s.vat = d.counterparty_vat "
            f"WHERE d.company_id = ? AND d.kind = ? AND d.status IN ({','.join('?' * len(statuses))}) "
            "AND d.issue_date >= ? AND d.issue_date < ? || '~' ORDER BY d.issue_date, d.series, d.aa",  # = μήνας
            [company_id, kind, *statuses, year_month, year_month],
        ).fetchall()
    return [dict(r) for r in rows]
