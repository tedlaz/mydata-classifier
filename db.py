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

import json
import os
import re as _re
import sqlite3
from datetime import UTC, datetime

import auth

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("MYDATA_DATA_DIR", _BASE_DIR)
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "mydata.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def backup_bytes() -> bytes:
    """Συνεπές στιγμιότυπο όλης της βάσης (online backup API· ασφαλές με WAL)."""
    src, mem = get_conn(), sqlite3.connect(":memory:")
    try:
        src.backup(mem)
        return mem.serialize()
    finally:
        src.close()
        mem.close()


def restore_bytes(data: bytes) -> str:
    """Αντικαθιστά τη βάση με το backup `data`. Πρώτα κρατά αντίγραφο της τρέχουσας
    (mydata-before-restore-*.db στον φάκελο δεδομένων) και επιστρέφει το όνομά του."""
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

        safety = f"{_SAFETY_PREFIX}{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        live, dst = get_conn(), sqlite3.connect(os.path.join(_DATA_DIR, safety))
        try:
            live.backup(dst)
            mem.backup(live)
        finally:
            live.close()
            dst.close()
    finally:
        mem.close()
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
    birth_year             INTEGER               -- ατομική: έτος γέννησης (μειωμένοι συντελεστές νέων)
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
    name             TEXT
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
}
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
        # companies: άδεια ακυρώσεων (επικίνδυνη ενέργεια — κλειστή εξ ορισμού).
        comp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
        if "allow_cancel" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN allow_cancel INTEGER DEFAULT 0")
        for col in ("allow_send", "allow_classify", "allow_new", "locked"):  # και οι υπάρχουσες ξεκινούν κλειστές
            if col not in comp_cols:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} INTEGER DEFAULT 0")
        if "vat_period" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN vat_period TEXT DEFAULT 'm'")
        if "entity_type" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN entity_type TEXT DEFAULT 'legal'")
        if "birth_year" not in comp_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN birth_year INTEGER")
        if "rule_post_mode" not in {r["name"] for r in conn.execute("PRAGMA table_info(supplier_rules)")}:
            conn.execute("ALTER TABLE supplier_rules ADD COLUMN rule_post_mode INTEGER DEFAULT 0")
        cls_cols = {r["name"] for r in conn.execute("PRAGMA table_info(classifications)")}
        for col, decl in _CLASSIFICATION_COLUMNS.items():
            if col not in cls_cols:
                conn.execute(f"ALTER TABLE classifications ADD COLUMN {col} {decl}")
        if "counterparty_name" in doc_cols:
            conn.execute("ALTER TABLE documents DROP COLUMN counterparty_name")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_template_period "
            "ON documents(company_id, template_id, period) WHERE template_id IS NOT NULL"
        )
        if "monthly_day" not in {r["name"] for r in conn.execute("PRAGMA table_info(expense_templates)")}:
            conn.execute("ALTER TABLE expense_templates ADD COLUMN monthly_day INTEGER")


# --------------------------------------------------------------------- #
# Ρυθμίσεις (settings)
# --------------------------------------------------------------------- #
def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


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
        "allow_cancel": bool(row["allow_cancel"]),
        "allow_send": bool(row["allow_send"]),
        "allow_classify": bool(row["allow_classify"]),
        "allow_new": bool(row["allow_new"]),
        "locked": bool(row["locked"]),
        "vat_period": "q" if row["vat_period"] == "q" else "m",
        "entity_type": "sole" if row["entity_type"] == "sole" else "legal",
        "birth_year": row["birth_year"],
    }


def birth_year(value) -> int | None:
    try:
        y = int(value)
    except (TypeError, ValueError):
        return None
    return y if 1900 <= y <= datetime.now().year else None


def list_companies() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM companies ORDER BY id").fetchall()
    return [_company_row_to_dict(r) for r in rows]


def add_company(data: dict) -> int | None:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO companies (company_name, aade_user_id, aade_subscription_key, "
            "aade_vat_number, mydata_env, use_accountant, allow_cancel, allow_send, allow_classify, allow_new, vat_period, entity_type, birth_year) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.get("company_name", ""),
                auth.enc(data.get("AADE_USER_ID") or ""),
                auth.enc(data.get("AADE_SUBSCRIPTION_KEY") or ""),
                data.get("AADE_VAT_NUMBER", ""),
                data.get("MYDATA_ENV", "prod"),
                1 if data.get("use_accountant") else 0,
                1 if data.get("allow_cancel") else 0,
                1 if data.get("allow_send") else 0,
                1 if data.get("allow_classify") else 0,
                1 if data.get("allow_new") else 0,
                "q" if data.get("vat_period") == "q" else "m",
                "sole" if data.get("entity_type") == "sole" else "legal",
                birth_year(data.get("birth_year")),
            ),
        )
        return cur.lastrowid


def update_company(company_id: int, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET company_name = ?, aade_user_id = ?, "
            "aade_subscription_key = ?, aade_vat_number = ?, mydata_env = ?, "
            "use_accountant = ?, vat_period = ?, entity_type = ?, birth_year = ?, "
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
                1 if data.get("use_accountant") else 0,
                "q" if data.get("vat_period") == "q" else "m",
                "sole" if data.get("entity_type") == "sole" else "legal",
                birth_year(data.get("birth_year")),
                1 if data.get("allow_cancel") else 0,
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


def list_suppliers(company_id: int | None = None) -> list[dict]:
    """Ολόκληρος ο κοινός κατάλογος· με company_id μόνο οι συναλλασσόμενοι της εταιρείας
    (με παραστατικά ή δική της πρόταση χαρακτηρισμού)."""
    q = "SELECT vat, name FROM suppliers"
    params: tuple = ()
    if company_id is not None:
        q += (
            " WHERE vat IN (SELECT counterparty_vat FROM documents WHERE company_id = ?"
            " UNION SELECT vat FROM supplier_rules WHERE company_id = ?)"
        )
        params = (company_id, company_id)
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


def delete_supplier(vat: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM suppliers WHERE vat = ?", (vat,))
        conn.execute("DELETE FROM supplier_rules WHERE vat = ?", (vat,))


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
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, status, cls_json FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, doc["mark"]),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO documents (company_id, kind, mark, issue_date, "
                "counterparty_vat, invoice_type, series, aa, "
                "total_net, total_vat, total_gross, is_self_issued, status, "
                "lines_json, cls_json, classification_mark, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        set_cls = ", cls_json = ?" if not keep or fill_cls else ""
        # Το MARK χαρακτηρισμού από το myDATA γράφεται όποτε έρχεται μη κενό — και σε
        # keep — ώστε να συμπληρώνεται και σε ήδη αποθηκευμένα «Επιβεβαιωμένα».
        # Κενή τιμή (ενσωματωμένος χαρακτηρισμός) δεν σβήνει ό,τι έχουμε τοπικά.
        cls_mark = doc.get("classification_mark") or ""
        set_mark = ", classification_mark = ?" if cls_mark else ""
        params = [
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
        ]
        if set_cls:
            params.append(cls_json)
        if cls_mark:
            params.append(cls_mark)
        params.append(existing["id"])
        conn.execute(
            "UPDATE documents SET issue_date = ?, counterparty_vat = ?, "
            "invoice_type = ?, series = ?, aa = ?, "
            "total_net = ?, total_vat = ?, total_gross = ?, is_self_issued = ?, "
            f"status = ?, lines_json = ?, updated_at = ?{set_cls}{set_mark} "
            "WHERE id = ?",
            params,
        )
        _set_extra_totals(conn, company_id, doc)


def _set_extra_totals(conn: sqlite3.Connection, company_id: int, doc: dict) -> None:
    """Λοιπά σύνολα σύνοψης (παρακρατήσεις, τέλη κ.λπ.), όταν τα έφερε το myDATA."""
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
            f"WHERE d.company_id = ? AND d.kind = ? AND d.status IN ({','.join('?' * len(statuses))})",
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
                "UPDATE documents SET status = 'confirmed', source = 'manual', "
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


def unclassify(company_id: int | None, marks: list[str]) -> int:
    """Αναιρεί τον τοπικό χαρακτηρισμό ή τη στημένη απόρριψη/ακύρωση (μόνο status='classified',
    όχι πρόχειρα νέας εγγραφής): το παραστατικό επιστρέφει στα αχαρακτήριστα. Επιστρέφει πόσα άλλαξαν."""
    if not company_id or not marks:
        return 0
    q = ",".join("?" * len(marks))
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM documents WHERE company_id = ? AND mark IN ({q}) "
            "AND status = 'classified' AND local_action IN ('classify', 'reject', 'cancel')",
            (company_id, *marks),
        )]
        for doc_id in ids:
            conn.execute("DELETE FROM classifications WHERE document_id = ?", (doc_id,))
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
            "UPDATE documents SET status = 'sent', classification_mark = ?, sent_at = ? "
            "WHERE id = ?",
            (classification_mark or "", now, doc_id),
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
            "UPDATE documents SET status = 'confirmed', confirmed_at = ?"
            f"{set_mark} WHERE id = ?",
            params,
        )


def delete_document(company_id: int | None, mark: str) -> None:
    if not company_id:
        return
    with get_conn() as conn:
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
    """Μετά από επιτυχή διαβίβαση Νέας εγγραφής: πραγματικό mark + «Απεσταλμένο»."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET mark = ?, status = 'sent', classification_mark = ?, "
            "sent_at = ? WHERE id = ?",
            (new_mark or ("SENT-" + str(doc_id)), new_mark or "", now, doc_id),
        )


def delete_document_by_id(doc_id: int) -> None:
    with get_conn() as conn:
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
            "SELECT id, name, monthly_day FROM expense_templates WHERE company_id = ? ORDER BY name",
            (company_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_template(company_id: int | None, template_id) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, draft_json, monthly_day FROM expense_templates WHERE company_id = ? AND id = ?",
            (company_id or 0, template_id),
        ).fetchone()
    return {"id": row["id"], "name": row["name"], "draft": json.loads(row["draft_json"]),
            "monthly_day": row["monthly_day"]} if row else None


def set_template_monthly_day(company_id: int | None, template_id, day: int | None) -> None:
    """Ημέρα του μήνα για τη μηνιαία εγγραφή (None = όχι μηνιαίο)."""
    with get_conn() as conn:
        conn.execute("UPDATE expense_templates SET monthly_day = ? WHERE company_id = ? AND id = ?",
                     (day, company_id or 0, template_id))


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
    """Παραστατικά ενός έτους (κατά ημ/νία έκδοσης) με όλα τα σύνολα σύνοψης, για την ετήσια σύνοψη."""
    if not company_id:
        return []
    cols = ", ".join(("issue_date", "invoice_type", "total_net", "total_vat", "cls_json") + EXTRA_TOTALS)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM documents WHERE company_id = ? AND kind = ? "
            f"AND status IN ({','.join('?' * len(statuses))}) AND substr(issue_date, 1, 4) = ?",
            [company_id, kind, *statuses, year],
        ).fetchall()
    return [dict(r) for r in rows]


def period_documents(company_id: int | None, kind: str, date_from: str, date_to: str) -> list[dict]:
    """Όλα τα παραστατικά ενός διαστήματος (yyyy-mm-dd, κατά ημ/νία έκδοσης), κάθε κατάστασης,
    με γραμμές και χαρακτηρισμούς — για τη δήλωση ΦΠΑ."""
    if not company_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.mark, d.issue_date, d.invoice_type, d.series, d.aa, d.counterparty_vat, d.status, "
            "d.local_action, d.total_net, d.total_vat, d.lines_json, d.cls_json, s.name AS counterparty_name "
            "FROM documents AS d LEFT JOIN suppliers AS s ON s.vat = d.counterparty_vat "
            "WHERE d.company_id = ? AND d.kind = ? AND d.issue_date BETWEEN ? AND ? "
            "ORDER BY d.issue_date, d.series, d.aa",
            (company_id, kind, date_from, date_to),
        ).fetchall()
    return [dict(r) for r in rows]


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
            "AND substr(d.issue_date, 1, 7) = ? ORDER BY d.issue_date, d.series, d.aa",
            [company_id, kind, *statuses, year_month],
        ).fetchall()
    return [dict(r) for r in rows]
