"""
myDATA Expense Classifier - Flask UI
Εκτέλεση:  python app.py  →  http://127.0.0.1:5000
"""

import calendar
import glob
import json
import os
import random
import re
import statistics
import sys
import threading
import tomllib
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

import auth
import db
import vat_return
from classifications import (
    CREDIT_INVOICE_TYPES,
    EU_COUNTRIES,
    EXPENSE_CATEGORIES,
    EXPENSE_TYPES,
    INCOME_CATEGORIES,
    INCOME_TYPES,
    INVOICE_TYPE_NAMES,
    TAX_CATEGORIES,
    TAX_TYPES,
    VAT_CATEGORY_RATES,
    VAT_TYPES,
    country_allowed,
    country_rule,
)
from ensure_env import ensure_env, env_path
from mydata_client import (
    SELF_EXPENSE_TYPES,
    ExpenseInvoice,
    InvoiceLine,
    MyDataClient,
    MyDataError,
)

ensure_env()  # δημιουργεί .env με ασφαλές FLASK_SECRET στην πρώτη εκκίνηση
load_dotenv(env_path())

# Ώρα Ελλάδας: όλες οι «σήμερα/τώρα» τιμές να είναι ανεξάρτητες από τη ζώνη του server
ATHENS = ZoneInfo("Europe/Athens")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")
# Τα cookies αγνοούν το port: το default «session» το πατάει κάθε άλλη τοπική εφαρμογή
# (ή δεύτερο instance) στο 127.0.0.1 → άκυρο cookie → ξανά login.
app.config["SESSION_COOKIE_NAME"] = "mydata_session"


@app.before_request
def _same_origin_post():
    """CSRF: POST μόνο από σελίδες της ίδιας εφαρμογής. Ο browser στέλνει πάντα Origin σε
    cross-site POST· χωρίς Origin/Referer (curl, tests) επιτρέπεται."""
    if request.method == "POST":
        src = request.headers.get("Origin") or request.headers.get("Referer")
        if src and urlparse(src).netloc != request.host:
            abort(403)


_PUBLIC = {"login", "setup", "static"}


@app.before_request
def _require_login():
    """Κεντρικός χρήστης (βλ. auth.py): χωρίς χρήστη → /setup, χωρίς ξεκλείδωμα → /login.
    Το session["auth"] πρέπει να ταιριάζει με το nonce στη μνήμη: ένα πλαστό cookie
    (με το FLASK_SECRET) δεν αρκεί, και κάθε restart ζητά ξανά κωδικό."""
    if app.testing or request.endpoint in _PUBLIC:
        return None
    if db.get_auth() is None:
        return redirect(url_for("setup"))
    if not auth.unlocked() or session.get("auth") != auth.nonce:
        return redirect(url_for("login"))
    return None


# Ενέργειες που ανοίγει μόνο η αντίστοιχη ρύθμιση της εταιρείας (Παράμετροι) — κλειστές εξ ορισμού.
# Το /send ελέγχει ανά παραστατικό (classify → allow_classify, create → allow_new).
_GATED = {
    "allow_classify": ({"auto_classify", "classify", "submit", "bulk_classify"},
                       "Οι χαρακτηρισμοί είναι απενεργοποιημένοι για την εταιρεία — ενεργοποίησέ τους στις Παραμέτρους εταιρείας."),
    "allow_new": ({"new_expense", "new_expense_submit", "expense_template_save", "expense_template_delete",
                   "recurring", "recurring_days", "recurring_create"},
                  "Η «Νέα εγγραφή» είναι απενεργοποιημένη για την εταιρεία — ενεργοποίησέ την στις Παραμέτρους εταιρείας."),
}


@app.before_request
def _require_feature():
    for flag, (endpoints, msg) in _GATED.items():
        if request.endpoint in endpoints and not (get_active_company() or {}).get(flag):
            flash(msg, "error")
            return redirect(url_for("invoices"))
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db.get_auth():
        return redirect(url_for("login"))
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        if not user or not pw or pw != request.form.get("password2"):
            flash("Όνομα χρήστη υποχρεωτικό· κωδικός υποχρεωτικός, ίδιος και στις δύο θέσεις.", "error")
        else:
            db.save_auth(auth.create(user, pw))
            db.encrypt_credentials()
            session["auth"] = auth.nonce
            flash("✔ Δημιουργήθηκε ο χρήστης. Τα κλειδιά myDATA αποθηκεύονται πλέον κρυπτογραφημένα.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("login.html", setup=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    rec = db.get_auth()
    if rec is None:
        return redirect(url_for("setup"))
    if request.method == "POST":
        if auth.unlock(request.form.get("username", "").strip(), request.form.get("password", ""), rec):
            session["auth"] = auth.nonce
            db.encrypt_credentials()  # π.χ. plaintext κλειδιά από εισαγωγή/παλιό backup
            return redirect(url_for("dashboard"))
        flash("Λάθος όνομα χρήστη ή κωδικός.", "error")
    return render_template("login.html", setup=False)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    auth.lock()
    return redirect(url_for("login"))


@app.route("/password", methods=["POST"])
def change_password():
    back = redirect(url_for("parameters", tab="security"))
    rec = db.get_auth()
    user = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if not auth.unlock(rec["auth_user"], request.form.get("old_password", ""), rec):
        flash("Λάθος τρέχων κωδικός.", "error")
        return back
    session["auth"] = auth.nonce  # το unlock ανανεώνει το nonce
    if not user or not pw or pw != request.form.get("password2"):
        flash("Όνομα χρήστη υποχρεωτικό· νέος κωδικός υποχρεωτικός, ίδιος και στις δύο θέσεις.", "error")
        return back
    db.save_auth(auth.rewrap(user, pw))
    flash("✔ Άλλαξε ο κωδικός.", "ok")
    return back


@app.template_filter("el_amount")
def format_el_amount(value) -> str:
    """Ποσό με ελληνικά διαχωριστικά χιλιάδων και δεκαδικών."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if abs(amount) < 0.005:
        amount = 0.0
    return f"{amount:,.2f}".translate(str.maketrans({",": ".", ".": ","}))


# Cache τελευταίας αναζήτησης (απλή λύση για single-user εργαλείο)

# ------------------------------------------------------------------ #
# "Κανόνες" ανά ΑΦΜ προμηθευτή: ο τελευταίος επιτυχημένος χαρακτηρισμός
# αποθηκεύεται σε JSON και προτείνεται αυτόματα την επόμενη φορά.
# ------------------------------------------------------------------ #
# ------------------------------------------------------------------ #
# Εταιρείες (πολυ-εταιρική λειτουργία): λίστα προφίλ σε companies.json,
# όπως [{"company_name": ..., "AADE_USER_ID": ..., "AADE_SUBSCRIPTION_KEY": ...,
#        "AADE_VAT_NUMBER": ..., "MYDATA_ENV": "dev"|"prod"}, ...]
# Η ενεργή εταιρεία (δείκτης) αποθηκεύεται σε active_company.json.
# ------------------------------------------------------------------ #
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Φάκελος δεδομένων: μπορεί να δείχνει σε mounted volume (container) μέσω
# MYDATA_DATA_DIR· αλλιώς δίπλα στον κώδικα (τοπική εκτέλεση, όπως πριν).
_DATA_DIR = os.getenv("MYDATA_DATA_DIR", _BASE_DIR)
os.makedirs(_DATA_DIR, exist_ok=True)

# Μόνιμη αποθήκευση σε SQLite (βλ. db.py). Δημιουργία schema στην εκκίνηση.
db.init_db()


def _combos_version(name: str) -> tuple | None:
    """Έκδοση από όνομα αρχείου ΑΑΔΕ, π.χ. syndiasmoi_xaraktirismwn_v1.0.8.xlsx → (1, 0, 8)."""
    m = re.search(r"v(\d+(?:\.\d+)*)", os.path.basename(name or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _bundled_combos() -> tuple[str, tuple] | None:
    """Το νεότερο αρχείο συνδυασμών που συνοδεύει την εφαρμογή (docs/)."""
    files = [(f, _combos_version(f)) for f in glob.glob(os.path.join(_BASE_DIR, "docs", "syndiasmoi_xaraktirismwn_v*.xlsx"))]
    files = [x for x in files if x[1]]
    return max(files, key=lambda x: x[1]) if files else None


def _load_bundled_combos() -> None:
    """Πρώτη εκκίνηση (ή νεότερο αρχείο σε νέα έκδοση της εφαρμογής): φόρτωση των συνδυασμών
    ΑΑΔΕ. Ποτέ πάνω από νεότερη ή ίδια έκδοση που υπάρχει ήδη· ο «Καθαρισμός» δεν ξαναφορτώνει."""
    bundled = _bundled_combos()
    if not bundled:
        return
    path, version = bundled
    stored = _combos_version("v" + (db.get_setting("combos_version") or ""))
    if stored and stored >= version:
        return
    try:
        with open(path, "rb") as fh:
            if db.import_combos_xlsx(fh.read()):
                db.set_setting("combos_version", ".".join(map(str, version)))
    except Exception as e:  # noqa: BLE001 — χαλασμένο αρχείο δεν σταματά την εκκίνηση
        print(f"Αποτυχία φόρτωσης συνδυασμών από {path}: {e}")


_load_bundled_combos()


def _active_company_id() -> int | None:
    c = get_active_company()
    return c["id"] if c else None


def get_accountant() -> dict:
    """Κοινά credentials λογιστή (προαιρετικά)."""
    return db.get_accountant()


def save_accountant(data: dict):
    db.save_accountant(data)


def load_companies() -> list:
    """Λίστα εταιρειών (dicts με τα ίδια κλειδιά όπως πριν + εσωτερικό "id")."""
    return db.list_companies()


def get_active_index() -> int:
    """Θέση της ενεργής εταιρείας στη (σταθερά ταξινομημένη) λίστα."""
    companies = load_companies()
    if not companies:
        return 0
    active_id = db.get_active_company_id()
    for i, c in enumerate(companies):
        if c["id"] == active_id:
            return i
    return 0


def get_active_company() -> dict | None:
    companies = load_companies()
    if not companies:
        return None
    return companies[get_active_index()]


def send_allowed() -> bool:
    """Κάθε αποστολή στο myDATA (χαρακτηρισμοί, νέες εγγραφές, ακυρώσεις, απορρίψεις)
    επιτρέπεται μόνο αν το έχει ενεργοποιήσει η εταιρεία· αλλιώς μόνο ανάγνωση."""
    c = get_active_company()
    return bool(c and c.get("allow_send"))


def cancel_allowed() -> bool:
    """Ακυρώσεις ΚΑΙ απορρίψεις παραστατικών επιτρέπονται μόνο αν το έχει ενεργοποιήσει η
    εταιρεία (Παράμετροι) — και είναι ενεργή η αποστολή στο myDATA."""
    c = get_active_company()
    return send_allowed() and bool(c.get("allow_cancel"))


SEND_DISABLED_MSG = ("Η αποστολή στο myDATA είναι απενεργοποιημένη για την εταιρεία — "
                     "ενεργοποίησέ την στις Παραμέτρους εταιρείας.")
CANCEL_DISABLED_MSG = ("Οι ακυρώσεις/απορρίψεις παραστατικών είναι απενεργοποιημένες για την εταιρεία — "
                       "ενεργοποίησέ τες στις Παραμέτρους εταιρείας.")


def get_own_vat() -> str:
    c = get_active_company()
    if c and c.get("AADE_VAT_NUMBER"):
        return str(c["AADE_VAT_NUMBER"]).strip()
    return (os.getenv("AADE_VAT_NUMBER") or "").strip()


# ------------------------------------------------------------------ #
# Συναλλασσόμενοι: επωνυμία ανά ΑΦΜ ΚΟΙΝΗ για όλες τις εταιρείες· προτάσεις
# χαρακτηρισμού ΤΗΣ ΕΝΕΡΓΗΣ ΕΤΑΙΡΕΙΑΣ. Λεπτά wrappers γύρω από το db.
# ------------------------------------------------------------------ #
def load_rules() -> dict:
    return db.load_rules(_active_company_id())


def save_rule(issuer_vat: str | None, ctype: str, ccat: str, vat_type: str = ""):
    db.save_rule(_active_company_id(), issuer_vat, ctype, ccat, vat_type)


def _learn_patterns(inv, entries: list[dict]) -> dict:
    """Προτάσεις από έναν χαρακτηρισμό, ανά κατηγορία ΦΠΑ γραμμής:
    {vat_category: {category, type, vat_type}}. Για κάθε κατηγορία ΦΠΑ κρατά το Ε3 με το
    μεγαλύτερο ποσό και τον χαρακτηρισμό ΦΠΑ της ίδιας γραμμής/ομάδας. Δέχεται εγγραφές
    ανά γραμμή (line_number) ή ανά παραστατικό (line_number None + vat_category ομάδας)."""
    line_cat = {ln.line_number: str(ln.vat_category or "") for ln in inv.lines or []}
    best: dict = {}
    vat_of: dict = {}
    for e in entries:
        if e.get("line_number") is None:
            key = str(e.get("vat_category") or "")
        else:
            key = line_cat.get(e["line_number"], "")
        ctype = e.get("classification_type") or ""
        if ctype.startswith("VAT_"):
            vat_of.setdefault(key, ctype)
        elif ctype and (key not in best or (e.get("amount") or 0) > (best[key].get("amount") or 0)):
            best[key] = e
    return {
        k: {"category": e["classification_category"], "type": e["classification_type"], "vat_type": vat_of.get(k, "")}
        for k, e in best.items()
    }


def _doc_patterns(row: dict) -> dict:
    """Προτάσεις {vat_category: {category, type, vat_type}} από τον χαρακτηρισμό ενός
    παραστατικού (τοπικός → συγκεντρωτικός myDATA → ανά γραμμή myDATA)."""
    inv = _row_to_invoice(row)
    entries = db.get_local_classification(row["id"])
    if entries:
        return _learn_patterns(inv, entries)

    def add(line, cat, r):
        entries.append({"line_number": line, "vat_category": cat, "classification_type": r["type"],
                        "classification_category": r["category"], "amount": r["amount"] or 0})
        if r.get("vat_type"):
            entries.append({"line_number": line, "vat_category": cat, "classification_type": r["vat_type"],
                            "classification_category": "", "amount": 0})

    remote = _remote_per_invoice(inv)
    if remote:
        pi_e3, pi_vat = remote
        for cat, rows in pi_e3.items():
            for r in rows:
                add(None, cat, dict(r, vat_type=pi_vat.get(cat, "")))
    else:
        line_cls, doc_cls = _document_cls_rows([], inv)
        if doc_cls and len(inv.lines) == 1:  # συγκεντρωτικός σε μονόγραμμο → στη γραμμή του
            line_cls.setdefault(inv.lines[0].line_number, []).extend(doc_cls)
            doc_cls = []
        for ln, rows in line_cls.items():
            for r in rows:
                add(ln, None, r)
        for r in doc_cls:
            add(None, None, r)
    return _learn_patterns(inv, entries)


def _learn(inv, entries: list[dict], post_mode: int = 0) -> None:
    """Ενημερώνει τις προτάσεις του συναλλασσόμενου από τον χαρακτηρισμό που αποθηκεύτηκε,
    μαζί με τον τρόπο του (ανά γραμμή / συγκεντρωτικά)."""
    cid = _active_company_id()
    db.save_rule_patterns(cid, inv.issuer_vat, _learn_patterns(inv, entries))
    db.set_rule_post_mode(cid, inv.issuer_vat, post_mode)


def _doc_post_mode(row: dict) -> int:
    """Τρόπος χαρακτηρισμού παραστατικού: τοπικός συγκεντρωτικός ή συγκεντρωτικός του myDATA."""
    return 1 if row.get("cls_post_mode") or _remote_per_invoice(_row_to_invoice(row)) else 0


def _patterns_for(vat: str | None) -> dict:
    """Προτάσεις ανά κατηγορία ΦΠΑ για έναν συναλλασσόμενο ({} αν δεν υπάρχουν)."""
    return db.load_rule_patterns(_active_company_id(), vat).get(vat, {}) if vat else {}


def load_names() -> dict:
    return db.load_names()


def save_names(names: dict):
    db.save_names(names)


def enrich_names(invoices, vat_attr="issuer_vat", name_attr="issuer_name"):
    """Συμπληρώνει ελλείπουσες επωνυμίες αντισυμβαλλομένου: πρώτα μαθαίνει από όσα
    παραστατικά έχουν όνομα, μετά γεμίζει τα κενά από την cache, και τέλος
    (προαιρετικά) ρωτάει VIES/Μητρώο ΑΑΔΕ για άγνωστα ΑΦΜ.

    Ο αντισυμβαλλόμενος διαβάζεται/γράφεται στα πεδία vat_attr/name_attr, ώστε η
    ΙΔΙΑ τεχνική & ο ΙΔΙΟΣ κοινός πίνακας (suppliers, μέσω load_names/save_names)
    να χρησιμοποιείται και για προμηθευτές (issuer, έξοδα) και για πελάτες
    (counterpart, έσοδα)."""
    import gsis

    names = load_names()
    changed = False

    # φάση 1: εκμάθηση από παραστατικά που έχουν επωνυμία
    for inv in invoices:
        vat = getattr(inv, vat_attr, None)
        name = getattr(inv, name_attr, None)
        if vat and name and names.get(vat) != name:
            names[vat] = name
            changed = True

    # φάση 2: συμπλήρωση από τον κοινό πίνακα ΠΡΩΤΑ — ό,τι υπάρχει ήδη εκεί δεν
    # ξαναζητείται ποτέ από εξωτερική υπηρεσία.
    import vies

    missing: list[str] = []
    seen_missing: set[str] = set()
    for inv in invoices:
        vat = getattr(inv, vat_attr, None)
        if getattr(inv, name_attr, None) or not vat:
            continue
        cached = names.get(vat)
        if cached:
            setattr(inv, name_attr, cached)
        elif vat not in seen_missing:
            seen_missing.add(vat)
            missing.append(vat)

    # φάση 3: ΜΑΖΙΚΗ αναζήτηση των μοναδικών άγνωστων ΑΦΜ στο VIES.
    # Το VIES REST δεν έχει batch endpoint, οπότε η "μαζική" αποστολή γίνεται
    # με παράλληλες κλήσεις (thread pool) - μία ανά μοναδικό ΑΦΜ, ταυτόχρονα.
    if missing:
        from concurrent.futures import ThreadPoolExecutor

        workers = min(8, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            found = dict(zip(missing, pool.map(vies.lookup_name, missing)))

        # fallback στο Μητρώο ΑΑΔΕ/GSIS για όσα δεν βρέθηκαν (επίσης παράλληλα)
        if gsis.gsis_available():
            rest = [v for v, n in found.items() if not n]
            if rest:
                with ThreadPoolExecutor(max_workers=min(4, len(rest))) as pool:
                    for v, n in zip(rest, pool.map(gsis.lookup_name, rest)):
                        found[v] = n

        for vat, name in found.items():
            if name:
                names[vat] = name
                changed = True

        # τελική συμπλήρωση όσων παραστατικών περίμεναν αποτέλεσμα
        for inv in invoices:
            if not getattr(inv, name_attr, None) and getattr(inv, vat_attr, None):
                setattr(inv, name_attr, names.get(getattr(inv, vat_attr)))

    if changed:
        save_names(names)
    return invoices


def enrich_issuer_names(invoices):
    """Επωνυμίες εκδοτών/προμηθευτών (έξοδα): αντισυμβαλλόμενος = issuer."""
    return enrich_names(invoices, "issuer_vat", "issuer_name")


def enrich_counterpart_names(invoices):
    """Επωνυμίες πελατών (έσοδα): αντισυμβαλλόμενος = counterpart."""
    return enrich_names(invoices, "counterpart_vat", "counterpart_name")


def get_client() -> MyDataClient:
    """Client με τα credentials της ενεργής εταιρείας - fallback στο .env
    αν δεν έχει οριστεί καμία εταιρεία (συμβατότητα με παλιά εγκατάσταση)."""
    company = get_active_company()
    if company:
        # Περίπτωση 2: η εταιρεία χρησιμοποιεί κοινά credentials λογιστή -
        # στέλνεται το ΑΦΜ της ως entityVatNumber σε κάθε κλήση.
        if company.get("use_accountant"):
            acc = get_accountant()
            user_id = (acc.get("user_id") or "").strip()
            sub_key = (acc.get("subscription_key") or "").strip()
            env = (acc.get("env") or company.get("MYDATA_ENV") or "prod").strip()
            entity_vat = str(company.get("AADE_VAT_NUMBER") or "").strip()
            if not (user_id and sub_key):
                raise MyDataError(
                    "Η εταιρεία χρησιμοποιεί credentials λογιστή, αλλά "
                    "δεν έχουν οριστεί. Συμπλήρωσέ τα στη σελίδα «Λογιστής»."
                )
            if not entity_vat:
                raise MyDataError(
                    "Για κλήση μέσω λογιστή απαιτείται το ΑΦΜ της εταιρείας "
                    "(συμπλήρωσέ το στη Διαχείριση εταιρειών)."
                )
            return MyDataClient(user_id, sub_key, env, entity_vat=entity_vat)

        # Περίπτωση 1: τα δικά της credentials (ξεχωριστό key ανά πελάτη)
        user_id = (company.get("AADE_USER_ID") or "").strip()
        sub_key = (company.get("AADE_SUBSCRIPTION_KEY") or "").strip()
        env = (company.get("MYDATA_ENV") or "prod").strip()
        if not user_id or not sub_key:
            raise MyDataError(
                f"Η εταιρεία «{company.get('company_name', ';')}» δεν έχει "
                "πλήρη credentials - συμπλήρωσέ τα στη Διαχείριση εταιρειών."
            )
        # Προστασία: αν εδώ έχουν καταχωρηθεί τα κοινά credentials του λογιστή χωρίς
        # να έχει ενεργοποιηθεί το use_accountant, η κλήση ΔΕΝ φέρει entityVatNumber
        # και το AADE αποδίδει την κίνηση στα παραστατικά του λογιστή, όχι της εταιρείας.
        acc = get_accountant()
        acc_user_id = (acc.get("user_id") or "").strip()
        if acc_user_id and acc_user_id == user_id:
            raise MyDataError(
                f"Η εταιρεία «{company.get('company_name', ';')}» χρησιμοποιεί τα "
                "credentials του λογιστή, αλλά χωρίς να έχει οριστεί ως κλήση μέσω "
                "λογιστή. Έτσι δεν στέλνεται το ΑΦΜ της εταιρείας (entityVatNumber) "
                "και το AADE θα καταχωρούσε την κίνηση στα παραστατικά του λογιστή. "
                "Ενεργοποίησε το «χρήση credentials λογιστή» και συμπλήρωσε το ΑΦΜ "
                "της εταιρείας στη Διαχείριση εταιρειών."
            )
        return MyDataClient(user_id, sub_key, env, own_vat=str(company.get("AADE_VAT_NUMBER") or ""))
    user_id = os.getenv("AADE_USER_ID")
    sub_key = os.getenv("AADE_SUBSCRIPTION_KEY")
    env = os.getenv("MYDATA_ENV", "dev")
    if not user_id or not sub_key:
        raise MyDataError(
            "Δεν έχει οριστεί εταιρεία. Πρόσθεσε μία στη σελίδα "
            "«Εταιρείες» (ή όρισε AADE_USER_ID/AADE_SUBSCRIPTION_KEY στο .env)."
        )
    return MyDataClient(user_id, sub_key, env)


def _norm_cls(entries) -> set:
    """Κανονικοποίηση χαρακτηρισμών σε σύνολο για σύγκριση (αγνοεί σειρά/γραμμή)."""
    s = set()
    for e in entries or []:
        if e.get("transaction_mode"):
            s.add(("__TM__", str(e["transaction_mode"])))
        else:
            s.add(
                (
                    e.get("type") or "",
                    e.get("category") or "",
                    round(float(e.get("amount") or 0), 2),
                )
            )
    return s


def _invoice_to_doc(inv) -> dict:
    """Σειριοποιεί ExpenseInvoice σε dict για αποθήκευση στον πίνακα documents."""
    return {
        "mark": inv.mark,
        "issue_date": inv.issue_date,
        "issuer_vat": inv.issuer_vat,
        "issuer_name": inv.issuer_name,
        "invoice_type": inv.invoice_type,
        "series": inv.series,
        "aa": inv.aa,
        "total_net": inv.total_net,
        "total_vat": inv.total_vat,
        "total_gross": inv.total_gross,
        "is_self_issued": inv.is_self_issued,
        "extra_totals": getattr(inv, "extra_totals", None) or {},
        "lines": [
            {
                "line_number": ln.line_number,
                "net_value": ln.net_value,
                "vat_amount": ln.vat_amount,
                "vat_category": ln.vat_category,
                "has_expenses_classification": ln.has_expenses_classification,
                "classifications": ln.classifications,
                "taxes": ln.taxes,
            }
            for ln in inv.lines
        ],
        "cls_info": getattr(inv, "cls_info", []) or [],
        # MARK χαρακτηρισμού από το myDATA (για όσα ήταν ήδη χαρακτηρισμένα εκεί).
        "classification_mark": getattr(inv, "classification_mark", "") or "",
        "raw_xml": getattr(inv, "raw_xml", "") or "",
    }


def _income_to_doc(inv) -> dict:
    """Σαν το _invoice_to_doc, αλλά ο αντισυμβαλλόμενος είναι ο ΠΕΛΑΤΗΣ (counterpart):
    αποθηκεύεται στη στήλη counterparty_vat (η upsert διαβάζει doc["issuer_vat"])."""
    doc = _invoice_to_doc(inv)
    doc["issuer_vat"] = inv.counterpart_vat
    doc["issuer_name"] = inv.counterpart_name
    return doc


def _row_to_invoice(row: dict, names: dict | None = None) -> ExpenseInvoice:
    """Ξαναχτίζει ExpenseInvoice (με γραμμές + cls_info) από γραμμή documents.
    Το όνομα εκδότη προκύπτει από τον πίνακα suppliers (κοινός). Δώσε `names`
    (χάρτης vat→name) όταν φτιάχνεις πολλά, για αποφυγή Ν queries."""
    vat = row.get("counterparty_vat")
    issuer_name = names.get(vat) if names is not None else db.get_supplier_name(vat)
    lines = [
        InvoiceLine(
            line_number=ln.get("line_number"),
            net_value=ln.get("net_value") or 0.0,
            vat_amount=ln.get("vat_amount") or 0.0,
            vat_category=ln.get("vat_category"),
            has_expenses_classification=ln.get("has_expenses_classification", False),
            classifications=ln.get("classifications") or [],
            taxes=ln.get("taxes") or [],
        )
        for ln in json.loads(row.get("lines_json") or "[]")
    ]
    inv = ExpenseInvoice(
        mark=row["mark"],
        uid=None,
        issuer_vat=row.get("counterparty_vat"),
        issuer_name=issuer_name,
        invoice_type=row.get("invoice_type"),
        series=row.get("series"),
        aa=row.get("aa"),
        issue_date=row.get("issue_date"),
        total_net=row.get("total_net"),
        total_vat=row.get("total_vat"),
        total_gross=row.get("total_gross"),
        lines=lines,
        cls_info=json.loads(row.get("cls_json") or "[]"),
    )
    # Επιπλέον πεδία για εμφάνιση (γεμίζουν από το ledger, όχι από το XML).
    inv.classification_mark = row.get("classification_mark") or ""
    inv.local_action = row.get("local_action") or "classify"
    inv.source = row.get("source") or "rest"
    inv.extra_totals = {c: row.get(c) or 0.0 for c in db.EXTRA_TOTALS}
    # None = δεν έχει αναλυθεί ακόμη (παλιά ανάκτηση) → η σελίδα δείχνει μόνο τα σύνολα.
    inv.taxes = json.loads(row["taxes_json"]) if row.get("taxes_json") is not None else None
    return inv


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


def _range_key(kind: str) -> str:
    """Ρύθμιση με το τελευταίο διάστημα ανάκτησης, ανά βιβλίο και ανά (ενεργή) εταιρεία."""
    return f"last_{'income_' if kind == 'income' else ''}range:{_active_company_id()}"


def _fetch_dates(scope: str):
    """Ημερομηνίες της φόρμας ανάκτησης (yyyy-mm-dd → dd/MM/yyyy) + ενεργή εταιρεία.
    Επιστρέφει (df, dt, cid, None) ή (…, redirect) όταν κάτι λείπει."""
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    back = redirect(url_for("sync", scope=scope, date_from=date_from, date_to=date_to))
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        flash("Μη έγκυρες ημερομηνίες.", "error")
        return None, None, None, back
    if start > end:
        flash("Η ημερομηνία «Από» πρέπει να προηγείται της «Έως».", "error")
        return None, None, None, back
    df, dt = start.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")
    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία. Πρόσθεσε μία στη σελίδα «Εταιρείες».", "error")
        return None, None, None, redirect(url_for("companies"))
    return df, dt, cid, None


def _fetch_error(e: Exception) -> str:
    return str(e) if isinstance(e, MyDataError) else f"Σφάλμα επικοινωνίας: {e}"


def _fetch_expense_range(cid: int, df: str, dt: str) -> str:
    """Ανάκτηση εξόδων διαστήματος στο τοπικό βιβλίο· επιστρέφει το μήνυμα επιτυχίας (σφάλματα ανεβαίνουν)."""
    client = get_client()
    unclassified = client.request_unclassified_expenses(df, dt)
    classified = client.request_classified_expenses(df, dt)

    enrich_issuer_names(unclassified)
    enrich_issuer_names(classified)

    # Ακυρωμένα: αφαίρεση από το τοπικό βιβλίο (μπορεί να είχαν κατέβει πριν ακυρωθούν).
    removed = 0
    for mark in client.last_cancelled:
        row = db.get_document(cid, mark)
        if row and row["kind"] == "expense":
            db.delete_document(cid, mark)
            removed += 1

    # Αποθήκευση στο μόνιμο ledger (SQLite). Το upsert ΔΕΝ χαμηλώνει την τοπική
    # πρόοδο: αχαρακτήριστα που τοπικά είναι classified/sent μένουν ως έχουν,
    # ενώ όσα το myDATA δείχνει χαρακτηρισμένα περνούν σε «Ολοκληρωμένα».
    for inv in unclassified:
        db.upsert_document(cid, "expense", _invoice_to_doc(inv), "unclassified")
    for inv in classified:
        db.upsert_document(cid, "expense", _invoice_to_doc(inv), "confirmed")
    n0 = _classify_zero_docs(cid, [inv.mark for inv in unclassified])

    db.set_setting(_range_key("expense"), f"{df} – {dt}")
    return (
        f"✔ Ανακτήθηκαν και αποθηκεύτηκαν {len(unclassified) + len(classified)} "
        "παραστατικά."
        + (f" {n0} με μηδενική αξία χαρακτηρίστηκαν τοπικά (2.5 / E3_585_016)." if n0 else "")
        + (f" Αφαιρέθηκαν {removed} ακυρωμένα." if removed else "")
    )


def _fetch_income_range(cid: int, df: str, dt: str) -> str:
    """Ανάκτηση εσόδων διαστήματος στο τοπικό βιβλίο· επιστρέφει το μήνυμα επιτυχίας (σφάλματα ανεβαίνουν)."""
    unclassified, classified, cancelled_marks = get_client().request_income(df, dt)

    # Επωνυμίες πελατών: ίδια τεχνική & ίδιος κοινός πίνακας (suppliers) με τους
    # προμηθευτές — εκμάθηση + cache + VIES/GSIS για άγνωστα ΑΦΜ.
    enrich_counterpart_names(unclassified)
    enrich_counterpart_names(classified)

    # Ακυρωμένα: αφαίρεση από το τοπικό βιβλίο (μπορεί να είχαν κατέβει πριν ακυρωθούν).
    removed = 0
    for mark in cancelled_marks:
        if db.get_document(cid, mark):
            db.delete_document(cid, mark)
            removed += 1

    for inv in unclassified:
        db.upsert_document(cid, "income", _income_to_doc(inv), "unclassified")
    for inv in classified:
        db.upsert_document(cid, "income", _income_to_doc(inv), "classified")

    db.set_setting(_range_key("income"), f"{df} – {dt}")
    return (
        f"✔ Ανακτήθηκαν {len(unclassified) + len(classified)} παραστατικά εσόδων "
        f"({len(unclassified)} αχαρακτήριστα, {len(classified)} χαρακτηρισμένα)"
        + (f" · αφαιρέθηκαν {removed} ακυρωμένα." if removed else ".")
    )


# Σελίδα «Ανάκτηση»: ποια βιβλία (σειρά: πρώτα έσοδα, μετά έξοδα — όπως στο μενού).
_SYNC_SCOPES = {"both": ("income", "expense"), "income": ("income",), "expense": ("expense",)}
_SYNC_NOUNS = {"both": "εσόδων & εξόδων", "income": "εσόδων", "expense": "εξόδων"}


@app.route("/sync", methods=["POST"])
def sync_run():
    """Ανάκτηση / Διαγραφή / Διαγραφή και ανάκτηση για έσοδα, έξοδα ή και τα δύο. Κάθε βιβλίο
    ανεξάρτητα: αποτυχία του ενός δεν σταματά το άλλο."""
    scope = request.form.get("scope") if request.form.get("scope") in _SYNC_SCOPES else "both"
    action = request.form.get("action") if request.form.get("action") in ("fetch", "delete", "refetch") else "fetch"
    df, dt, cid, bad = _fetch_dates(scope)
    if bad:
        return bad
    ok = 0
    for kind in _SYNC_SCOPES[scope]:
        label = "Έσοδα" if kind == "income" else "Έξοδα"
        prefix = f"{label}: " if scope == "both" else ""
        if action in ("delete", "refetch"):
            n = db.delete_documents_range(cid, kind, request.form["date_from"], request.form["date_to"])
            flash(f"{prefix}Διαγράφηκαν {n} παραστατικά {_SYNC_NOUNS[kind]} για το διάστημα {df} – {dt}.", "ok")
            if action == "delete":
                ok += 1
                continue
        try:
            run = _fetch_income_range if kind == "income" else _fetch_expense_range
            flash(prefix + run(cid, df, dt), "ok")
            ok += 1
        except Exception as e:  # noqa: BLE001 — network κ.λπ., δεν θέλουμε 500 στο route
            flash(prefix + _fetch_error(e), "error")
    if not ok:
        return redirect(url_for("sync", scope=scope, date_from=request.form["date_from"],
                                date_to=request.form["date_to"]))
    if action != "delete" and "expense" in _SYNC_SCOPES[scope]:
        if db.get_setting("auto_classify") == "1" and _auto_classify_candidates(cid):
            return redirect(url_for("auto_classify"))
    return redirect(url_for({"income": "income", "expense": "invoices"}.get(scope, "sync"),
                            **({"scope": scope} if scope == "both" else {})))


def _classify_zero_docs(cid: int, marks: list) -> int:
    """Έξοδα με μηδενική αξία και ΦΠΑ: το myDATA δεν δέχεται χαρακτηρισμό, άρα χαρακτηρίζονται μόνο τοπικά
    (όπως το «Ήδη χαρακτηρισμένο») ως 2.5 / E3_585_016. Μόνο όσα είναι ακόμα αχαρακτήριστα τοπικά."""
    n = 0
    for mark in marks:
        row = db.get_document(cid, mark)
        if not row or row["status"] != "unclassified":
            continue
        inv = _row_to_invoice(row)
        if inv.invoice_type == "1.5" or abs(inv.total_net or 0) >= 0.005 or abs(inv.total_vat or 0) >= 0.005:
            continue
        lines = inv.lines or [InvoiceLine(line_number=1, net_value=0.0, vat_amount=0.0, vat_category=None,
                                          has_expenses_classification=False)]
        entries = [e for ln in lines for e in _bulk_line_entries(ln, NO_VAT_RIGHT, "E3_585_016", "none")]
        db.save_local_classification(cid, mark, entries, manual=True)
        n += 1
    return n


def _auto_classify_candidates(cid: int) -> list:
    """Αχαρακτήριστα έξοδα με αποθηκευμένη πρόταση συναλλασσόμενου (εκτός 1.5, όπως στο bulk)."""
    rules, names = load_rules(), db.load_names()
    out = []
    for row in db.get_documents(cid, "expense", ["unclassified"]):
        inv = _row_to_invoice(row, names)
        vat = inv.issuer_vat or ""
        if inv.invoice_type != "1.5" and (rules.get(vat) or _patterns_for(vat)):
            out.append(inv)
    return sorted(out, key=lambda i: i.issue_date or "")


@app.route("/invoices/auto-classify")
def auto_classify():
    """Οθόνη επιβεβαίωσης του αυτόματου χαρακτηρισμού: υποβάλλει στο bulk_classify (action=rules)."""
    cid = _active_company_id()
    return render_template(
        "auto_classify.html",
        invoices=_auto_classify_candidates(cid) if cid else [],
        rules=load_rules(),
        type_names=INVOICE_TYPE_NAMES,
        credit_types=CREDIT_INVOICE_TYPES,
    )


# Καρτέλες κατάστασης του βιβλίου εξόδων (σειρά ροής).
_EXPENSE_VIEWS = ("unclassified", "classified", "sent", "confirmed")


PER_PAGE = 50


def paginate(items: list, page_arg) -> tuple[list, int, int]:
    """(στοιχεία σελίδας, τρέχουσα σελίδα, σύνολο σελίδων) με PER_PAGE ανά σελίδα."""
    try:
        page = max(1, int(page_arg or 1))
    except ValueError:
        page = 1
    total_pages = max(1, -(-len(items) // PER_PAGE))
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    return items[start : start + PER_PAGE], page, total_pages


@app.template_global()
def page_numbers(page: int, total: int, edge: int = 2, around: int = 1) -> list:
    """Αριθμοί σελίδων για το pager: πάντα οι πρώτες/τελευταίες `edge`, και `around`
    γύρω από την τρέχουσα· None = κενό («…»). Ένα μόνο κενό γεμίζει με τον αριθμό."""
    keep = sorted(
        p for p in range(1, total + 1)
        if p <= edge or p > total - edge or abs(p - page) <= around
    )
    out, prev = [], 0
    for p in keep:
        if p - prev == 2:
            out.append(p - 1)
        elif p - prev > 2:
            out.append(None)
        out.append(p)
        prev = p
    return out



def _two_years(today: date) -> list[str]:
    """Όλοι οι μήνες του προηγούμενου και του τρέχοντος έτους, ως "yyyy-mm" (Ιαν προηγ. έτους πρώτος)."""
    return [f"{y}-{m:02d}" for y in (today.year - 1, today.year) for m in range(1, 13)]


def _month_strip(cid: int | None, kinds) -> dict:
    """Λωρίδα μηνών (προηγούμενο + τρέχον έτος): ανά μήνα έσοδα, έξοδα (χαρακτηρισμένα) και
    αχαρακτήριστα των βιβλίων `kinds`. Για την Ανάκτηση και την κεφαλίδα των βιβλίων."""
    now = datetime.now(ATHENS)
    months = _two_years(now.date())
    per_month = {ym: {"income": 0, "expense": 0, "pending": 0, "total": 0} for ym in months}
    for kind in kinds:
        for ym, st in (db.month_counts(cid, kind, months[0] + "-01") if cid else {}).items():
            if ym in per_month:
                pending = st.get("unclassified", 0)
                per_month[ym][kind] += sum(st.values()) - pending
                per_month[ym]["pending"] += pending
                per_month[ym]["total"] += sum(st.values())
    return {"months": [(ym, per_month[ym]) for ym in months], "this_month": now.strftime("%Y-%m")}


@app.route("/sync")
def sync():
    """Ενιαία σελίδα ανάκτησης από το myDATA / διαγραφής διαστήματος, για έσοδα, έξοδα ή και τα δύο."""
    now = datetime.now(ATHENS)
    today = now.strftime("%Y-%m-%d")
    cid = _active_company_id()
    scope = request.args.get("scope") if request.args.get("scope") in _SYNC_SCOPES else "both"
    kinds = _SYNC_SCOPES[scope]
    # «Από την τελευταία ανάκτηση»: η παλαιότερη από τις τελευταίες των επιλεγμένων βιβλίων, για να μη χαθεί τίποτα.
    ranges = [r for r in (db.get_setting(_range_key(k)) for k in kinds) if r]
    end = lambda r: datetime.strptime(r.split("–")[-1].strip(), "%d/%m/%Y")  # noqa: E731
    return render_template(
        "sync.html",
        scope=scope,
        noun=_SYNC_NOUNS[scope],
        **_month_strip(cid, kinds),
        range_date_from=request.args.get("date_from", "").strip() or today,
        range_date_to=request.args.get("date_to", "").strip() or today,
        date_range=min(ranges, key=end) if ranges else None,
        # Τελευταία ανάκτηση + τελευταίο MARK ανά βιβλίο (τα MARK του myDATA είναι χρονολογικά).
        last=[(label, db.get_setting(_range_key(k)),
               max((r["mark"] for r in db.get_documents(cid, k) if (r["mark"] or "").isdigit()), key=int, default=None)
               if cid else None)
              for k, label in (("income", "Έσοδα"), ("expense", "Έξοδα"))],
    )


@app.route("/invoices")
def invoices():
    view = request.args.get("view")
    if view not in _EXPENSE_VIEWS:
        # Χωρίς (έγκυρο) tab: το πρώτο με εγγραφές· κανένα → Ανάκτηση / Διαγραφή.
        cid = _active_company_id()
        by_status = db.count_by_status(cid, "expense") if cid else {}
        view = next((v for v in _EXPENSE_VIEWS if by_status.get(v)), None)
        if view is None:
            return redirect(url_for("sync", scope="expense"))
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "desc" if view in ("classified", "confirmed") else "asc")
    reverse = direction == "desc"
    filters = {
        "mark": request.args.get("f_mark", "").strip(),
        "date": request.args.get("f_date", "").strip(),
        "issuer": request.args.get("f_issuer", "").strip(),
        "type": request.args.get("f_type", "").strip(),
    }

    def sort_key(inv):
        if sort == "name":
            return (inv.issuer_name or inv.issuer_vat or "").lower()
        if sort == "total":
            return inv.total_gross or 0.0
        # default: ημερομηνία (ISO μορφή yyyy-mm-dd, ταξινομείται σωστά ως string)
        return inv.issue_date or ""

    cid = _active_company_id()
    names = db.load_names()  # κοινός χάρτης vat→επωνυμία (μία φορά)
    buckets = {v: [] for v in _EXPENSE_VIEWS}
    for row in db.get_documents(cid, "expense") if cid else []:
        buckets.setdefault(row["status"], []).append(_row_to_invoice(row, names))

    counts = {v: len(buckets[v]) for v in _EXPENSE_VIEWS}

    # Φιλτράρισμα (substring, case-insensitive όπου έχει νόημα).
    items = buckets.get(view, [])
    fm = filters["mark"]
    fi, ft = filters["issuer"].lower(), filters["type"]

    # Ημερομηνία: υποστηρίζει εύρος «από:έως» με μερικές ημερομηνίες (YYYY, YYYY-MM,
    # YYYY-MM-DD), π.χ. «2026-03:2026-04» ή «2026-03-12:2026-05-15». Χωρίς «:» =
    # απλό substring (π.χ. «2026-03» = Μάρτιος). Οι συγκρίσεις είναι lexicographic
    # πάνω σε ISO ημερομηνίες· το «~» ως άνω όριο κάνει το prefix inclusive.
    fd = filters["date"]
    date_sub = date_lo = date_hi = None
    if fd:
        if ":" in fd:
            lo, _, hi = fd.partition(":")
            date_lo = lo.strip() or None
            date_hi = (hi.strip() + "~") if hi.strip() else None
        else:
            date_sub = fd

    if any(filters.values()):

        def _match(inv):
            if fm and fm not in (inv.mark or ""):
                return False
            d = inv.issue_date or ""
            if date_sub and date_sub not in d:
                return False
            if date_lo and d < date_lo:
                return False
            if date_hi and d > date_hi:
                return False
            if (
                fi
                and fi
                not in ((inv.issuer_name or "") + " " + (inv.issuer_vat or "")).lower()
            ):
                return False
            return not (ft and ft not in (inv.invoice_type or ""))

        items = [i for i in items if _match(i)]

    items = sorted(items, key=sort_key, reverse=reverse)
    total = len(items)
    page_items, page, total_pages = paginate(items, request.args.get("page"))
    if view == "sent":
        for inv in page_items:
            inv.cls_rows = _cls_list_rows(inv.cls_info)
            inv.cls_flags = [e for e in inv.cls_info or [] if e.get("transaction_mode")]

    return render_template(
        "invoices.html",
        view=view,
        invoices=page_items,
        counts=counts,
        categories=EXPENSE_CATEGORIES,
        types=EXPENSE_TYPES,
        vat_types=VAT_TYPES,
        type_names=INVOICE_TYPE_NAMES,
        credit_types=CREDIT_INVOICE_TYPES,
        rules=load_rules(),
        sort=sort,
        dir=direction,
        page=page,
        total_pages=total_pages,
        total=total,
        strip=_month_strip(cid, ("expense",)),
        per_page=PER_PAGE,
        filters=filters,
    )


@app.route("/document/<mark>")
def document(mark):
    """Αναλυτική προβολή παραστατικού με επιλογές αλλαγής χαρακτηρισμού / απόρριψης."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None:
        flash("Το παραστατικό δεν βρέθηκε. Κάνε νέα αναζήτηση.", "error")
        return redirect(url_for("invoices"))
    inv = _row_to_invoice(row)
    # Ενέργειες σε επίπεδο παραστατικού (απόρριψη/ακύρωση/απόκλιση) - δεν ανήκουν
    # σε γραμμή, μένουν στο κάτω μπλοκ.
    flags = [c for c in (inv.cls_info or []) if c.get("transaction_mode")]
    line_cls, doc_cls = _document_cls_rows(db.get_local_classification(row["id"]), inv)
    # URL επιστροφής στη λίστα, διατηρώντας ταξινόμηση/σελίδα/φίλτρα (fallback: view).
    # Έσοδα: ίδια προβολή, μόνο για ανάγνωση (χωρίς ενέργειες χαρακτηρισμού/απόρριψης).
    is_income = row.get("kind") == "income"
    back = request.args.get("back") or url_for("income" if is_income else "invoices", view=row["status"])
    # Προηγούμενο/επόμενο: ίδιο βιβλίο και κατάσταση, με την προεπιλεγμένη σειρά του βιβλίου (ημ/νία).
    siblings = sorted(
        (r for r in db.get_documents(cid, row["kind"], [row["status"]])),
        key=lambda r: (r["issue_date"] or "", r["mark"]),
    )
    pos = next(i for i, r in enumerate(siblings) if r["mark"] == mark)
    return render_template(
        "document.html",
        inv=inv,
        is_income=is_income,
        can_cancel=cancel_allowed(),
        status=row["status"],
        local_action=row.get("local_action") or "classify",
        source=row.get("source") or "rest",
        classification_mark=row.get("classification_mark"),
        type_desc=INVOICE_TYPE_NAMES.get(inv.invoice_type or ""),
        vat_rates=VAT_CATEGORY_RATES,
        tax_types=TAX_TYPES,
        tax_categories=TAX_CATEGORIES,
        back=back,
        prev_mark=siblings[pos - 1]["mark"] if pos > 0 else None,
        next_mark=siblings[pos + 1]["mark"] if pos + 1 < len(siblings) else None,
        pos=pos + 1,
        siblings_total=len(siblings),
        line_cls=line_cls,
        doc_cls=doc_cls,
        flags=flags,
        categories=INCOME_CATEGORIES if is_income else EXPENSE_CATEGORIES,
        types=INCOME_TYPES if is_income else EXPENSE_TYPES,
        vat_types=VAT_TYPES,
        xml_tree=(xml_tree := _xml_tree(row.get("raw_xml"))),
        xml_links=_xml_links(xml_tree),
        has_xml=bool(row.get("raw_xml")),
    )


# Ελληνικές ετικέτες για τα πεδία του XML του myDATA (άγνωστο πεδίο → το όνομά του ως έχει).
XML_LABELS = {
    "invoice": "Παραστατικό", "uid": "UID", "mark": "ΜΑΡΚ", "cancelledByMark": "Ακυρώθηκε από ΜΑΡΚ",
    "authenticationCode": "Κωδικός αυθεντικοποίησης", "transmissionFailure": "Αδυναμία διαβίβασης",
    "qrCodeUrl": "QR code (URL)", "issuer": "Εκδότης", "counterpart": "Λήπτης",
    "vatNumber": "ΑΦΜ", "country": "Χώρα", "branch": "Εγκατάσταση", "name": "Επωνυμία",
    "address": "Διεύθυνση", "street": "Οδός", "number": "Αριθμός", "postalCode": "Τ.Κ.", "city": "Πόλη",
    "documentIdNo": "Αρ. ταυτότητας/διαβατηρίου", "supplyAccountNo": "Αρ. παροχής",
    "countryDocumentId": "Χώρα έκδοσης εγγράφου", "invoiceHeader": "Επικεφαλίδα", "series": "Σειρά",
    "aa": "Α/Α", "issueDate": "Ημ/νία έκδοσης", "invoiceType": "Τύπος παραστατικού",
    "vatPaymentSuspension": "Αναστολή καταβολής ΦΠΑ", "currency": "Νόμισμα", "exchangeRate": "Ισοτιμία",
    "correlatedInvoices": "Συσχετιζόμενα παραστατικά", "selfPricing": "Αυτοτιμολόγηση",
    "dispatchDate": "Ημ/νία αποστολής", "dispatchTime": "Ώρα αποστολής", "vehicleNumber": "Αρ. οχήματος",
    "movePurpose": "Σκοπός διακίνησης", "fuelInvoice": "Παραστατικό καυσίμων",
    "specialInvoiceCategory": "Ειδική κατηγορία", "invoiceVariationType": "Τύπος απόκλισης",
    "otherCorrelatedEntities": "Λοιπές συσχετιζόμενες οντότητες", "otherDeliveryNoteHeader": "Στοιχεία διακίνησης",
    "isDeliveryNote": "Δελτίο αποστολής", "otherMovePurposeTitle": "Λοιπός σκοπός διακίνησης",
    "thirdPartyCollection": "Είσπραξη για λογαριασμό τρίτων", "multipleConnectedMarks": "Συνδεδεμένα ΜΑΡΚ",
    "tableAA": "Α/Α τραπεζιού", "totalCancelDeliveryOrders": "Ακύρωση παραγγελιών",
    "paymentMethods": "Τρόποι πληρωμής", "paymentMethodDetails": "Πληρωμή", "type": "Τύπος",
    "amount": "Ποσό", "paymentMethodInfo": "Πληροφορίες πληρωμής", "tipAmount": "Φιλοδώρημα",
    "transactionId": "Αναγνωριστικό συναλλαγής", "tid": "TID", "ProvidersSignature": "Υπογραφή παρόχου",
    "ECRToken": "ECR token", "invoiceDetails": "Γραμμή", "lineNumber": "Α/Α γραμμής",
    "recType": "Είδος γραμμής", "TaricNo": "Κωδ. TARIC", "itemCode": "Κωδ. είδους",
    "itemDescr": "Περιγραφή είδους", "fuelCode": "Κωδ. καυσίμου", "quantity": "Ποσότητα",
    "measurementUnit": "Μονάδα μέτρησης", "invoiceDetailType": "Επισήμανση γραμμής",
    "netValue": "Καθαρή αξία", "vatCategory": "Κατηγορία ΦΠΑ", "vatAmount": "Ποσό ΦΠΑ",
    "vatExemptionCategory": "Κατηγορία εξαίρεσης ΦΠΑ", "dienergia": "ΠΟΛ 1177/2018",
    "discountOption": "Δικαίωμα έκπτωσης", "withheldAmount": "Παρακράτηση φόρου",
    "withheldPercentCategory": "Κατηγορία παρακράτησης", "stampDutyAmount": "Χαρτόσημο",
    "stampDutyPercentCategory": "Κατηγορία χαρτοσήμου", "feesAmount": "Τέλη",
    "feesPercentCategory": "Κατηγορία τελών", "otherTaxesPercentCategory": "Κατηγορία λοιπών φόρων",
    "otherTaxesAmount": "Λοιποί φόροι", "deductionsAmount": "Κρατήσεις", "lineComments": "Σχόλια γραμμής",
    "quantity15": "Ποσότητα 15°C", "otherMeasurementUnitQuantity": "Ποσότητα άλλης μονάδας",
    "otherMeasurementUnitTitle": "Άλλη μονάδα μέτρησης", "notVAT195": "Εκτός άρθρου 39α",
    "taxesTotals": "Φόροι παραστατικού", "taxes": "Φόρος", "taxType": "Τύπος φόρου",
    "taxCategory": "Κατηγορία φόρου", "underlyingValue": "Ποσό υπολογισμού", "taxAmount": "Ποσό φόρου",
    "id": "Α/Α", "invoiceSummary": "Σύνοψη", "totalNetValue": "Σύνολο καθαρής αξίας",
    "totalVatAmount": "Σύνολο ΦΠΑ", "totalWithheldAmount": "Σύνολο παρακρατήσεων",
    "totalFeesAmount": "Σύνολο τελών", "totalStampDutyAmount": "Σύνολο χαρτοσήμου",
    "totalOtherTaxesAmount": "Σύνολο λοιπών φόρων", "totalDeductionsAmount": "Σύνολο κρατήσεων",
    "totalGrossValue": "Συνολική αξία", "incomeClassification": "Χαρακτηρισμός εσόδων",
    "expensesClassification": "Χαρακτηρισμός εξόδων", "classificationType": "Τύπος χαρακτηρισμού",
    "classificationCategory": "Κατηγορία χαρακτηρισμού", "transactionMode": "Είδος συναλλαγής",
    "vatClassification": "Χαρακτηρισμός ΦΠΑ", "otherTransportDetails": "Λοιπά στοιχεία μεταφοράς",
    "loadingAddress": "Διεύθυνση φόρτωσης", "deliveryAddress": "Διεύθυνση παράδοσης",
    "startShippingBranch": "Εγκατάσταση έναρξης", "completeShippingBranch": "Εγκατάσταση ολοκλήρωσης",
    "downloadingInvoiceUrl": "URL λήψης", "invoiceUrl": "URL παραστατικού",
}


# Κωδικοί ΑΑΔΕ χωρίς δική τους λίστα στην εφαρμογή (για την προβολή «Όλα τα στοιχεία»).
XML_EXTRA_CODES = {
    "measurementUnit": {"1": "Τεμάχια", "2": "Κιλά", "3": "Λίτρα", "4": "Μέτρα", "5": "Τετραγωνικά μέτρα",
                        "6": "Κυβικά μέτρα", "7": "Τεμάχια (λοιπές περιπτώσεις)"},
    "transactionMode": {"1": "Απόρριψη", "2": "Απόκλιση ποσών"},
    "movePurpose": {
        "1": "Πώληση", "2": "Πώληση για λογαριασμό τρίτων", "3": "Δειγματισμός", "4": "Έκθεση",
        "5": "Επιστροφή", "6": "Φύλαξη", "7": "Επεξεργασία / συναρμολόγηση", "8": "Μεταξύ εγκαταστάσεων οντότητας",
        "9": "Αγορά", "10": "Εφοδιασμός πλοίων και αεροσκαφών", "11": "Δωρεάν διάθεση", "12": "Εγγύηση",
        "13": "Χρησιδανεισμός", "14": "Αποθήκευση σε τρίτους", "15": "Επιστροφή από φύλαξη", "16": "Ανακύκλωση",
        "17": "Καταστροφή άχρηστου υλικού", "18": "Διακίνηση παγίων (ενδοδιακίνηση)", "19": "Λοιπές διακινήσεις",
    },
    # ponytail: οι συνηθέστερες χώρες/νομίσματα· άλλος κωδικός εμφανίζεται σκέτος — προσθέστε εδώ.
    "country": {
        "GR": "Ελλάδα", "AT": "Αυστρία", "BE": "Βέλγιο", "BG": "Βουλγαρία", "CY": "Κύπρος", "CZ": "Τσεχία",
        "DE": "Γερμανία", "DK": "Δανία", "EE": "Εσθονία", "ES": "Ισπανία", "FI": "Φινλανδία", "FR": "Γαλλία",
        "HR": "Κροατία", "HU": "Ουγγαρία", "IE": "Ιρλανδία", "IT": "Ιταλία", "LT": "Λιθουανία",
        "LU": "Λουξεμβούργο", "LV": "Λετονία", "MT": "Μάλτα", "NL": "Ολλανδία", "PL": "Πολωνία",
        "PT": "Πορτογαλία", "RO": "Ρουμανία", "SE": "Σουηδία", "SI": "Σλοβενία", "SK": "Σλοβακία",
        "GB": "Ηνωμένο Βασίλειο", "US": "Η.Π.Α.", "CH": "Ελβετία", "NO": "Νορβηγία", "IS": "Ισλανδία",
        "LI": "Λιχτενστάιν", "TR": "Τουρκία", "AL": "Αλβανία", "MK": "Βόρεια Μακεδονία", "RS": "Σερβία",
        "ME": "Μαυροβούνιο", "BA": "Βοσνία-Ερζεγοβίνη", "UA": "Ουκρανία", "RU": "Ρωσία", "IL": "Ισραήλ",
        "AE": "Ηνωμένα Αραβικά Εμιράτα", "CN": "Κίνα", "JP": "Ιαπωνία", "KR": "Νότια Κορέα", "IN": "Ινδία",
        "CA": "Καναδάς", "AU": "Αυστραλία", "BR": "Βραζιλία", "EG": "Αίγυπτος", "SG": "Σιγκαπούρη",
        "HK": "Χονγκ Κονγκ",
    },
    "currency": {
        "EUR": "Ευρώ", "USD": "Δολάριο ΗΠΑ", "GBP": "Λίρα Αγγλίας", "CHF": "Φράγκο Ελβετίας",
        "JPY": "Γιεν Ιαπωνίας", "CNY": "Γιουάν Κίνας", "TRY": "Λίρα Τουρκίας", "CAD": "Δολάριο Καναδά",
        "AUD": "Δολάριο Αυστραλίας", "SEK": "Κορώνα Σουηδίας", "DKK": "Κορώνα Δανίας",
        "NOK": "Κορώνα Νορβηγίας", "PLN": "Ζλότι Πολωνίας", "CZK": "Κορώνα Τσεχίας",
        "HUF": "Φιορίνι Ουγγαρίας", "RON": "Λέου Ρουμανίας", "BGN": "Λεβ Βουλγαρίας",
        "RSD": "Δηνάριο Σερβίας", "ALL": "Λεκ Αλβανίας", "MKD": "Δηνάριο Β. Μακεδονίας",
        "ILS": "Σέκελ Ισραήλ", "AED": "Ντιρχάμ ΗΑΕ",
    },
    "invoiceVariationType": {"1": "Διαβίβαση λήπτη λόγω παράλειψης εκδότη"},
    "vatExemptionCategory": {
        "1": "Άρθρο 3 ΚΦΠΑ (εκτός πεδίου)", "2": "Άρθρο 5 ΚΦΠΑ", "3": "Άρθρο 13 ΚΦΠΑ", "4": "Άρθρο 14 ΚΦΠΑ",
        "5": "Άρθρο 16 ΚΦΠΑ", "6": "Άρθρο 19 ΚΦΠΑ", "7": "Άρθρο 22 ΚΦΠΑ", "8": "Άρθρο 24 ΚΦΠΑ",
        "9": "Άρθρο 25 ΚΦΠΑ", "10": "Άρθρο 26 ΚΦΠΑ", "11": "Άρθρο 27 ΚΦΠΑ",
        "12": "Άρθρο 27 ΚΦΠΑ (πλοία ανοικτής θαλάσσης)", "13": "Άρθρο 27.1.γ ΚΦΠΑ (πλοία ανοικτής θαλάσσης)",
        "14": "Άρθρο 28 ΚΦΠΑ", "15": "Άρθρο 39 ΚΦΠΑ", "16": "Άρθρο 39α ΚΦΠΑ", "17": "Άρθρο 40 ΚΦΠΑ",
        "18": "Άρθρο 41 ΚΦΠΑ", "19": "Άρθρο 47 ΚΦΠΑ", "20": "ΦΠΑ εμπεριεχόμενος", "21": "Άρθρο 43 ΚΦΠΑ",
        "22": "Άρθρο 44 ΚΦΠΑ", "23": "Άρθρο 50 ΚΦΠΑ", "24": "Άρθρο 4 ΚΦΠΑ", "25": "ΠΟΛ 1029/1995",
        "26": "ΠΟΛ 1167/2015", "27": "Λοιπές εξαιρέσεις ΦΠΑ", "28": "Άρθρο 24 περ. β' παρ. 1 ΚΦΠΑ",
        "29": "Άρθρο 47β ΚΦΠΑ (OSS μη ενωσιακό)", "30": "Άρθρο 47γ ΚΦΠΑ (OSS ενωσιακό)",
        "31": "Άρθρο 47δ ΚΦΠΑ (IOSS)",
    },
}


def _xml_tree(xml: str | None) -> list[dict]:
    """XML του παραστατικού → δέντρο [{label, value, children}] για προβολή, χωρίς namespaces.
    Κάθε κωδικός (τύπος, κατηγορία ΦΠΑ, χαρακτηρισμοί, φόροι, πληρωμή κ.λπ.) με την περιγραφή του."""
    import xml.etree.ElementTree as ET

    from mydata_client import PAYMENT_METHODS, VAT_CATEGORIES

    if not xml:
        return []
    codes = {
        **XML_EXTRA_CODES,
        "invoiceType": INVOICE_TYPE_NAMES,
        "vatCategory": {**VAT_CATEGORY_RATES, **VAT_CATEGORIES},
        "classificationType": {**EXPENSE_TYPES, **INCOME_TYPES, **{k: v for k, v in VAT_TYPES.items() if k}},
        "classificationCategory": {**EXPENSE_CATEGORIES, **INCOME_CATEGORIES},
        "taxType": TAX_TYPES,
        "withheldPercentCategory": TAX_CATEGORIES["1"],
        "feesPercentCategory": TAX_CATEGORIES["2"],
        "otherTaxesPercentCategory": TAX_CATEGORIES["3"],
        "stampDutyPercentCategory": TAX_CATEGORIES["4"],
    }

    def node(el, parent="", siblings=None):
        tag = el.tag.split("}")[-1]
        value = (el.text or "").strip()
        if (tag, parent) == ("type", "paymentMethodDetails"):
            names = PAYMENT_METHODS
        elif tag == "taxCategory":  # η κατηγορία εξαρτάται από τον τύπο φόρου δίπλα της
            names = TAX_CATEGORIES.get((siblings or {}).get("taxType", ""), {})
        else:
            names = codes.get(tag, {})
        if value in names:
            value = f"{value} · {names[value]}"
        kids = {c.tag.split("}")[-1]: (c.text or "").strip() for c in el}
        label = XML_LABELS.get(tag, tag)
        if tag == "invoiceDetails" and kids.get("lineNumber"):  # «Γραμμή 1», «Γραμμή 2», …
            label = f"{label} {kids['lineNumber']}"
        return {"tag": tag, "label": label, "value": value,
                "children": [node(c, tag, kids) for c in el]}

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return node(root)["children"]


def _xml_links(tree: list[dict]) -> dict:
    """URL λήψης και QR code του παραστατικού (μόνο http/https), από το δέντρο του _xml_tree."""
    found = {}

    def walk(nodes):
        for n in nodes:
            v = n["value"]
            if n["tag"] in ("downloadingInvoiceUrl", "qrCodeUrl") and v.lower().startswith(("https://", "http://")):
                found.setdefault("download" if n["tag"] == "downloadingInvoiceUrl" else "qr", v)
            walk(n["children"])

    walk(tree)
    return found


@app.route("/document/<mark>/xml")
def document_xml(mark):
    """Λήψη του XML του παραστατικού όπως το έδωσε το myDATA."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if not row or not row.get("raw_xml"):
        flash("Δεν υπάρχει αποθηκευμένο XML — κάνε νέα ανάκτηση του διαστήματος.", "error")
        return redirect(url_for("document", mark=mark))
    return Response(row["raw_xml"], mimetype="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{mark}.xml"'})


def _save_as_rule(cid: int, vat: str, patterns: dict, post_mode: int = 0) -> None:
    """Προτάσεις ανά κατηγορία ΦΠΑ + βασική (η πρώτη) + τρόπος για τον προμηθευτή."""
    db.save_rule_patterns(cid, vat, patterns)
    first = next(iter(patterns.values()))
    db.save_rule(cid, vat, first["type"], first["category"], first["vat_type"])
    db.set_rule_post_mode(cid, vat, post_mode)


def _latest_patterns(cid: int | None, vats: set) -> dict:
    """{vat: (row, patterns)}: η πιο πρόσφατη ολοκληρωμένη εγγραφή εξόδου με χαρακτηρισμό Ε3."""
    out: dict = {}
    if not (cid and vats):
        return out
    confirmed = sorted(
        (d for d in db.get_documents(cid, "expense", ["confirmed"]) if d["counterparty_vat"] in vats),
        key=lambda d: (d["issue_date"] or "", d["mark"]), reverse=True,
    )
    for d in confirmed:
        if d["counterparty_vat"] not in out and (pats := _doc_patterns(d)):
            out[d["counterparty_vat"]] = (d, pats)
    return out


@app.route("/document/<mark>/as-rule", methods=["POST"])
def document_as_rule(mark):
    """Ο χαρακτηρισμός ολοκληρωμένου παραστατικού εξόδου γίνεται πρόταση του προμηθευτή
    (ανά κατηγορία ΦΠΑ + βασική)."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None or row["kind"] != "expense" or row["status"] != "confirmed" or not row["counterparty_vat"]:
        flash("Προτάσεις ορίζονται μόνο από ολοκληρωμένο παραστατικό εξόδου με ΑΦΜ εκδότη.", "error")
        return redirect(url_for("document", mark=mark))
    patterns = _doc_patterns(row)
    if not patterns:
        flash("Το παραστατικό δεν έχει χαρακτηρισμό Ε3 για να γίνει πρόταση.", "error")
        return redirect(url_for("document", mark=mark))
    _save_as_rule(cid, row["counterparty_vat"], patterns, _doc_post_mode(row))
    flash("✔ Οι χαρακτηρισμοί του παραστατικού ορίστηκαν ως προτάσεις του προμηθευτή.", "ok")
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):  # μόνο εσωτερική επιστροφή (π.χ. Συναλλασσόμενοι)
        return redirect(nxt)
    return redirect(url_for("document", mark=mark, back=request.form.get("back") or None))


def _pair_cls_rows(entries: list[dict], keep_all: bool = False) -> list[dict]:
    """Ζευγαρώνει κάθε E3 χαρακτηρισμό με γραμμή ΦΠΑ (VAT_xxx) ίδιου ποσού και
    επιστρέφει UI rows {category, type, amount, vat_type}.
    keep_all (για εμφάνιση): κρατά και κατηγορίες χωρίς E3 και όσους ΦΠΑ
    χαρακτηρισμούς δεν ζευγάρωσαν, σε δική τους γραμμή."""
    e3s = [
        e
        for e in entries
        if (e.get("type") or (keep_all and e.get("category")))
        and not (e.get("type") or "").startswith("VAT_")
    ]
    pool = [e for e in entries if (e.get("type") or "").startswith("VAT_")]
    rows = []
    for e in e3s:
        amt = round(float(e.get("amount") or 0), 2)
        vt = next(
            (v for v in pool if round(float(v.get("amount") or 0), 2) == amt), None
        )
        if vt:
            pool.remove(vt)
        rows.append(
            {
                "category": e.get("category") or "",
                "type": e.get("type") or "",
                "amount": e.get("amount"),
                "vat_type": (vt or {}).get("type", ""),
            }
        )
    if keep_all:
        rows += [
            {"category": "", "type": "", "amount": v.get("amount"), "vat_type": v["type"]}
            for v in pool
        ]
    return rows


def _cls_list_rows(entries: list[dict]) -> list[dict]:
    """Όλοι οι χαρακτηρισμοί ενός παραστατικού ως {category, type, vat_type, amount}
    για τη λίστα· ζευγάρωμα ΦΠΑ ανά γραμμή όπου είναι γνωστή. Χωρίς σημαίες."""
    grouped: dict = {}
    for e in entries or []:
        if not e.get("transaction_mode"):
            grouped.setdefault(e.get("line"), []).append(e)
    return [r for es in grouped.values() for r in _pair_cls_rows(es, keep_all=True)]


def _document_cls_rows(local: list[dict], inv) -> tuple[dict, list[dict]]:
    """Χαρακτηρισμοί για την προβολή παραστατικού, σε στήλες
    {category, type, vat_type, amount}:
    (ανά αριθμό γραμμής, σε επίπεδο παραστατικού — χωρίς αριθμό γραμμής).
    Πηγή κατά προτεραιότητα: τοπικός χαρακτηρισμός → ξεχωριστή υποβολή στο
    myDATA (cls_info με «line») → ενσωματωμένος στις γραμμές του παραστατικού."""
    if local:
        entries = [
            {
                "line": c.get("line_number"),
                "type": c.get("classification_type"),
                "category": c.get("classification_category"),
                "amount": c.get("amount"),
            }
            for c in local
        ]
    elif any("line" in e for e in inv.cls_info or []):
        # Συγκεντρωτικός στο myDATA: οι «γραμμές» του είναι συμβολικές → επίπεδο παραστατικού.
        per_invoice = _looks_per_invoice(inv)
        entries = [
            dict(e, line=None) if per_invoice else e
            for e in inv.cls_info
            if not e.get("transaction_mode")
        ]
    else:
        entries = [
            dict(c, line=ln.line_number) for ln in inv.lines for c in ln.classifications
        ]
        if not entries:
            # Συγκεντρωτικός χαρακτηρισμός χωρίς αντιστοίχιση σε γραμμές.
            entries = [
                dict(e, line=None)
                for e in inv.cls_info or []
                if not e.get("transaction_mode")
            ]
    # Αριθμός γραμμής που δεν υπάρχει στο παραστατικό → επίπεδο παραστατικού.
    valid = {ln.line_number for ln in inv.lines}
    grouped: dict = {}
    for e in entries:
        line = e.get("line") if e.get("line") in valid else None
        grouped.setdefault(line, []).append(e)
    rows = {ln: _pair_cls_rows(es, keep_all=True) for ln, es in grouped.items()}
    return rows, rows.pop(None, [])


# ---- Χαρακτηρισμός ανά παραστατικό (postPerInvoice) ---------------------------
# Κανόνες ΑΑΔΕ («SendExpensesClassificationPostPerInvoiceGuidelines»). Οι γραμμές
# αθροίζονται σε μία συγκεντρωτική γραμμή ανά κατηγορία ΦΠΑ (π.χ. 24%, 13%, χωρίς ΦΠΑ)·
# κάθε ομάδα παίρνει Ε3 και —μόνο οι κατηγορίες 1–6— χαρακτηρισμό ΦΠΑ με τα αθροίσματα
# καθαρής/ΦΠΑ της. Οι 7 (0%) και 8 (άνευ ΦΠΑ) δεν χαρακτηρίζονται ως ΦΠΑ.
# ponytail: δεν υποστηρίζεται το άρθρο 39α (vatExemptionCategory 16) — δεν κρατάμε
# αιτία εξαίρεσης ανά γραμμή· αν χρειαστεί, αποθήκευση της και ομαδοποίηση κατά (cat, exemption).
_PI_VAT_CATEGORIES = ("1", "2", "3", "4", "5", "6")
THIRD_PARTY = "category2_9"  # έξοδα για λογαριασμό τρίτων


def _vat_groups(inv) -> list[dict]:
    """Συγκεντρωτικές γραμμές ανά κατηγορία ΦΠΑ: [{vat_category, net, vat, lines,
    classify_vat}]. classify_vat = η ομάδα παίρνει χαρακτηρισμό ΦΠΑ (κατηγορίες 1–6)."""
    groups: dict[str, list] = {}
    for ln in inv.lines or []:
        g = groups.setdefault(str(ln.vat_category or ""), [0.0, 0.0, 0])
        g[0] += ln.net_value or 0
        g[1] += ln.vat_amount or 0
        g[2] += 1
    return [
        {"vat_category": c, "net": round(n, 2), "vat": round(v, 2), "lines": k,
         "classify_vat": c in _PI_VAT_CATEGORIES}
        for c, (n, v, k) in sorted(groups.items())
    ]


def _per_invoice_errors(inv, e3: dict, vat_choice: dict) -> list[str]:
    """Έλεγχος χαρακτηρισμού ανά παραστατικό. e3 = {vat_category: [{category, type,
    amount}]} ανά συγκεντρωτική γραμμή, vat_choice = {vat_category: VAT_xxx ή ""}."""
    rows = [r for rs in e3.values() for r in rs]
    if not rows:
        return ["συμπλήρωσε τουλάχιστον έναν χαρακτηρισμό Ε3"]
    errors = []
    if any(r["category"] == THIRD_PARTY for r in rows):
        if len(rows) > 1:
            errors.append("με κατηγορία 2.9 επιτρέπεται μόνο ένας χαρακτηρισμός Ε3")
        if any(vat_choice.values()):
            errors.append("με κατηγορία 2.9 δεν υποβάλλεται χαρακτηρισμός ΦΠΑ")
    for g in _vat_groups(inv):
        cat = g["vat_category"]
        # ΦΠΑ που δεν χαρακτηρίζεται ως ΦΠΑ (π.χ. χωρίς δικαίωμα έκπτωσης) προστίθεται στο Ε3.
        left_vat = g["vat"] if not (g["classify_vat"] and vat_choice.get(cat)) else 0.0
        expected = round(g["net"] + left_vat, 2)
        got = round(sum(r["amount"] for r in e3.get(cat, [])), 2)
        if abs(got - expected) > 0.01:
            errors.append(
                f"{_vat_group_label(cat)}: Ε3 {got:.2f} € ενώ πρέπει {expected:.2f} € "
                f"(καθαρή {g['net']:.2f} € + αχαρακτήριστος ΦΠΑ {left_vat:.2f} €)"
            )
    return errors


def _is_vat_entry(e: dict) -> bool:
    return (e.get("type") or "").startswith("VAT_")


def _looks_per_invoice(inv) -> bool:
    """Ο χαρακτηρισμός του myDATA (cls_info, με «line») έγινε συγκεντρωτικά; Τότε οι
    «γραμμές» του είναι συμβολικές: τα ποσά Ε3 δεν χωράνε ανά γραμμή του παραστατικού,
    αλλά χωράνε στο σύνολό του (καθαρή ≤ Ε3 ≤ μικτή, με ανοχή στρογγυλοποίησης)."""
    e3 = [e for e in inv.cls_info or [] if "line" in e and not _is_vat_entry(e)
          and not e.get("transaction_mode") and (e.get("type") or e.get("category"))]
    if not e3 or not inv.lines:
        return False
    by_line: dict = {}
    for e in e3:
        by_line[e["line"]] = by_line.get(e["line"], 0.0) + (e.get("amount") or 0)
    fits_lines = all(
        (ln.net_value or 0) - 0.01 <= by_line.get(ln.line_number, 0.0)
        <= (ln.net_value or 0) + (ln.vat_amount or 0) + 0.01
        for ln in inv.lines
    ) and set(by_line) <= {ln.line_number for ln in inv.lines}
    net = sum(ln.net_value or 0 for ln in inv.lines)
    gross = net + sum(ln.vat_amount or 0 for ln in inv.lines)
    return not fits_lines and net - 0.05 <= sum(by_line.values()) <= gross + 0.05


def _remote_per_invoice(inv) -> tuple[dict, dict] | None:
    """Προσυμπλήρωση του συγκεντρωτικού τρόπου από χαρακτηρισμό που έγινε έτσι στο
    myDATA: (pi_e3 ανά ομάδα ΦΠΑ, pi_vat). Κάθε Ε3/ΦΠΑ πάει στην ομάδα με το πλησιέστερο
    ποσό (το myDATA δεν επιστρέφει κατηγορία ΦΠΑ). None αν δεν είναι συγκεντρωτικός."""
    if not _looks_per_invoice(inv):
        return None
    groups = _vat_groups(inv)
    pi_e3: dict[str, list] = {g["vat_category"]: [] for g in groups}
    pi_vat: dict[str, str] = {}
    vat_groups = [g for g in groups if g["classify_vat"]]
    for e in inv.cls_info:
        amount = e.get("amount") or 0
        if e.get("transaction_mode"):
            continue
        if _is_vat_entry(e):
            if vat_groups:
                g = min(vat_groups, key=lambda g: abs(g["net"] - amount))
                pi_vat[g["vat_category"]] = e["type"]
        elif e.get("type") or e.get("category"):
            g = min(groups, key=lambda g: min(abs(g["net"] - amount), abs(g["net"] + g["vat"] - amount)))
            pi_e3[g["vat_category"]].append(
                {"category": e.get("category") or "", "type": e.get("type") or "", "amount": amount}
            )
    return pi_e3, pi_vat


def _vat_group_label(cat: str) -> str:
    rate = VAT_CATEGORY_RATES.get(cat)
    return f"ΦΠΑ {rate}" if rate and rate[0].isdigit() else "Χωρίς ΦΠΑ"


def _rule_per_invoice(inv, rule: dict, patterns: dict) -> tuple[dict, dict]:
    """Συγκεντρωτικός χαρακτηρισμός από την πρόταση του συναλλασσόμενου: (pi_e3, pi_vat)
    ανά ομάδα ΦΠΑ. Κάθε ομάδα παίρνει την πρόταση της κατηγορίας ΦΠΑ της, αλλιώς τη βασική."""
    pi_e3: dict[str, list] = {}
    pi_vat: dict[str, str] = {}
    for g in _vat_groups(inv):
        r = patterns.get(g["vat_category"]) or rule
        # Κατηγορία 2.5 (χωρίς δικαίωμα έκπτωσης): χωρίς χαρακτηρισμό ΦΠΑ, ο ΦΠΑ στο Ε3.
        if g["classify_vat"]:
            pi_vat[g["vat_category"]] = "" if r.get("category") == NO_VAT_RIGHT else (r.get("vat_type") or "VAT_361")
        amount = g["net"] if pi_vat.get(g["vat_category"]) else g["net"] + g["vat"]
        pi_e3[g["vat_category"]] = [
            {"category": r.get("category", ""), "type": r.get("type", ""), "amount": round(amount, 2)}
        ]
    return pi_e3, pi_vat


def _per_invoice_entries(inv, e3: dict, vat_choice: dict) -> list[dict]:
    """Εγγραφές για αποθήκευση/αποστολή (line_number None = επίπεδο παραστατικού).
    Κάθε Ε3 κρατά την κατηγορία ΦΠΑ της ομάδας του (για την επανεμφάνιση στη φόρμα)·
    στο myDATA τα πεδία ΦΠΑ στέλνονται μόνο στους χαρακτηρισμούς ΦΠΑ."""
    entries = []
    for g in _vat_groups(inv):
        cat = g["vat_category"]
        group_cat = int(cat) if cat.isdigit() else None
        for r in e3.get(cat, []):
            entries.append(
                {
                    "line_number": None,
                    "classification_type": r["type"],
                    "classification_category": r["category"],
                    "amount": r["amount"],
                    "vat_category": group_cat,
                }
            )
        vt = vat_choice.get(cat)
        if g["classify_vat"] and vt:
            entries.append(
                {
                    "line_number": None,
                    "classification_type": vt,
                    "classification_category": "",
                    "amount": g["net"],
                    "vat_category": group_cat,
                    "vat_amount": g["vat"],
                }
            )
    return entries


def _line_classification_rows(
    inv, rule: dict, existing: dict | None = None, patterns: dict | None = None
) -> list[dict]:
    """Ομάδες χαρακτηρισμού ΑΝΑ ΓΡΑΜΜΗ του αρχικού παραστατικού. Το myDATA απαιτεί
    χαρακτηρισμό ΚΑΘΕ γραμμής, με άθροισμα E3 ίσο με την αξία της (σφάλματα
    303/304/306 όταν λείπουν γραμμές ή δεν κλείνουν τα ποσά). Κάθε ομάδα =
    {line_number, net, vat, vat_category, rows:[{category, type, amount, vat_type}]}.
    existing: υπάρχοντες χαρακτηρισμοί ανά γραμμή (από _document_cls_rows) για προσυμπλήρωση.
    patterns: προτάσεις του συναλλασσόμενου ανά κατηγορία ΦΠΑ (υπερισχύουν της βασικής rule)."""
    lines = inv.lines or []
    if not lines:
        # Παλιά/ελλιπή δεδομένα χωρίς ανάλυση γραμμών: μία συνθετική γραμμή.
        lines = [
            InvoiceLine(
                line_number=1,
                net_value=inv.total_net or 0.0,
                vat_amount=inv.total_vat or 0.0,
                vat_category=None,
                has_expenses_classification=False,
            )
        ]
    single = len(lines) == 1
    groups = []
    for ln in lines:
        net = round(ln.net_value or 0, 2)
        vat = round(ln.vat_amount or 0, 2)
        # Προσυμπλήρωση: χαρακτηρισμοί της ίδιας γραμμής· για μονόγραμμο
        # παραστατικό δέξου και τον συγκεντρωτικό (cls_info) ως fallback.
        rows = [r for r in (existing or {}).get(ln.line_number, []) if r["type"] or r["category"]]
        if not rows:
            src = ln.classifications or (inv.cls_info if single else []) or []
            rows = _pair_cls_rows(src)
        if not rows:
            # Πρόταση του συναλλασσόμενου για την κατηγορία ΦΠΑ της γραμμής, αλλιώς η βασική.
            r = (patterns or {}).get(str(ln.vat_category or "")) or rule
            no_vat_right = r.get("category") == NO_VAT_RIGHT
            rows = [
                {
                    "category": r.get("category", ""),
                    "type": r.get("type", ""),
                    # Κατηγορία 2.5 (χωρίς δικαίωμα έκπτωσης): ο ΦΠΑ μπαίνει στο Ε3.
                    "amount": round(net + vat, 2) if no_vat_right else net,
                    # Γραμμή χωρίς ΦΠΑ (ή 2.5) → «Χωρίς χαρακτηρισμό ΦΠΑ», ό,τι κι αν λέει ο κανόνας.
                    "vat_type": "" if (vat == 0 or no_vat_right) else (r.get("vat_type") or "VAT_361"),
                }
            ]
        groups.append(
            {
                "line_number": ln.line_number or (len(groups) + 1),
                "net": net,
                "vat": vat,
                "vat_category": str(ln.vat_category or ""),
                "rows": rows,
            }
        )
    return groups


def _safe_back(value: str | None) -> str:
    """Διεύθυνση επιστροφής μόνο εντός εφαρμογής (όχι //host)· αλλιώς κενό."""
    value = (value or "").strip()
    return value if value.startswith("/") and not value.startswith(("//", "/\\")) else ""


@app.route("/classify/<mark>")
def classify(mark):
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None:
        flash("Το παραστατικό δεν βρέθηκε. Κάνε νέα αναζήτηση.", "error")
        return redirect(url_for("invoices"))
    inv = _row_to_invoice(row)
    rule = load_rules().get(inv.issuer_vat or "", {})
    patterns = _patterns_for(inv.issuer_vat)  # προτάσεις ανά κατηγορία ΦΠΑ
    # Χωρίς δική της πρόταση η εταιρεία, προσυμπλήρωση από άλλη εταιρεία (με ένδειξη).
    rule_source = None
    rule_mode = db.get_rule_post_mode(cid, inv.issuer_vat)
    if not rule and not patterns:
        foreign = db.foreign_rule(cid, inv.issuer_vat)
        if foreign:
            rule, patterns, rule_source = foreign["default"], foreign["patterns"], foreign["company_name"]
            rule_mode = foreign["post_mode"]
    # Υπάρχων χαρακτηρισμός από όλες τις πηγές (τοπικός → myDATA → ενσωματωμένος).
    local = db.get_local_classification(row["id"])
    post_mode = int(row.get("cls_post_mode") or 0)
    line_cls, doc_cls = _document_cls_rows([] if post_mode else local, inv)
    if doc_cls and len(inv.lines) <= 1:  # συγκεντρωτικός σε μονόγραμμο → στη γραμμή του
        line_cls.setdefault(inv.lines[0].line_number if inv.lines else 1, []).extend(doc_cls)
    # Συγκεντρωτικά (ανά παραστατικό): προσυμπλήρωση από τον αποθηκευμένο, αλλιώς από τον κανόνα.
    # pi_e3 = {κατηγορία ΦΠΑ: [Ε3]} ανά συγκεντρωτική γραμμή.
    vat_groups = _vat_groups(inv)
    cats = [g["vat_category"] for g in vat_groups]
    is_vat = lambda e: (e["classification_type"] or "").startswith("VAT_")  # noqa: E731
    pi_e3: dict[str, list] = {c: [] for c in cats}
    if post_mode:
        for e in local:
            if not is_vat(e):
                cat = str(e["vat_category"] or "")
                pi_e3[cat if cat in pi_e3 else cats[0]].append(
                    {"category": e["classification_category"], "type": e["classification_type"], "amount": e["amount"]}
                )
        pi_vat = {str(e["vat_category"]): e["classification_type"] for e in local if is_vat(e) and e["vat_category"]}
    else:
        pi_e3, pi_vat = _rule_per_invoice(inv, rule, patterns)
    # Χωρίς τοπικό χαρακτηρισμό: αν ο υπάρχων του myDATA έγινε συγκεντρωτικά, η φόρμα
    # ανοίγει στον συγκεντρωτικό τρόπο με τα δικά του ποσά.
    remote_pi = None if (post_mode or local) else _remote_per_invoice(inv)
    if remote_pi:
        pi_e3, pi_vat = remote_pi
    pi_allowed = inv.invoice_type != "1.5" and bool(inv.lines)
    # Χωρίς κανέναν υπάρχοντα χαρακτηρισμό, η φόρμα ανοίγει στον τρόπο της πρότασης.
    if not (post_mode or local or inv.cls_info):
        post_mode = int(bool(rule_mode and pi_allowed))
    empty = {"category": "", "type": "", "amount": None}
    uncl, uncl_prev, uncl_next = _unclassified_around(inv)
    back = _safe_back(request.args.get("back"))
    return render_template(
        "classify.html",
        inv=inv,
        back=back,
        uncl_total=len(uncl),
        uncl_pos=next((i + 1 for i, r in enumerate(uncl) if r["mark"] == inv.mark), None),
        uncl_prev=uncl_prev and uncl_prev["mark"],
        uncl_next=uncl_next and uncl_next["mark"],
        post_mode=1 if remote_pi else post_mode,
        remote_pi=bool(remote_pi),
        pi_allowed=pi_allowed,
        vat_groups=vat_groups,
        pi_e3={c: rows or [empty] for c, rows in pi_e3.items()},
        pi_vat=pi_vat,
        line_groups=_line_classification_rows(inv, rule, line_cls, patterns),
        learned=len(patterns) > 1,
        rule_source=rule_source,
        has_existing=bool(inv.cls_info),
        already_classified=row["status"] != "unclassified",
        type_desc=INVOICE_TYPE_NAMES.get(inv.invoice_type or ""),
        vat_rates=VAT_CATEGORY_RATES,
        categories=EXPENSE_CATEGORIES,
        types=EXPENSE_TYPES,
        vat_types=VAT_TYPES,
        # Επιτρεπόμενοι συνδυασμοί ΕΞΟΔΩΝ (category2_x) για τον τύπο· κενό = χωρίς
        # περιορισμό (fallback: εμφανίζονται όλες οι επιλογές).
        combos={
            k: v
            for k, v in db.combos_for_type(inv.invoice_type).items()
            if k.startswith("category2")
        },
    )


@app.route("/submit/<mark>", methods=["POST"])
def submit(mark):
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None:
        flash("Το παραστατικό δεν βρέθηκε.", "error")
        return redirect(url_for("invoices"))
    inv = _row_to_invoice(row)
    back = _safe_back(request.form.get("back"))
    was_unclassified = row["status"] == "unclassified"
    manual = request.form.get("action") == "manual"

    # Συγκεντρωτικά (ανά παραστατικό): μία συγκεντρωτική γραμμή ανά κατηγορία ΦΠΑ,
    # με Ε3 (+ χαρακτηρισμό ΦΠΑ στις κατηγορίες 1–6).
    if request.form.get("post_mode") == "1":
        if inv.invoice_type == "1.5" or not inv.lines:
            flash("Ο χαρακτηρισμός ανά παραστατικό δεν επιτρέπεται για αυτό το παραστατικό.", "error")
            return redirect(url_for("classify", mark=mark, back=back or None))
        e3: dict[str, list] = {}
        for grp, ccat, ctype, amount in zip(
            request.form.getlist("pi_group"),
            request.form.getlist("pi_category"),
            request.form.getlist("pi_type"),
            request.form.getlist("pi_amount"),
        ):
            if not (ccat and ctype and amount.strip()):
                continue
            try:
                e3.setdefault(grp, []).append(
                    {"category": ccat, "type": ctype, "amount": round(float(amount), 2)}
                )
            except ValueError:
                flash(f"Μη έγκυρο ποσό «{amount}».", "error")
                return redirect(url_for("classify", mark=mark, back=back or None))
        # Τα ποσά ΦΠΑ τα υπολογίζει ο server από τις γραμμές — η φόρμα δίνει μόνο τον τύπο.
        vat_choice = {
            g["vat_category"]: request.form.get(f"pi_vat_type_{g['vat_category']}", "")
            for g in _vat_groups(inv)
            if g["classify_vat"]
        }
        errors = _per_invoice_errors(inv, e3, vat_choice)
        if errors:
            flash("⚠ Ο συγκεντρωτικός χαρακτηρισμός δεν είναι σωστός: " + " · ".join(errors) + ".", "error")
            return redirect(url_for("classify", mark=mark, back=back or None))
        entries = _per_invoice_entries(inv, e3, vat_choice)
        db.save_local_classification(cid, mark, entries, manual=manual, post_mode=1)
        _learn(inv, entries, post_mode=1)
        first = next(r for rs in e3.values() for r in rs)
        save_rule(inv.issuer_vat, first["type"], first["category"], next(iter(vat_choice.values()), ""))
        return _saved_redirect(back, manual, inv, was_unclassified)

    # Γραμμές χαρακτηρισμού ΑΝΑ ΓΡΑΜΜΗ παραστατικού (parallel λίστες από τη φόρμα):
    # κάθε γραμμή = αριθμός γραμμής παραστατικού + κατηγορία + τύπος E3 + ποσό
    # (+ προαιρετικό ΦΠΑ ίδιου ποσού). Το line_number έρχεται από την ΠΡΑΓΜΑΤΙΚΗ
    # γραμμή του παραστατικού - όχι σειριακά - ώστε να μη σκάσει σε σφάλμα 303/304.
    line_nos = request.form.getlist("line_number")
    cats = request.form.getlist("category")
    typs = request.form.getlist("type")
    amounts = request.form.getlist("amount")
    vts = request.form.getlist("vat_type")

    classifications = []
    rule_from = None
    for lno, ccat, ctype, amount, vat_type in zip(line_nos, cats, typs, amounts, vts):
        if not (ccat and ctype and amount.strip()):
            continue
        try:
            amt = round(float(amount), 2)
        except ValueError:
            flash(f"Μη έγκυρο ποσό «{amount}».", "error")
            return redirect(url_for("classify", mark=mark, back=back or None))
        try:
            line_no = int(lno)
        except TypeError:
            line_no = 1
        except ValueError:
            line_no = 1
        classifications.append(
            {
                "line_number": line_no,
                "classification_type": ctype,
                "classification_category": ccat,
                "amount": amt,
            }
        )
        # Γραμμή ΦΠΑ (VAT_xxx) με ΙΔΙΟ ποσό (καθαρή αξία), χωρίς category/vatAmount.
        if vat_type:
            classifications.append(
                {
                    "line_number": line_no,
                    "classification_type": vat_type,
                    "classification_category": "",
                    "amount": amt,
                }
            )
        if rule_from is None:
            rule_from = (ctype, ccat, vat_type)

    if not classifications:
        flash("Δεν συμπληρώθηκε καμία γραμμή χαρακτηρισμού.", "error")
        return redirect(url_for("classify", mark=mark, back=back or None))

    # Έλεγχος κάλυψης ΑΝΑ ΓΡΑΜΜΗ: το myDATA απαιτεί χαρακτηρισμό ΚΑΘΕ γραμμής του
    # αρχικού παραστατικού, με άθροισμα E3 στο εύρος [καθαρή, μικτή] της γραμμής -
    # καθαρή όταν το έξοδο έχει δικαίωμα έκπτωσης ΦΠΑ (E3=καθαρή + γραμμή ΦΠΑ),
    # μικτή όταν δεν έχει. Αλλιώς σφάλματα 303 (γραμμή χωρίς χαρακτηρισμό), 304
    # (πλήθος γραμμών ≠ πλήθος χαρακτηρισμών), 306 (άθροισμα ≠ αξία γραμμής).
    inv_lines = inv.lines or []
    if inv_lines:
        by_line: dict[int, list[dict]] = {}
        for c in classifications:
            by_line.setdefault(c["line_number"], []).append(c)
        errors = []
        stray = sorted(set(by_line) - {ln.line_number for ln in inv_lines})
        if stray:
            errors.append(
                "γραμμές που δεν υπάρχουν στο παραστατικό: "
                + ", ".join(map(str, stray))
            )
        for ln in inv_lines:
            net_i = round(ln.net_value or 0, 2)
            gross_i = round(net_i + (ln.vat_amount or 0), 2)
            entries = by_line.get(ln.line_number, [])
            if not entries:
                errors.append(
                    f"η γραμμή {ln.line_number} (καθαρή {net_i:.2f} €) "
                    "δεν χαρακτηρίστηκε"
                )
                continue
            e3_sum = round(
                sum(
                    e["amount"]
                    for e in entries
                    if not e["classification_type"].startswith("VAT_")
                ),
                2,
            )
            # Έλεγχος αθροίσματος μόνο όταν ξέρουμε την αξία της γραμμής (net>0).
            if gross_i > 0 and not (net_i - 0.01 <= e3_sum <= gross_i + 0.01):
                errors.append(
                    f"γραμμή {ln.line_number}: άθροισμα E3 {e3_sum:.2f} € εκτός "
                    f"εύρους {net_i:.2f}–{gross_i:.2f} €"
                )
        if errors:
            flash(
                "⚠ Ο χαρακτηρισμός δεν καλύπτει σωστά όλες τις γραμμές — το myDATA "
                "θα τον απέρριπτε (303/304/306): " + " · ".join(errors) + ".",
                "error",
            )
            return redirect(url_for("classify", mark=mark, back=back or None))

    # «Χειροκίνητο»: το παραστατικό είναι ήδη χαρακτηρισμένο στην πύλη myDATA - το
    # καταγράφουμε ΜΟΝΟ τοπικά (→ «Ολοκληρωμένα», tag Manually), χωρίς αποστολή.
    db.save_local_classification(cid, mark, classifications, manual=manual)
    _learn(inv, classifications)  # προτάσεις ανά κατηγορία ΦΠΑ

    # αποθήκευση κανόνα ανά συναλλασσόμενο από την πρώτη γραμμή χαρακτηρισμού
    if rule_from:
        save_rule(inv.issuer_vat, rule_from[0], rule_from[1], rule_from[2])
    return _saved_redirect(back, manual, inv, was_unclassified)


def _unclassified_around(inv):
    """Αχαρακτήριστα εξόδων σε σειρά βιβλίου + το προηγούμενο/επόμενο του inv (ή None)."""
    key = lambda r: (r["issue_date"] or "", r["mark"])  # noqa: E731
    rest = sorted(db.get_documents(_active_company_id(), "expense", ["unclassified"]), key=key)
    here = (inv.issue_date or "", inv.mark)
    prev = next((r for r in reversed(rest) if key(r) < here), None)
    nxt = next((r for r in rest if key(r) > here), None)
    return rest, prev, nxt


def _saved_redirect(back: str, manual: bool, inv, was_unclassified: bool = False):
    """Μετά την αποθήκευση. Με back (η λίστα από όπου ήρθε, με ταξινόμηση/φίλτρα/σελίδα):
    χαρακτηρισμός αχαρακτήριστου → επόμενο αχαρακτήριστο (κρατώντας το back), και στο τέλος η λίστα·
    διόρθωση → κατευθείαν η λίστα. Χωρίς back: η συνηθισμένη ροή."""
    if not back:
        return _classified_redirect(manual, inv)
    if was_unclassified:
        rest, _, nxt = _unclassified_around(inv)
        nxt = nxt or (rest[0] if rest else None)
        if nxt:
            flash(f"✔ Ο χαρακτηρισμός του {inv.mark} αποθηκεύτηκε. Επόμενο προς χαρακτηρισμό ({len(rest)} απομένουν).", "ok")
            return redirect(url_for("classify", mark=nxt["mark"], back=back))
    flash(f"✔ Ο χαρακτηρισμός του {inv.mark} αποθηκεύτηκε.", "ok")
    return redirect(back)


def _classified_redirect(manual: bool, inv):
    """Μήνυμα + μετάβαση μετά την αποθήκευση χαρακτηρισμού (και για τους δύο τρόπους):
    στο επόμενο αχαρακτήριστο (σειρά βιβλίου), αλλιώς στη λίστα."""
    rest, _, nxt = _unclassified_around(inv)
    nxt = nxt or (rest[0] if rest else None)
    if nxt:
        flash(f"✔ Ο χαρακτηρισμός του {inv.mark} αποθηκεύτηκε. Επόμενο προς χαρακτηρισμό ({len(rest)} απομένουν).", "ok")
        return redirect(url_for("classify", mark=nxt["mark"]))
    if manual:
        flash(
            "✔ Καταγράφηκε τοπικά ως ήδη χαρακτηρισμένο στο myDATA (χειροκίνητο). "
            "Θα το βρεις στα «Ολοκληρωμένα».",
            "ok",
        )
        return redirect(url_for("invoices", view="confirmed"))
    flash(
        "✔ Ο χαρακτηρισμός αποθηκεύτηκε τοπικά. Στείλ' τον στο myDATA από την "
        "καρτέλα «Χαρακτηρισμένα» με το κουμπί «Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(url_for("invoices", view="classified"))


NO_VAT_RIGHT = "category2_5"  # γενικά έξοδα χωρίς δικαίωμα έκπτωσης ΦΠΑ


def _combo_allowed(invoice_type: str, category: str, e3_type: str) -> bool:
    """Επιτρέπει η ΑΑΔΕ τον συνδυασμό κατηγορίας/τύπου Ε3 για τον τύπο παραστατικού;
    Χωρίς φορτωμένους συνδυασμούς για τον τύπο: δεν περιορίζουμε."""
    combos = {k: v for k, v in db.combos_for_type(invoice_type).items() if k.startswith("category2")}
    if not combos:
        return True
    allowed = combos.get(category) or []
    return "*" in allowed or e3_type in allowed


def _bulk_line_entries(line, category: str, e3_type: str, vat_choice: str) -> list[dict]:
    """Χαρακτηρισμός μίας γραμμής στον μαζικό χαρακτηρισμό. vat_choice: "auto" (VAT_361
    όπου υπάρχει ΦΠΑ), "none" (χωρίς χαρακτηρισμό ΦΠΑ) ή συγκεκριμένο VAT_xxx.
    Χωρίς χαρακτηρισμό ΦΠΑ — και πάντα στην κατηγορία 2.5 — ο ΦΠΑ της γραμμής μπαίνει
    στο Ε3 (ποσό = μικτή)· αλλιώς Ε3 = καθαρή + χαρακτηρισμός ΦΠΑ με ποσό την καθαρή."""
    net = round(line.net_value or 0, 2)
    vat = round(line.vat_amount or 0, 2)
    no_vat_cls = vat > 0 and (vat_choice == "none" or category == NO_VAT_RIGHT)
    entries = [
        {
            "line_number": line.line_number,
            "classification_type": e3_type,
            "classification_category": category,
            "amount": round(net + vat, 2) if no_vat_cls else net,
        }
    ]
    if vat > 0 and not no_vat_cls:
        entries.append(
            {
                "line_number": line.line_number,
                "classification_type": vat_choice if vat_choice.startswith("VAT_") else "VAT_361",
                "classification_category": "",
                "amount": net,
            }
        )
    return entries


@app.route("/bulk_classify", methods=["POST"])
def bulk_classify():
    """
    Συνολικός χαρακτηρισμός: ο ίδιος συνδυασμός εφαρμόζεται σε κάθε γραμμή
    ενός ή περισσότερων επιλεγμένων παραστατικών (ανά γραμμή - το myDATA δεν
    δέχεται το στοιχείο classificationPostMode στο XML, σφάλμα 340· ο συγκεντρωτικός
    χαρακτηρισμός γίνεται με την παράμετρο postPerInvoice: από τη φόρμα, ή εδώ όταν
    η «πρόταση ανά συναλλασσόμενο» είναι συγκεντρωτική).
    """
    marks = request.form.getlist("marks")
    ctype = request.form.get("bulk_type", "")
    ccat = request.form.get("bulk_category", "")
    vat_type = request.form.get("bulk_vat_type", "auto")
    use_rules = (
        request.form.get("action") == "rules"
    )  # "με βάση την πρόταση ανά συναλλασσόμενο"
    rules = load_rules()

    if not marks:
        flash("Δεν επιλέχθηκε κανένα παραστατικό.", "error")
        return redirect(url_for("invoices"))
    if not use_rules and not (ctype and ccat):
        flash("Επίλεξε κατηγορία και τύπο χαρακτηρισμού.", "error")
        return redirect(url_for("invoices"))
    if vat_type not in ("auto", "none") and vat_type not in VAT_TYPES:
        flash("Μη έγκυρος χαρακτηρισμός ΦΠΑ.", "error")
        return redirect(url_for("invoices"))

    cid = _active_company_id()
    ok, failed = [], []

    for mark in marks:
        row = db.get_document(cid, mark) if cid else None
        if row is None:
            failed.append(f"{mark}: δεν βρέθηκε στη λίστα")
            continue
        inv = _row_to_invoice(row)
        if (inv.invoice_type or "") == "1.5":
            failed.append(
                f"{mark}: ο συνολικός χαρακτηρισμός δεν επιτρέπεται για τύπο 1.5"
            )
            continue

        # Ο χαρακτηρισμός γίνεται ανά γραμμή (το myDATA δεν δέχεται το στοιχείο XML
        # classificationPostMode=1, σφάλμα 340). Fallback σε συμβολική γραμμή 1 αν λείπουν γραμμές.
        src_lines = inv.lines or [
            InvoiceLine(
                line_number=1,
                net_value=inv.total_net or 0.0,
                vat_amount=inv.total_vat or 0.0,
                vat_category=None,
                has_expenses_classification=False,
            )
        ]

        # Συνδυασμός ανά γραμμή: (type, category, vat). Με «πρόταση ανά συναλλασσόμενο» κάθε
        # γραμμή —ή ομάδα ΦΠΑ, αν η πρόταση είναι συγκεντρωτική— παίρνει την πρόταση της
        # κατηγορίας ΦΠΑ της, αλλιώς τη βασική του.
        post_mode = 0
        if use_rules:
            default = rules.get(inv.issuer_vat or "") or {}
            pats = _patterns_for(inv.issuer_vat)
            post_mode = db.get_rule_post_mode(cid, inv.issuer_vat) if inv.lines else 0
            if post_mode:
                pi_e3, pi_vat = _rule_per_invoice(inv, default, pats)
                pairs = [(r["type"], r["category"]) for rs in pi_e3.values() for r in rs]
            else:
                choice = {}
                for line in src_lines:
                    r = pats.get(str(line.vat_category or "")) or default
                    choice[line.line_number] = (r.get("type"), r.get("category"), r.get("vat_type") or "auto")
                pairs = [(t, c) for t, c, _ in choice.values()]
            if not all(t and c for t, c in pairs):
                failed.append(
                    f"{mark}: δεν υπάρχει αποθηκευμένη πρόταση για τον συναλλασσόμενο "
                    f"{inv.issuer_vat or '—'}"
                )
                continue
        else:
            choice = {line.line_number: (ctype, ccat, vat_type) for line in src_lines}
            pairs = [(ctype, ccat)]

        # Επίσημοι συνδυασμοί ΑΑΔΕ για τον τύπο του παραστατικού (αλλιώς απόρριψη στην αποστολή).
        bad = sorted({(c, t) for t, c in pairs if not _combo_allowed(inv.invoice_type, c, t)})
        if bad:
            failed.append(
                f"{mark}: ο συνδυασμός {' , '.join(f'{c} / {t}' for c, t in bad)} δεν επιτρέπεται "
                f"από την ΑΑΔΕ για τον τύπο {inv.invoice_type}"
            )
            continue

        if post_mode:  # συγκεντρωτικά (postPerInvoice), όπως ορίζει η πρόταση
            errors = _per_invoice_errors(inv, pi_e3, pi_vat)
            if errors:
                failed.append(f"{mark}: " + " · ".join(errors))
                continue
            classifications = _per_invoice_entries(inv, pi_e3, pi_vat)
        else:
            # Το ποσό της γραμμής VAT_xxx ισούται με την ΚΑΘΑΡΗ ΑΞΙΑ (σφάλμα 306), vatAmount null (337).
            classifications = [
                e
                for line in src_lines
                for e in _bulk_line_entries(line, choice[line.line_number][1], choice[line.line_number][0],
                                            choice[line.line_number][2])
            ]

        # Αποθήκευση ΤΟΠΙΚΑ (χωρίς αποστολή στο myDATA).
        db.save_local_classification(cid, mark, classifications, post_mode=post_mode)
        _learn(inv, classifications, post_mode)
        if not use_rules:  # ρητή επιλογή → γίνεται και η βασική πρόταση του συναλλασσόμενου
            save_rule(inv.issuer_vat, ctype, ccat, vat_type if vat_type.startswith("VAT_") else "")
        ok.append(mark)

    if ok:
        flash(
            f"✔ Χαρακτηρίστηκαν τοπικά {len(ok)} παραστατικά. Στείλ' τα στο myDATA "
            "από την καρτέλα «Χαρακτηρισμένα» με το κουμπί «Αποστολή στο myDATA».",
            "ok",
        )
    for f in failed:
        flash(f"Αποτυχία - {f}", "error")

    return redirect(url_for("invoices", view="classified" if ok else "unclassified"))


@app.route("/unclassify", methods=["POST"])
def unclassify():
    """Επαναφορά τοπικά χαρακτηρισμένων (μη απεσταλμένων) στα αχαρακτήριστα. Οι δικές μας
    «Νέες εγγραφές» δεν έχουν αχαρακτήριστη μορφή → απλώς διαγράφονται."""
    cid = _active_company_id()
    marks = request.form.getlist("marks")
    if not marks:
        flash("Δεν επιλέχθηκε κανένα παραστατικό.", "error")
        return redirect(url_for("invoices", view="classified"))
    drafts = [m for m in marks if (db.get_document(cid, m) or {}).get("local_action") == "create"]
    for m in drafts:
        db.delete_document(cid, m)
    n = db.unclassify(cid, marks)
    if drafts:
        flash(f"🗑 Διαγράφηκαν {len(drafts)} τοπικές «Νέες εγγραφές».", "ok")
    if n or not drafts:
        flash(f"↩ Επέστρεψαν {n} παραστατικά στα αχαρακτήριστα.", "ok")
    return redirect(url_for("invoices", view="unclassified" if n or not drafts else "classified"))


@app.route("/send", methods=["POST"])
def send():
    """Μαζική αποστολή στο myDATA των τοπικά χαρακτηρισμένων (όλων ή επιλεγμένων).
    Επιτυχία ανά παραστατικό → κατάσταση «Απεσταλμένο»."""
    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("invoices"))
    if not send_allowed():
        flash(SEND_DISABLED_MSG, "error")
        return redirect(url_for("invoices", view="classified"))

    selected = set(request.form.getlist("marks"))
    rows = db.get_documents(cid, "expense", ["classified"])
    if selected:
        rows = [r for r in rows if r["mark"] in selected]
    if not rows:
        flash("Δεν υπάρχουν χαρακτηρισμένα παραστατικά για αποστολή.", "error")
        return redirect(url_for("invoices", view="classified"))

    client = get_client()
    ok, failed = [], []
    for row in rows:
        mark = row["mark"]
        action = row.get("local_action") or "classify"
        try:
            if action in ("reject", "cancel") and not cancel_allowed():
                # η άδεια μπορεί να αφαιρέθηκε αφού μπήκε στην ουρά
                failed.append(f"{mark}: {CANCEL_DISABLED_MSG}")
                continue
            flag = {"create": "allow_new", "reject": None, "cancel": None}.get(action, "allow_classify")
            if flag and not get_active_company().get(flag):
                failed.append(f"{mark}: {_GATED[flag][1]}")
                continue
            if action == "reject":
                result = client.reject_invoice(mark)
            elif action == "cancel":
                result = client.cancel_invoice(mark)
            elif action == "create":
                draft = json.loads(row.get("draft_json") or "{}")
                result = client.send_self_expense_invoice(
                    draft.get("invoice_type", ""),
                    draft.get("series", "0"),
                    draft.get("aa", "1"),
                    draft.get("issue_date", ""),
                    draft.get("lines", []),
                    issuer_vat=draft.get("issuer_vat", ""),
                    issuer_country=draft.get("issuer_country", "GR"),
                    issuer_name=draft.get("issuer_name", ""),
                    issuer_address={k: draft.get("issuer_" + k, "") for k in ("street", "postal_code", "city")},
                    own_vat=get_own_vat(),
                    payment_method=draft.get("payment_method", "5"),
                    withheld=draft.get("withheld_amount") or 0.0,
                    deductions=draft.get("deductions_amount") or 0.0,
                    withheld_base=draft.get("withheld_base"),
                )
            else:  # classify / correct
                classifications = [
                    {
                        "line_number": e["line_number"],
                        "classification_type": e["classification_type"],
                        "classification_category": e["classification_category"] or "",
                        "amount": e["amount"],
                        "vat_category": e.get("vat_category"),
                        "vat_amount": e.get("vat_amount"),
                    }
                    for e in db.get_local_classification(row["id"])
                ]
                if not classifications:
                    failed.append(f"{mark}: λείπει ο τοπικός χαρακτηρισμός")
                    continue
                result = client.send_expenses_classification(
                    mark, classifications, post_per_invoice=bool(row.get("cls_post_mode"))
                )
        except MyDataError as e:
            failed.append(f"{mark}: {e}")
            continue

        if result["status"] and result["status"].lower() == "success":
            if action == "cancel":
                db.delete_document_by_id(row["id"])
                ok.append(f"{mark} (ακυρώθηκε)")
            elif action == "create":
                db.finalize_created(row["id"], result.get("invoice_mark"))
                ok.append(result.get("invoice_mark") or mark)
            else:
                db.mark_sent(row["id"], result.get("classification_mark"))
                ok.append(mark)
        else:
            failed.append(f"{mark}: " + "; ".join(result["errors"] or [str(result)]))

    if ok:
        flash(
            f"✔ Στάλθηκαν στο myDATA {len(ok)} παραστατικά. Πάτα «Ενημέρωση» "
            "αργότερα για επιβεβαίωση (το myDATA αργεί έως ~8 ώρες).",
            "ok",
        )
    for f in failed:
        flash(f"Αποτυχία - {f}", "error")

    return redirect(url_for("invoices", view="sent" if ok else "classified"))


@app.route("/refresh", methods=["POST"])
def refresh():
    """Ενημέρωση: ζητά από το myDATA το διάστημα των «Απεσταλμένων» (από τη
    μικρότερη έως τη μεγαλύτερη ημερομηνία έκδοσης). Όσα το myDATA δείχνει πλέον
    με τον ίδιο χαρακτηρισμό περνούν σε «Ολοκληρωμένα»."""
    cid = _active_company_id()
    sent = db.get_documents(cid, "expense", ["sent"]) if cid else []
    if not sent:
        flash("Δεν υπάρχουν απεσταλμένα παραστατικά προς ενημέρωση.", "error")
        return redirect(url_for("invoices", view="sent"))

    dates = [r["issue_date"] for r in sent if r["issue_date"]]
    if not dates:
        flash("Τα απεσταλμένα παραστατικά δεν έχουν ημερομηνία έκδοσης.", "error")
        return redirect(url_for("invoices", view="sent"))
    df = date.fromisoformat(min(dates)).strftime("%d/%m/%Y")
    dt = date.fromisoformat(max(dates)).strftime("%d/%m/%Y")

    try:
        classified = get_client().request_classified_expenses(df, dt)
    except MyDataError as e:
        flash(str(e), "error")
        return redirect(url_for("invoices", view="sent"))
    except Exception as e:  # noqa: BLE001 — network κ.λπ.
        flash(f"Σφάλμα επικοινωνίας: {e}", "error")
        return redirect(url_for("invoices", view="sent"))

    by_mark = {i.mark: i for i in classified}
    confirmed = 0
    for row in sent:
        inv = by_mark.get(row["mark"])
        if inv is None:
            continue
        remote = _norm_cls(getattr(inv, "cls_info", []))
        local = _norm_cls(json.loads(row.get("cls_json") or "[]"))
        # Επιβεβαίωση όταν το myDATA δείχνει τον ίδιο χαρακτηρισμό (ή την απόρριψη).
        if remote and (remote == local or local == {("__TM__", "1")}):
            db.mark_confirmed(
                row["id"], getattr(inv, "classification_mark", "") or None
            )
            confirmed += 1

    if confirmed:
        flash(f"✔ Επιβεβαιώθηκαν {confirmed} παραστατικά από το myDATA.", "ok")
    else:
        flash(
            "Το myDATA δεν έχει δείξει ακόμη τους χαρακτηρισμούς. Δοκίμασε ξανά "
            "αργότερα (μπορεί να χρειαστεί έως ~8 ώρες).",
            "ok",
        )
    return redirect(url_for("invoices", view="sent"))


# ------------------------------------------------------------------ #
# ΒΙΒΛΙΟ ΕΣΟΔΩΝ (φάση ανάκτησης/προβολής): κατεβάζουμε τα παραστατικά που έχουμε
# εκδώσει εμείς και τα δείχνουμε ως «Αχαρακτήριστα» / «Χαρακτηρισμένα». Δεν
# υποβάλλεται χαρακτηρισμός εσόδων από την εφαρμογή ακόμη.
# ------------------------------------------------------------------ #
_INCOME_VIEWS = ("unclassified", "classified")


@app.route("/income")
def income():
    view = request.args.get("view")
    if view not in _INCOME_VIEWS:
        # Χωρίς (έγκυρο) tab: το πρώτο με εγγραφές· κανένα → Ανάκτηση / Διαγραφή.
        cid = _active_company_id()
        by_status = db.count_by_status(cid, "income") if cid else {}
        view = next((v for v in _INCOME_VIEWS if by_status.get(v)), None)
        if view is None:
            return redirect(url_for("sync", scope="income"))
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "desc" if view == "classified" else "asc")
    reverse = direction == "desc"
    filters = {
        "mark": request.args.get("f_mark", "").strip(),
        "date": request.args.get("f_date", "").strip(),
        "issuer": request.args.get("f_issuer", "").strip(),
        "type": request.args.get("f_type", "").strip(),
    }

    def sort_key(inv):
        if sort == "name":
            return (inv.issuer_name or inv.issuer_vat or "").lower()
        if sort == "total":
            return inv.total_gross or 0.0
        return inv.issue_date or ""

    cid = _active_company_id()
    names = db.load_names()
    buckets = {v: [] for v in _INCOME_VIEWS}
    for row in db.get_documents(cid, "income") if cid else []:
        buckets.setdefault(row["status"], []).append(_row_to_invoice(row, names))

    counts = {v: len(buckets[v]) for v in _INCOME_VIEWS}

    items = buckets.get(view, [])
    fm = filters["mark"]
    fi, ft = filters["issuer"].lower(), filters["type"]
    fd = filters["date"]
    date_sub = date_lo = date_hi = None
    if fd:
        if ":" in fd:
            lo, _, hi = fd.partition(":")
            date_lo = lo.strip() or None
            date_hi = (hi.strip() + "~") if hi.strip() else None
        else:
            date_sub = fd

    if any(filters.values()):

        def _match(inv):
            if fm and fm not in (inv.mark or ""):
                return False
            d = inv.issue_date or ""
            if date_sub and date_sub not in d:
                return False
            if date_lo and d < date_lo:
                return False
            if date_hi and d > date_hi:
                return False
            if (
                fi
                and fi
                not in ((inv.issuer_name or "") + " " + (inv.issuer_vat or "")).lower()
            ):
                return False
            return bool(not (ft and ft not in (inv.invoice_type or "")))

        items = [i for i in items if _match(i)]

    items = sorted(items, key=sort_key, reverse=reverse)
    total = len(items)
    page_items, page, total_pages = paginate(items, request.args.get("page"))

    return render_template(
        "income.html",
        view=view,
        invoices=page_items,
        counts=counts,
        categories=INCOME_CATEGORIES,
        types=INCOME_TYPES,
        vat_types=VAT_TYPES,
        type_names=INVOICE_TYPE_NAMES,
        credit_types=CREDIT_INVOICE_TYPES,
        sort=sort,
        dir=direction,
        page=page,
        total_pages=total_pages,
        total=total,
        strip=_month_strip(cid, ("income",)),
        per_page=PER_PAGE,
        filters=filters,
    )


# ------------------------------------------------------------------ #
# ΑΝΑΦΟΡΕΣ
# ------------------------------------------------------------------ #
# Χαρακτηρισμένα έσοδα = status 'classified'. Χαρακτηρισμένα έξοδα = οποιαδήποτε
# κατάσταση πέρα από 'unclassified' (τοπικά χαρακτηρισμένα, απεσταλμένα ή
# επιβεβαιωμένα — όλα έχουν χαρακτηρισμό).
_INCOME_CLASSIFIED_STATUSES = ["classified"]
_EXPENSE_CLASSIFIED_STATUSES = ["classified", "sent", "confirmed"]
_VAT_CATEGORY_LABELS = {
    "1": "24%",
    "2": "13%",
    "3": "6%",
    "4": "17%",
    "5": "9%",
    "6": "4%",
    "7": "0%",
    "8": "Χωρίς ΦΠΑ",
    "9": "3%",
    "10": "4% (νησιά)",
}
_CLASSIFICATION_NAMES = {**INCOME_TYPES, **EXPENSE_TYPES}
_CLASSIFICATION_CATEGORY_NAMES = {**INCOME_CATEGORIES, **EXPENSE_CATEGORIES}


def _report_number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _report_financial_parts(doc: dict, group_by_vat: bool) -> list[dict]:
    totals = {
        "net": _report_number(doc.get("total_net")),
        "vat": _report_number(doc.get("total_vat")),
        "gross": _report_number(doc.get("total_gross")),
    }
    if not group_by_vat:
        return [{"line": None, "vat_category": "", **totals}]

    lines = [line for line in doc.get("lines", []) if isinstance(line, dict)]
    if not lines:
        return [{"line": None, "vat_category": "", **totals}]

    weights = {
        "net": [abs(_report_number(line.get("net_value"))) for line in lines],
        "vat": [abs(_report_number(line.get("vat_amount"))) for line in lines],
        "gross": [
            abs(
                _report_number(line.get("net_value"))
                + _report_number(line.get("vat_amount"))
            )
            for line in lines
        ],
    }
    parts = []
    for index, line in enumerate(lines):
        part = {
            "line": line.get("line_number"),
            "vat_category": str(line.get("vat_category") or ""),
        }
        for amount_name in ("net", "vat", "gross"):
            denominator = sum(weights[amount_name])
            share = (
                weights[amount_name][index] / denominator
                if denominator
                else 1 / len(lines)
            )
            part[amount_name] = totals[amount_name] * share
        parts.append(part)
    return parts


def _report_classifications(doc: dict) -> list[dict]:
    result = []
    for entry in doc.get("classifications", []):
        if not isinstance(entry, dict) or entry.get("transaction_mode"):
            continue
        classification_type = str(
            entry.get("type") or entry.get("classification_type") or ""
        )
        category = str(
            entry.get("category") or entry.get("classification_category") or ""
        )
        if classification_type.startswith("VAT_") or not (
            classification_type or category
        ):
            continue
        result.append(
            {
                "key": (category, classification_type),
                "line": entry.get("line", entry.get("line_number")),
                "weight": abs(_report_number(entry.get("amount"))),
            }
        )
    return result


_E3_GROUP_NAMES = {"E3_585": "Λοιπά Έξοδα"}  # λογαριασμοί χωρίς κοινό όνομα στους τύπους τους


def _e3_group_label(group: str) -> str:
    """Όνομα λογαριασμού Ε3 (π.χ. E3_561) από τους τύπους του (E3_561_001…): το κοινό τους
    πρόθεμα, κομμένο σε ολόκληρη λέξη — «Πωλήσεις αγαθών & υπηρεσιών»."""
    if group in _E3_GROUP_NAMES:
        return _E3_GROUP_NAMES[group]
    names = [n for k, n in _CLASSIFICATION_NAMES.items() if k == group or k.startswith(group + "_")]
    if len(names) <= 1:
        return names[0] if names else ""
    prefix = os.path.commonprefix(names)
    return prefix.rsplit(" ", 1)[0].rstrip(" -/&(") if " " in prefix else ""


def _olap_cube(cid: int | None) -> dict:
    """Πρώτη ύλη του κύβου OLAP (το pivot γίνεται στον browser, static/olap.js).
    docs: ένα ανά παραστατικό (διαστάσεις επιπέδου παραστατικού). facts: ένα ανά
    (παραστατικό × γραμμή/κατηγορία ΦΠΑ × χαρακτηρισμό Ε3) = [doc, κατηγ. Ε3, τύπος Ε3, κατηγ. ΦΠΑ,
    καθαρή, ΦΠΑ, σύνολο, παρακρ., λοιποί φόροι, ψηφ. τέλος, τέλη, κρατήσεις, μη εκπιπτόμενος ΦΠΑ].
    Μη εκπιπτόμενος = ΦΠΑ εξόδων χωρίς χαρακτηρισμό ΦΠΑ 361–366 (όπως στη δήλωση Φ2). Τα ποσά μοιράζονται
    αναλογικά (γραμμές κατά ποσό, χαρακτηρισμοί κατά ποσό)· οι φόροι επιπέδου παραστατικού ακολουθούν
    το μερίδιο καθαρής αξίας. Τα πιστωτικά αφαιρούνται (αρνητικό πρόσημο)."""
    # ponytail: όλα τα παραστατικά της εταιρείας στον browser — αν γίνουν δεκάδες χιλιάδες,
    # φίλτρο διαστήματος στο query.
    docs, facts, with_stock = [], [], stock_in("olap")
    for kind, statuses in (("income", _INCOME_CLASSIFIED_STATUSES), ("expense", _EXPENSE_CLASSIFIED_STATUSES)):
        for d in db.olap_documents(cid, kind, statuses):
            i = len(docs)
            docs.append({"k": kind[:2], "mark": d["mark"], "date": d["issue_date"] or "", "type": d["invoice_type"] or "",
                         "series": d["series"] or "", "aa": d["aa"] or "", "cp": d["counterparty_vat"] or "",
                         "cpn": d["counterparty_name"] or ""})
            d["lines"] = json.loads(d["lines_json"] or "[]")
            d["classifications"] = json.loads(d["cls_json"] or "[]")
            sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
            taxes = [sign * _report_number(d["total_" + t]) for t in
                     ("withheld", "other_taxes", "stamp_duty", "fees", "deductions")]
            # Εκπιπτόμενος ΦΠΑ ανά γραμμή (έξοδα)· ο υπόλοιπος ΦΠΑ της γραμμής είναι μη εκπιπτόμενος.
            deductible = vat_return.deductible_by_line(d) if kind == "expense" else None
            parts = _report_financial_parts(d, True)
            net_total = sum(abs(p["net"]) for p in parts)
            # Γραμμές κάθε χαρακτηρισμού Ε3: το «line» του myDATA μπορεί να είναι ομάδα ανά κατηγορία ΦΠΑ.
            e3 = _report_classifications(d)
            targets = vat_return.cls_lines([{"line": a["line"], "amount": a["weight"]} for a in e3],
                                           {ln.get("line_number"): ln for ln in d["lines"]})
            e3 = e3 or [{"key": ("", ""), "line": None, "weight": 1.0}]
            for p in parts:
                frac = abs(p["net"]) / net_total if net_total else 1 / len(parts)
                allocs = ([a for a, ns in zip(e3, targets) if ns and p["line"] in ns]
                          or [a for a, ns in zip(e3, targets) if ns is None] or e3)
                weights = sum(a["weight"] for a in allocs)
                line_vat = sign * p["vat"]
                nondeductible = 0.0
                if deductible is not None and line_vat:
                    nondeductible = min(max(1 - deductible.get(p["line"], 0.0) / line_vat, 0.0), 1.0)
                for a in allocs:
                    if not with_stock and a["key"][0] in STOCK_CATEGORIES:  # αποθέματα: όχι έξοδο της περιόδου
                        continue
                    share = a["weight"] / weights if weights else 1 / len(allocs)
                    amounts = [sign * p[x] * share for x in ("net", "vat", "gross")] + [t * frac * share for t in taxes]
                    amounts.append(amounts[1] * nondeductible)
                    facts.append([i, *a["key"], p["vat_category"], *(round(v, 4) for v in amounts)])
    used = lambda n: {f[n] for f in facts}  # noqa: E731
    return {
        "docs": docs, "facts": facts,
        "labels": {
            "type": {t: INVOICE_TYPE_NAMES.get(t, "") for t in {d["type"] for d in docs}},
            # «… (-) / (+)» στο τέλος: σήμανση προσήμου του Ε3, περιττή στον κύβο
            "e3c": {c: re.sub(r"\s*\([-+]\)(\s*/\s*\([-+]\))?\s*$", "", _CLASSIFICATION_CATEGORY_NAMES.get(c, ""))
                    for c in used(1)},
            "e3t": {t: _CLASSIFICATION_NAMES.get(t, "") for t in used(2)},
            "e3g": {g: _e3_group_label(g) for g in {"_".join(t.split("_")[:2]) for t in used(2) if t}},
            "vat": {v: _VAT_CATEGORY_LABELS.get(v, v) for v in used(3)},
        },
    }


# Ετήσια σύνοψη (όπως η «Σύνοψη» της πύλης myDATA): ανά μήνα, έσοδα και έξοδα.
_YEARLY_COLUMNS = ("net", "vat", "withheld", "other_taxes", "stamp_duty", "fees", "deductions", "third_party",
                   "assets", "depreciation", "stock")
# Στήλες ποσών (μετά την καθαρή αξία) του πίνακα ετήσιας σύνοψης ΚΑΙ του modal «Αναλυτικά».
YEARLY_TABLE_COLS = [("vat", "ΦΠΑ"), ("withheld", "Φόροι<br>παρακρ."), ("other_taxes", "Λοιποί<br>φόροι"),
                     ("stamp_duty", "Ψηφιακό<br>τέλος συν."), ("fees", "Τέλη"), ("deductions", "Κρατήσεις"),
                     ("third_party", "Έσοδα/Έξοδα<br>τρίτων")]
ASSET_CATEGORY = "category2_7"  # αγορές παγίων: κεφαλαιοποιούνται, δεν είναι έξοδο χρήσης
DEPRECIATION_TYPE = "E3_587"    # αποσβέσεις (π.χ. εγγραφή 17.x)
# Αποθέματα έναρξης (2.13) / λήξης (2.14): εγγραφές για το κόστος πωληθέντων, όχι έξοδα της περιόδου —
# εκτός από τα έξοδα όλων των αναφορών (σύνοψη, πίνακας ελέγχου, OLAP, ανάλυση Ε3).
STOCK_CATEGORIES = ("category2_13", "category2_14")
OPENING_STOCK, CLOSING_STOCK = STOCK_CATEGORIES
# Κόστος πωληθέντων = αποθέματα έναρξης + αγορές αποθεμάτων (2.1 εμπορεύματα / 2.2 πρώτες ύλες) − αποθέματα λήξης.
PURCHASE_CATEGORIES = ("category2_1", "category2_2")


# Ποιες αναφορές μετρούν τα αποθέματα στα έξοδα: ρύθμιση στις Παραμέτρους → «Αναφορές».
# (κλειδί, τίτλος, προεπιλογή, περιγραφή)
STOCK_REPORTS = (
    ("yearly", "Ετήσια / Μηνιαία σύνοψη", True, "Όπως το συνοπτικό βιβλίο του myDATA. Το καθαρό κέρδος μετρά πάντα μόνο τη μεταβολή τους (έναρξης − λήξης)."),
    ("dashboard", "Πίνακας ελέγχου", False, "Κάρτες εσόδων-εξόδων, αποτέλεσμα, γράφημα, κορυφαίοι προμηθευτές και πελάτες."),
    ("olap", "OLAP", False, "Όλα τα μέτρα του κύβου. Με ενεργό, εμφανίζονται ως κατηγορία Ε3 2.13 / 2.14."),
)


def stock_in(report: str) -> bool:
    """Αν η αναφορά μετρά τα αποθέματα (2.13 / 2.14) στα έξοδα."""
    default = next(d for k, _, d, _ in STOCK_REPORTS if k == report)
    return db.get_setting("stock_in_" + report, "1" if default else "0") == "1"


def _stock_amount(cls_json: str | None) -> float:
    """Ποσό των χαρακτηρισμών αποθεμάτων (2.13 / 2.14) ενός παραστατικού."""
    return sum(e.get("amount") or 0 for e in json.loads(cls_json or "[]") if e.get("category") in STOCK_CATEGORIES)
_GREEK_MONTHS = ("Ιαν.", "Φεβ.", "Μαρ.", "Απρ.", "Μαΐ.", "Ιουν.", "Ιουλ.", "Αυγ.", "Σεπ.", "Οκτ.", "Νοέ.", "Δεκ.")


def _yearly_totals(docs: list[dict], include_stock: bool = False) -> dict:
    """{μήνας 1–12: {στήλη: ποσό}}. Τα πιστωτικά αφαιρούνται. «Τρίτων» = ποσά χαρακτηρισμών
    κατηγορίας x_9 (για λογαριασμό τρίτων). assets = Ε3 κατηγορίας 2.7 (αγορές παγίων),
    depreciation = Ε3 αποσβέσεων (E3_587…), από τον ισχύοντα χαρακτηρισμό (cls_json). stock = αποθέματα
    2.13/2.14· include_stock=True τα κρατά στην καθαρή αξία (όπως το συνοπτικό βιβλίο του myDATA),
    αλλιώς η καθαρή αξία είναι χωρίς αυτά (πίνακας ελέγχου, σύγκριση με το Ε3)."""
    months = {m: dict.fromkeys(_YEARLY_COLUMNS, 0.0) for m in range(1, 13)}
    for d in docs:
        try:
            month = int((d["issue_date"] or "")[5:7])
        except ValueError:
            continue
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        e3 = [e for e in json.loads(d["cls_json"] or "[]") if not (e.get("type") or "").startswith("VAT_")]
        third = sum(e.get("amount") or 0 for e in e3 if (e.get("category") or "").endswith("_9"))
        assets = sum(e.get("amount") or 0 for e in e3 if e.get("category") == ASSET_CATEGORY)
        depreciation = sum(e.get("amount") or 0 for e in e3 if (e.get("type") or "").startswith(DEPRECIATION_TYPE))
        stock = sum(e.get("amount") or 0 for e in e3 if e.get("category") in STOCK_CATEGORIES)
        row = months[month]
        for col, value in (("net", (d["total_net"] or 0) - (0 if include_stock else stock)), ("vat", d["total_vat"]), ("withheld", d["total_withheld"]),
                           ("other_taxes", d["total_other_taxes"]), ("stamp_duty", d["total_stamp_duty"]),
                           ("fees", d["total_fees"]), ("deductions", d["total_deductions"]), ("third_party", third),
                           ("assets", assets), ("depreciation", depreciation), ("stock", stock)):
            row[col] += sign * (value or 0)
    return {m: {c: round(v, 2) for c, v in r.items()} for m, r in months.items()}


def _yearly_vat_periods(rows: list[dict], year: str, now) -> list[dict]:
    """Κάρτες ΦΠΑ κάτω από το διάγραμμα, κατά την περίοδο ΦΠΑ της εταιρείας: ανά μήνα
    (μηνιαίος) ή ανά τρίμηνο (τριμηνιαίος). partial = περίοδος που δεν έχει κλείσει."""
    if (get_active_company() or {}).get("vat_period") == "q":
        return [dict(rows[i]["vat_quarter"], short=f"{_QUARTER_NAMES[i // 3]} τριμ.",
                     title=rows[i]["vat_quarter"]["span"]) for i in range(0, len(rows), 3)]
    current = year == str(now.year)
    return [dict(r["vat_month"], short=_GREEK_MONTHS[i], title=r["label"],
                 partial=current and i + 1 == now.month) for i, r in enumerate(rows)]


def _yearly_statuses(kind: str, unclassified: bool) -> list[str]:
    """Καταστάσεις της ετήσιας σύνοψης· unclassified=True προσθέτει και τα αχαρακτήριστα
    (για σύγκριση με το συνοπτικό βιβλίο του myDATA, που τα μετρά όλα)."""
    statuses = _INCOME_CLASSIFIED_STATUSES if kind == "income" else _EXPENSE_CLASSIFIED_STATUSES
    return statuses + ["unclassified"] if unclassified else statuses


@app.route("/reports/yearly")
def reports_yearly():
    cid = _active_company_id()
    now = datetime.now(ATHENS)
    years = sorted(set(db.document_years(cid)) | {str(now.year)}, reverse=True)
    year = request.args.get("year", "")
    year = year if year in years else str(now.year)
    unc = request.args.get("unc") == "1"  # και τα αχαρακτήριστα (κουμπί στην κεφαλίδα)
    income = _yearly_totals(db.yearly_documents(cid, "income", _yearly_statuses("income", unc), year), stock_in("yearly"))
    expense = _yearly_totals(db.yearly_documents(cid, "expense", _yearly_statuses("expense", unc), year), stock_in("yearly"))
    unc_count = sum(len(db.yearly_documents(cid, k, ["unclassified"], year)) for k in ("income", "expense"))
    last = now.month if year == str(now.year) else 12  # τρέχον έτος: έως τον τρέχοντα μήνα
    rows = [
        {"label": f"{_GREEK_MONTHS[m - 1]} {year}", "income": income[m], "expense": expense[m],
         "balance": round(income[m]["net"] - expense[m]["net"], 2)}
        for m in range(1, last + 1)
    ]
    _add_vat_periods(rows, year)
    _add_f2_periods(rows, cid, year)
    cur_m = now.month if year == str(now.year) else None
    per_q = request.args.get("per") == "q"  # πίνακας ανά τρίμηνο αντί ανά μήνα
    if per_q:
        table_rows = []
        for q in range(0, len(rows), 3):
            ms = rows[q:q + 3]
            part = lambda p: {c: round(sum(r[p][c] for r in ms), 2) for c in _YEARLY_COLUMNS}  # noqa: E731
            table_rows.append({"label": f"{_QUARTER_NAMES[q // 3]} τρίμ. {year}", "income": part("income"),
                               "expense": part("expense"), "balance": round(sum(r["balance"] for r in ms), 2),
                               "vat_quarter": ms[0]["vat_quarter"], "f2_quarter": ms[0]["f2_quarter"], "docs": {"quarter": q // 3 + 1},
                               "now": bool(cur_m) and (cur_m - 1) // 3 == q // 3})
    else:
        table_rows = [dict(r, docs={"month": i + 1}, now=i + 1 == cur_m) for i, r in enumerate(rows)]
    total = lambda part: {c: round(sum(r[part][c] for r in rows), 2) for c in _YEARLY_COLUMNS}  # noqa: E731
    pl = _pl(db.yearly_documents(cid, "income", _INCOME_CLASSIFIED_STATUSES, year),
             db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, year))  # ίδιο με την ανάλυση Ε3
    # Όλες οι στήλες, πάντα — ίδιες με το συνοπτικό βιβλίο του myDATA.
    return render_template(
        "reports_yearly.html", year=year, years=years, rows=rows, cols=YEARLY_TABLE_COLS, pl=pl,
        unc=unc, unc_count=unc_count, per_q=per_q, table_rows=table_rows,
        income_total=total("income"), expense_total=total("expense"),
        chart=_yearly_chart(rows), current_month=now.month if year == str(now.year) else None,
        max_net=max([abs(r[p]["net"]) for r in table_rows for p in ("income", "expense")] + [1]),
        vat_periods=_yearly_vat_periods(rows, year, now),
        spark_in=_spark([r["income"]["net"] for r in rows]), spark_out=_spark([r["expense"]["net"] for r in rows]),
    )


@app.route("/reports/yearly/docs")
def reports_yearly_docs():
    """Τμήμα HTML για το modal της ετήσιας σύνοψης: τα παραστατικά εσόδων ή εξόδων ενός μήνα,
    με τα ίδια πρόσημα (πιστωτικά αρνητικά) και σύνολα με τη γραμμή του πίνακα."""
    kind = request.args.get("kind", "")
    year, month, quarter = request.args.get("year", ""), request.args.get("month", ""), request.args.get("quarter", "")
    if quarter.isdigit() and 1 <= int(quarter) <= 4:  # ολόκληρο τρίμηνο (πίνακας ανά τρίμηνο)
        q = int(quarter)
        months, label = range(q * 3 - 2, q * 3 + 1), f"{_QUARTER_NAMES[q - 1]} τρίμηνο {year}"
    elif month.isdigit() and 1 <= int(month) <= 12:
        months, label = [int(month)], f"{_GREEK_MONTHS[int(month) - 1]} {year}"
    else:
        months = None
    if kind not in ("income", "expense") or not year.isdigit() or not months:
        return "Μη έγκυρη επιλογή.", 400
    statuses = _yearly_statuses(kind, request.args.get("unc") == "1")
    docs = [d for m in months for d in db.month_documents(_active_company_id(), kind, statuses, f"{year}-{m:02d}")]
    for d in docs:
        d["credit"] = d["invoice_type"] in CREDIT_INVOICE_TYPES
        d["t"] = _yearly_totals([d], stock_in("yearly"))[int(d["issue_date"][5:7])]  # ίδιος υπολογισμός με τον πίνακα (πρόσημο, πάγια, τρίτων)
        d["gross"] = (-1 if d["credit"] else 1) * (d["total_gross"] or 0)
    total = {c: round(sum(d["t"][c] for d in docs), 2) for c in _YEARLY_COLUMNS}
    total["gross"] = round(sum(d["gross"] for d in docs), 2)
    return render_template(
        "_yearly_docs.html", docs=docs, total=total, kind=kind, cols=YEARLY_TABLE_COLS,
        label=label, type_names=INVOICE_TYPE_NAMES,
    )


def _e3_breakdown(docs: list[dict]) -> list[dict]:
    """Άθροιση των χαρακτηρισμών Ε3 (όχι ΦΠΑ) ανά (τύπος, κατηγορία) από τον ισχύοντα
    χαρακτηρισμό (cls_json). Τα πιστωτικά αφαιρούνται· count = πλήθος παραστατικών."""
    groups: dict = {}
    for d in docs:
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        seen = set()
        for e in json.loads(d["cls_json"] or "[]"):
            typ, cat = e.get("type") or "", e.get("category") or ""
            if typ.startswith("VAT_") or not (typ or cat):
                continue
            g = groups.setdefault((typ, cat), {"type": typ, "category": cat, "amount": 0.0, "count": 0})
            g["amount"] += sign * (e.get("amount") or 0)
            if (typ, cat) not in seen:
                g["count"] += 1
                seen.add((typ, cat))
    rows = sorted(groups.values(), key=lambda g: (g["type"], g["category"]))
    for g in rows:
        g["amount"] = round(g["amount"], 2)
        g["type_label"] = _CLASSIFICATION_NAMES.get(g["type"], "")
        g["category_label"] = _CLASSIFICATION_CATEGORY_NAMES.get(g["category"], "")
        g["asset"] = g["category"] == ASSET_CATEGORY
        g["depreciation"] = g["type"].startswith(DEPRECIATION_TYPE)
        g["stock"] = g["category"] in STOCK_CATEGORIES
    return rows


def _pl(inc_docs: list[dict], exp_docs: list[dict]) -> dict:
    """Αποτελέσματα χρήσης από τα ποσά Ε3 — ένας υπολογισμός για το «Καθαρό κέρδος» της ετήσιας σύνοψης,
    την ανάλυση Ε3 και τον φόρο: έσοδα − κόστος πωληθέντων (έναρξης + αγορές 2.1/2.2 − λήξης) − έξοδα χρήσης.
    Οι αγορές παγίων (2.7) μένουν χωριστά: κεφαλαιοποιούνται, στο κέρδος μπαίνουν μόνο οι αποσβέσεις."""
    income, expense = _e3_breakdown(inc_docs), _e3_breakdown(exp_docs)
    of = lambda cats: [g for g in expense if g["category"] in cats]  # noqa: E731
    total = lambda rows: round(sum(g["amount"] for g in rows), 2)  # noqa: E731
    opening, purchases, closing = of((OPENING_STOCK,)), of(PURCHASE_CATEGORIES), of((CLOSING_STOCK,))
    assets = of((ASSET_CATEGORY,))
    other = [g for g in expense if g["category"] not in (*STOCK_CATEGORIES, *PURCHASE_CATEGORIES, ASSET_CATEGORY)]
    pl = {"income": income, "opening": opening, "purchases": purchases, "closing": closing, "expense": other,
          "assets": assets, "inc_total": total(income), "opening_total": total(opening),
          "purchases_total": total(purchases), "closing_total": total(closing), "exp_total": total(other),
          "assets_total": total(assets), "depreciation": total([g for g in other if g["depreciation"]])}
    pl["cogs"] = round(pl["opening_total"] + pl["purchases_total"] - pl["closing_total"], 2)
    pl["gross"] = round(pl["inc_total"] - pl["cogs"], 2)
    pl["profit"] = round(pl["gross"] - pl["exp_total"], 2)
    pl["nd_vat"] = _nondeductible_vat(exp_docs)
    return pl


def _nondeductible_vat(exp_docs: list[dict]) -> dict:
    """Πληροφοριακά: ΦΠΑ εξόδων που δεν εκπίπτει (χωρίς χαρακτηρισμό ΦΠΑ 361–366, όπως στο OLAP / Φ2)
    και πόσος από αυτόν έχει ήδη μπει στα ποσά Ε3 (in_e3) ή όχι (out_e3). Δεν αλλάζει το κέρδος."""
    total = in_e3 = 0.0
    for d in exp_docs:
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        nd = abs(sign * (d["total_vat"] or 0) - sum(vat_return.deductible_by_line(d).values()))
        if nd < 0.005:
            continue
        cls = [e for e in json.loads(d["cls_json"] or "[]") if not (e.get("type") or "").startswith("VAT_")]
        stock = sum(e.get("amount") or 0 for e in cls if e.get("category") in STOCK_CATEGORIES)
        e3 = sum(e.get("amount") or 0 for e in cls) - stock
        # ponytail: Ε3 − καθαρή μπορεί να περιέχει και άλλους φόρους (π.χ. τέλη) — όριο το nd· ακριβές ανά γραμμή αν χρειαστεί.
        total += sign * nd
        in_e3 += sign * min(nd, max(0.0, e3 - ((d["total_net"] or 0) - stock)))
    return {"total": round(total, 2), "in_e3": round(in_e3, 2), "out_e3": round(total - in_e3, 2)}


def _year_profit(cid: int | None, year: str) -> tuple[float, float]:
    """(καθαρό κέρδος Ε3, παρακρατήσεις εσόδων) μιας χρήσης — ίδιος υπολογισμός με το modal «Καθαρό κέρδος»."""
    inc = db.yearly_documents(cid, "income", _INCOME_CLASSIFIED_STATUSES, year)
    profit = _pl(inc, db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, year))["profit"]
    return profit, round(sum(v["withheld"] for v in _yearly_totals(inc).values()), 2)


def _year_tax(cid: int | None, year: int, profit: float, withheld: float) -> dict:
    """Εκκαθάριση φόρου της χρήσης year για την ενεργή εταιρεία· η προκαταβολή που πληρώθηκε
    = η προκαταβολή που βγάζει η εκκαθάριση της προηγούμενης χρήσης."""
    company = get_active_company() or {}
    prev_profit, prev_withheld = _year_profit(cid, str(year - 1))
    prev_prepay = _tax_due(prev_profit, year - 1, company, prev_withheld, 0.0)["prepay"]
    return _tax_due(profit, year, company, withheld, prev_prepay)


@app.route("/reports/yearly/e3")
def reports_yearly_e3():
    """Τμήμα HTML για το modal «Καθαρό κέρδος»: έσοδα και έξοδα του έτους ανά χαρακτηρισμό Ε3."""
    year = request.args.get("year", "")
    if not year.isdigit():
        return "Μη έγκυρο έτος.", 400
    cid = _active_company_id()
    inc_docs = db.yearly_documents(cid, "income", _INCOME_CLASSIFIED_STATUSES, year)
    exp_docs = db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, year)
    pl = _pl(inc_docs, exp_docs)
    net = lambda docs: round(sum(v["net"] for v in _yearly_totals(docs, True).values()), 2)  # noqa: E731
    withheld = round(sum(v["withheld"] for v in _yearly_totals(inc_docs).values()), 2)
    # ?print=1: αυτόνομη σελίδα A4 για «Αποθήκευση ως PDF» από τον browser.
    return render_template(
        "yearly_e3_print.html" if request.args.get("print") else "_yearly_e3.html",
        year=year, pl=pl, tax=_year_tax(cid, int(year), pl["profit"], withheld),
        inc_net=net(inc_docs), exp_net=net(exp_docs), now_str=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )


_QUARTER_NAMES = ("Α΄", "Β΄", "Γ΄", "Δ΄")


def _vat_bounds(year: int, kind: str, n: int) -> tuple[str, str]:
    """Ημερομηνίες (yyyy-mm-dd) της περιόδου ΦΠΑ: kind «m» = μήνας n, «q» = τρίμηνο n."""
    import calendar

    first, last = (n, n) if kind == "m" else (3 * n - 2, 3 * n)
    return f"{year}-{first:02d}-01", f"{year}-{last:02d}-{calendar.monthrange(year, last)[1]:02d}"


def _vat_due_period(kind: str, now) -> tuple[int, int]:
    """(έτος, n) της περιόδου ΦΠΑ που δηλώνεται τώρα: η προηγούμενη του τρέχοντος μήνα/τριμήνου."""
    cur = now.month if kind == "m" else (now.month - 1) // 3 + 1
    return (now.year, cur - 1) if cur > 1 else (now.year - 1, 12 if kind == "m" else 4)


def _vat_auto_carry(cid: int | None, year: int, kind: str, n: int) -> tuple[float, float]:
    """(401, 483) αυτόματα από την προηγούμενη περίοδο ίδιου τύπου: το «ποσό για έκπτωση» (502)
    και το χρεωστικό έως 30 € που δεν αποδόθηκε. Επειδή κάθε περίοδος εξαρτάται από τη
    μεταφορά της προηγούμενης, η αλυσίδα ξεκινά από την πρώτη περίοδο με δεδομένα."""
    years = [int(y) for y in db.document_years(cid) if y.isdigit()]
    if not years:
        return 0.0, 0.0
    per_year = 12 if kind == "m" else 4
    credit = debit = 0.0
    for y in range(min(years), year + 1):
        for i in range(1, per_year + 1):
            if (y, i) >= (year, n):
                return credit, debit
            date_from, date_to = _vat_bounds(y, kind, i)
            codes = vat_return.compute(
                db.period_documents(cid, "income", date_from, date_to),
                db.period_documents(cid, "expense", date_from, date_to),
                credit, debit,
            )["codes"]
            credit, debit = codes["502"], codes["carry"]
    return credit, debit


# ------------------------------------------------------------------ #
# Πρόβλεψη αποτελέσματος τρέχοντος έτους
# ------------------------------------------------------------------ #
_FORECAST_STATUSES = ["unclassified", "classified", "sent", "confirmed"]


def _forecast(cur: list[float], prev: list[float], done: int, start: int = 0) -> tuple[list[float], str]:
    """Σημειακή πρόβλεψη 12 μηνών (λίστες 0–11). Οι μήνες < done είναι πραγματικοί· οι υπόλοιποι:
    περσινός μήνας × (φετινό / περσινό ίδιου διαστήματος), αλλιώς μέσος όρος μήνα, αλλιώς πέρσι.
    Βάση = μήνες start..done-1 (start = πρώτος μήνας με κίνηση: νέα εταιρεία ≠ μηδενικοί μήνες)."""
    actual, prev_same, n = sum(cur[start:done]), sum(prev[start:done]), done - start
    if n > 0 and prev_same > 0:
        ratio = actual / prev_same
        rest, label = [p * ratio for p in prev[done:]], f"εποχικότητα προηγούμενου έτους ×{ratio:.2f}".replace(".", ",")
    elif n > 0:
        rest, label = [actual / n] * (12 - done), "μέσος όρος ολοκληρωμένων μηνών"
    else:
        rest, label = prev[done:], "περσινά ποσά (δεν υπάρχει ακόμα ολοκληρωμένος μήνας με κίνηση)"
    return [round(v, 2) for v in cur[:done] + rest], label


def _scenarios(cur_in, cur_out, prev_in, prev_out, done: int, seed: int, start: int = 0, fixed: list[float] | None = None,
               known_in: list[float] | None = None, known_out: list[float] | None = None, runs: int = 5000) -> dict | None:
    """Bootstrap: κάθε υπόλοιπος μήνας παίρνει τον «συντελεστή» ενός τυχαίου ολοκληρωμένου μήνα
    (ίδιου για έσοδα/έξοδα — κρατά τη συσχέτιση). fixed = ντετερμινιστικές εκροές ανά μήνα (σταθερά
    έξοδα, κόστος παγίων) που αφαιρούνται χωρίς τύχη· known_in / known_out = προγραμματισμένα ποσά
    ανά μήνα: πιάνονται ή χάνονται, ποτέ δεν ξεπερνιούνται — ο μήνας k απέχει από το αναμενόμενο
    όσο ο ολοκληρωμένος μήνας k, αλλά μόνο προς το χειρότερο (έσοδα ≤, έξοδα ≥ πρόγραμμα). Επιστρέφει P10/P50/P90 του αποτελέσματος
    έτους και τα σωρευτικά P10/P90 ανά μήνα (μόνο οι μήνες ≥ done)· None αν < 2 μήνες.
    ponytail: με λίγους μήνες το εύρος υποεκτιμάται — ιστορικό περισσότερων ετών αν χρειαστεί."""
    if done - start < 2:
        return None
    fixed = fixed or [0.0] * 12
    rng = random.Random(seed)
    base = sum(cur_in[:done]) - sum(cur_out[:done]) - sum(fixed[:done])

    def sampler(cur, prev):
        prev_same = sum(prev[start:done])
        if prev_same <= 0:  # χωρίς ιστορικό: το ποσό ενός ολοκληρωμένου μήνα
            return lambda m, k: cur[k]
        ratio = sum(cur[start:done]) / prev_same
        return lambda m, k: prev[m] * (cur[k] / prev[k] if prev[k] > 0 else ratio)

    def factors(cur, prev):  # μήνας k: πραγματικό / αναμενόμενο (ίδια λογική με τον sampler)
        prev_same = sum(prev[start:done])
        ratio = sum(cur[start:done]) / prev_same if prev_same > 0 else 0
        mean = sum(cur[start:done]) / (done - start)
        exp = lambda k: prev[k] * ratio if prev_same > 0 else mean  # noqa: E731
        return {k: cur[k] / exp(k) if exp(k) > 0 else 1.0 for k in range(start, done)}

    s_in, s_out = sampler(cur_in, prev_in), sampler(cur_out, prev_out)
    if known_in:
        f_in = factors(cur_in, prev_in)
        s_in = lambda m, k: known_in[m] * min(f_in[k], 1.0)  # noqa: E731
    if known_out:
        f_out = factors(cur_out, prev_out)
        s_out = lambda m, k: known_out[m] * max(f_out[k], 1.0)  # noqa: E731
    paths = []
    for _ in range(runs):
        acc, path = base, []
        for m in range(done, 12):
            k = rng.randrange(start, done)
            acc += s_in(m, k) - s_out(m, k) - fixed[m]
            path.append(acc)
        paths.append(path)
    q = lambda xs: statistics.quantiles(xs, n=10)  # noqa: E731
    per_month = [q([p[i] for p in paths]) for i in range(12 - done)]
    final = per_month[-1]
    return {"p10": round(final[0], 2), "p50": round(final[4], 2), "p90": round(final[8], 2),
            "lo": [round(m[0], 2) for m in per_month], "hi": [round(m[8], 2) for m in per_month]}


_FIXED_E3 = ("E3_585_014", "E3_585_007", "E3_581_")  # ενοίκια, ΕΦΚΑ αυτοαπασχολούμενων, μισθοδοσία
_DEPR_FULL_LIMIT, _DEPR_RATE = 1500, 0.20  # πάγιο < 1.500 €: 100% στη χρήση αγοράς· αλλιώς 20% τον χρόνο


def _expense_lines(docs: list[dict]) -> tuple[list[dict], list[float]]:
    """Από τους χαρακτηρισμούς (χωρίς ΦΠΑ, πιστωτικά αρνητικά): ([μήνας 0–11]: {E3 type: ποσό} χωρίς
    πάγια / αποσβέσεις / αποθέματα, [ποσά γραμμών αγορών παγίων 2.7])."""
    by_type, assets = [{} for _ in range(12)], []
    for d in docs:
        try:
            m = int((d["issue_date"] or "")[5:7]) - 1
        except ValueError:
            continue
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        for e in json.loads(d["cls_json"] or "[]"):
            t, cat, amount = e.get("type") or "", e.get("category"), sign * (e.get("amount") or 0)
            if cat == ASSET_CATEGORY:
                assets.append(amount)
            elif t and not t.startswith(("VAT_", DEPRECIATION_TYPE)) and cat not in STOCK_CATEGORIES:
                by_type[m][t] = by_type[m].get(t, 0.0) + amount
    return by_type, assets


def _fixed_types(by_type: list[dict], done: int, start: int) -> dict[str, float]:
    """Σταθερά έξοδα → μηνιαίο ποσό προβολής (μέσος όρος έως 3 τελευταίων ολοκληρωμένων μηνών).
    Σταθερός = γνωστός κωδικός (ενοίκια, ΕΦΚΑ, μισθοδοσία) ή τύπος που υπάρχει σε καθέναν από
    τους 3 τελευταίους ολοκληρωμένους μήνες."""
    last = range(max(start, done - 3), done)
    types = {t for m in range(start, done) for t in by_type[m]}
    fixed = {t for t in types if t.startswith(_FIXED_E3)}
    if len(last) == 3:
        fixed |= {t for t in types if all(by_type[m].get(t) for m in last)}
    return {t: round(sum(by_type[m].get(t, 0.0) for m in last) / len(last), 2) for t in sorted(fixed)} if last else {}


def _depreciation(asset_lines: dict[int, list[float]], year: int, small: float = 0.0, large: float = 0.0,
                  months: int = 12) -> dict:
    """Αποσβέσεις έτους κατά τον κανόνα: γραμμή < 1.500 € αποσβένεται 100% στη χρήση αγοράς,
    αλλιώς 20% τον χρόνο (5 χρόνια — άρα μετρούν και οι αγορές των 4 προηγούμενων ετών).
    small / large = εκτιμώμενες νέες αγορές (σύνολο παγίων < 1.500 € / ≥ 1.500 € το καθένα). Τα large
    αγοράζονται στον τρέχοντα ανοιχτό μήνα: 20% × months/12 (μήνες χρήσης μέσα στο έτος, μαζί με τον μήνα αγοράς).
    Οι ήδη καταχωρημένες αποσβέσεις (E3_587) δεν μετρούν: θα διπλομετρούσαν."""
    rate = lambda v: 1.0 if abs(v) < _DEPR_FULL_LIMIT else _DEPR_RATE  # noqa: E731
    cur = asset_lines.get(year, [])
    older = sum(v for y in range(year - 4, year) for v in asset_lines.get(y, []) if abs(v) >= _DEPR_FULL_LIMIT)
    d = {"bought": sum(cur), "cur": sum(v * rate(v) for v in cur), "older_base": older, "older": older * _DEPR_RATE,
         "small": small, "large": large, "large_depr": large * _DEPR_RATE * months / 12, "months": months}
    d["total"] = d["cur"] + d["older"] + small + d["large_depr"]
    return {k: round(v, 2) for k, v in d.items()}


# Φόρος εισοδήματος: κλίμακες ανά έτος ισχύος — επεξεργάσιμες στις Παραμέτρους («Φορολογία»),
# αποθηκεύονται στο settings.tax_scales· χωρίς αποθήκευση ισχύουν οι προεπιλογές.
# limits = άνω όρια κλιμακίων (το τελευταίο ποσοστό ισχύει «και πάνω»)· ποσοστά σε %.
_DEFAULT_TAX_SCALES = [
    {"from_year": 2020, "legal_rate": 22, "prepay_sole": 55, "prepay_legal": 80,
     "limits": [10000, 20000, 30000, 40000], "rates": [9, 22, 28, 36, 44], "young": []},
    {"from_year": 2026, "legal_rate": 22, "prepay_sole": 55, "prepay_legal": 80,
     "limits": [10000, 20000, 30000, 40000, 60000], "rates": [9, 20, 26, 34, 39, 44],
     "young": [{"max_age": 25, "rates": [0, 0, 26, 34, 39, 44]}, {"max_age": 30, "rates": [9, 9, 26, 34, 39, 44]}]},
]


def _tax_scales() -> list[dict]:
    try:
        scales = json.loads(db.get_setting("tax_scales") or "null")
    except ValueError:
        scales = None
    return sorted(scales or _DEFAULT_TAX_SCALES, key=lambda s: s["from_year"])


def _tax_regime(year: int, scales: list[dict] | None = None) -> dict:
    """Το καθεστώς με το μεγαλύτερο from_year ≤ year (αλλιώς το παλαιότερο)."""
    scales = scales or _tax_scales()
    return next((s for s in reversed(scales) if s["from_year"] <= year), scales[0])


def _income_tax(profit: float, year: int, company: dict, regime: dict) -> float:
    """Φόρος κερδών χρήσης: νομικό πρόσωπο σταθερός συντελεστής· ατομική κλίμακα, με τους συντελεστές
    νέων αν η ηλικία (έτος − έτος γέννησης) ≤ max_age. Ζημία → 0."""
    if profit <= 0:
        return 0.0
    if company.get("entity_type") != "sole":
        return round(profit * regime["legal_rate"] / 100, 2)
    rates, born = regime["rates"], company.get("birth_year")
    if born:
        rates = next((y["rates"] for y in sorted(regime["young"], key=lambda y: y["max_age"])
                      if year - born <= y["max_age"]), rates)
    tax, low = 0.0, 0.0
    for high, rate in zip([*regime["limits"], float("inf")], rates):
        tax += max(0.0, min(profit, high) - low) * rate / 100
        low = high
    return round(tax, 2)


def _tax_due(profit: float, year: int, company: dict, withheld: float, prev_prepay: float) -> dict:
    """Εκκαθάριση χρήσης: φόρος − παρακρατήσεις (myDATA εσόδων) − προκαταβολή που πληρώθηκε πέρσι
    + προκαταβολή επόμενου έτους (ποσοστό του φόρου μείον τις παρακρατήσεις, ποτέ αρνητική).
    due < 0 = επιστροφή / συμψηφισμός.
    ponytail: χωρίς μεταφορά ζημιών, ελάχιστο τεκμαρτό εισόδημα, μείωση προκαταβολής 50% τα 3 πρώτα
    έτη, εισφορά αλληλεγγύης και λοιπά εισοδήματα του φυσικού προσώπου — εκτίμηση, όχι εκκαθαριστικό."""
    regime = _tax_regime(year)
    sole = company.get("entity_type") == "sole"
    tax = _income_tax(profit, year, company, regime)
    rate = regime["prepay_sole" if sole else "prepay_legal"]
    prepay = round(max(0.0, tax * rate / 100 - withheld), 2)
    return {"profit": round(profit, 2), "tax": tax, "withheld": round(withheld, 2), "prev_prepay": round(prev_prepay, 2),
            "prepay": prepay, "prepay_rate": rate, "due": round(tax - withheld - prev_prepay + prepay, 2),
            "sole": sole, "regime_year": regime["from_year"]}


def _amount_arg(name: str, source=None) -> float | None:
    """Ποσό ≥ 0 από query string ή source (π.χ. request.form): «1500», «1.500», «1.500,50», «1500.5»· κενό → None, άκυρο → ValueError."""
    raw = (request.args if source is None else source).get(name, "").strip().replace(" ", "")
    if "," in raw or re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):  # «1.500,50» / «20.000»
        raw = raw.replace(".", "").replace(",", ".")
    if not raw:
        return None
    if not re.fullmatch(r"\d{1,12}(\.\d{1,2})?", raw):
        raise ValueError(name)
    return min(float(raw), 1e8)


def _spread(total: float, weights: list[float]) -> list[float]:
    """Μοιράζει το total αναλογικά των weights (ισόποσα αν δεν έχουν θετικό άθροισμα)· η διαφορά
    στρογγυλοποίησης πάει στον τελευταίο, ώστε το άθροισμα να είναι ακριβώς total."""
    w = weights if sum(weights) > 0 and all(v >= 0 for v in weights) else [1.0] * len(weights)
    out = [round(total * v / sum(w), 2) for v in w]
    if out:
        out[-1] = round(total - sum(out[:-1]), 2)
    return out


def _plan(total: float, stat: list[float], booked: list[float]) -> list[float]:
    """Πρόγραμμα υπόλοιπων μηνών ΕΠΙΠΛΕΟΝ των ήδη καταχωρημένων ποσών (booked): κάθε μήνας = booked +
    μερίδιο του total, αναλογικά της στατιστικής πρόβλεψης (stat)."""
    return [round(b + r, 2) for b, r in zip(booked, _spread(total, stat))]


def _forecast_chart(inc: list[float], out: list[float], res_m: list[float], prev_res: list[float], sc: dict | None,
                    done: int, dep: float = 0.0) -> dict:
    """Γεωμετρία SVG (viewBox 760×280): σωρευτικές γραμμές εσόδων/εξόδων/αποτελέσματος — συνεχείς
    στους πραγματικούς μήνες, διακεκομμένες στην πρόβλεψη — με βεντάλια P10–P90 και περσινή αναφορά.
    res_m = αποτέλεσμα ανά μήνα (με το κόστος παγίων dep τον Δεκέμβριο)."""
    from itertools import accumulate

    W, H, left, right, top, bottom = 760, 280, 58, 118, 16, 30
    base = H - bottom
    ci, co, res, pr = (list(accumulate(v)) for v in (inc, out, res_m, prev_res))
    values = ci + co + res + pr + (sc["lo"] + sc["hi"] if sc else []) + [0]
    step = _nice_step(max(values) - min(min(values), 0))
    lo, hi = step * (min(values) // step), step * -(-max(values) // step)
    hi = hi if hi > lo else lo + step
    slot = (W - left - right) / 12
    x = lambda i: round(left + slot * (i + 0.5), 1)  # noqa: E731
    y = lambda v: round(base - (v - lo) / (hi - lo) * (base - top), 1)  # noqa: E731
    pts = lambda vals, idx: " ".join(f"{x(i)},{y(vals[i])}" for i in idx)  # noqa: E731
    split = max(done - 1, 0)
    actual, future = range(done), range(split, 12)

    def line(vals):
        return {"actual": "M" + pts(vals, actual) if done > 1 else "", "future": "M" + pts(vals, future),
                "area": f"M{x(0)},{y(max(lo, 0))} L" + pts(vals, range(12)) + f" L{x(11)},{y(max(lo, 0))}Z"}

    fan = ""
    if sc:
        start = res[done - 1]
        fan = ("M" + f"{x(split)},{y(start)} " + " ".join(f"{x(done + i)},{y(v)}" for i, v in enumerate(sc["hi"]))
               + " L" + " ".join(f"{x(done + i)},{y(v)}" for i, v in reversed(list(enumerate(sc["lo"])))) + "Z")
    # Ετικέτες τέλους: ελάχιστη απόσταση 15px ώστε να μη συγκρούονται.
    ends = sorted([{"key": "in", "label": "Έσοδα", "v": ci[-1]}, {"key": "out", "label": "Έξοδα", "v": co[-1]},
                   {"key": "res", "label": "Αποτέλεσμα", "v": res[-1]}], key=lambda e: y(e["v"]))
    last = -99
    for e in ends:
        e["y"], e["ly"] = y(e["v"]), max(y(e["v"]), last + 15)
        last = e["ly"]
    months = [{"label": _GREEK_MONTHS[i], "cx": x(i), "x": round(left + slot * i, 1), "w": round(slot, 1),
               "in": round(ci[i], 2), "out": round(co[i], 2), "res": round(res[i], 2), "prev": round(pr[i], 2),
               "forecast": i >= done, "dep": dep if i == 11 else 0,
               "lo": sc["lo"][i - done] if sc and i >= done else None, "hi": sc["hi"][i - done] if sc and i >= done else None}
              for i in range(12)]
    ticks = [{"v": lo + step * k, "y": y(lo + step * k)} for k in range(int(round((hi - lo) / step)) + 1)]
    return {"w": W, "h": H, "left": left, "right": W - right, "top": top, "base": base, "zero": y(0),
            "split_x": round(left + slot * done, 1), "done": done, "months": months, "ticks": ticks, "ends": ends,
            "in": line(ci), "out": line(co), "res": line(res), "prev": "M" + pts(pr, range(12)), "fan": fan,
            "end_x": x(11)}


@app.route("/reports/forecast")
def reports_forecast():
    cid = _active_company_id()
    now = datetime.now(ATHENS)
    year, done, with_stock = now.year, now.month - 1, stock_in("yearly")
    # Εκτιμώμενες νέες αγορές παγίων έως το τέλος του έτους: σύνολο παγίων < 1.500 € / ≥ 1.500 € το καθένα.
    # Προγραμματισμένα έσοδα / μεταβλητά έξοδα υπόλοιπων μηνών: αντικαθιστούν τη στατιστική πρόβλεψη.
    errors = []

    def amount(name, label):
        try:
            return _amount_arg(name)
        except ValueError:
            errors.append(f"Μη έγκυρο ποσό: {label}.")
            return None

    small = amount("assets_small", f"αγορές παγίων κάτω από {_DEPR_FULL_LIMIT:,} €".replace(",", ".")) or 0.0
    large = amount("assets_large", f"αγορές παγίων από {_DEPR_FULL_LIMIT:,} € και πάνω".replace(",", ".")) or 0.0
    if 0 < large < _DEPR_FULL_LIMIT:
        errors.append("Οι αγορές παγίων από 1.500 € και πάνω θέλουν ποσό τουλάχιστον 1.500 € (ή κενό).")
        large = 0.0
    planned_in = amount("planned_income", "προγραμματισμένα έσοδα")
    planned_out = amount("planned_expense", "προγραμματισμένα έξοδα")

    past = sorted(int(y) for y in db.document_years(cid) if y.isdigit() and int(y) < year)
    years = sorted({year, year - 1, *past})
    docs = {(k, y): db.yearly_documents(cid, k, _FORECAST_STATUSES, str(y)) for k in ("income", "expense") for y in years}
    totals = {key: _yearly_totals(d, with_stock) for key, d in docs.items()}
    col = lambda key, c: [totals[key][m][c] for m in range(1, 13)]  # noqa: E731
    lines = {y: _expense_lines(docs[("expense", y)]) for y in years}  # y → (by_type, γραμμές παγίων)
    asset_lines = {y: lines[y][1] for y in years}
    for y in range(years[0] - 4, years[0]):  # αγορές ≥ 1.500 € που αποσβένονται ακόμα
        asset_lines[y] = _expense_lines(db.yearly_documents(cid, "expense", _FORECAST_STATUSES, str(y)))[1]

    # Έξοδα χρήσης χωρίς αγορές παγίων και καταχωρημένες αποσβέσεις (αντί γι' αυτές: αποσβέσεις κατά τον κανόνα).
    def operating(y):
        key = ("expense", y)
        return [n - a - dp for n, a, dp in zip(col(key, "net"), col(key, "assets"), col(key, "depreciation"))]

    cur_in, prev_in = col(("income", year), "net"), col(("income", year - 1), "net")
    cur_op, prev_op = operating(year), operating(year - 1)
    start = next((m for m in range(done) if cur_in[m] or cur_op[m]), done)  # πρώτος μήνας με κίνηση
    fixed = _fixed_types(lines[year][0], done, start)
    fixed_m = lambda y: [sum(lines[y][0][m].get(t, 0.0) for t in fixed) for m in range(12)]  # noqa: E731
    cur_fixed, prev_fixed = fixed_m(year), fixed_m(year - 1)
    cur_var = [o - f for o, f in zip(cur_op, cur_fixed)]
    prev_var = [o - f for o, f in zip(prev_op, prev_fixed)]

    f_in, label_in = _forecast(cur_in, prev_in, done, start)
    f_var, label_var = _forecast(cur_var, prev_var, done, start)
    forecast_in, forecast_var = round(sum(f_in[done:]), 2), round(sum(f_var[done:]), 2)  # στατιστικά, για τα placeholder
    if planned_in is not None or planned_out is not None:
        # Χαρακτηρισμένα / ολοκληρωμένα παραστατικά των μηνών της πρόβλεψης: προστίθενται στο πρόγραμμα.
        b_docs = {k: db.yearly_documents(cid, k, ["classified", "sent", "confirmed"], str(year)) for k in ("income", "expense")}
        b_in_t, b_out_t = (_yearly_totals(b_docs[k], with_stock) for k in ("income", "expense"))
        b_types = _expense_lines(b_docs["expense"])[0]
        booked_in = [b_in_t[m + 1]["net"] for m in range(done, 12)]
        booked_var = [b_out_t[m + 1]["net"] - b_out_t[m + 1]["assets"] - b_out_t[m + 1]["depreciation"]
                      - sum(b_types[m].get(t, 0.0) for t in fixed) for m in range(done, 12)]

    def plan(total, stat, booked):
        b = round(sum(booked), 2)
        label = f"προγραμματισμένα {format_el_amount(total)} €, αναλογικά της εποχικότητας"
        return _plan(total, stat, booked), label + (f" + καταχωρημένα {format_el_amount(b)} €" if b else "")

    if planned_in is not None:
        f_in[done:], label_in = plan(planned_in, f_in[done:], booked_in)
    if planned_out is not None:
        f_var[done:], label_var = plan(planned_out, f_var[done:], booked_var)
    f_fixed = [round(v, 2) for v in cur_fixed[:done]] + [round(sum(fixed.values()), 2)] * (12 - done)
    dep = _depreciation(asset_lines, year, small, large, months=12 - done)  # αγορά μέσα στον τρέχοντα μήνα
    dep_m = [0.0] * 11 + [dep["total"]]  # απόσβεση: εγγραφή τέλους χρήσης

    f_op = [round(a + b, 2) for a, b in zip(f_var, f_fixed)]
    res_m = [round(i - o - d, 2) for i, o, d in zip(f_in, f_op, dep_m)]
    sc = _scenarios(cur_in, cur_var, prev_in, prev_var, done, seed=year, start=start,
                    fixed=[a + b for a, b in zip(f_fixed, dep_m)],
                    known_in=f_in if planned_in is not None else None, known_out=f_var if planned_out is not None else None)
    rows = [{"label": f"{_GREEK_MONTHS[m]} {year}", "income": f_in[m], "fixed": f_fixed[m], "variable": f_var[m],
             "dep": dep_m[m], "expense": round(f_op[m] + dep_m[m], 2), "balance": res_m[m], "forecast": m >= done,
             "planned": m >= done and (planned_in is not None or planned_out is not None)}
            for m in range(12)]
    tot = lambda xs: round(sum(xs), 2)  # noqa: E731
    total = {"income": tot(f_in), "fixed": tot(f_fixed), "variable": tot(f_var), "op": tot(f_op),
             "dep": dep["total"], "expense": round(tot(f_op) + dep["total"], 2), "result": tot(res_m)}
    ytd = {"income": tot(cur_in[:done]), "expense": tot(cur_op[:done])}

    # Προηγούμενα έτη με τα ίδια μέτρα (σταθερά = οι ίδιοι κωδικοί, αποσβέσεις κατά τον κανόνα).
    history = []
    for y in reversed(past):
        inc, fx, op = tot(col(("income", y), "net")), tot(fixed_m(y)), tot(operating(y))
        d = _depreciation(asset_lines, y)["total"]
        history.append({"year": y, "income": inc, "fixed": fx, "variable": round(op - fx, 2), "dep": d,
                        "expense": round(op + d, 2), "result": round(inc - op - d, 2)})
    prev_dep = _depreciation(asset_lines, year - 1)["total"]
    prev_res = [i - o - (prev_dep if m == 11 else 0) for m, (i, o) in enumerate(zip(prev_in, prev_op))]
    # «Αναμενόμενο» = η σημειακή πρόβλεψη· με πρόγραμμα, η διάμεσος μετά τον κίνδυνο απόκλισης.
    planned = planned_in is not None or planned_out is not None
    expected = sc["p50"] if planned and sc else total["result"]
    if sc:  # το εύρος να περιέχει πάντα το αναμενόμενο
        sc["p10"], sc["p90"] = min(sc["p10"], expected), max(sc["p90"], expected)
    # Φόρος επί της σημειακής πρόβλεψης (με πρόγραμμα: το αποτέλεσμα του προγράμματος, όχι η διάμεσος μετά τον
    # κίνδυνο — ίδια έσοδα με την παρακράτηση). Παρακρατήσεις = πραγματικές των ολοκληρωμένων μηνών (τα έσοδα του
    # τρέχοντος μήνα είναι πρόβλεψη) + ατομική με παρακρατήσεις: και στα έσοδα της πρόβλεψης, με το ίδιο ποσοστό
    # (παρακράτηση / καθαρά έσοδα) φέτος, αλλιώς πέρσι.
    cur_w, prev_w = col(("income", year), "withheld"), col(("income", year - 1), "withheld")
    w_rate = next((round(sum(w) / sum(i), 4) for w, i in ((cur_w[:done], cur_in[:done]), (prev_w, prev_in)) if sum(i) > 0), 0.0)
    if (get_active_company() or {}).get("entity_type") != "sole":
        w_rate = 0.0
    f_income = round(sum(f_in[done:]), 2)
    w_future = round(max(w_rate, 0.0) * f_income, 2)
    tax = _year_tax(cid, year, total["result"], round(sum(cur_w[:done]) + w_future, 2))
    tax.update(withheld_forecast=w_future, withheld_rate=round(w_rate * 100, 2), forecast_income=f_income,
               withheld_span=f"{_GREEK_MONTHS[0]}–{_GREEK_MONTHS[done - 1]}" if done else "")
    return render_template(
        "reports_forecast.html", year=year, prev_year=year - 1, done=done, rows=rows, total=total, ytd=ytd,
        history=history, sc=sc, start=start, expected=expected, tax=tax, plan_result=total["result"] if planned else None, labels={"income": label_in, "expense": label_var},
        has_prev=any(prev_in) or any(prev_op), prev_in_total=tot(prev_in), errors=errors,
        fixed=[{"label": EXPENSE_TYPES.get(t, t), "code": t, "amount": v} for t, v in fixed.items()],
        dep=dep, form={k: request.args.get(k, "") for k in ("assets_small", "assets_large", "planned_income", "planned_expense")},
        forecast_in=forecast_in, forecast_var=forecast_var,
        depr_limit=_DEPR_FULL_LIMIT, depr_rate=round(_DEPR_RATE * 100),
        chart=_forecast_chart(f_in, f_op, res_m, prev_res, sc, done, dep["total"]),
    )

@app.route("/reports/vat")
def reports_vat():
    """Δήλωση ΦΠΑ (Φ2) για μήνα (period=m1…m12) ή τρίμηνο (q1…q4) ενός έτους, ανάλογα με την
    περίοδο ΦΠΑ της εταιρείας (vat_period). Το 401 υπολογίζεται αυτόματα από την προηγούμενη
    περίοδο· τιμή στο prev_credit υπερισχύει (κενό = αυτόματα)."""
    cid = _active_company_id()
    kind = (get_active_company() or {}).get("vat_period") or "m"
    now = datetime.now(ATHENS)
    years = sorted(set(db.document_years(cid)) | {str(now.year)}, reverse=True)
    year = request.args.get("year", "")
    year = year if year in years else str(now.year)
    period = request.args.get("period", "")
    if not (len(period) >= 2 and period[0] == kind and period[1:].isdigit()
            and 1 <= int(period[1:]) <= (12 if kind == "m" else 4)):
        # Προεπιλογή: η προηγούμενη περίοδος (αυτή που δηλώνεται τώρα).
        due_y, due_n = _vat_due_period(kind, now)
        period = f"{kind}{due_n}"
        if "year" not in request.args:
            year = str(due_y)
    n, y = int(period[1:]), int(year)
    date_from, date_to = _vat_bounds(y, kind, n)
    auto_credit, auto_debit = _vat_auto_carry(cid, y, kind, n)

    def manual(arg):  # κενό = αυτόματα
        value = (request.args.get(arg) or "").strip().replace(",", ".")
        try:
            return max(0.0, float(value)) if value else None
        except ValueError:
            return None

    manual_credit, manual_debit = manual("prev_credit"), manual("prev_debit")
    result = vat_return.compute(
        db.period_documents(cid, "income", date_from, date_to),
        db.period_documents(cid, "expense", date_from, date_to),
        auto_credit if manual_credit is None else manual_credit,
        auto_debit if manual_debit is None else manual_debit,
    )
    return render_template(
        "reports_vat.html", years=years, year=year, period=period,
        manual_credit=manual_credit, auto_credit=auto_credit, manual_debit=manual_debit, auto_debit=auto_debit,
        date_from=date_from, date_to=date_to, months=_GREEK_MONTHS, quarters=_QUARTER_NAMES,
        c=result["codes"], invoiced_vat=result["invoiced_vat"],
    )


def _add_vat_periods(rows: list[dict], year: str) -> None:
    """Σε κάθε μήνα (rows[i] = μήνας i+1): ΦΠΑ εκροών/εισροών/διαφορά του μήνα και του
    τριμήνου του. Τρίμηνο που δεν έχει κλείσει (τρέχον έτος) αθροίζει μόνο όσους μήνες υπάρχουν."""
    def vat(rs):
        out, inp = round(sum(r["income"]["vat"] for r in rs), 2), round(sum(r["expense"]["vat"] for r in rs), 2)
        return {"out": out, "in": inp, "diff": round(out - inp, 2)}

    for i, r in enumerate(rows):
        q = i // 3
        months = rows[q * 3:q * 3 + 3]
        r["vat_month"] = vat([r])
        r["vat_quarter"] = dict(
            vat(months),
            label=f"{_QUARTER_NAMES[q]} τρίμηνο {year}",
            span=f"{_GREEK_MONTHS[q * 3]}–{_GREEK_MONTHS[q * 3 + len(months) - 1]}",
            partial=len(months) < 3,
        )


def _add_f2_periods(rows: list[dict], cid, year: str) -> None:
    """Για το popup ΦΠΑ του πίνακα: ό,τι μετρά η δήλωση Φ2 (vat_return.compute) ανά μήνα (f2_month)
    και τρίμηνο (f2_quarter) — έσοδα μόνο οι γραμμές με ΦΠΑ, έξοδα μόνο με εκπιπτόμενο ΦΠΑ κατά τους
    χαρακτηρισμούς ΦΠΑ. n_* = βάση (καθαρή αξία)· out = ΦΠΑ τιμολογίων (337 ± στρογγυλοποίηση 422/402)·
    t_* = όλη η καθαρή αξία των ίδιων παραστατικών, x_* = το μέρος της εκτός ΦΠΑ (t = x + n). Οι πράξεις λήπτη
    (364–366) βγαίνουν και από τις δύο πλευρές (ο φόρος τους συμψηφίζεται) → μετρούν στα «εκτός ΦΠΑ»."""
    docs = {k: db.period_documents(cid, k, f"{year}-01-01", f"{year}-12-31") for k in ("income", "expense")}
    signed = lambda ds: sum((-1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1) * (d["total_net"] or 0) for d in ds)  # noqa: E731

    def f2(ms):
        pick = lambda k: [d for d in docs[k] if int(d["issue_date"][5:7]) in ms and vat_return._sent(d)  # noqa: E731
                          and d.get("local_action") not in ("reject", "cancel")]  # όπως το compute
        inc, exp = pick("income"), pick("expense")
        c = vat_return.compute(inc, exp)["codes"]
        rc_base, rc_vat = (sum(c.get(f"{p}{i}", 0.0) for i in (4, 5, 6)) for p in ("36", "38"))
        n_out, n_in = round(c["307"] - rc_base, 2), round(c["367"] - rc_base, 2)
        out, inp = round(c["337"] + c["428"] - c["410"] - rc_vat, 2), round(c["387"] - rc_vat, 2)
        t_out, t_in = round(signed(inc), 2), round(signed(exp), 2)
        x_out, x_in = round(t_out - n_out, 2), round(t_in - n_in, 2)
        return {"t_out": t_out, "t_in": t_in, "t_diff": round(t_out - t_in, 2), "x_out": x_out, "n_out": n_out, "out": out, "x_in": x_in, "n_in": n_in, "in": inp,
                "x_diff": round(x_out - x_in, 2), "n_diff": round(n_out - n_in, 2), "diff": round(out - inp, 2)}

    for i, r in enumerate(rows):
        q = i // 3
        r["f2_month"] = f2({i + 1})
        r["f2_quarter"] = dict(f2(set(range(q * 3 + 1, min(q * 3 + 3, len(rows)) + 1))),
                               **{k: r["vat_quarter"][k] for k in ("label", "span", "partial")})


def _nice_step(top: float, ticks: int = 4) -> float:
    """Στρογγυλό βήμα άξονα (1/2/2.5/5 × 10ⁿ) ώστε ~ticks γραμμές να καλύπτουν το top."""
    import math

    raw = max(top, 1.0) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def _yearly_chart(rows: list[dict]) -> dict:
    """Γεωμετρία SVG (viewBox 760×260): ανά μήνα μπάρα εσόδων και στοιβαγμένη μπάρα εξόδων —
    κάτω τα έξοδα χωρίς αγορές παγίων, πάνω οι αγορές παγίων (κατηγορία 2.7). Αρνητικά (μήνας
    με μόνο πιστωτικά) σχεδιάζονται μηδενικά — φαίνονται στον πίνακα."""
    W, H, left, right, top, bottom = 760, 220, 58, 8, 14, 30
    base = H - bottom
    top_value = max([r["income"]["net"] for r in rows] + [r["expense"]["net"] for r in rows] + [0])
    step = _nice_step(top_value)
    n_ticks = max(1, -(-top_value // step))  # όσες γραμμές χρειάζονται (χωρίς περιττό κενό από πάνω)
    ymax = step * n_ticks
    plot_h, slot = base - top, (W - left - right) / max(len(rows), 1)
    bar_w = min(22.0, slot * 0.3)
    y = lambda v: base - max(v, 0) / ymax * plot_h  # noqa: E731

    def seg(x: float, lo: float, hi: float, rounded: bool) -> str:
        """Τμήμα μπάρας από το lo έως το hi (σε ευρώ)· rounded = στρογγυλεμένη κορυφή (4px)."""
        by, ty, r = y(lo), y(hi), 4.0
        if by - ty < 0.5:
            return ""
        if not rounded or by - ty < r:
            return f"M{x:.1f},{ty:.1f}h{bar_w:.1f}V{by:.1f}H{x:.1f}Z"
        return (f"M{x:.1f},{by:.1f}V{ty + r:.1f}Q{x:.1f},{ty:.1f} {x + r:.1f},{ty:.1f}"
                f"H{x + bar_w - r:.1f}Q{x + bar_w:.1f},{ty:.1f} {x + bar_w:.1f},{ty + r:.1f}V{by:.1f}Z")

    gap = 2 * ymax / plot_h  # 2px κενό (σε ευρώ) ανάμεσα στα στοιβαγμένα τμήματα
    months = []
    for i, r in enumerate(rows):
        cx = left + slot * (i + 0.5)
        exp_net, assets = r["expense"]["net"], max(r["expense"]["assets"], 0)
        op = max(exp_net - assets, 0)  # έξοδα χωρίς αγορές παγίων
        has_assets = y(op) - y(exp_net) >= 0.5
        months.append({
            "label": r["label"].split()[0], "full": r["label"], "cx": round(cx, 1),
            "x": round(left + slot * i, 1), "w": round(slot, 1),
            "income_path": seg(cx - bar_w - 1, 0, r["income"]["net"], True),  # 2px κενό ανάμεσα
            "op_path": seg(cx + 1, 0, op, not has_assets),
            "asset_path": seg(cx + 1, op + (gap if op else 0), exp_net, True) if has_assets else "",
            "income": r["income"]["net"], "expense": exp_net, "op": round(op, 2), "assets": assets,
            "balance": r["balance"], "profit": round(r["income"]["net"] - op, 2),
        })
    return {"w": W, "h": H, "left": left, "right": W - right, "base": base, "top": top, "months": months,
            "has_assets": any(m["asset_path"] for m in months),
            "ticks": [{"v": step * k, "y": round(y(step * k), 1)} for k in range(int(n_ticks) + 1)]}


def _spark(values: list[float], w: int = 120, h: int = 34) -> str:
    """Σημεία polyline για sparkline (viewBox w×h)."""
    lo, hi = min(values + [0]), max(values + [0])
    span, step = (hi - lo) or 1, w / max(len(values) - 1, 1)
    return " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(values))


@app.route("/dashboard")
def dashboard():
    """Πίνακας ελέγχου: εκκρεμότητες, σύνοψη έτους, ΦΠΑ περιόδου, κορυφαίοι προμηθευτές/πελάτες, πρόσφατα."""
    import calendar

    company = get_active_company()
    if not company:
        return redirect(url_for("companies"))
    cid, now = company["id"], datetime.now(ATHENS)
    year, today = str(now.year), now.date()
    exp_st, inc_st = db.count_by_status(cid, "expense"), db.count_by_status(cid, "income")

    # Σύνοψη έτους (ίδιοι υπολογισμοί με την Ετήσια σύνοψη).
    with_stock = stock_in("dashboard")
    income = _yearly_totals(db.yearly_documents(cid, "income", _INCOME_CLASSIFIED_STATUSES, year), with_stock)
    expense = _yearly_totals(db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, year), with_stock)
    rows = [{"label": f"{_GREEK_MONTHS[m - 1]} {year}", "income": income[m], "expense": expense[m],
             "balance": round(income[m]["net"] - expense[m]["net"], 2)} for m in range(1, now.month + 1)]
    ytd = lambda part, col: round(sum(r[part][col] for r in rows), 2)  # noqa: E731
    kpi = {
        "income": ytd("income", "net"), "expense": ytd("expense", "net"),
        "vat": round(ytd("income", "vat") - ytd("expense", "vat"), 2),
        "spark_in": _spark([r["income"]["net"] for r in rows]),
        "spark_out": _spark([r["expense"]["net"] for r in rows]),
    }
    kpi["result"] = round(kpi["income"] - kpi["expense"], 2)
    _add_vat_periods(rows, year)

    # ΦΠΑ: η περίοδος που δηλώνεται τώρα, με προθεσμία το τέλος του επόμενου μήνα.
    kind = company.get("vat_period") or "m"
    vy, vn = _vat_due_period(kind, now)
    date_from, date_to = _vat_bounds(vy, kind, vn)
    # Τριμηνιαία: μετά την προθεσμία (τέλος 1ου μήνα του τριμήνου) δείξε το τρέχον τρίμηνο.
    running = date_to < (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    if running:
        vy, vn = now.year, (now.month - 1) // 3 + 1
        date_from, date_to = _vat_bounds(vy, kind, vn)
    codes = vat_return.compute(
        db.period_documents(cid, "income", date_from, date_to),
        db.period_documents(cid, "expense", date_from, date_to),
        *_vat_auto_carry(cid, vy, kind, vn),
    )["codes"]
    last_month = vn if kind == "m" else 3 * vn
    dy, dm = (vy, last_month + 1) if last_month < 12 else (vy + 1, 1)
    due = date(dy, dm, calendar.monthrange(dy, dm)[1])
    vat = {
        "label": (_QUARTER_NAMES[vn - 1] + " τρίμηνο" if kind == "q" else _GREEK_MONTHS[vn - 1]) + f" {vy}",
        "period": f"{kind}{vn}", "year": str(vy), "pay": codes["511"], "credit": codes["502"],
        "out": codes["337"], "in": codes["430"], "due": due.strftime("%d/%m/%Y"),
        "days": (due - today).days, "kind": kind, "running": running,
    }

    # Εκκρεμότητες: μόνο όσες έχουν κάτι να γίνει.
    todos = []

    def todo(n, icon, title, sub, href, tone):
        if n:
            todos.append({"n": n, "icon": icon, "title": title, "sub": sub, "href": href, "tone": tone})

    todo(exp_st.get("unclassified", 0), "🧾", "Έξοδα προς χαρακτηρισμό", "Αχαρακτήριστα παραστατικά εξόδων",
         url_for("invoices", view="unclassified"), "warn")
    todo(exp_st.get("classified", 0), "📤", "Χαρακτηρισμοί προς αποστολή", "Έτοιμοι τοπικά — αποστολή στο myDATA",
         url_for("invoices", view="classified"), "accent")
    todo(exp_st.get("sent", 0), "⏳", "Αναμένουν επιβεβαίωση", "Απεσταλμένα — ανανέωση από το myDATA",
         url_for("invoices", view="sent"), "info")
    todo(inc_st.get("unclassified", 0), "💶", "Έσοδα προς χαρακτηρισμό", "Αχαρακτήριστα παραστατικά εσόδων",
         url_for("income", view="unclassified"), "warn")
    due_monthly = [t for t in _recurring_pending(cid, today.strftime("%Y-%m")) if t["monthly_day"] <= today.day]
    todo(len(due_monthly), "🔁", "Μηνιαίες εγγραφές προς δημιουργία", ", ".join(t["name"] for t in due_monthly),
         url_for("recurring"), "accent")
    if vat["days"] <= 15:
        todo(f"{max(vat['days'], 0)}ημ", "🏛", f"Δήλωση ΦΠΑ {vat['label']}",
             f"Προθεσμία {vat['due']}" + (f" · προς καταβολή {format_el_amount(vat['pay'])} €" if vat["pay"] else ""),
             url_for("reports_vat", year=vat["year"], period=vat["period"]), "err" if vat["days"] <= 5 else "warn")

    # Πρόοδος βιβλίου εξόδων (donut): τμήματα ως stroke-dasharray σε κύκλο pathLength=100.
    exp_total = sum(exp_st.values())
    segments, acc = [], 0.0
    for key, label, color, view in (("confirmed", "Επιβεβαιωμένα", "var(--ok)", "confirmed"),
                                    ("sent", "Απεσταλμένα", "var(--info)", "sent"),
                                    ("classified", "Προς αποστολή", "var(--accent)", "classified"),
                                    ("unclassified", "Αχαρακτήριστα", "var(--warn)", "unclassified")):
        n = exp_st.get(key, 0)
        pct = n / exp_total * 100 if exp_total else 0
        segments.append({"label": label, "n": n, "pct": round(pct, 2), "offset": round(-acc, 2),
                         "color": color, "href": url_for("invoices", view=view)})
        acc += pct
    done_pct = round(exp_st.get("confirmed", 0) / exp_total * 100) if exp_total else 0

    # Κορυφαίοι προμηθευτές/πελάτες έτους + πρόσφατη κίνηση.
    exp_docs = db.period_documents(cid, "expense", f"{year}-01-01", f"{year}-12-31")
    inc_docs = db.period_documents(cid, "income", f"{year}-01-01", f"{year}-12-31")

    def top_of(docs, stock):
        top: dict = {}
        for d in docs:
            sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
            net = (d["total_net"] or 0) - (_stock_amount(d["cls_json"]) if stock else 0)
            if not net:  # μόνο αποθέματα: δεν είναι αγορά από τον συναλλασσόμενο
                continue
            t = top.setdefault(d["counterparty_vat"], {"name": d["counterparty_name"] or d["counterparty_vat"] or "—",
                                                       "vat": d["counterparty_vat"], "amount": 0.0, "n": 0})
            t["amount"] += sign * net
            t["n"] += 1
        return sorted(top.values(), key=lambda t: -t["amount"])[:5]

    top = top_of(exp_docs, not with_stock)
    # Λιανική (χωρίς ΑΦΜ) δεν είναι πελάτης.
    top_in = top_of([d for d in inc_docs if d["counterparty_vat"]], False)
    recent = sorted([dict(d, kind="expense") for d in exp_docs] + [dict(d, kind="income") for d in inc_docs],
                    key=lambda d: (d["issue_date"] or "", d["mark"] or ""), reverse=True)[:12]
    for d in recent:
        d["credit"] = d["invoice_type"] in CREDIT_INVOICE_TYPES

    return render_template(
        "dashboard.html", company=company, year=year, kpi=kpi, chart=_yearly_chart(rows), vat=vat,
        vat_periods=_yearly_vat_periods(rows, year, now),
        todos=todos, segments=segments,
        last_ranges=(("εξόδων", db.get_setting(_range_key("expense")), url_for("sync", scope="expense")),
                     ("εσόδων", db.get_setting(_range_key("income")), url_for("sync", scope="income"))), exp_total=exp_total, done_pct=done_pct, inc_st=inc_st,
        top=top, top_max=max([t["amount"] for t in top] + [1]),
        top_in=top_in, top_in_max=max([t["amount"] for t in top_in] + [1]), recent=recent,
        type_names=INVOICE_TYPE_NAMES, today=today.strftime("%d/%m/%Y"), hour=now.hour,
    )


@app.route("/reports")
def reports():
    """OLAP: κύβος ανάλυσης εσόδων-εξόδων (pivot, slice, drill-down στον browser)."""
    return render_template("reports.html", cube=_olap_cube(_active_company_id()))


def _copy_lines(inv, local: list[dict]) -> list[dict]:
    """Γραμμές φόρμας «Νέας εγγραφής» από υπάρχον παραστατικό: μία γραμμή ανά E3
    χαρακτηρισμό. Σε split γραμμή ο ΦΠΑ μοιράζεται αναλογικά (υπόλοιπο στην τελευταία)."""
    line_rows, doc_rows = _document_cls_rows(local, inv)
    out = []
    for ln in inv.lines:
        net, vat = ln.net_value or 0.0, ln.vat_amount or 0.0
        rows = [r for r in line_rows.get(ln.line_number, []) if r["type"]] or [
            {"category": "", "type": "", "amount": net}
        ]
        vat_left = vat
        for i, r in enumerate(rows):
            amt = net if len(rows) == 1 or r["amount"] is None else r["amount"]
            v = vat_left if i == len(rows) - 1 else round(vat * amt / net, 2) if net else 0.0
            vat_left = round(vat_left - v, 2)
            out.append(
                {
                    "amount": round(amt, 2),
                    "classification_category": r["category"],
                    "classification_type": r["type"],
                    "vat_category": str(ln.vat_category or "8"),
                    "vat_amount": round(v, 2),
                    "vat_type": r.get("vat_type", "") if v > 0 else "",
                }
            )
    if not out:  # χωρίς αναλυτικές γραμμές: από τον συγκεντρωτικό χαρακτηρισμό
        out = [
            {
                "amount": r["amount"] or 0.0,
                "classification_category": r["category"],
                "classification_type": r["type"],
                "vat_category": "8",
                "vat_amount": 0.0,
            }
            for r in doc_rows
            if r["type"]
        ]
    return out


def _copy_draft(row: dict) -> dict:
    """Draft νέας εγγραφής ως αντίγραφο υπάρχουσας: ίδια στοιχεία και γραμμές,
    σημερινή ημερομηνία, κενός Α/Α."""
    draft = json.loads(row.get("draft_json") or "{}")
    if not draft.get("lines"):
        inv = _row_to_invoice(row)
        draft = {
            "invoice_type": inv.invoice_type,
            "series": inv.series or "",
            "issuer_vat": "" if (inv.invoice_type or "").startswith("17.") else inv.issuer_vat or "",
            "issuer_country": "GR",
            "lines": _copy_lines(inv, db.get_local_classification(row["id"])),
        }
    draft["issue_date"] = datetime.now(ATHENS).strftime("%Y-%m-%d")
    draft["aa"] = ""  # ο Α/Α πληκτρολογείται από τον χρήστη
    return draft


@app.route("/new_expense")
def new_expense():
    from mydata_client import SELF_EXPENSE_TYPES

    # Επεξεργασία υπάρχοντος τοπικού draft (Νέα εγγραφή στα «Χαρακτηρισμένα»).
    edit_mark = request.args.get("edit", "").strip()
    draft = None
    if edit_mark:
        cid = _active_company_id()
        row = db.get_document(cid, edit_mark) if cid else None
        if row and (row.get("local_action") == "create"):
            draft = json.loads(row.get("draft_json") or "{}")
        else:
            flash("Η εγγραφή προς επεξεργασία δεν βρέθηκε.", "error")
            edit_mark = ""

    # Αντιγραφή ολοκληρωμένης δικής μας εγγραφής (χωρίς υπόχρεο εκδότη) σε νέα.
    copy_mark = request.args.get("copy", "").strip()
    if copy_mark and not draft:
        cid = _active_company_id()
        row = db.get_document(cid, copy_mark) if cid else None
        if (
            row
            and row.get("status") == "confirmed"
            and row.get("invoice_type") in SELF_EXPENSE_TYPES
        ):
            draft = _copy_draft(row)
        else:
            flash(
                "Αντιγραφή γίνεται μόνο από ολοκληρωμένη εγγραφή τύπου 13.x–17.x.",
                "error",
            )
            copy_mark = ""

    # Πρότυπο (π.χ. ΕΦΚΑ, ενοίκιο): η αποθηκευμένη φόρμα με σημερινή ημ/νία και κενό Α/Α.
    template = None
    if request.args.get("template") and not draft:
        template = db.get_template(_active_company_id(), request.args["template"])
        if template:
            d = template["draft"]
            # Κάθε πρότυπο έχει δική του σειρά· ο Α/Α συνεχίζει από την τελευταία εγγραφή της.
            aa = db.next_aa(_active_company_id(), _self_counterparty(d), d.get("series", ""))
            draft = dict(d, issue_date=datetime.now(ATHENS).strftime("%Y-%m-%d"), aa=str(aa))
        else:
            flash("Το πρότυπο δεν βρέθηκε.", "error")

    return _render_new_expense(draft, edit_mark if draft else "", copy_mark, template=template)


def _self_counterparty(draft: dict) -> str:
    """Ο συναλλασσόμενος (counterparty_vat) που θα αποθηκευτεί για τη «Νέα εγγραφή» — ίδιοι
    κανόνες με το new_expense_submit: 17.x = εμείς, 13.3/13.4 = κανένας, αλλιώς ο εκδότης."""
    from mydata_client import SELF_TYPES_NO_ISSUER

    itype = draft.get("invoice_type") or ""
    if itype.startswith("17."):
        return get_own_vat()
    return "" if itype in SELF_TYPES_NO_ISSUER else (draft.get("issuer_vat") or "").strip()


@app.route("/new_expense/template", methods=["POST"])
def expense_template_save():
    """Η τρέχουσα φόρμα «Νέας εγγραφής» ως πρότυπο (χωρίς ημ/νία και Α/Α)."""
    cid = _active_company_id()
    name = " ".join(request.form.get("template_name", "").split())[:80]
    if not cid or not name:
        flash("Δώσε όνομα προτύπου (π.χ. «ΕΦΚΑ εργοδότη»).", "error")
        return _render_new_expense(_draft_from_form(), request.form.get("edit_mark", "").strip(), "", 422)
    draft = _draft_from_form()
    series = draft.get("series", "")
    other = db.template_with_series(cid, series, name) if series else None
    if not series or other:
        flash(f"Η σειρά «{series}» χρησιμοποιείται ήδη από το πρότυπο «{other}» — κάθε πρότυπο θέλει δική του σειρά."
              if other else "Δώσε σειρά στο πρότυπο — ο Α/Α του συνεχίζει μέσα στη σειρά.", "error")
        return _render_new_expense(draft, request.form.get("edit_mark", "").strip(), "", 422)
    draft.pop("issue_date", None)
    draft.pop("aa", None)
    draft["lines"] = [ln for ln in draft["lines"] if ln] or [None]
    tid = db.save_template(cid, name, draft)
    flash(f"✔ Αποθηκεύτηκε το πρότυπο «{name}».", "ok")
    return redirect(url_for("new_expense", template=tid))


@app.route("/new_expense/template/<int:tid>/delete", methods=["POST"])
def expense_template_delete(tid):
    db.delete_template(_active_company_id(), tid)
    flash("✔ Το πρότυπο διαγράφηκε.", "ok")
    return redirect(url_for("new_expense"))


def _render_new_expense(draft: dict | None, edit_mark: str = "", copy_mark: str = "", status: int = 200,
                        template: dict | None = None):
    """Η φόρμα «Νέας εγγραφής» — κενή, με draft (επεξεργασία/αντιγραφή) ή ξανά με τις
    τιμές του χρήστη μετά από σφάλμα, ώστε να μη χάνεται ό,τι συμπλήρωσε."""
    from mydata_client import (
        NEW_ENTRY_TYPES,
        PAYMENT_METHODS,
        SELF_TYPE_RULES,
        SELF_TYPES_INTRA_EU,
        SELF_TYPES_ISSUER_OPTIONAL,
        SELF_TYPES_NO_ISSUER,
        SELF_TYPES_WITH_VAT,
        VAT_CATEGORIES,
    )

    # Επιτρεπόμενοι συνδυασμοί ΕΞΟΔΩΝ ανά self-expense τύπο (για αλυσιδωτό φιλτράρισμα).
    combos_all = {
        t: {k: v for k, v in db.combos_for_type(t).items() if k.startswith("category2")}
        for t in NEW_ENTRY_TYPES
    }
    return render_template(
        "new_expense.html",
        self_types=NEW_ENTRY_TYPES,
        categories=EXPENSE_CATEGORIES,
        types=EXPENSE_TYPES,
        vat_categories=VAT_CATEGORIES,
        vat_types={k: v for k, v in VAT_TYPES.items() if k},  # χαρακτηρισμοί ΦΠΑ γραμμής
        payment_methods=PAYMENT_METHODS,
        today=datetime.now(ATHENS).strftime("%Y-%m-%d"),
        draft=draft,
        edit_mark=edit_mark,
        copy_mark=copy_mark,
        template=template,
        templates=db.list_templates(_active_company_id()),
        suppliers=db.list_suppliers(_active_company_id()),
        eu_countries=sorted(EU_COUNTRIES),
        country_rules={t: country_rule(t) for t in NEW_ENTRY_TYPES},
        # Κανόνες ΑΑΔΕ ανά τύπο για τη φόρμα (ίδια πηγή με το mydata_client).
        with_vat=sorted(SELF_TYPES_WITH_VAT),
        intra_eu=SELF_TYPES_INTRA_EU,
        no_issuer=sorted(SELF_TYPES_NO_ISSUER),
        issuer_optional=sorted(SELF_TYPES_ISSUER_OPTIONAL),
        no_payment=sorted(f for f, r in SELF_TYPE_RULES.items() if not r["payment"]),
        combos_all=combos_all,
    ), status


def _draft_from_form() -> dict:
    """Οι τιμές που έστειλε ο χρήστης (και οι κενές γραμμές), στη μορφή draft της φόρμας."""
    f = request.form

    def num(v: str):
        try:
            return float(v) if v.strip() else None
        except ValueError:
            return None

    cols = [f.getlist(k) for k in
            ("line_amount", "line_category", "line_type", "line_vat_category", "line_vat_amount",
             "line_vat_type")]
    at = lambda col, i, default="": col[i] if i < len(col) else default  # noqa: E731
    lines = [
        {
            "amount": num(at(cols[0], i)),
            "classification_category": at(cols[1], i),
            "classification_type": at(cols[2], i),
            "vat_category": at(cols[3], i, "8") or "8",
            "vat_amount": num(at(cols[4], i)) or 0.0,
            "vat_type": at(cols[5], i),
        }
        for i in range(len(cols[0]))
    ]
    return {
        "withheld_amount": num(f.get("withheld_amount", "")),
        "deductions_amount": num(f.get("deductions_amount", "")),
        "withheld_base": num(f.get("withheld_base", "")),
        **{k: f.get(k, "").strip() for k in
           ("invoice_type", "issue_date", "series", "aa", "issuer_vat", "issuer_country", "payment_method",
            "issuer_name", "issuer_street", "issuer_postal_code", "issuer_city")},
        "lines": lines or [None],
    }


@app.route("/draft/<mark>/delete", methods=["POST"])
def draft_delete(mark):
    """Διαγραφή τοπικού draft «Νέας εγγραφής» (δεν έχει διαβιβαστεί στο myDATA)."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None or row.get("local_action") != "create":
        flash("Η εγγραφή δεν βρέθηκε ή δεν είναι τοπική «Νέα εγγραφή».", "error")
        return redirect(url_for("invoices", view="classified"))
    db.delete_document(cid, mark)
    flash(f"✔ Η τοπική εγγραφή {mark} διαγράφηκε.", "ok")
    return redirect(url_for("invoices", view="classified"))


@app.route("/new_expense", methods=["POST"])
def new_expense_submit():
    from mydata_client import (
        NEW_ENTRY_TYPES,
        SELF_TYPES_ISSUER_OPTIONAL,
        SELF_TYPES_INTRA_EU,
        SELF_TYPES_NO_ISSUER,
        SELF_TYPES_WITH_VAT,
    )

    edit_mark = request.form.get("edit_mark", "").strip()

    def again():
        """Σφάλμα: ξανά η φόρμα με ό,τι συμπλήρωσε ο χρήστης (όχι κενή), για διόρθωση."""
        return _render_new_expense(_draft_from_form(), edit_mark, "", 422)

    invoice_type = request.form.get("invoice_type", "")
    series = request.form.get("series", "").strip()
    aa = request.form.get("aa", "").strip()
    issue_date = request.form.get("issue_date", "")

    if not series or not aa:
        flash("Συμπλήρωσε τη σειρά και τον Α/Α της εγγραφής.", "error")
        return again()

    if invoice_type not in NEW_ENTRY_TYPES:
        flash("Μη έγκυρος τύπος εγγραφής.", "error")
        return again()

    # γραμμές: παράλληλες λίστες από τη φόρμα
    amounts = request.form.getlist("line_amount")
    cats = request.form.getlist("line_category")
    typs = request.form.getlist("line_type")
    vat_cats = request.form.getlist("line_vat_category")
    vat_amts = request.form.getlist("line_vat_amount")
    vat_types = request.form.getlist("line_vat_type")
    lines = []
    for i, (amt, cat, typ) in enumerate(zip(amounts, cats, typs)):
        if not amt.strip():
            continue
        if not (cat and typ):
            flash("Κάθε γραμμή με ποσό πρέπει να έχει κατηγορία και τύπο.", "error")
            return again()
        try:
            vat_amount = float(vat_amts[i]) if i < len(vat_amts) and vat_amts[i].strip() else 0.0
        except ValueError:
            flash(f"Μη έγκυρο ποσό ΦΠΑ στη γραμμή {i + 1}.", "error")
            return again()
        # Χαρακτηρισμός ΦΠΑ της γραμμής (μόνο αν έχει ΦΠΑ· προεπιλογή VAT_361).
        vat_type = (vat_types[i] if i < len(vat_types) else "") or "VAT_361"
        if not vat_type.startswith("VAT_") or vat_type not in VAT_TYPES:
            flash(f"Μη έγκυρος χαρακτηρισμός ΦΠΑ στη γραμμή {i + 1}.", "error")
            return again()
        try:
            lines.append(
                {
                    "amount": float(amt),
                    "classification_category": cat,
                    "classification_type": typ,
                    "vat_category": (vat_cats[i] if i < len(vat_cats) else "8") or "8",
                    "vat_amount": vat_amount,
                    "vat_type": vat_type if vat_amount > 0 else "",
                }
            )
        except ValueError:
            flash(f"Μη έγκυρο ποσό στη γραμμή {i + 1}.", "error")
            return again()

    if not lines:
        flash("Συμπλήρωσε τουλάχιστον μία γραμμή.", "error")
        return again()

    # Μισθοδοσία: παρακράτηση φόρου + κρατήσεις ΕΦΚΑ εργαζομένου, σε επίπεδο παραστατικού.
    withheld = deductions = 0.0
    withheld_base = None
    if invoice_type == "17.1":
        try:
            withheld = round(float(request.form.get("withheld_amount") or 0), 2)
            deductions = round(float(request.form.get("deductions_amount") or 0), 2)
            withheld_base = round(float(request.form.get("withheld_base") or 0), 2) or None
        except ValueError:
            flash("Μη έγκυρο ποσό παρακράτησης φόρου ή κρατήσεων ΕΦΚΑ.", "error")
            return again()
        if withheld < 0 or deductions < 0 or withheld + deductions > sum(ln["amount"] for ln in lines):
            flash("Παρακράτηση φόρου και κρατήσεις ΕΦΚΑ: μη αρνητικά και όχι πάνω από το σύνολο αποδοχών.", "error")
            return again()
        if withheld and not (withheld_base and withheld <= withheld_base):
            flash("Συμπλήρωσε το ποσό πάνω στο οποίο γίνεται η παρακράτηση φόρου "
                  "(όχι μικρότερο από τον φόρο).", "error")
            return again()
        if not withheld:
            withheld_base = None

    # ΦΠΑ μόνο στις λιανικές 13.1/13.2/13.31 (ΑΑΔΕ: αλλιώς σφάλματα 215/218).
    if invoice_type not in SELF_TYPES_WITH_VAT:
        if any(ln["vat_amount"] for ln in lines):
            flash(
                f"Ο τύπος {invoice_type} δεν έχει ΦΠΑ — ΦΠΑ επιτρέπεται μόνο στα "
                "13.1, 13.2, 13.31 και στη διαβίβαση λόγω παράλειψης εκδότη.",
                "error",
            )
            return again()
        for ln in lines:
            ln["vat_category"] = "8"
            ln["vat_type"] = ""
    elif invoice_type in SELF_TYPES_INTRA_EU and any(ln["vat_category"] == "8" for ln in lines):
        flash(f"Ο τύπος {invoice_type} θέλει κατηγορία ΦΠΑ (π.χ. 24%) με τον ΦΠΑ της αυτοπαράδοσης "
              "— όχι «Χωρίς ΦΠΑ».", "error")
        return again()

    # Το δικό μας ΑΦΜ χρειάζεται πάντα: ως εκδότης στα 17.x, ως αντισυμβαλλόμενος
    # (λήπτης) στα 14.x/15.1/16.1 - error 204 "Counterpart is mandatory".
    payment_method = request.form.get("payment_method", "5")
    own_vat = get_own_vat()
    if not own_vat:
        flash(
            "Λείπει το ΑΦΜ της εταιρείας - συμπλήρωσέ το στη Διαχείριση εταιρειών "
            "(ή ως AADE_VAT_NUMBER στο .env). Απαιτείται ως εκδότης στα 17.x "
            "ή ως λήπτης στους άλλους τύπους.",
            "error",
        )
        return again()

    if invoice_type.startswith("17."):
        issuer_vat = own_vat
        issuer_country = "GR"
    elif invoice_type in SELF_TYPES_NO_ISSUER or (  # κοινόχρηστα/συνδρομές: χωρίς εκδότη
        invoice_type in SELF_TYPES_ISSUER_OPTIONAL  # λιανικές: προαιρετικός — εδώ κενός
        and not request.form.get("issuer_vat", "").strip()
    ):
        issuer_vat = ""
        issuer_country = "GR"
    else:
        issuer_vat = request.form.get("issuer_vat", "").strip()
        issuer_country = (
            request.form.get("issuer_country", "GR").strip() or "GR"
        ).upper()
        if not issuer_vat:
            flash(
                "Για τον τύπο αυτό απαιτείται το ΑΦΜ του εκδότη "
                "(π.χ. 997072577 για τον ΕΦΚΑ).",
                "error",
            )
            return again()
        if not country_allowed(invoice_type, issuer_country):
            rule = country_rule(invoice_type)
            flash(
                f"Μη αποδεκτή χώρα εκδότη «{issuer_country}» για τον τύπο {invoice_type}: "
                + ("επιτρέπεται μόνο Ελλάδα (GR)." if rule == "gr"
                   else "απαιτείται χώρα της ΕΕ (εκτός GR)." if rule == "eu"
                   else "απαιτείται χώρα εκτός ΕΕ." if rule == "third"
                   else "δώσε έγκυρο κωδικό 2 γραμμάτων."),
                "error",
            )
            return again()

    # Εκδότης εκτός Ελλάδας: η ΑΑΔΕ θέλει επωνυμία και διεύθυνση (σφάλμα 204).
    foreign = {k: request.form.get("issuer_" + k, "").strip() for k in ("name", "street", "postal_code", "city")}
    if issuer_vat and issuer_country != "GR":
        if not (foreign["name"] and foreign["postal_code"] and foreign["city"]):
            flash("Για εκδότη εκτός Ελλάδας συμπλήρωσε επωνυμία, Τ.Κ. και πόλη.", "error")
            return again()
    else:
        foreign = dict.fromkeys(foreign, "")

    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("companies"))

    # Αποθήκευση ΤΟΠΙΚΑ στα «Χαρακτηρισμένα» (χωρίς διαβίβαση). Η μαζική «Αποστολή
    # στο myDATA» θα εκτελέσει το SendInvoices (δημιουργία+διαβίβαση+χαρακτηρισμός).
    draft = {
        "invoice_type": invoice_type,
        "series": series,
        "aa": aa,
        "issue_date": issue_date,
        "lines": lines,
        "issuer_vat": issuer_vat,
        "issuer_country": issuer_country,
        **{"issuer_" + k: v for k, v in foreign.items()},
        "payment_method": payment_method,
        "withheld_amount": withheld,
        "deductions_amount": deductions,
        "withheld_base": withheld_base,
    }
    if foreign["name"]:  # ο συναλλασσόμενος κρατά χώρα και διεύθυνση για τις επόμενες εγγραφές
        db.save_supplier_address(issuer_vat, foreign["name"], issuer_country, foreign["street"],
                                 foreign["postal_code"], foreign["city"])
    # Μηνιαίο πρότυπο: μία εγγραφή ανά μήνα. Στην επεξεργασία κρατιέται το πρότυπο της παλιάς.
    edit_mark = request.form.get("edit_mark", "").strip()
    existing = db.get_document(cid, edit_mark) if edit_mark else None
    if existing and existing.get("local_action") != "create":
        existing = None
    tid = (existing or {}).get("template_id") or request.form.get("template_id", "").strip()
    origin = (int(tid), issue_date[:7]) if str(tid).isdigit() else None
    if origin:
        taken = db.template_periods(cid, origin[1]).get(origin[0])
        if taken and taken["mark"] != edit_mark:
            flash(f"Υπάρχει ήδη εγγραφή του προτύπου για τον μήνα {origin[1]} ({taken['mark']}).", "error")
            return again()
    # Επεξεργασία: αντικατάσταση του υπάρχοντος τοπικού draft.
    if existing:
        db.delete_document(cid, edit_mark)
    _store_self_expense(cid, draft, origin)
    verb = "ενημερώθηκε" if edit_mark else "αποθηκεύτηκε"
    flash(
        f"✔ Η εγγραφή {invoice_type} ({NEW_ENTRY_TYPES[invoice_type]}) {verb} "
        "τοπικά στα «Χαρακτηρισμένα». Μάζεψε κι άλλες και στείλ' τες μαζικά με "
        "«Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(_safe_back(request.form.get("back")) or url_for("invoices", view="classified"))


def _store_self_expense(cid: int, draft: dict, origin: tuple | None = None) -> int | None:
    """Έγκυρο draft «Νέας εγγραφής» → τοπική εγγραφή στα «Χαρακτηρισμένα».
    None αν υπάρχει ήδη εγγραφή του ίδιου προτύπου για τον μήνα (origin)."""
    lines = draft["lines"]
    withheld, deductions = draft["withheld_amount"] or 0.0, draft["deductions_amount"] or 0.0
    withheld_base = draft["withheld_base"]
    total_net = round(sum(line["amount"] for line in lines), 2)
    total_vat = round(sum(line["vat_amount"] for line in lines), 2)
    def line_cls(line):  # Ε3 + (αν υπάρχει ΦΠΑ) ο χαρακτηρισμός ΦΠΑ της γραμμής
        out = [{"type": line["classification_type"], "category": line["classification_category"],
                "amount": line["amount"]}]
        if line["vat_type"]:
            out.append({"type": line["vat_type"], "category": "", "amount": line["amount"]})
        return out

    disp_lines = [
        {
            "line_number": i + 1,
            "net_value": line["amount"],
            "vat_amount": line["vat_amount"],
            "vat_category": line["vat_category"],
            "has_expenses_classification": True,
            "classifications": line_cls(line),
        }
        for i, line in enumerate(lines)
    ]
    cls_info = [c for line in lines for c in line_cls(line)]
    doc = {
        "issue_date": draft["issue_date"],
        "issuer_vat": draft["issuer_vat"],
        "issuer_name": draft.get("issuer_name") or None,
        "invoice_type": draft["invoice_type"],
        "series": draft["series"],
        "aa": draft["aa"],
        "total_net": total_net,
        "total_vat": total_vat,
        "total_gross": round(total_net + total_vat - withheld - deductions, 2),
        "lines": disp_lines,
        "extra_totals": {
            "total_withheld": withheld,
            "total_deductions": deductions,
            "taxes_json": json.dumps(
                [t for t in ({"type": "1", "category": "11", "base": withheld_base, "amount": withheld},
                             {"type": "5", "category": "", "base": None, "amount": deductions})
                 if t["amount"]]),
        },
    }
    return db.create_local_expense(cid, doc, draft, cls_info, origin)


def _recurring_pending(cid: int | None, period: str) -> list[dict]:
    """Μηνιαία πρότυπα χωρίς εγγραφή για τον μήνα."""
    done = db.template_periods(cid, period)
    return [t for t in db.list_templates(cid) if t["monthly_day"] and t["id"] not in done]


def _draft_for_month(cid: int, template: dict, period: str) -> dict | None:
    """Το πρότυπο ως έγκυρο draft για τον μήνα: ημ/νία = ημέρα προτύπου (ή τελευταία του μήνα),
    Α/Α συνέχεια της σειράς. None αν το πρότυπο δεν έχει γραμμές με ποσό."""
    d = template["draft"]
    y, m = map(int, period.split("-"))
    day = min(template["monthly_day"], calendar.monthrange(y, m)[1])
    lines = []
    for ln in d.get("lines") or []:
        if not ln or not ln.get("amount"):
            continue
        vat_amount = ln.get("vat_amount") or 0.0
        lines.append(dict(ln, vat_amount=vat_amount, vat_category=ln.get("vat_category") or "8",
                          vat_type=(ln.get("vat_type") or "VAT_361") if vat_amount > 0 else ""))
    if not lines:
        return None
    counterparty = _self_counterparty(d)
    withheld = d.get("withheld_amount") or 0.0
    return dict(
        d,
        lines=lines,
        issue_date=f"{period}-{day:02d}",
        aa=str(db.next_aa(cid, counterparty, d.get("series", ""))),
        issuer_vat=counterparty,
        issuer_country=d.get("issuer_country") or "GR",
        payment_method=d.get("payment_method") or "5",
        withheld_amount=withheld,
        deductions_amount=d.get("deductions_amount") or 0.0,
        withheld_base=d.get("withheld_base") if withheld else None,
    )


def _period_arg(value: str | None) -> str:
    return value if value and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) else datetime.now(ATHENS).strftime("%Y-%m")


@app.route("/recurring")
def recurring():
    """Μηνιαίες εγγραφές από πρότυπα: ημέρα ανά πρότυπο + δημιουργία για έναν μήνα."""
    cid = _active_company_id()
    period = _period_arg(request.args.get("period"))
    return render_template("recurring.html", period=period, templates=db.list_templates(cid),
                           done=db.template_periods(cid, period))


@app.route("/recurring/days", methods=["POST"])
def recurring_days():
    cid = _active_company_id()
    for t in db.list_templates(cid):
        v = request.form.get(f"day_{t['id']}", "").strip()
        db.set_template_monthly_day(cid, t["id"], min(max(int(v), 1), 31) if v.isdigit() else None)
    flash("✔ Αποθηκεύτηκαν οι ημέρες των μηνιαίων εγγραφών.", "ok")
    return redirect(url_for("recurring", period=request.form.get("period")))


@app.route("/recurring/create", methods=["POST"])
def recurring_create():
    cid = _active_company_id()
    period = _period_arg(request.form.get("period"))
    if not get_own_vat():  # ίδιος έλεγχος με τη «Νέα εγγραφή» (εκδότης 17.x / λήπτης)
        flash("Λείπει το ΑΦΜ της εταιρείας - συμπλήρωσέ το στη Διαχείριση εταιρειών.", "error")
        return redirect(url_for("recurring", period=period))
    ids = set(request.form.getlist("template_ids"))
    made, skipped, empty = [], [], []
    for t in _recurring_pending(cid, period) if cid else []:
        if str(t["id"]) not in ids:
            continue
        template = db.get_template(cid, t["id"])
        draft = _draft_for_month(cid, template, period)
        if draft is None:
            empty.append(t["name"])
        elif _store_self_expense(cid, draft, (t["id"], period)):
            made.append(t["name"])
        else:  # ταυτόχρονη δημιουργία — το unique index το απέτρεψε
            skipped.append(t["name"])
    if made:
        flash(f"✔ Δημιουργήθηκαν για {period}: {', '.join(made)}. Έλεγξε τα ποσά (Επεξεργασία) "
              "και στείλ' τα με «Αποστολή στο myDATA».", "ok")
    if skipped:
        flash(f"Υπήρχαν ήδη για {period}: {', '.join(skipped)}.", "error")
    if empty:
        flash(f"Χωρίς γραμμές με ποσό (διόρθωσε το πρότυπο): {', '.join(empty)}.", "error")
    if not (made or skipped or empty):
        flash("Δεν επιλέχθηκε κανένα εκκρεμές πρότυπο.", "error")
        return redirect(url_for("recurring", period=period))
    return redirect(url_for("invoices", view="classified"))


@app.route("/cancel/<mark>", methods=["POST"])
def cancel(mark):
    """Ακύρωση αυτοτιμολογούμενου: στήνεται ΤΟΠΙΚΑ στα «Χαρακτηρισμένα» και
    εκτελείται (CancelInvoice) με τη μαζική «Αποστολή στο myDATA»."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None:
        flash("Το παραστατικό δεν βρέθηκε. Κάνε νέα αναζήτηση.", "error")
        return redirect(url_for("invoices"))
    if not _row_to_invoice(row).is_self_issued:
        flash(
            "Μόνο παραστατικά που έχεις διαβιβάσει εσύ (π.χ. 17.x) μπορούν να ακυρωθούν. "
            "Για παραστατικά τρίτων εκδοτών χρησιμοποίησε την Απόρριψη.",
            "error",
        )
        return redirect(url_for("invoices"))
    if not cancel_allowed():
        flash(CANCEL_DISABLED_MSG, "error")
        return redirect(url_for("document", mark=mark))
    db.stage_action(cid, mark, "cancel")
    flash(
        f"✔ Το παραστατικό {mark} μπήκε στα «Χαρακτηρισμένα» ως ΑΚΥΡΩΣΗ. "
        "Θα εκτελεστεί με το κουμπί «Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(url_for("invoices", view="classified"))


@app.route("/income/cancel/<mark>", methods=["POST"])
def income_cancel(mark):
    """Ακύρωση παραστατικού ΕΣΟΔΟΥ (εκδότης είμαστε εμείς): CancelInvoice ΑΜΕΣΩΣ — τα
    έσοδα δεν έχουν μαζική «Αποστολή». Με επιτυχία αφαιρείται και από το τοπικό βιβλίο."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None or row.get("kind") != "income":
        flash("Το παραστατικό εσόδου δεν βρέθηκε. Κάνε νέα ανάκτηση.", "error")
        return redirect(url_for("income"))
    if not cancel_allowed():
        flash(CANCEL_DISABLED_MSG, "error")
        return redirect(url_for("document", mark=mark))
    try:
        result = get_client().cancel_invoice(mark)
    except MyDataError as e:
        flash(f"Η ακύρωση απέτυχε: {e}", "error")
        return redirect(url_for("document", mark=mark))
    if (result.get("status") or "").lower() != "success":
        flash(f"Η ακύρωση απέτυχε: {'; '.join(result['errors']) or result.get('status')}", "error")
        return redirect(url_for("document", mark=mark))
    db.delete_document(cid, mark)
    flash(f"✔ Το παραστατικό εσόδου {mark} ακυρώθηκε (MARK ακύρωσης {result.get('cancellation_mark') or '—'}).", "ok")
    return redirect(url_for("income", view=row["status"]))


@app.route("/reject/<mark>", methods=["POST"])
def reject(mark):
    """Απόρριψη παραστατικού τρίτου (transactionMode=1): στήνεται ΤΟΠΙΚΑ στα
    «Χαρακτηρισμένα» και διαβιβάζεται με τη μαζική «Αποστολή στο myDATA»."""
    cid = _active_company_id()
    if not cancel_allowed():
        flash(CANCEL_DISABLED_MSG, "error")
        return redirect(url_for("document", mark=mark))
    if not db.stage_action(cid, mark, "reject"):
        flash("Το παραστατικό δεν βρέθηκε. Κάνε νέα αναζήτηση.", "error")
        return redirect(url_for("invoices"))
    flash(
        f"✔ Το παραστατικό {mark} μπήκε στα «Χαρακτηρισμένα» ως ΑΠΟΡΡΙΨΗ. "
        "Στείλ' το με το κουμπί «Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(url_for("invoices", view="classified"))


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.route("/parameters")
def parameters():
    tab = request.args.get("tab", "reports")
    # Παλιοί σύνδεσμοι: συναλλασσόμενοι και εταιρείες έχουν δική τους σελίδα.
    if tab == "suppliers":
        return redirect(url_for("suppliers"))
    if tab == "companies":
        return redirect(url_for("companies"))
    if tab not in {"accountant", "combinations", "reports", "backup", "security", "tax"}:
        tab = "reports"
    return render_template(
        "parameters.html",
        active_tab=tab,
        auth_user=(db.get_auth() or {}).get("auth_user", ""),
        acc=get_accountant(),
        combinations_count=db.combos_count(),
        combinations_invoice_types=sorted(
            db.combos_invoice_types(), key=lambda t: [int(x) if x.isdigit() else 0 for x in t.split(".")]
        ),
        type_names=INVOICE_TYPE_NAMES,
        acc_companies=[c["company_name"] for c in load_companies() if c.get("use_accountant")],
        combinations_version=db.get_setting("combos_version"),
        stock_reports=[(k, title, desc, stock_in(k)) for k, title, _, desc in STOCK_REPORTS],
        auto_classify=db.get_setting("auto_classify") == "1",
        # Όλα τα tabs αποδίδονται μαζί· η εναλλαγή γίνεται στον browser χωρίς reload.
        safety_backups=db.list_safety_backups(),
        keep_safety=db.keep_safety(),
        backup_info=_backup_info(),
        tax_scales=_tax_scales(),
        tax_custom=bool(db.get_setting("tax_scales")),
    )


def _backup_info() -> dict:
    """Σύνοψη τρέχουσας βάσης για την καρτέλα «Αντίγραφα ασφαλείας» (μέγεθος μαζί με το WAL)."""
    stats = db.company_stats().values()
    wal = db.DB_PATH + "-wal"
    return {
        "path": db.DB_PATH,
        "size": os.path.getsize(db.DB_PATH) + (os.path.getsize(wal) if os.path.exists(wal) else 0),
        "companies": len(load_companies()),
        "documents": sum((s["income"] or 0) + (s["expense"] or 0) for s in stats),
    }


@app.route("/parameters/reports", methods=["POST"])
def parameters_reports():
    for key, *_ in STOCK_REPORTS:
        db.set_setting("stock_in_" + key, "1" if request.form.get(key) else "0")
    flash("✔ Αποθηκεύτηκαν οι ρυθμίσεις αναφορών.", "ok")
    return redirect(url_for("parameters", tab="reports"))


def _tax_scales_from_form(form) -> list[dict]:
    """Καθεστώτα από τη φόρμα «Φορολογία». Ανά καθεστώς i, γραμμή κλιμακίου k: s{i}_lim_{k} (άνω όριο — κενό στην
    τελευταία = «και πάνω»), s{i}_rate_{k} (γενική %, κενό = η γραμμή αγνοείται), s{i}_y{j}_{k} (νέοι %, κενό = γενική)·
    s{i}_yage_{j} = ανώτατη ηλικία ομάδας νέων j (κενό = η ομάδα αγνοείται). ValueError(μήνυμα) σε άκυρη τιμή."""
    def num(name, label, pct=False):
        try:
            v = _amount_arg(name, form)
        except ValueError:
            raise ValueError(f"Μη έγκυρη τιμή: {label}.") from None
        if pct and v is not None and v > 100:
            raise ValueError(f"Ποσοστό πάνω από 100%: {label}.")
        return v

    scales = []
    for i in range(int(form.get("count", 0) or 0)):
        year = num(f"s{i}_from_year", "έτος ισχύος")
        if year is None:
            continue
        where = f"κλίμακα από {int(year)}"
        rows = [k for k in range(int(form.get(f"s{i}_rows", 0) or 0)) if form.get(f"s{i}_rate_{k}", "").strip()]
        if not rows:
            raise ValueError(f"Χρειάζεται τουλάχιστον ένα κλιμάκιο ({where}).")
        rates = [num(f"s{i}_rate_{k}", f"συντελεστής ({where})", pct=True) for k in rows]
        limits = [num(f"s{i}_lim_{k}", f"όριο κλιμακίου ({where})") for k in rows[:-1]]
        if None in limits or any(b <= a for a, b in zip([0.0, *limits], limits)):
            raise ValueError(f"Τα όρια των κλιμακίων πρέπει να είναι συμπληρωμένα και αύξοντα ({where}).")
        young = []
        for j in range(int(form.get(f"s{i}_ycols", 0) or 0)):
            age = num(f"s{i}_yage_{j}", f"ηλικία νέων ({where})")
            if age is not None:
                young.append({"max_age": int(age), "rates": [
                    r if (v := num(f"s{i}_y{j}_{k}", f"συντελεστής νέων ({where})", pct=True)) is None else v
                    for k, r in zip(rows, rates)]})
        scale = {"from_year": int(year), "limits": limits, "rates": rates,
                 "young": sorted(young, key=lambda y: y["max_age"])}
        for key, label in (("legal_rate", "συντελεστής νομικών προσώπων"), ("prepay_sole", "προκαταβολή ατομικής"),
                           ("prepay_legal", "προκαταβολή νομικών προσώπων")):
            v = num(f"s{i}_{key}", f"{label} ({where})", pct=True)
            if v is None:
                raise ValueError(f"Συμπλήρωσε: {label} ({where}).")
            scale[key] = v
        scales.append(scale)
    years = [s["from_year"] for s in scales]
    if not scales or len(set(years)) != len(years):
        raise ValueError("Χρειάζεται τουλάχιστον μία κλίμακα και κάθε έτος ισχύος μία φορά.")
    return sorted(scales, key=lambda s: s["from_year"])


@app.route("/parameters/tax", methods=["POST"])
def parameters_tax():
    scales = _tax_scales()
    if request.form.get("action") == "reset":
        db.set_setting("tax_scales", "")
        flash("✔ Επαναφέρθηκαν οι προεπιλεγμένες κλίμακες φόρου.", "ok")
    elif request.form.get("action") == "add":
        new = dict(json.loads(json.dumps(scales[-1])), from_year=max(scales[-1]["from_year"] + 1, datetime.now(ATHENS).year))
        db.set_setting("tax_scales", json.dumps(scales + [new], ensure_ascii=False))
        flash(f"✔ Νέα κλίμακα από {new['from_year']} (αντίγραφο της τελευταίας) — διόρθωσε τις τιμές.", "ok")
    elif request.form.get("delete", "").isdigit() and len(scales) > 1:
        gone = scales.pop(min(int(request.form["delete"]), len(scales) - 1))
        db.set_setting("tax_scales", json.dumps(scales, ensure_ascii=False))
        flash(f"✔ Διαγράφηκε η κλίμακα από {gone['from_year']}.", "ok")
    else:
        try:
            db.set_setting("tax_scales", json.dumps(_tax_scales_from_form(request.form), ensure_ascii=False))
            flash("✔ Αποθηκεύτηκαν οι κλίμακες φόρου.", "ok")
        except ValueError as e:
            flash(str(e), "error")
    return redirect(url_for("parameters", tab="tax"))


@app.route("/parameters/automation", methods=["POST"])
def parameters_automation():
    db.set_setting("auto_classify", "1" if request.form.get("auto_classify") else "0")
    flash("✔ Αποθηκεύτηκε η ρύθμιση αυτόματου χαρακτηρισμού.", "ok")
    return redirect(url_for("parameters", tab="reports"))


@app.route("/companies")
def companies():
    return render_template(
        "companies.html",
        companies=load_companies(),
        active=get_active_index(),
        stats=db.company_stats(),
        acc=get_accountant(),
    )


def _company_tax_fields() -> dict:
    sole = request.form.get("entity_type") == "sole"  # νομικό πρόσωπο: το (κρυφό) έτος γέννησης δεν κρατιέται
    return {"entity_type": "sole" if sole else "legal", "birth_year": request.form.get("birth_year", "").strip() if sole else ""}


def _sole_without_birth_year() -> bool:
    """Ατομική χωρίς έγκυρο έτος γέννησης → μήνυμα λάθους (χρειάζεται για τους συντελεστές νέων)."""
    f = _company_tax_fields()
    if f["entity_type"] == "sole" and db.birth_year(f["birth_year"]) is None:
        flash("Για ατομική επιχείρηση χρειάζεται έγκυρο έτος γέννησης του επιχειρηματία.", "error")
        return True
    return False


@app.route("/companies/add", methods=["POST"])
def companies_add():
    name = request.form.get("company_name", "").strip()
    user_id = request.form.get("aade_user_id", "").strip()
    sub_key = request.form.get("aade_subscription_key", "").strip()
    vat = request.form.get("aade_vat_number", "").strip()
    env = request.form.get("mydata_env", "prod")
    use_acc = bool(request.form.get("use_accountant"))
    if _sole_without_birth_year():
        return redirect(url_for("companies"))
    if not name or (not use_acc and not (user_id and sub_key)):
        flash(
            "Επωνυμία υποχρεωτική. User ID/Subscription Key απαιτούνται εκτός αν "
            "χρησιμοποιούνται credentials λογιστή.",
            "error",
        )
        return redirect(url_for("companies"))
    new_id = db.add_company(
        {
            "company_name": name,
            "AADE_USER_ID": user_id,
            "AADE_SUBSCRIPTION_KEY": sub_key,
            "AADE_VAT_NUMBER": vat,
            "MYDATA_ENV": env,
            "use_accountant": use_acc,
            "allow_cancel": bool(request.form.get("allow_cancel")),
            "allow_send": bool(request.form.get("allow_send")),
            "allow_classify": bool(request.form.get("allow_classify")),
            "allow_new": bool(request.form.get("allow_new")),
            "vat_period": request.form.get("vat_period", "m"),
            **_company_tax_fields(),
        }
    )
    if db.get_active_company_id() is None and new_id is not None:
        db.set_active_company_id(new_id)
    flash(f"✔ Προστέθηκε η εταιρεία «{name}».", "ok")
    return redirect(url_for("companies"))


@app.route("/companies/update/<int:idx>", methods=["POST"])
def companies_update(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("companies"))
    name = request.form.get("company_name", "").strip()
    user_id = request.form.get("aade_user_id", "").strip()
    # Το κλειδί δεν στέλνεται πίσω στη φόρμα: κενό = κράτα το αποθηκευμένο.
    sub_key = request.form.get("aade_subscription_key", "").strip() or comps[idx]["AADE_SUBSCRIPTION_KEY"]
    if _sole_without_birth_year():
        return redirect(url_for("companies"))
    if not name or (
        not bool(request.form.get("use_accountant")) and not (user_id and sub_key)
    ):
        flash(
            "Επωνυμία υποχρεωτική. User ID/Subscription Key απαιτούνται εκτός αν "
            "χρησιμοποιούνται credentials λογιστή.",
            "error",
        )
        return redirect(url_for("companies"))
    db.update_company(
        comps[idx]["id"],
        {
            "company_name": name,
            "AADE_USER_ID": user_id,
            "AADE_SUBSCRIPTION_KEY": sub_key,
            "AADE_VAT_NUMBER": request.form.get("aade_vat_number", "").strip(),
            "MYDATA_ENV": request.form.get("mydata_env", "prod"),
            "use_accountant": bool(request.form.get("use_accountant")),
            "allow_cancel": bool(request.form.get("allow_cancel")),
            "allow_send": bool(request.form.get("allow_send")),
            "allow_classify": bool(request.form.get("allow_classify")),
            "allow_new": bool(request.form.get("allow_new")),
            "vat_period": request.form.get("vat_period", "m"),
            **_company_tax_fields(),
        },
    )
    flash(f"✔ Ενημερώθηκε η εταιρεία «{name}».", "ok")
    back = request.form.get("back", "")  # modal ενεργής εταιρείας (base.html) → πίσω στη σελίδα του
    return redirect(back if back.startswith("/") and not back.startswith(("//", "/\\")) else url_for("companies"))


@app.route("/companies/delete/<int:idx>", methods=["POST"])
def companies_delete(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("companies"))
    removed = comps[idx]
    was_active = db.get_active_company_id() == removed["id"]
    db.delete_company(
        removed["id"]
    )  # cascade: παραστατικά της εταιρείας (οι προμηθευτές είναι κοινοί)
    # Αν διαγράφηκε η ενεργή, όρισε την πρώτη που απομένει (αν υπάρχει).
    if was_active:
        remaining = load_companies()
        if remaining:
            db.set_active_company_id(remaining[0]["id"])
    flash(f"✔ Διαγράφηκε η εταιρεία «{removed.get('company_name', '')}».", "ok")
    return redirect(url_for("companies"))


@app.route("/companies/select/<int:idx>", methods=["POST"])
def companies_select(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("companies"))
    db.set_active_company_id(comps[idx]["id"])
    flash(f"✔ Ενεργή εταιρεία: «{comps[idx].get('company_name', '')}».", "ok")
    return redirect(url_for("dashboard"))


@app.route("/supplier_lookup/<vat>")
def supplier_lookup(vat):
    """Τοπική αναζήτηση ΑΦΜ στον (κοινό) πίνακα προμηθευτών."""
    name = db.get_supplier_name((vat or "").strip())
    return {"found": bool(name), "name": name or ""}


@app.route("/check_vat/<country>/<afm>")
def check_vat(country, afm):
    """AJAX έλεγχος ΑΦΜ μέσω VIES: εγκυρότητα + επωνυμία (αν κοινοποιείται)."""
    import vies

    result = vies.check_vat(country, afm)
    if result is None:
        return {"ok": False, "message": "Η υπηρεσία VIES δεν απάντησε - δοκίμασε ξανά."}
    if not result["valid"]:
        return {
            "ok": True,
            "valid": False,
            "message": "✖ Το ΑΦΜ δεν βρέθηκε ενεργό στο VIES.",
        }
    msg = "✔ Έγκυρο ΑΦΜ"
    if result["name"]:
        msg += f" · {result['name']}"
        # αποθήκευση στην cache επωνυμιών της ενεργής εταιρείας
        names = load_names()
        _, num = vies.normalize(country, afm)
        if names.get(num) != result["name"]:
            names[num] = result["name"]
            save_names(names)
    return {"ok": True, "valid": True, "name": result["name"], "message": msg}


@app.route("/accountant", methods=["GET", "POST"])
def accountant():
    if request.method == "POST":
        save_accountant(
            {
                "user_id": request.form.get("user_id", "").strip(),
                # κενό = κράτα το αποθηκευμένο (δεν στέλνεται πίσω στη φόρμα)
                "subscription_key": request.form.get("subscription_key", "").strip()
                or get_accountant().get("subscription_key", ""),
                "env": request.form.get("env", "prod"),
            }
        )
        flash("✔ Αποθηκεύτηκαν τα credentials λογιστή.", "ok")
        return redirect(url_for("parameters", tab="accountant"))
    return redirect(url_for("parameters", tab="accountant"))


# ------------------------------------------------------------------ #
# Συναλλασσόμενοι — πελάτες & προμηθευτές (κοινοί για όλες τις εταιρείες)
# ------------------------------------------------------------------ #
@app.route("/suppliers")
def suppliers():
    """Κατάλογος συναλλασσόμενων (πελάτες & προμηθευτές), με σελιδοποίηση."""
    all_suppliers = db.list_suppliers(_active_company_id() or 0)  # μόνο της ενεργής εταιρείας
    # Φίλτρο: substring σε ΑΦΜ ή επωνυμία (χωρίς διάκριση πεζών/κεφαλαίων και τόνων).
    def fold(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", s.casefold()) if not unicodedata.combining(c))

    q = request.args.get("q", "").strip()
    items = [s for s in all_suppliers if fold(q) in fold(f"{s['vat']} {s['name'] or ''}")]
    page_items, page, total_pages = paginate(items, request.args.get("page"))
    cid = _active_company_id()
    patterns, rules = db.load_rule_patterns(cid), load_rules()
    # Χωρίς πρόταση: η πιο πρόσφατη ολοκληρωμένη εγγραφή εξόδου με χαρακτηρισμό (για αντιγραφή).
    latest = _latest_patterns(cid, {s["vat"] for s in all_suppliers} - set(patterns) - set(rules))
    return render_template(
        "counterparties.html",
        latest={vat: d for vat, (d, _) in latest.items()},
        suppliers=page_items,
        suppliers_total=len(all_suppliers),
        with_rule=sum(1 for s in all_suppliers if s["vat"] in patterns or s["vat"] in rules),
        filtered_total=len(items),
        q=q,
        page=page,
        total_pages=total_pages,
        patterns=patterns,
        rules=rules,
        vat_label=_vat_group_label,
    )


@app.route("/suppliers/rules-from-latest", methods=["POST"])
def suppliers_rules_from_latest():
    """Μαζικά: όσοι δεν έχουν πρόταση παίρνουν τον χαρακτηρισμό της τελευταίας ολοκληρωμένης εγγραφής τους."""
    cid = _active_company_id()
    have = set(db.load_rule_patterns(cid)) | set(load_rules())
    latest = _latest_patterns(cid, {s["vat"] for s in db.list_suppliers(cid or 0)} - have)
    for vat, (row, pats) in latest.items():
        _save_as_rule(cid, vat, pats, _doc_post_mode(row))
    flash(f"✔ Ορίστηκαν προτάσεις για {len(latest)} συναλλασσόμενους από την τελευταία ολοκληρωμένη εγγραφή τους.", "ok")
    return redirect(url_for("suppliers", q=request.form.get("q") or None))


@app.route("/suppliers/<vat>/rules", methods=["GET", "POST"])
def supplier_rules(vat):
    """Επεξεργασία προτάσεων χαρακτηρισμού του συναλλασσόμενου (ενεργή εταιρεία): μία
    γραμμή ανά κατηγορία ΦΠΑ, '*' = βασική (όταν δεν ταιριάζει κατηγορία ΦΠΑ)."""
    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("suppliers"))
    back = request.values.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("suppliers")
    if request.method == "POST":
        patterns: dict = {}
        errors = []
        for vc, ccat, ctype, vt in zip(
            request.form.getlist("vat_category"), request.form.getlist("category"),
            request.form.getlist("type"), request.form.getlist("vat_type"),
        ):
            if not (ccat or ctype):
                continue  # κενή γραμμή
            label = "Βασική" if vc == db.DEFAULT_RULE else _vat_group_label(vc)
            if vc != db.DEFAULT_RULE and vc not in VAT_CATEGORY_RATES:
                errors.append(f"άγνωστη κατηγορία ΦΠΑ «{vc}»")
            elif not (ccat in EXPENSE_CATEGORIES and ctype in EXPENSE_TYPES and vt in VAT_TYPES):
                errors.append(f"{label}: συμπλήρωσε κατηγορία και τύπο Ε3")
            elif vc in patterns:
                errors.append(f"{label}: υπάρχει δεύτερη γραμμή για την ίδια κατηγορία ΦΠΑ")
            else:
                patterns[vc] = {"category": ccat, "type": ctype, "vat_type": vt}
        if errors:
            flash("⚠ " + " · ".join(errors) + ".", "error")
            return redirect(url_for("supplier_rules", vat=vat, back=back))
        # Χωρίς βασική: γίνεται βασική η πρώτη γραμμή (fallback για άγνωστες κατηγορίες ΦΠΑ).
        if patterns and db.DEFAULT_RULE not in patterns:
            patterns[db.DEFAULT_RULE] = next(iter(patterns.values()))
        db.replace_rule_patterns(cid, vat, patterns)
        db.set_rule_post_mode(cid, vat, request.form.get("post_mode") == "1")
        flash(f"✔ Αποθηκεύτηκαν οι προτάσεις του {vat}." if patterns else f"✔ Διαγράφηκαν οι προτάσεις του {vat}.", "ok")
        return redirect(back)

    pats = db.load_all_rule_patterns(cid, vat)
    rows = [dict(p, vat_category=vc) for vc, p in sorted(pats.items(), key=lambda kv: (kv[0] != db.DEFAULT_RULE, kv[0]))]
    return render_template(
        "supplier_rules.html",
        vat=vat,
        name=db.get_supplier_name(vat),
        rows=rows or [{"vat_category": db.DEFAULT_RULE, "category": "", "type": "", "vat_type": "VAT_361"}],
        back=back,
        post_mode=db.get_rule_post_mode(cid, vat),
        default_rule=db.DEFAULT_RULE,
        vat_rates=VAT_CATEGORY_RATES,
        categories=EXPENSE_CATEGORIES,
        types=EXPENSE_TYPES,
        vat_types=VAT_TYPES,
    )


@app.route("/suppliers/save", methods=["POST"])
def suppliers_save():
    vat = request.form.get("vat", "").strip()
    name = request.form.get("name", "").strip()
    if not vat:
        flash("Το ΑΦΜ είναι υποχρεωτικό.", "error")
        return redirect(url_for("suppliers"))
    db.upsert_supplier(vat, name)
    flash(f"✔ Αποθηκεύτηκε ο συναλλασσόμενος {vat}.", "ok")
    return redirect(url_for("suppliers"))


@app.route("/suppliers/rename", methods=["POST"])
def suppliers_rename():
    """Γρήγορη διόρθωση επωνυμίας από τους πίνακες των βιβλίων (χωρίς ανανέωση σελίδας).
    Ενημερώνει τον κοινό κατάλογο ΑΦΜ → επωνυμία· η πρόταση χαρακτηρισμού μένει ως έχει."""
    vat = request.form.get("vat", "").strip()
    name = " ".join(request.form.get("name", "").split())[:200]
    if not vat or not name:
        return {"ok": False, "error": "Απαιτούνται ΑΦΜ και επωνυμία."}, 400
    db.upsert_supplier(vat, name)
    return {"ok": True, "name": name}


@app.route("/suppliers/delete/<vat>", methods=["POST"])
def suppliers_delete(vat):
    db.delete_supplier(vat)
    flash(f"✔ Διαγράφηκε ο συναλλασσόμενος {vat}.", "ok")
    return redirect(url_for("suppliers"))


@app.route("/suppliers/import", methods=["POST"])
def suppliers_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash(
            "Επίλεξε αρχείο .txt (μία γραμμή ανά συναλλασσόμενο: ΑΦΜ<κενό>Επωνυμία).",
            "error",
        )
        return redirect(url_for("suppliers"))
    data = f.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    n = db.import_suppliers_txt(text)
    flash(f"✔ Εισήχθησαν/ενημερώθηκαν {n} συναλλασσόμενοι.", "ok")
    return redirect(url_for("suppliers"))


@app.route("/suppliers/export")
def suppliers_export():
    return Response(
        db.export_suppliers_txt(),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=counterparties.txt"},
    )


# ------------------------------------------------------------------ #
# Εταιρείες & Λογιστής: import/export από/προς το αρχείο companies.json (δίσκος)
# ------------------------------------------------------------------ #
_COMPANY_KEYS = (
    "company_name",
    "AADE_USER_ID",
    "AADE_SUBSCRIPTION_KEY",
    "AADE_VAT_NUMBER",
    "MYDATA_ENV",
    "use_accountant",
    "vat_period",
    "entity_type",
    "birth_year",
)


@app.route("/companies/export")
def companies_export():
    comps = load_companies()
    payload = {
        "active": get_active_index(),
        "companies": [{k: c.get(k) for k in _COMPANY_KEYS} for c in comps],
        "accountant": get_accountant(),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=companies.json"},
    )


@app.route("/companies/import", methods=["POST"])
def companies_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Επίλεξε αρχείο companies.json.", "error")
        return redirect(url_for("companies"))
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        flash(f"Μη έγκυρο companies.json: {e}", "error")
        return redirect(url_for("companies"))

    file_companies = data.get("companies", []) if isinstance(data, dict) else data
    accountant = data.get("accountant", {}) if isinstance(data, dict) else {}

    # Συγχώνευση κατά επωνυμία: υπάρχουσα → ενημέρωση, αλλιώς προσθήκη.
    existing = {c["company_name"]: c["id"] for c in load_companies()}
    added = updated = 0
    for fc in file_companies:
        payload = {k: fc.get(k) for k in _COMPANY_KEYS}
        name = payload.get("company_name")
        if not name:
            continue
        if name in existing:
            db.update_company(existing[name], payload)
            updated += 1
        else:
            db.add_company(payload)
            added += 1
    if accountant:
        save_accountant(accountant)
    if db.get_active_company_id() is None:
        comps = load_companies()
        if comps:
            db.set_active_company_id(comps[0]["id"])
    flash(
        f"✔ Εισαγωγή από companies.json: {added} νέες, {updated} ενημερώθηκαν"
        + (" + λογιστής" if accountant else "")
        + ".",
        "ok",
    )
    return redirect(url_for("companies"))


# «Αρχείο πελάτη»: ο λογιστής δίνει στον πελάτη την εταιρεία του, κρυπτογραφημένη με κωδικό,
# για να βλέπει μόνο αναφορές. Μόνο τα κλειδιά της εταιρείας — ΠΟΤΕ του λογιστή.
@app.route("/companies/share/<int:idx>", methods=["POST"])
def company_share(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("companies"))
    pw = request.form.get("password", "")
    if len(pw) < 8 or pw != request.form.get("password2"):
        flash("Κωδικός αρχείου: τουλάχιστον 8 χαρακτήρες, ίδιος και στις δύο θέσεις.", "error")
        return redirect(url_for("companies"))
    c = comps[idx]
    company = {k: c.get(k) for k in _COMPANY_KEYS} | {"use_accountant": False}
    if not (c.get("AADE_USER_ID") and c.get("AADE_SUBSCRIPTION_KEY")):
        flash("Η εταιρεία δεν έχει δικά της κλειδιά ΑΑΔΕ: ο πελάτης θα συμπληρώσει τα δικά του από το myAADE.", "ok")
    fname = re.sub(r'[\\/:*?"<>|]+', "_", c["company_name"]).strip() or "company"
    return Response(
        auth.seal({"company": company}, pw),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}.my"},
    )


@app.route("/companies/import-share", methods=["POST"])
def companies_import_share():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Επίλεξε το αρχείο που σου έδωσε ο λογιστής.", "error")
        return redirect(url_for("companies"))
    try:
        company = auth.unseal(f.read(), request.form.get("password", ""))["company"]
        payload = {k: company.get(k) for k in _COMPANY_KEYS} | {"use_accountant": False}
        name = (payload.get("company_name") or "").strip()
        if not name:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        flash("Λάθος κωδικός ή μη έγκυρο αρχείο.", "error")
        return redirect(url_for("companies"))
    existing = {c["company_name"]: c["id"] for c in load_companies()}
    if name in existing:
        cid = existing[name]
        db.update_company(cid, payload)
    else:
        cid = db.add_company(payload)
    db.lock_company(cid)
    if db.get_active_company_id() is None:
        db.set_active_company_id(cid)
    flash(f"✔ Εισαγωγή της «{name}» από τον λογιστή: μόνο αναφορές (κλειδωμένη).", "ok")
    return redirect(url_for("companies"))


# ------------------------------------------------------------------ #
# Αντίγραφα ασφαλείας: λήψη / επαναφορά ολόκληρης της βάσης
# ------------------------------------------------------------------ #
@app.route("/backup")
def backup_download():
    return Response(
        db.backup_bytes(),
        mimetype="application/vnd.sqlite3",
        headers={"Content-Disposition": f"attachment; filename=mydata-backup-{datetime.now():%Y%m%d-%H%M}.db"},
    )


@app.route("/restore", methods=["POST"])
def backup_restore():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Επίλεξε αρχείο αντιγράφου (.db).", "error")
        return redirect(url_for("parameters", tab="backup"))
    try:
        safety = db.restore_bytes(f.read())
    except ValueError as e:
        flash(f"Αποτυχία επαναφοράς: {e}", "error")
        return redirect(url_for("parameters", tab="backup"))
    flash(_RESTORED_MSG.format(safety), "ok")
    return redirect(url_for("parameters", tab="backup"))


_RESTORED_MSG = "✔ Έγινε επαναφορά. Η προηγούμενη βάση φυλάχτηκε ως {} — για αναίρεση πάτησε «Αναίρεση» στη λίστα παρακάτω."


@app.route("/restore/safety", methods=["POST"])
def backup_restore_safety():
    try:
        with open(db.safety_path(request.form.get("name", "")), "rb") as f:
            data = f.read()
        safety = db.restore_bytes(data)  # εκτός with: το prune μπορεί να σβήσει αυτό το αρχείο
    except ValueError as e:
        flash(f"Αποτυχία επαναφοράς: {e}", "error")
        return redirect(url_for("parameters", tab="backup"))
    flash(_RESTORED_MSG.format(safety), "ok")
    return redirect(url_for("parameters", tab="backup"))


@app.route("/backup/safety/<name>")
def backup_safety_download(name):
    try:
        path = db.safety_path(name)
    except ValueError:
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/backup/safety/delete", methods=["POST"])
def backup_safety_delete():
    try:
        os.remove(db.safety_path(request.form.get("name", "")))
        flash("✔ Το αντίγραφο διαγράφηκε.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("parameters", tab="backup"))


@app.route("/parameters/backup", methods=["POST"])
def parameters_backup():
    try:
        n = int(request.form.get("keep_safety_backups", ""))
    except ValueError:
        n = 0
    if not 1 <= n <= 50:
        flash("Το πλήθος αντιγράφων πρέπει να είναι από 1 έως 50.", "error")
        return redirect(url_for("parameters", tab="backup"))
    db.set_setting("keep_safety_backups", n)
    db.prune_safety_backups()
    flash(f"✔ Θα κρατούνται τα {n} τελευταία αντίγραφα.", "ok")
    return redirect(url_for("parameters", tab="backup"))


# ------------------------------------------------------------------ #
# Συνδυασμοί χαρακτηρισμών (ΑΑΔΕ): εισαγωγή/εκκαθάριση
# ------------------------------------------------------------------ #
@app.route("/combinations")
def combinations():
    return redirect(url_for("parameters", tab="combinations"))


@app.route("/combinations/import", methods=["POST"])
def combinations_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Επίλεξε αρχείο (csv/tsv/txt) με τους συνδυασμούς της ΑΑΔΕ.", "error")
        return redirect(url_for("parameters", tab="combinations"))
    data = f.read()
    if data[:2] == b"PK":  # xlsx (zip) - αρχείο ΑΑΔΕ «Συνδυασμοί χαρακτηρισμών»
        try:
            n = db.import_combos_xlsx(data)
        except Exception as e:  # noqa: BLE001
            flash(f"Αποτυχία ανάγνωσης xlsx: {e}", "error")
            return redirect(url_for("parameters", tab="combinations"))
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        n = db.import_combos_csv(text)
    if n:
        # Νεότερη έκδοση της εφαρμογής φέρνει νεότερο αρχείο → αντικαθιστά· ίδια/παλαιότερη όχι.
        version = _combos_version(f.filename) or (_bundled_combos() or (None, None))[1]
        db.set_setting("combos_version", ".".join(map(str, version)) if version else "")
        flash(
            f"✔ Εισήχθησαν {n} συνδυασμοί για {len(db.combos_invoice_types())} τύπους παραστατικών.",
            "ok",
        )
    else:
        flash(
            "Δεν βρέθηκαν έγκυροι συνδυασμοί στο αρχείο (αναμένονται κελιά όπως 1.1, "
            "category2_4, E3_585_011 ανά γραμμή). Στείλε μου το αρχείο να προσαρμόσω τον parser.",
            "error",
        )
    return redirect(url_for("parameters", tab="combinations"))


@app.route("/combinations/clear", methods=["POST"])
def combinations_clear():
    db.clear_combos()
    flash(
        "✔ Καθαρίστηκαν οι συνδυασμοί (η φόρμα δείχνει ξανά όλες τις επιλογές).", "ok"
    )
    return redirect(url_for("parameters", tab="combinations"))


# Νέα έκδοση στο GitHub; Μόνο στο Windows build (frozen): ο installer είναι για αυτό.
# Ένας έλεγχος στην εκκίνηση, στο παρασκήνιο· offline/σφάλμα → απλώς κανένα banner.
_update = None


def _check_update():
    global _update
    try:
        with open(os.path.join(_BASE_DIR, "pyproject.toml"), "rb") as fh:
            current = tomllib.load(fh)["project"]["version"]
        rel = requests.get("https://api.github.com/repos/tedlaz/mydata-classifier/releases/latest", timeout=5).json()
        latest = rel["tag_name"].lstrip("v")
        if tuple(map(int, latest.split("."))) > tuple(map(int, current.split("."))):
            url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"].endswith(".exe")), rel["html_url"])
            _update = {"version": latest, "url": url}
    except Exception:
        pass


if getattr(sys, "frozen", False):
    threading.Thread(target=_check_update, daemon=True).start()


@app.context_processor
def inject_company():
    # self_types: τύποι που εκδίδουμε εμείς (13.x–17.x) → επιτρέπεται «Αντιγραφή».
    if request.endpoint in _PUBLIC:
        return {}  # login/setup: κλειδωμένη, τα κλειδιά των εταιρειών δεν αποκρυπτογραφούνται
    idx, comps = get_active_index(), load_companies()
    return {"active_company": comps[idx] if comps else None, "active_index": idx, "self_types": SELF_EXPENSE_TYPES, "update": _update}


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
