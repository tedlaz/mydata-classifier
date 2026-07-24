"""
Μόνιμη αποθήκευση σε SQLite (βιβλίο εσόδων-εξόδων).

Αντικαθιστά τα προηγούμενα JSON stores (companies.json, supplier_names_*,
supplier_rules_*, pending_classifications_*) και το per-session pickle cache.

- Εταιρείες / λογιστής / ρυθμίσεις: πίνακες companies / accountant / settings.
- Προμηθευτές (επωνυμία + κανόνας χαρακτηρισμού): πίνακας suppliers, ανά εταιρεία.
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name           TEXT NOT NULL,
    aade_user_id           TEXT,
    aade_subscription_key  TEXT,
    aade_vat_number        TEXT,
    mydata_env             TEXT DEFAULT 'prod',
    use_accountant         INTEGER DEFAULT 0
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

-- Προμηθευτές: ΚΟΙΝΟΙ για όλες τις εταιρείες (επωνυμία + κανόνας χαρακτηρισμού).
CREATE TABLE IF NOT EXISTS suppliers (
    vat              TEXT PRIMARY KEY,
    name             TEXT,
    rule_type        TEXT,
    rule_category    TEXT,
    rule_vat_type    TEXT
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
}


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

        # documents: νέες στήλες + κατάργηση counterparty_name (το όνομα βγαίνει από suppliers).
        doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        for col, decl in _DOCUMENT_COLUMNS.items():
            if col not in doc_cols:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")
        if "counterparty_name" in doc_cols:
            conn.execute("ALTER TABLE documents DROP COLUMN counterparty_name")


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
        "AADE_USER_ID": row["aade_user_id"] or "",
        "AADE_SUBSCRIPTION_KEY": row["aade_subscription_key"] or "",
        "AADE_VAT_NUMBER": row["aade_vat_number"] or "",
        "MYDATA_ENV": row["mydata_env"] or "prod",
        "use_accountant": bool(row["use_accountant"]),
    }


def list_companies() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM companies ORDER BY id").fetchall()
    return [_company_row_to_dict(r) for r in rows]


def add_company(data: dict) -> int | None:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO companies (company_name, aade_user_id, aade_subscription_key, "
            "aade_vat_number, mydata_env, use_accountant) VALUES (?, ?, ?, ?, ?, ?)",
            (
                data.get("company_name", ""),
                data.get("AADE_USER_ID", ""),
                data.get("AADE_SUBSCRIPTION_KEY", ""),
                data.get("AADE_VAT_NUMBER", ""),
                data.get("MYDATA_ENV", "prod"),
                1 if data.get("use_accountant") else 0,
            ),
        )
        return cur.lastrowid


def update_company(company_id: int, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET company_name = ?, aade_user_id = ?, "
            "aade_subscription_key = ?, aade_vat_number = ?, mydata_env = ?, "
            "use_accountant = ? WHERE id = ?",
            (
                data.get("company_name", ""),
                data.get("AADE_USER_ID", ""),
                data.get("AADE_SUBSCRIPTION_KEY", ""),
                data.get("AADE_VAT_NUMBER", ""),
                data.get("MYDATA_ENV", "prod"),
                1 if data.get("use_accountant") else 0,
                company_id,
            ),
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
        "user_id": row["user_id"] or "",
        "subscription_key": row["subscription_key"] or "",
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
                data.get("user_id", ""),
                data.get("subscription_key", ""),
                data.get("env", "prod"),
            ),
        )


# --------------------------------------------------------------------- #
# Προμηθευτές: ΚΟΙΝΟΙ για όλες τις εταιρείες (επωνυμία + κανόνας χαρακτηρισμού)
# --------------------------------------------------------------------- #
def load_rules() -> dict:
    """{vat: {"type", "category", "vat_type"}} για εγγραφές με αποθηκευμένο κανόνα."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT vat, rule_type, rule_category, rule_vat_type FROM suppliers "
            "WHERE rule_type IS NOT NULL"
        ).fetchall()
    return {
        r["vat"]: {
            "type": r["rule_type"],
            "category": r["rule_category"],
            "vat_type": r["rule_vat_type"] or "",
        }
        for r in rows
    }


def save_rule(vat: str | None, ctype: str, ccat: str, vat_type: str = "") -> None:
    if not vat:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO suppliers (vat, rule_type, rule_category, rule_vat_type) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(vat) DO UPDATE SET "
            "rule_type = excluded.rule_type, rule_category = excluded.rule_category, "
            "rule_vat_type = excluded.rule_vat_type",
            (vat, ctype, ccat, vat_type),
        )


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


def list_suppliers() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT vat, name, rule_type, rule_category, rule_vat_type "
            "FROM suppliers ORDER BY name IS NULL, name, vat"
        ).fetchall()
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
            "SELECT id, status FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, doc["mark"]),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO documents (company_id, kind, mark, issue_date, "
                "counterparty_vat, invoice_type, series, aa, "
                "total_net, total_vat, total_gross, is_self_issued, status, "
                "lines_json, cls_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    now,
                ),
            )
            return
        # υπάρχει ήδη: μην χαμηλώσεις το status
        keep = _STATUS_RANK.get(existing["status"], 0) >= _STATUS_RANK.get(
            incoming_status, 0
        )
        new_status = existing["status"] if keep else incoming_status
        # Το cls_json από το myDATA το κρατάμε μόνο όταν όντως προχωράμε σε
        # confirmed (τοπικός χαρακτηρισμός έχει δικό του cls_json).
        set_cls = ", cls_json = ?" if not keep else ""
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
        if not keep:
            params.append(cls_json)
        params.append(existing["id"])
        conn.execute(
            "UPDATE documents SET issue_date = ?, counterparty_vat = ?, "
            "invoice_type = ?, series = ?, aa = ?, "
            "total_net = ?, total_vat = ?, total_gross = ?, is_self_issued = ?, "
            f"status = ?, lines_json = ?, updated_at = ?{set_cls} WHERE id = ?",
            params,
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


def count_by_status(company_id: int, kind: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents "
            "WHERE company_id = ? AND kind = ? GROUP BY status",
            (company_id, kind),
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def sum_totals(
    company_id: int | None,
    kind: str,
    statuses: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Αθροίσματα (καθαρή αξία, ΦΠΑ, σύνολο) + πλήθος για παραστατικά ενός kind,
    προαιρετικά φιλτραρισμένα ανά status και εύρος ημερομηνίας έκδοσης (ISO
    yyyy-mm-dd, inclusive). Επιστρέφει {"net","vat","gross","count"}."""
    if not company_id:
        return {"net": 0.0, "vat": 0.0, "gross": 0.0, "count": 0}
    q = (
        "SELECT COALESCE(SUM(total_net),0) AS net, COALESCE(SUM(total_vat),0) AS vat, "
        "COALESCE(SUM(total_gross),0) AS gross, COUNT(*) AS count FROM documents "
        "WHERE company_id = ? AND kind = ?"
    )
    params: list = [company_id, kind]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    if date_from:
        q += " AND issue_date >= ?"
        params.append(date_from)
    if date_to:
        q += " AND issue_date <= ?"
        params.append(date_to)
    with get_conn() as conn:
        row = conn.execute(q, params).fetchone()
    return {
        "net": row["net"] or 0.0,
        "vat": row["vat"] or 0.0,
        "gross": row["gross"] or 0.0,
        "count": row["count"] or 0,
    }


def report_documents(
    company_id: int | None,
    kind: str,
    statuses: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Παραστατικά και χαρακτηρισμοί που χρειάζονται για αναλυτικές αναφορές."""
    if not company_id:
        return []
    q = (
        "SELECT id, issue_date, invoice_type, total_net, total_vat, total_gross, "
        "lines_json, cls_json FROM documents WHERE company_id = ? AND kind = ?"
    )
    params: list = [company_id, kind]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    if date_from:
        q += " AND issue_date >= ?"
        params.append(date_from)
    if date_to:
        q += " AND issue_date <= ?"
        params.append(date_to)

    with get_conn() as conn:
        documents = [dict(r) for r in conn.execute(q, params).fetchall()]
        if not documents:
            return []
        ids = [doc["id"] for doc in documents]
        cls_rows = conn.execute(
            "SELECT document_id, line_number, classification_type, "
            "classification_category, amount FROM classifications "
            f"WHERE document_id IN ({','.join('?' * len(ids))}) ORDER BY id",
            ids,
        ).fetchall()

    by_document: dict[int, list[dict]] = {}
    for row in cls_rows:
        by_document.setdefault(row["document_id"], []).append(
            {
                "line": row["line_number"],
                "type": row["classification_type"],
                "category": row["classification_category"],
                "amount": row["amount"],
            }
        )
    for doc in documents:
        doc["lines"] = json.loads(doc.pop("lines_json") or "[]")
        local_cls = by_document.get(doc["id"])
        doc["classifications"] = (
            local_cls if local_cls is not None else json.loads(doc.pop("cls_json") or "[]")
        )
        if local_cls is not None:
            doc.pop("cls_json", None)
    return documents


def save_local_classification(
    company_id: int | None, mark: str, entries: list[dict], manual: bool = False
) -> bool:
    """Αποθηκεύει ΤΟΠΙΚΑ τον χαρακτηρισμό ανά γραμμή.
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
            "classification_category, vat_type, amount) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    doc_id,
                    e.get("line_number"),
                    e.get("classification_type"),
                    e.get("classification_category") or "",
                    e.get("vat_type") or "",
                    e.get("amount"),
                )
                for e in entries
            ],
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


def get_local_classification(doc_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT line_number, classification_type, classification_category, "
            "vat_type, amount FROM classifications WHERE document_id = ? "
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


def mark_confirmed(doc_id: int) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            (now, doc_id),
        )


def delete_document(company_id: int | None, mark: str) -> None:
    if not company_id:
        return
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM documents WHERE company_id = ? AND mark = ?",
            (company_id, mark),
        )


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
    company_id: int, doc: dict, draft: dict, cls_info: list
) -> int | None:
    """Δημιουργεί ΤΟΠΙΚΑ αυτοτιμολογούμενη εγγραφή εξόδου (Νέα εγγραφή), σε κατάσταση
    «Χαρακτηρισμένο» με local_action='create'. Διαβιβάζεται μαζικά μέσω /send.
    Το mark είναι προσωρινό μέχρι τη διαβίβαση."""
    import uuid as _uuid

    now = datetime.now(UTC).isoformat(timespec="seconds")
    tmp_mark = "NEW-" + _uuid.uuid4().hex[:12]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO documents (company_id, kind, mark, issue_date, "
            "counterparty_vat, invoice_type, series, aa, "
            "total_net, total_vat, total_gross, is_self_issued, status, "
            "local_action, draft_json, lines_json, cls_json, updated_at) "
            "VALUES (?, 'expense', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'classified', "
            "'create', ?, ?, ?, ?)",
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
            ),
        )
        return cur.lastrowid


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
