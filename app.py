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
import textwrap
import shutil
import sqlite3
import threading
import time
import tomllib
import traceback
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    has_request_context,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import Markup

import aade_form
import auth
import db
import vat_return
from classifications import (
    CREDIT_INVOICE_TYPES,
    EU_COUNTRIES,
    EXPENSE_CATEGORIES,
    EXPENSE_TYPES,
    FEES_CATEGORIES,
    INCOME_CATEGORIES,
    INCOME_TYPES,
    INVOICE_TYPE_NAMES,
    OTHER_TAX_CATEGORIES,
    STAMP_DUTY_CATEGORIES,
    TAX_CATEGORIES,
    TAX_TYPES,
    VAT_CATEGORY_RATES,
    VAT_TYPES,
    WITHHELD_CATEGORIES,
)
from ensure_env import ensure_env, env_path
from mydata_client import (
    SELF_EXPENSE_TYPES,
    ExpenseInvoice,
    InvoiceLine,
    MyDataClient,
    MyDataError,
    archive_records,
    draft_xml,
    needs_vat_cls,
    xsd_enum,
    xsd_errors,
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


_PUBLIC = {"login", "setup", "setup_restore", "static"}


@app.before_request
def _require_login():
    """Κεντρικός χρήστης (βλ. auth.py): χωρίς χρήστη → /setup, χωρίς ξεκλείδωμα → /login.
    Το session["auth"] πρέπει να ταιριάζει με το nonce στη μνήμη: ένα πλαστό cookie
    (με το FLASK_SECRET) δεν αρκεί, και κάθε restart ζητά ξανά κωδικό."""
    if app.testing or request.endpoint in _PUBLIC:
        return None
    rec = db.get_auth()
    if rec is None:
        return redirect(url_for("setup"))
    if not auth.unlocked() or session.get("auth") != auth.nonce:
        if not auth.recall(request.cookies.get(_REMEMBER), app.secret_key, rec):
            return redirect(url_for("login"))
        session["auth"] = auth.nonce
    return None


_REMEMBER = "mydata_remember"


def _remember(resp, salt: str, force: bool = False):
    """«Να με θυμάσαι σε αυτή τη συσκευή»: cookie 30 ημερών (βλ. auth.remember)."""
    if force or request.form.get("remember"):
        resp.set_cookie(_REMEMBER, auth.remember(app.secret_key, salt), max_age=30 * 86400,
                        httponly=True, samesite="Lax")
    return resp


# Ενέργειες που ανοίγει μόνο η αντίστοιχη ρύθμιση της εταιρείας (Παράμετροι) — κλειστές εξ ορισμού.
# Το /send ελέγχει ανά παραστατικό (classify → allow_classify, create → allow_new).
_GATED = {
    "allow_classify": ({"auto_classify", "classify", "submit", "bulk_classify"},
                       "Οι χαρακτηρισμοί είναι απενεργοποιημένοι για την εταιρεία — ενεργοποίησέ τους στις Παραμέτρους εταιρείας."),
    "allow_new": ({"new_expense", "entry", "entry_submit", "expense_template_save", "expense_template_delete",
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


@app.before_request
def _require_same_company():
    """Η ενεργή εταιρεία είναι μία για όλες τις καρτέλες: POST από σελίδα άλλης εταιρείας (το _cid
    βάζει το app.js) δεν εκτελείται. Οι σελίδες «Εταιρείες» εξαιρούνται: εκεί αλλάζει η ίδια η εταιρεία."""
    cid = request.form.get("_cid") if request.method == "POST" else None
    if not cid or (request.endpoint or "").startswith("companies") or cid == str(_active_company_id()):
        return None
    flash("Η ενεργή εταιρεία άλλαξε σε άλλη καρτέλα — η ενέργεια δεν εκτελέστηκε. Η σελίδα δείχνει πλέον την τρέχουσα εταιρεία.", "error")
    ref = urlparse(request.referrer or "")
    return redirect(_safe_back(ref.path + (f"?{ref.query}" if ref.query else "")) or url_for("dashboard"))


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
            rec = auth.create(user, pw)
            db.save_auth(rec)
            db.encrypt_credentials()
            session["auth"] = auth.nonce
            flash("✔ Δημιουργήθηκε ο χρήστης. Τα κλειδιά myDATA αποθηκεύονται πλέον κρυπτογραφημένα.", "ok")
            return _remember(redirect(url_for("dashboard")), rec["auth_salt"])
    return render_template("login.html", setup=True)


@app.route("/setup/restore", methods=["POST"])
def setup_restore():
    """Πρώτη εκκίνηση: επαναφορά αντιγράφου αντί για νέο χρήστη. Μόνο χωρίς χρήστη — αλλιώς θα ήταν
    επαναφορά χωρίς login. Ο χρήστης/κωδικός είναι πλέον αυτός του αντιγράφου."""
    if db.get_auth():
        return redirect(url_for("login"))
    f = request.files.get("file")
    try:
        db.restore_bytes(f.read() if f else b"", keep=False)
    except ValueError as e:
        flash(f"Αποτυχία επαναφοράς: {e}", "error")
        return redirect(url_for("setup"))
    flash("✔ Έγινε επαναφορά. Συνδέσου με τον χρήστη και τον κωδικό του αντιγράφου.", "ok")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    rec = db.get_auth()
    if rec is None:
        return redirect(url_for("setup"))
    if request.method == "POST":
        if auth.unlock(request.form.get("username", "").strip(), request.form.get("password", ""), rec):
            session["auth"] = auth.nonce
            db.encrypt_credentials()  # π.χ. plaintext κλειδιά από εισαγωγή/παλιό backup
            return _remember(redirect(url_for("dashboard")), rec["auth_salt"])
        flash("Λάθος όνομα χρήστη ή κωδικός.", "error")
    return render_template("login.html", setup=False)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    auth.lock()
    resp = redirect(url_for("login"))
    resp.delete_cookie(_REMEMBER)
    return resp


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
    new = auth.rewrap(user, pw)
    db.save_auth(new)
    flash("✔ Άλλαξε ο κωδικός.", "ok")
    # νέο salt → οι άλλες συσκευές ξεχνιούνται· αυτή μένει θυμημένη αν ήταν
    return _remember(back, new["auth_salt"], force=bool(request.cookies.get(_REMEMBER)))


@app.template_filter("mark5")
def short_mark(mark) -> str:
    """MARK στα βιβλία: μόνο τα τελευταία 5 ψηφία (ολόκληρο στο title), για στενότερη στήλη."""
    mark = str(mark or "")
    return "…" + mark[-5:] if len(mark) > 5 else mark


@app.template_filter("cpname")
def short_counterparty_name(name):
    """Σύντομη επωνυμία συναλλασσομένου για εμφάνιση (η ΒΔ κρατά την πλήρη του VIES)."""
    import vies

    return vies.short_name(name)


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
    """Έκδοση από όνομα αρχείου ΑΑΔΕ, π.χ. syndiasmoi_xaraktirismwn_v2.0.2.xlsx → (2, 0, 2)."""
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
    # Ο αυτόματος κύκλος δουλεύει σε δική του εταιρεία (g.auto_cid) χωρίς να αλλάζει την ενεργή του χρήστη.
    active_id = (has_request_context() and g.get("auto_cid")) or db.get_active_company_id()
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


def _accountant_missing(data: dict) -> bool:
    """Υποχρεωτικός λογιστής + αποστολή, χωρίς credentials λογιστή: η αποστολή δεν ανοίγει."""
    acc = get_accountant()
    if data.get("accountant_required") and data.get("allow_send") and not (
        (acc.get("user_id") or "").strip() and (acc.get("subscription_key") or "").strip()
    ):
        flash("Ο λογιστής είναι υποχρεωτικός για αποστολές, αλλά δεν έχουν οριστεί τα credentials του — "
              "η αποστολή στο myDATA παραμένει κλειστή. Συμπλήρωσέ τα στις Παραμέτρους → Λογιστής.", "error")
        return True
    return False


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

        t0 = time.monotonic()
        workers = min(8, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            found = dict(zip(missing, pool.map(vies.lookup_name, missing)))
        n_vies = sum(1 for n in found.values() if n)

        # fallback στο Μητρώο ΑΑΔΕ/GSIS για όσα δεν βρέθηκαν (επίσης παράλληλα)
        if gsis.gsis_available():
            rest = [v for v, n in found.items() if not n]
            if rest:
                with ThreadPoolExecutor(max_workers=min(4, len(rest))) as pool:
                    for v, n in zip(rest, pool.map(gsis.lookup_name, rest)):
                        found[v] = n
        ok = sum(1 for n in found.values() if n)
        if has_request_context():  # μία συγκεντρωτική γραμμή στο log της ενέργειας (Ιστορικό συνδέσεων)
            g.setdefault("mydata_log", []).append(
                f"{datetime.now(ATHENS):%H:%M:%S}  Επωνυμίες "
                f"{'πελατών' if vat_attr == 'counterpart_vat' else 'προμηθευτών'} (VIES/ΑΑΔΕ): "
                f"{len(found)} ΑΦΜ ελέγχθηκαν → ✓ {ok} επιβεβαιώθηκαν (VIES {n_vies}, ΑΑΔΕ {ok - n_vies}), "
                f"{len(found) - ok} χωρίς επωνυμία  {time.monotonic() - t0:.1f}s")

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


def _get_only(request):
    def guarded(method, url, *args, **kwargs):
        if str(method).upper() != "GET":
            raise PermissionError(f"Αυτόματος κύκλος: μπλοκαρίστηκε {method} {urlparse(url).path} — "
                                  "επιτρέπεται μόνο ανάγνωση από το myDATA.")
        return request(method, url, *args, **kwargs)
    return guarded


def get_client(send: str = "") -> MyDataClient:
    """Client της ενεργής εταιρείας, ένας ανά request: οι ίδιες κλήσεις (π.χ. RequestTransmittedDocs για
    έσοδα, έξοδα και «Νέα από MARK») γίνονται μία φορά (cache στο MyDataClient._pages). Κάθε απάντηση
    του myDATA γράφεται στο log της τρέχουσας ενέργειας. send=<ενέργεια>: client αποστολής (βλ. _make_client)."""
    key = "mydata_client" + (f"_{send}" if send else "")
    cycle_ro = has_request_context() and g.get("auto_cid") and g.get("auto_level") != "send"
    if cycle_ro and send:
        raise PermissionError(f"Αυτόματος κύκλος: η ενέργεια «{send}» δεν επιτρέπεται σε αυτό το βήμα.")
    if has_request_context() and key in g:
        return g.get(key)
    client = _make_client(send=send) if send else _make_client()
    if cycle_ro:  # αυτόματος κύκλος εκτός βήματος αποστολής: ο client κάνει μόνο GET (καμία εγγραφή στο myDATA)
        client.session.request = _get_only(client.session.request)
    via = "κλειδιά λογιστή" if client.entity_vat else "κλειδιά εταιρείας"  # entity_vat μόνο στον client του λογιστή
    client.session.hooks["response"].append(lambda resp, *a, **k: _log_mydata(resp, via=via))
    client.session.hooks["response"].append(_archive_mydata)
    if has_request_context():
        setattr(g, key, client)
    return client


# ------------------------------------------------------------------ #
# Log συνδέσεων με το myDATA: ένα αρχείο ανά ενέργεια της εφαρμογής (όλες οι κλήσεις της + τα μηνύματα
# αποτελέσματος), στο <data>/mydata_logs/<εταιρεία>. Κρατιούνται τα τελευταία MYDATA_LOG_KEEP ανά εταιρεία.
# ------------------------------------------------------------------ #
_LOG_DIR = os.path.join(_DATA_DIR, "mydata_logs")
MYDATA_LOG_KEEP = 10  # προεπιλογή· η ρύθμιση keep_mydata_logs (Παράμετροι → Αντίγραφα ασφαλείας) υπερισχύει


def _log_keep() -> int:
    try:
        return max(1, int(db.get_setting("keep_mydata_logs") or MYDATA_LOG_KEEP))
    except ValueError:
        return MYDATA_LOG_KEEP
_LOG_SECRET = ("pass", "key", "token", "secret")
_log_dir = lambda cid: os.path.join(_LOG_DIR, str(cid or 0))  # noqa: E731


def _log_mydata(resp, *args, via: str = "", **kwargs):
    """requests response hook (client του myDATA)."""
    if has_request_context():
        g.setdefault("mydata_log", []).append(_log_line(resp, via))


def _archive_mydata(resp, *args, **kwargs):
    """requests response hook: οι εγγραφές των RequestDocs/RequestTransmittedDocs μαζεύονται για τον
    κουβά (db.mydata_archive) και γράφονται στο τέλος του request (_write_mydata_archive)."""
    endpoint = urlparse(resp.url).path.rsplit("/", 1)[-1]
    if not (has_request_context() and resp.ok and endpoint in ("RequestDocs", "RequestTransmittedDocs")):
        return
    try:
        g.setdefault("mydata_archive", []).extend(archive_records(resp.content, endpoint))
    except Exception:  # noqa: BLE001 — ο κουβάς δεν σταματά ποτέ την ενέργεια
        app.logger.exception("mydata archive")


@app.after_request
def _write_mydata_archive(response):
    """Μετά την ενημέρωση του documents: ό,τι δεν κράτησε η εφαρμογή πάει στον κουβά (db.archive_sync)."""
    rows = g.pop("mydata_archive", None)
    if rows:
        try:
            db.archive_sync(_active_company_id(), rows)
        except Exception:  # noqa: BLE001 — ο κουβάς δεν σταματά ποτέ την ενέργεια
            app.logger.exception("mydata archive")
    return response


def _log_line(resp, via: str = "") -> str:
    """Μία γραμμή ανά κλήση· στα σφάλματα και η αρχή της απάντησης.
    Δεν γράφονται headers ούτε σώμα αιτήματος (εκεί είναι τα credentials)."""
    line = (f"{datetime.now(ATHENS):%H:%M:%S}  {resp.request.method} "
            f"{urlparse(resp.url).netloc}{resp.request.path_url}  →  "
            f"{'✓' if resp.ok else '✗'} {resp.status_code}  {resp.elapsed.total_seconds():.1f}s  "
            f"{len(resp.content) / 1024:.0f} KB" + (f"  [{via}]" if via else ""))
    if not resp.ok:
        line += "\n      " + resp.text[:2000].replace("\n", "\n      ")
    return line


@app.after_request
def _write_mydata_log(response):
    calls = g.pop("mydata_log", None)
    if not calls:
        return response
    company = get_active_company() or {}
    form = "  ".join(f"{k}={v}" for k, v in request.form.items() if v and not any(x in k.lower() for x in _LOG_SECRET))
    msgs = [f"{'✗' if cat == 'error' else '•'} [{cat}] {msg}" for cat, msg in session.get("_flashes", [])]
    text = "\n".join([f"{datetime.now(ATHENS):%d/%m/%Y %H:%M:%S} · {request.method} {request.path} · "
                      f"{company.get('company_name', '—')}"] + ([form] if form else [])
                     + ["", "Κλήσεις — myDATA και επωνυμίες συναλλασσομένων (VIES/ΑΑΔΕ):"] + calls
                     + (["", "Αποτέλεσμα:"] + msgs if msgs else []))
    d = _log_dir(company.get("id"))
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{datetime.now(ATHENS):%Y%m%d-%H%M%S-%f}.log"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        for old in sorted(os.listdir(d))[:-_log_keep()]:
            os.remove(os.path.join(d, old))
    except OSError:
        app.logger.exception("mydata log")  # το log δεν σταματά ποτέ την ενέργεια
    return response


def mydata_logs(cid, d: str | None = None) -> list[dict]:
    """Τα αποθηκευμένα logs της εταιρείας (ή του φακέλου d), νεότερο πρώτα: {name, title, body, failed}."""
    out, d = [], d or _log_dir(cid)
    for name in sorted(os.listdir(d), reverse=True) if os.path.isdir(d) else []:
        if not name.endswith(".log"):  # π.χ. τα .json του αυτόματου κύκλου
            continue
        with open(os.path.join(d, name), encoding="utf-8") as f:
            title, _, body = f.read().partition("\n")
        out.append({"name": name, "title": title, "body": body.strip(), "failed": "✗" in body})
    return out


ACCOUNTANT_ACTIONS = (  # ενέργειες αποστολής που μπορούν να γίνονται μέσω λογιστή (Παράμετροι → Λογιστής)
    ("reject", "Απορρίψεις παραστατικών εξόδων"),
    ("create", "Νέες εγγραφές (13.x / 14.x / 17.x)"),
    ("cancel", "Ακυρώσεις εξόδων / δικών μας εγγραφών"),
    ("income_cancel", "Ακυρώσεις εσόδων"),
)
ACCOUNTANT_ACTIONS_DEFAULT = "reject,create,cancel"


def accountant_actions() -> set[str]:
    """Ενέργειες που στέλνονται μέσω λογιστή· ο χαρακτηρισμός πάντα."""
    saved = db.get_setting("accountant_actions", ACCOUNTANT_ACTIONS_DEFAULT) or ""
    return {"classify"} | (set(saved.split(",")) & {k for k, _ in ACCOUNTANT_ACTIONS})


def _make_client(send: str = "") -> MyDataClient:
    """Client με τα credentials της ενεργής εταιρείας - fallback στο .env
    αν δεν έχει οριστεί καμία εταιρεία (συμβατότητα με παλιά εγκατάσταση).
    Λήψη (send=""): πάντα με τα credentials της εταιρείας· του λογιστή μόνο αν δεν έχει δικά της.
    Αποστολή (send=ενέργεια): με του λογιστή όταν τον χρησιμοποιεί και η ενέργεια είναι στις accountant_actions()
    (ο χαρακτηρισμός πάντα)· οι υπόλοιπες με τα κλειδιά της εταιρείας."""
    company = get_active_company()
    if company:
        via_acc = company.get("use_accountant") or company.get("accountant_required")
        own_user = (company.get("AADE_USER_ID") or "").strip()
        own = own_user and (company.get("AADE_SUBSCRIPTION_KEY") or "").strip()
        if via_acc and own_user == (get_accountant().get("user_id") or "").strip():
            own = ""  # είναι τα κλειδιά του λογιστή, όχι της εταιρείας → μέσω λογιστή, με entityVatNumber
        # Περίπτωση 2: κοινά credentials λογιστή - στέλνεται το ΑΦΜ της ως entityVatNumber σε κάθε κλήση.
        # Υποχρεωτικός λογιστής χωρίς credentials → καμία αποστολή (MyDataError), ποτέ με του πελάτη.
        if via_acc and ((send and send in accountant_actions()) or not own):
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


def _invoice_to_doc(inv) -> dict:
    """Σειριοποιεί ExpenseInvoice σε dict για αποθήκευση στον πίνακα documents."""
    return {
        "mark": inv.mark,
        "uid": getattr(inv, "uid", None),
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
    # Χαρακτηρισμός εξόδων χωρίς MARK = από την πύλη myDATA (RequestE3Info/RequestVatInfo)·
    # ο ενσωματωμένος έχει MARK = MARK παραστατικού, η χωριστή υποβολή το δικό της.
    inv.cls_source = (
        "Πύλη myDATA" if not inv.classification_mark and inv.cls_info and row.get("kind") == "expense" else ""
    )
    inv.local_action = row.get("local_action") or "classify"
    inv.doc_id = row.get("id")
    inv.cls_post_mode = int(row.get("cls_post_mode") or 0)
    # Απεσταλμένο που αλλάχτηκε τοπικά: ο προηγούμενος χαρακτηρισμός (βλ. db._snapshot_sent).
    inv.prev_cls = json.loads(json.loads(row["prev_state"])["cls_json"] or "[]") if row.get("prev_state") else None
    inv.source = row.get("source") or "rest"
    inv.late = bool(row.get("late_since"))
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


def _last_key(kind: str, cid: int) -> str:
    """Τελευταία επιτυχημένη ανάκτηση του βιβλίου (JSON {kind: τύπος, at: dd/mm/yyyy HH:MM}) για το header."""
    return f"last_sync:{kind}:{cid}"


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


def _fetch_expense_range(cid: int, df: str | None, dt: str | None, marks: dict | None = None) -> str:
    """Ανάκτηση εξόδων διαστήματος (ή, με marks={endpoint: MARK}, ό,τι ανέβηκε μετά) στο τοπικό βιβλίο·
    επιστρέφει το μήνυμα επιτυχίας (σφάλματα ανεβαίνουν)."""
    client = get_client()
    unclassified = client.request_unclassified_expenses(df, dt, marks)
    classified = client.request_classified_expenses(df, dt, marks)

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
    _note_new_cls(cid, "expense", classified, "confirmed")
    for inv in unclassified:
        db.upsert_document(cid, "expense", _invoice_to_doc(inv), "unclassified")
    for inv in classified:
        db.upsert_document(cid, "expense", _invoice_to_doc(inv), "confirmed")
    n0 = _classify_zero_docs(cid, [inv.mark for inv in unclassified])

    if df:
        db.set_setting(_range_key("expense"), f"{df} – {dt}")
    return (
        f"✔ Ανακτήθηκαν και αποθηκεύτηκαν {len(unclassified) + len(classified)} "
        "παραστατικά."
        + (f" {n0} με μηδενική αξία χαρακτηρίστηκαν τοπικά (2.5 / E3_585_016)." if n0 else "")
        + (f" Αφαιρέθηκαν {removed} ακυρωμένα." if removed else "")
    )


def _note_new_cls(cid: int, kind: str, invoices, status: str) -> None:
    """Όσα υπήρχαν ήδη τοπικά σε άλλη κατάσταση και το myDATA δείχνει πλέον χαρακτηρισμένα → g, για μία γραμμή
    «Χαρακτηρισμοί …» ανά ενέργεια (το «Νέα από MARK» μπορεί να τρέξει την ανάκτηση δύο φορές). Πριν το upsert."""
    if not has_request_context():
        return
    for inv in invoices:
        prev = db.get_document(cid, inv.mark)
        if prev and prev["status"] != status:
            g.setdefault("new_cls", {}).setdefault(kind, []).append(f"{inv.mark} ({(inv.issue_date or '')[:10]})")


def _fetch_income_range(cid: int, df: str | None, dt: str | None, marks: dict | None = None) -> str:
    """Ανάκτηση εσόδων διαστήματος (ή, με marks, ό,τι ανέβηκε μετά το MARK) στο τοπικό βιβλίο·
    επιστρέφει το μήνυμα επιτυχίας (σφάλματα ανεβαίνουν)."""
    unclassified, classified, cancelled_marks = get_client().request_income(
        df, dt, marks["RequestTransmittedDocs"] if marks else None)

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

    _note_new_cls(cid, "income", classified, "classified")
    for inv in unclassified:
        db.upsert_document(cid, "income", _income_to_doc(inv), "unclassified")
    for inv in classified:
        db.upsert_document(cid, "income", _income_to_doc(inv), "classified")

    if df:
        db.set_setting(_range_key("income"), f"{df} – {dt}")
    return (
        f"✔ Ανακτήθηκαν {len(unclassified) + len(classified)} παραστατικά εσόδων "
        f"({len(unclassified)} αχαρακτήριστα, {len(classified)} χαρακτηρισμένα)"
        + (f" · αφαιρέθηκαν {removed} ακυρωμένα." if removed else ".")
    )


# Σελίδα «Ανάκτηση»: ποια βιβλία (σειρά: πρώτα έσοδα, μετά έξοδα — όπως στο μενού).
_SYNC_SCOPES = {"both": ("income", "expense"), "income": ("income",), "expense": ("expense",)}
_SYNC_NOUNS = {"both": "εσόδων & εξόδων", "income": "εσόδων", "expense": "εξόδων"}


# «Νέα παραστατικά από MARK». Αμετάβλητο: το βιβλίο έχει ΟΛΑ τα παραστατικά με έκδοση ≥ «Αρχή» και MARK ≤ W,
# εκτός από τα καταγεγραμμένα κενά (διαγραφές), που ξανάρχονται στο επόμενο πάτημα.
# Τα endpoints κάθε βιβλίου: τα έξοδα παίρνουν και από τις δικές μας διαβιβάσεις (αυτοτιμολογούμενα, χαρακτηρισμοί).
_MARK_ENDPOINTS = {"income": ("RequestTransmittedDocs",), "expense": ("RequestDocs", "RequestTransmittedDocs")}


def _wm_key(kind: str, endpoint: str, cid: int) -> str:
    """Τελευταίο MARK (W) ως το οποίο έχουμε ΟΛΑ τα παραστατικά του βιβλίου (ανά endpoint και εταιρεία)."""
    return f"mark_wm:{kind}:{endpoint}:{cid}"


def _start_key(kind: str, cid: int) -> str:
    """«Αρχή» (yyyy-mm-dd): το «Από» του πρώτου πατήματος."""
    return f"mark_start:{kind}:{cid}"


def _mark_ready(cid: int, kind: str) -> bool:
    """Έχει γίνει το πρώτο πάτημα του «Νέα παραστατικά από MARK» για το βιβλίο."""
    return bool(db.get_setting(_start_key(kind, cid))) and all(  # W "0" (παλιές βάσεις) = χωρίς W
        db.get_setting(_wm_key(kind, ep, cid)) not in (None, "", "0") for ep in _MARK_ENDPOINTS[kind])


def _mark_reset(cid: int, kind: str) -> None:
    for key in [_wm_key(kind, ep, cid) for ep in _MARK_ENDPOINTS[kind]] + [_start_key(kind, cid)]:
        db.delete_setting(key)


def _init_watermarks(cid: int, kind: str) -> bool:
    """Μετά από ανάκτηση «Αρχή» → σήμερα: ό,τι υπήρχε στο myDATA από την Αρχή το έχουμε, άρα κάθε MARK που έχουμε
    ήδη δει είναι ασφαλές W. Τα MARK είναι κοινή αρίθμηση της ΑΑΔΕ: άδειο βιβλίο παίρνει το μέγιστο MARK του άλλου
    βιβλίου της εταιρείας — παραστατικά ή W του (ίδιο περιβάλλον prod/dev). Χωρίς κανένα, ΔΕΝ γράφεται W (με 0 το
    επόμενο πάτημα θα έφερνε όλο το ιστορικό του ΑΦΜ): το βιβλίο μένει σε αρχική ανάκτηση. Επιστρέφει αν γράφτηκε."""
    wms = [db.get_setting(_wm_key(k, ep, cid)) or "" for k, eps in _MARK_ENDPOINTS.items() for ep in eps]
    top = max([int(r["mark"]) for k in _MARK_ENDPOINTS for r in db.get_documents(cid, k) if (r["mark"] or "").isdigit()]
              + [int(w) for w in wms if w.isdigit()], default=0)
    if not top:
        return False
    for ep in _MARK_ENDPOINTS[kind]:
        db.set_setting(_wm_key(kind, ep, cid), top)
    return True


def _gr(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def _fetch_new(cid: int, kind: str, start: str) -> str:
    """«Νέα παραστατικά από MARK». Πρώτη φορά: ανάκτηση από την Αρχή (αλλιώς `start`, το «Από» της φόρμας) έως
    σήμερα· το W ορίζει μετά το sync_run (_init_watermarks), όταν έχουν ανακτηθεί όλα τα βιβλία του πατήματος.
    Μετά: ό,τι ανέβηκε με MARK > W, όποια κι αν είναι η ημερομηνία έκδοσης (εκπρόθεσμα, ακυρώσεις,
    χαρακτηρισμοί). Οι σελίδες του new_since περνούν απευθείας από την κανονική ανάκτηση (cache του client,
    καμία δεύτερη κλήση)· ανάκτηση διαστήματος μόνο για χαρακτηρισμούς παλιών τοπικών παραστατικών.
    Το W αλλάζει μόνο μετά από επιτυχία (σφάλμα → εξαίρεση, μένουν ως είχαν)."""
    run = _fetch_income_range if kind == "income" else _fetch_expense_range
    today = datetime.now(ATHENS).date().isoformat()
    if not _mark_ready(cid, kind):
        start = db.get_setting(_start_key(kind, cid)) or start
        run(cid, _gr(start), _gr(today))
        db.set_setting(_start_key(kind, cid), start)
        return f"✔ Νέα παραστατικά από MARK, αρχική ανάκτηση: ανακτήθηκαν τα παραστατικά από {_gr(start)} έως σήμερα."
    keys = {ep: _wm_key(kind, ep, cid) for ep in _MARK_ENDPOINTS[kind]}
    marks = {ep: int(db.get_setting(key)) for ep, key in keys.items()}
    client = get_client()
    tops, cand, refs = {}, set(), set()
    for ep, key in keys.items():
        tops[key], docs, r = client.new_since(ep, marks[ep])
        cand |= {m for m, _ in docs}
        refs |= r
    fresh = {m for m in cand if not db.get_document(cid, m)}
    if cand or refs:
        run(cid, None, None, marks)
    # Χαρακτηρισμοί παλιών τοπικών παραστατικών (δεν ήρθαν στη δέλτα): ανάκτηση του διαστήματός τους.
    # Τα ακυρωμένα έχουν ήδη αφαιρεθεί από το run.
    dates = [r["issue_date"][:10] for m in refs - cand
             if (r := db.get_document(cid, m)) and r["kind"] == kind and r["issue_date"]]
    if dates:
        # ponytail: ένα διάστημα min–max· αν απλώνονται σε πολλά έτη, ανά μήνα
        run(cid, _gr(min(dates)), _gr(max(dates)))
    for key, top in tops.items():
        db.set_setting(key, top)
    new = [r for m in fresh if (r := db.get_document(cid, m)) and r["kind"] == kind]
    this_month = datetime.now(ATHENS).strftime("%Y-%m")
    late = [r for r in new if (r["issue_date"] or "")[:7] < this_month]
    db.mark_late(cid, [r["mark"] for r in late])
    # Μόνο τα αχαρακτήριστα θέλουν προσοχή· τα ήδη χαρακτηρισμένα εκπρόθεσμα δεν αναφέρονται.
    todo = sorted(f"{r['mark']} ({r['issue_date'][:10]})" for r in late if r["status"] == "unclassified")
    return (f"✔ Νέα παραστατικά από MARK: {len(new)}"
            + (f" · ⏰ {len(todo)} αχαρακτήριστα εκπρόθεσμα: {', '.join(todo)}" if todo else "") + ".")


def _reset_if_overlap(cid: int, kind: str, date_from: str, date_to: str) -> bool:
    """Διαγραφή μέσα στο [Αρχή, σήμερα]: το βιβλίο δεν είναι πια πλήρες από την Αρχή, άρα μηδενίζεται η Αρχή/W
    και ζητείται ξανά αρχική ανάκτηση. Πριν από την Αρχή: τίποτα."""
    start = db.get_setting(_start_key(kind, cid))
    if not start or max(date_from, start) > min(date_to, datetime.now(ATHENS).date().isoformat()):
        return False
    _mark_reset(cid, kind)
    return True


@app.route("/sync/mark-reset", methods=["POST"])
def sync_mark_reset():
    """«Αλλαγή αρχής»: ξεχνά Αρχή/W/κενά· το επόμενο «Νέα παραστατικά από MARK» ξεκινά από το νέο «Από»."""
    scope = request.form.get("scope") if request.form.get("scope") in _SYNC_SCOPES else "both"
    cid = _active_company_id()
    if cid:
        for kind in _SYNC_SCOPES[scope]:
            _mark_reset(cid, kind)
        flash("Η αρχή του «Νέα παραστατικά από MARK» μηδενίστηκε. Το επόμενο πάτημα ξεκινά από την ημερομηνία «Από».", "ok")
    return redirect(url_for("sync", scope=scope, date_from=request.form.get("date_from"),
                            date_to=request.form.get("date_to")))


@app.route("/sync", methods=["POST"])
def sync_run():
    """Ανάκτηση / Νέα παραστατικά από MARK / Διαγραφή (διαστήματος ή όλων) για έσοδα, έξοδα ή και
    τα δύο. Κάθε βιβλίο ανεξάρτητα: αποτυχία του ενός δεν σταματά το άλλο."""
    scope = request.form.get("scope") if request.form.get("scope") in _SYNC_SCOPES else "both"
    action = request.form.get("action") if request.form.get("action") in ("fetch", "new", "delete") else "fetch"
    if action == "delete" and request.form.get("all"):
        cid = _active_company_id()
        for kind in _SYNC_SCOPES[scope] if cid else ():
            # ponytail: σβήνει όσα έχουν issue_date (όλα τα ανακτημένα/τοπικά έχουν)
            n = db.delete_documents_range(cid, kind, "", "9999-12-31")
            _mark_reset(cid, kind)
            db.delete_setting(_range_key(kind))
            db.delete_setting(_last_key(kind, cid))
            flash(f"Διαγράφηκαν όλα τα παραστατικά {_SYNC_NOUNS[kind]} ({n}).", "ok")
        return redirect(url_for("sync", scope=scope))
    df, dt, cid, bad = _fetch_dates(scope)
    if bad:
        return bad
    ok, done = 0, []
    for kind in _SYNC_SCOPES[scope]:
        label = "Έσοδα" if kind == "income" else "Έξοδα"
        prefix = f"{label}: " if scope == "both" else ""
        if action == "delete":
            n = db.delete_documents_range(cid, kind, request.form["date_from"], request.form["date_to"])
            reset = _reset_if_overlap(cid, kind, request.form["date_from"], request.form["date_to"])
            flash(f"{prefix}Διαγράφηκαν {n} παραστατικά {_SYNC_NOUNS[kind]} για το διάστημα {df} – {dt}."
                  + (" Χρειάζεται ξανά αρχική ανάκτηση." if reset else ""), "ok")
            ok += 1
            continue
        try:
            if action == "new":
                what = "Νέα από MARK" if _mark_ready(cid, kind) else f"Αρχική από MARK: {df} – σήμερα"
                flash(prefix + _fetch_new(cid, kind, request.form["date_from"]), "ok")
            else:
                what = f"Περίοδος {df} – {dt}"
                run = _fetch_income_range if kind == "income" else _fetch_expense_range
                flash(prefix + run(cid, df, dt), "ok")
            if new := g.get("new_cls", {}).pop(kind, None):
                flash(f"Χαρακτηρισμοί {_SYNC_NOUNS[kind]}: {len(new)} νέοι στο myDATA: {', '.join(new)}.", "ok")
            db.set_setting(_last_key(kind, cid), json.dumps(
                {"kind": what, "at": f"{datetime.now(ATHENS):%d/%m/%Y %H:%M}"}, ensure_ascii=False))
            ok += 1
            done.append(kind)
        except Exception as e:  # noqa: BLE001 — network κ.λπ., δεν θέλουμε 500 στο route
            flash(prefix + _fetch_error(e), "error")
    # W μετά από ΟΛΑ τα βιβλία του πατήματος: στο «Έσοδα & Έξοδα» άδεια έσοδα παίρνουν το MARK των εξόδων.
    for kind in done if action == "new" else ():
        if not _mark_ready(cid, kind) and db.get_setting(_start_key(kind, cid)) and not _init_watermarks(cid, kind):
            flash(("Έσοδα: " if kind == "income" else "Έξοδα: ") * (scope == "both")
                  + f"Κανένα παραστατικό ακόμα — το επόμενο πάτημα ξαναελέγχει από "
                  f"{_gr(db.get_setting(_start_key(kind, cid)))}.", "ok")
    if request.form.get("back") == "dashboard":  # «Ανάκτηση παραστατικών» από τον Πίνακα ελέγχου
        return redirect(url_for("dashboard"))
    if not ok or action == "delete":
        return redirect(url_for("sync", scope=scope, date_from=request.form["date_from"],
                                date_to=request.form["date_to"], tab=request.form.get("tab")))
    if "expense" in _SYNC_SCOPES[scope]:
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



_SK_W, _SK_H, _SK_PAD, _SK_GAP, _SK_BAR = 1000, 340, 12, 8, 12  # _SK_H: ελάχιστο ύψος
_SK_LINE, _SK_WRAP, _SK_LGAP = 14, 26, 6  # ετικέτες: ύψος γραμμής, χαρακτήρες ανά γραμμή, κενό μεταξύ τους
CLOSING_LABEL = "Αποθέματα λήξης"  # κόμβος εσόδου στις Ροές (μειώνουν το κόστος πωληθέντων)
_SK_MAX, _SK_MIN_SHARE = 15, 0.005  # έως 15 κόμβοι ανά πλευρά· κάτω από 0,5% του συνόλου → «Λοιποί»
_SK_X = {"src": 250, "mid": 480, "dst": 700}  # x των ράβδων: πηγές · σύνολο · προορισμοί (χώρος για ετικέτες)


def _sankey(inc_docs: list[dict], exp_docs: list[dict], pick: tuple | None = None, stock: bool = True, assets: bool = True):
    """Διάγραμμα ροής (Sankey) των Ροών: από πού έρχονται τα έσοδα → σύνολο → πού πάνε.
    Καθαρή αξία ανά παραστατικό (πιστωτικά αρνητικά). Αποθέματα όπως στο καθαρό κέρδος (κόστος πωληθέντων =
    έναρξης + αγορές − λήξης): τα αποθέματα έναρξης (2.13) είναι έξοδο, τα αποθέματα λήξης (2.14) καταλογίζονται
    ως έσοδο (κόμβος «Αποθέματα λήξης» στις πηγές). Το κέρδος φαίνεται πάντα (δεξιά)· η ζημία μπαίνει αριστερά.
    stock=False: χωρίς αποθέματα (ούτε έναρξης ούτε λήξης)· assets=False: χωρίς αγορές παγίων (2.7) στα έξοδα
    (κεφαλαιοποιούνται· οι αποσβέσεις μένουν).
    Κάθε πλευρά έχει εναλλακτικές ομαδοποιήσεις, προϋπολογισμένες (εναλλαγή μόνο με CSS).
    pick = (πλευρά, ομαδοποίηση, ετικέτα): αντί για το διάγραμμα, τα [(παραστατικό, ποσό στη ροή)] του κόμβου
    — για το modal· ίδιος υπολογισμός, άρα το σύνολό τους = το ποσό του κόμβου."""

    def items(docs, book):
        # [(παραστατικό, ποσό, χαρακτηρισμοί Ε3, αποθέματα λήξης;)]: το απόθεμα λήξης ενός παραστατικού εξόδων
        # βγαίνει από τα έξοδα και γίνεται χωριστό στοιχείο εσόδου.
        inc_out, exp_out = [], []
        for d in docs:
            e3 = [e for e in json.loads(d["cls_json"] or "[]") if not (e.get("type") or "").startswith("VAT_")]
            sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
            # Εκτός ροής: αποθέματα (αν !stock) και αγορές παγίων (αν !assets) — μόνο στα έξοδα.
            skip = {*(() if stock else STOCK_CATEGORIES), *(() if assets else (ASSET_CATEGORY,))} if book == "expense" else set()
            skipped = sum(e.get("amount") or 0 for e in e3 if e.get("category") in skip)
            e3 = [e for e in e3 if e.get("category") not in skip]
            closing = [e for e in e3 if e.get("category") == CLOSING_STOCK] if book == "expense" else []
            closing_amount = sum(e.get("amount") or 0 for e in closing)
            rest = [e for e in e3 if e not in closing]
            net = (d["total_net"] or 0) - skipped - closing_amount
            (inc_out if book == "income" else exp_out).append((d, sign * net, rest, False))
            if closing_amount:
                inc_out.append((d, sign * closing_amount, closing, True))
        return inc_out, exp_out

    def e3_by(label_of):
        # Η καθαρή αξία μοιράζεται αναλογικά στους χαρακτηρισμούς Ε3 του παραστατικού (κατά ποσό).
        def shares(_d, e3):
            w = [(label_of(e), e.get("amount") or 0) for e in e3]
            tot = sum(a for _, a in w)
            return [(label, a / tot) for label, a in w] if tot else [("Χωρίς χαρακτηρισμό", 1)]
        return shares

    def e3_category(e):
        cat = e.get("category") or ""
        return _CLASSIFICATION_CATEGORY_NAMES.get(cat) or cat or "Χωρίς κατηγορία"

    def e3_type(e):  # E3_561_001 → «561_001 Πωλήσεις αγαθών και υπηρεσιών Χονδρικές - Επιτηδευματιών»
        typ = e.get("type") or ""
        return f"{typ.removeprefix('E3_')} {_CLASSIFICATION_NAMES.get(typ, '')}".strip() if typ else "Χωρίς τύπο Ε3"

    def e3_account(e):  # E3_561_001 → «561 Πωλήσεις αγαθών & υπηρεσιών»
        group = "_".join((e.get("type") or "").split("_")[:2])
        return f"{group.removeprefix('E3_')} {_e3_group_label(group)}".strip() if group else "Χωρίς λογαριασμό Ε3"

    def party(no_vat):
        return lambda d, _e3: [(d["counterparty_name"] or d["counterparty_vat"], 1) if d["counterparty_vat"] else (no_vat, 1)]

    def group(rows, key):
        # [(ετικέτα, ποσό, [(παραστατικό, μερίδιο)])]: τα μεγαλύτερα (έως _SK_MAX, όσα ≥ 0,5%) + «Λοιποί».
        # Το διάγραμμα ψηλώνει όσο χρειάζεται, οπότε δείχνει όσα περισσότερα έχουν νόημα.
        out: dict = {}
        for d, amount, e3, closing in rows:
            for label, share in ([(CLOSING_LABEL, 1)] if closing else key(d, e3)):
                g = out.setdefault(label, [0.0, []])
                g[0] += amount * share
                g[1].append((d, amount * share))
        # Τα αποθέματα λήξης μένουν πάντα χωριστός κόμβος (ποτέ στα «Λοιποί»), σε κάθε ομαδοποίηση.
        fixed = [(CLOSING_LABEL, round(out[CLOSING_LABEL][0], 2), out.pop(CLOSING_LABEL)[1])] if CLOSING_LABEL in out else []
        fixed = [t for t in fixed if t[1] > 0.005]
        items = sorted(((k, round(v, 2), m) for k, (v, m) in out.items()), key=lambda t: -t[1])
        floor = _SK_MIN_SHARE * (sum(t[1] for t in items) + sum(t[1] for t in fixed))
        keep = [t for t in items[:_SK_MAX - len(fixed)] if t[1] > 0 and t[1] >= floor]
        rest = round(sum(t[1] for t in items) - sum(t[1] for t in keep), 2)
        # Ομάδες με αρνητικό υπόλοιπο (πιστωτικά) πάνε στα «Λοιποί»· αν αυτά βγουν αρνητικά, τα συμψηφίζουν
        # οι μικρότερες ομάδες, ώστε το άθροισμα να μένει ακριβώς το σύνολο.
        while rest < -0.005 and keep:
            rest = round(rest + keep.pop()[1], 2)
        kept = {t[0] for t in keep}
        others = [m for t in items if t[0] not in kept for m in t[2]]
        return keep + fixed + ([("Λοιποί", rest, others)] if rest > 0.005 else [])

    inc_rows, _ = items(inc_docs, "income")
    closing_rows, exp_rows = items(exp_docs, "expense")
    inc_rows += closing_rows
    inc = round(sum(r[1] for r in inc_rows), 2)
    exp = round(sum(r[1] for r in exp_rows), 2)
    if inc <= 0 and exp <= 0:
        return [] if pick else None
    profit = round(inc - exp, 2)
    total = max(inc, exp)
    sides = {
        "src": {"cust": ("Πελάτης", group(inc_rows, party("Λιανική"))),
                "e3": ("Κατηγορία Ε3", group(inc_rows, e3_by(e3_category))),
                "e3t": ("Τύπος Ε3", group(inc_rows, e3_by(e3_type))),
                "e3a": ("Λογαριασμός Ε3", group(inc_rows, e3_by(e3_account)))},
        "dst": {"e3": ("Κατηγορία Ε3", group(exp_rows, e3_by(e3_category))),
                "e3t": ("Τύπος Ε3", group(exp_rows, e3_by(e3_type))),
                "e3a": ("Λογαριασμός Ε3", group(exp_rows, e3_by(e3_account))),
                "sup": ("Προμηθευτής", group(exp_rows, party("Χωρίς ΑΦΜ")))},
    }
    if pick:
        side, key, label = pick
        return next((m for lbl, _v, m in sides.get(side, {}).get(key, ("", []))[1] if lbl == label), [])
    # Συμπληρωματικοί κόμβοι: ζημία αριστερά (κόκκινο), κέρδος πάντα πρώτο δεξιά (πράσινο, ακόμη και 0).
    for side, extra, first in (("src", ("Ζημία", -profit, "var(--err)") if profit < 0 else None, False),
                               ("dst", ("Κέρδος", max(profit, 0), "var(--ok)"), True)):
        for key, (title, nodes) in sides[side].items():
            # 7 διακριτά χρώματα (σταθερή σειρά, χωρίς ανακύκλωση)· από τον 8ο ουδέτερο γκρι, «Λοιποί» πιο ανοιχτό.
            # Τα αποθέματα λήξης έχουν πάντα το 7ο χρώμα (ίδιο σε κάθε ομαδοποίηση)· τότε οι υπόλοιποι παίρνουν 6.
            n_col = 6 if any(lbl == CLOSING_LABEL for lbl, _v, _m in nodes) else 7
            nodes = [(lbl, v, "var(--s-other)" if lbl == "Λοιποί" else "var(--s7)" if lbl == CLOSING_LABEL
                      else f"var(--s{i + 1})" if i < n_col else "var(--s-rest)") for i, (lbl, v, _m) in enumerate(nodes)]
            if extra:
                nodes = [extra, *nodes] if first else [*nodes, extra]
            sides[side][key] = (title, nodes)

    # Κάθε συνδυασμός (πηγή × προορισμός) σχεδιάζεται χωριστά: ύψος = όσο χρειάζονται οι ετικέτες της πιο
    # «γεμάτης» πλευράς, και οι ράβδοι/ροές απλώνονται σε όλο αυτό το ύψος (όχι στριμωγμένες πάνω).
    wrapped = {label: textwrap.wrap(label, _SK_WRAP) or [label] for s_ in sides.values() for _, n in s_.values()
               for label, _, _ in n}
    block = lambda label: (len(wrapped[label]) + 1) * _SK_LINE  # noqa: E731
    need = lambda nodes: sum(block(lbl) for lbl, _, _ in nodes) + _SK_LGAP * (len(nodes) - 1)  # noqa: E731

    def layout(side, nodes, height, k, mid_y):
        inner = height - 2 * _SK_PAD
        hs = [max(v, 0) * k for _, v, _ in nodes]
        y = _SK_PAD + (inner - sum(hs) - _SK_GAP * (len(nodes) - 1)) / 2
        cy, out, bottom = mid_y, [], -_SK_LGAP  # η πρώτη ετικέτα δεν ξεκινά πάνω από το διάγραμμα
        x_bar = _SK_X[side]
        for (label, value, color), h in zip(nodes, hs):
            # Ροή: από τη δεξιά άκρη της πηγής στο κέντρο, ή από το κέντρο στην αριστερή άκρη του προορισμού.
            (x0, y0), (x1, y1) = ((x_bar + _SK_BAR, y), (_SK_X["mid"], cy)) if side == "src" else                 ((_SK_X["mid"] + _SK_BAR, cy), (x_bar, y))
            xm = (x0 + x1) / 2
            path = (f"M{x0:.1f} {y0:.1f}C{xm:.1f} {y0:.1f} {xm:.1f} {y1:.1f} {x1:.1f} {y1:.1f}"
                    f"L{x1:.1f} {y1 + h:.1f}C{xm:.1f} {y1 + h:.1f} {xm:.1f} {y0 + h:.1f} {x0:.1f} {y0 + h:.1f}Z")
            top = max(y + h / 2 - block(label) / 2, bottom + _SK_LGAP)  # μπλοκ ετικέτας κεντραρισμένο στη ράβδο
            bottom = top + block(label)
            out.append({"label": label, "lines": wrapped[label], "value": value,
                        "pct": round(value / inc * 100) if inc > 0 else 0, "color": color,
                        "docs": color.startswith("var(--s"),  # κέρδος/ζημία (--ok/--err): διαφορά, όχι παραστατικά
                        "y": round(y, 1), "h": round(max(h, 1 if value > 0 else 0), 1), "cy": round(y + h / 2, 1),
                        "top": top, "path": path})
            y += h + _SK_GAP
            cy += h
        # Αντίστροφο πέρασμα: όσες βγήκαν κάτω από το διάγραμμα ανεβαίνουν, μόνο όσο χρειάζεται.
        limit = height - _SK_PAD
        for n in reversed(out):
            n["top"] = min(n["top"], limit - block(n["label"]))
            limit = n["top"] - _SK_LGAP
        for n in out:
            n["lcy"] = round(n["top"] + block(n["label"]) / 2, 1)  # κέντρο ετικέτας (για τη γραμμή-οδηγό)
            n["ys"] = [round(n["top"] + _SK_LINE * (i + 0.8), 1) for i in range(len(n["lines"]) + 1)]
        return out

    pairs = []
    for a, (_, left) in sides["src"].items():
        for b, (_, right) in sides["dst"].items():
            height = max(_SK_H, round(max(need(left), need(right)) + 2 * _SK_PAD))
            inner = height - 2 * _SK_PAD
            k = min((inner - _SK_GAP * (len(n) - 1)) / total for n in (left, right))
            mid_h = total * k
            mid_y = _SK_PAD + (inner - mid_h) / 2
            pairs.append({"src_key": a, "dst_key": b, "h": height, "mid": {"y": round(mid_y, 1), "h": round(mid_h, 1)},
                          "src": layout("src", left, height, k, mid_y), "dst": layout("dst", right, height, k, mid_y)})
    return {
        "w": _SK_W, "bar": _SK_BAR, "x": _SK_X, "inc": inc, "exp": exp, "profit": profit, "pairs": pairs,
        "src": {key: title for key, (title, _) in sides["src"].items()},
        "dst": {key: title for key, (title, _) in sides["dst"].items()},
    }


def _last_12_months(now: datetime) -> list[str]:
    """Οι τελευταίοι 12 μήνες έως τον τρέχοντα, ως "yyyy-mm" (ο παλαιότερος πρώτος). Για το month_spark."""
    return [f"{(now.year * 12 + now.month - 1 - k) // 12}-{(now.month - 1 - k) % 12 + 1:02d}" for k in range(11, -1, -1)]


STRIP_FROM_YEAR = "2024"  # προεπιλογή της παραμέτρου «Λωρίδα ετών από»
_STRIP_STATUSES = ("unclassified", "classified", "sent", "confirmed")


def _strip_from_year() -> int:
    return int(db.get_setting("strip_from_year") or STRIP_FROM_YEAR)


def _month_strip(cid: int | None, kinds) -> dict:
    """Λωρίδα ετών (από την παράμετρο «strip_from_year» έως το τρέχον): ανά έτος και ανά μήνα έσοδα, έξοδα
    (χαρακτηρισμένα) και αχαρακτήριστα ανά πλευρά, για τα βιβλία `kinds`. Για την Ανάκτηση και την κεφαλίδα των
    βιβλίων."""
    now = datetime.now(ATHENS)
    first = min(_strip_from_year(), now.year)
    zero = lambda: {"income": 0, "expense": 0, "in_pending": 0, "ex_pending": 0, "total": 0,  # noqa: E731
                    **dict.fromkeys(_STRIP_STATUSES, 0)}  # ανά κατάσταση: γραμμές της λωρίδας στα βιβλία
    per_month = {f"{y}-{m:02d}": zero() for y in range(first, now.year + 1) for m in range(1, 13)}
    for kind in kinds:
        pend = "in_pending" if kind == "income" else "ex_pending"
        for ym, st in (db.month_counts(cid, kind, f"{first}-01-01") if cid else {}).items():
            if ym in per_month:
                pending = st.get("unclassified", 0)
                per_month[ym][kind] += sum(st.values()) - pending
                per_month[ym][pend] += pending
                per_month[ym]["total"] += sum(st.values())
                for k in _STRIP_STATUSES:
                    per_month[ym][k] += st.get(k, 0)
    years = []
    for y in range(first, now.year + 1):
        months = [(ym, st) for ym, st in per_month.items() if ym.startswith(f"{y}-")]
        years.append({"y": str(y), "months": months, "tot": {k: sum(st[k] for _, st in months) for k in zero()}})
    return {"years": years, "this_month": now.strftime("%Y-%m")}


def _period_nav(strip: dict, fd: str, view: str, cid: int | None, kind: str) -> dict:
    """Πλοηγός περιόδου των βιβλίων (Έτος › Τρίμηνο › Μήνας › Εβδομάδα › Ημέρα), από το φίλτρο f_date: ψίχουλα, ‹ ›
    στο ίδιο επίπεδο και τα «παιδιά» του επιπέδου ως chips (πλήθος της καρτέλας `view`, κουκκίδα = υπάρχουν
    αχαρακτήριστα). Εβδομάδα = Δευτέρα–Κυριακή μέσα στον μήνα, φίλτρο «yyyy-mm-dd:yyyy-mm-dd». Διάστημα που δεν είναι
    τρίμηνο ή εβδομάδα: ετικέτα διαστήματος, παιδιά με τονισμένα όσα πέφτουν μέσα του."""
    this_month = strip["this_month"]
    today = datetime.now(ATHENS).strftime("%Y-%m-%d")
    years = [yr["y"] for yr in strip["years"]]
    months = {ym: st for yr in strip["years"] for ym, st in yr["months"]}
    zero = dict.fromkeys(_STRIP_STATUSES, 0)

    def add(sts):
        sts = list(sts)
        return {k: sum(st.get(k, 0) for st in sts) for k in _STRIP_STATUSES}

    def qr(y, q):  # φίλτρο τριμήνου «yyyy-mm:yyyy-mm»
        return f"{y}-{q * 3 - 2:02d}:{y}-{q * 3:02d}"

    def kid(label, f, st, on=False, dim=False, now=False, **extra):
        n = st.get(view, 0)
        return {"label": label, "f": f, "n": n, "dot": view != "unclassified" and bool(st.get("unclassified")),
                "on": on, "dim": dim or not n, "now": now, **extra}

    def kids_years(on=lambda y: False):
        return [kid(y, y, add(st for ym, st in months.items() if ym[:4] == y), on(y), now=y == this_month[:4])
                for y in years]

    def kids_quarters(y):
        return [kid(f"{_QUARTER_NAMES[q - 1]} τρίμ.", qr(y, q),
                    add(months.get(f"{y}-{m:02d}", zero) for m in range(q * 3 - 2, q * 3 + 1)),
                    dim=f"{y}-{q * 3 - 2:02d}" > this_month, now=y == this_month[:4] and (int(this_month[5:]) + 2) // 3 == q)
                for q in range(1, 5)]

    def kids_months(ms, on=lambda ym: False):
        return [kid(_GREEK_MONTHS[int(ym[5:]) - 1].rstrip("."), ym, months.get(ym, zero), on(ym), dim=ym > this_month,
                    now=ym == this_month, ym=ym) for ym in ms]

    day_cache = {}

    def days(ym):
        if ym not in day_cache:
            day_cache[ym] = {d: add([st]) for d, st in sorted((db.day_counts(cid, kind, ym) if cid else {}).items())}
        return day_cache[ym]

    def count(f):  # πλήθος της καρτέλας στο φίλτρο f (έτος, τρίμηνο, μήνας, ημέρα ή διάστημα)· None = όλα
        a, _, b = (f or "").partition(":")
        b = b if _ else a
        if f and not all(re.fullmatch(r"(\d{4}(-\d{2}){0,2})?", x) for x in (a, b)):
            return None  # ελεύθερο κείμενο
        n = 0
        for ym, st in months.items():
            if (a[:7] or "0") <= ym <= (b[:7] or "9") + "~":
                if (len(a) == 10 and ym == a[:7]) or (len(b) == 10 and ym == b[:7]):  # άκρο σε επίπεδο ημέρας
                    n += sum(dst.get(view, 0) for d, dst in days(ym).items() if (a or "0") <= d <= (b or "9") + "~")
                else:
                    n += st.get(view, 0)
        return n

    def weeks(ym):  # [(από, έως)] Δευτέρα–Κυριακή, κομμένες στα όρια του μήνα
        y, m = int(ym[:4]), int(ym[5:7])
        last = calendar.monthrange(y, m)[1]
        out, d = [], 1
        while d <= last:
            e = min(last, d + 6 - date(y, m, d).weekday())
            out.append((f"{ym}-{d:02d}", f"{ym}-{e:02d}"))
            d = e + 1
        return out

    def week_of(d):
        return next(w for w in weeks(d[:7]) if w[0] <= d <= w[1])

    def wlabel(w):
        return f"Εβδ {weeks(w[0][:7]).index(w) + 1}"

    def week_days(w):  # όλες οι ημέρες της εβδομάδας (το πολύ 7), και οι κενές
        dd = days(w[0][:7])
        return {f"{w[0][:8]}{d:02d}": dd.get(f"{w[0][:8]}{d:02d}", zero) for d in range(int(w[0][8:]), int(w[1][8:]) + 1)}

    def kids_weeks(ym):
        dd = days(ym)
        return [kid(wlabel(w), f"{w[0]}:{w[1]}", add(st for d, st in dd.items() if w[0] <= d <= w[1]),
                    dim=w[0] > today, now=w[0] <= today <= w[1], wd=f"{int(w[0][8:])}–{int(w[1][8:])}")
                for w in weeks(ym)]

    def kids_days(dd, on=lambda d: False):
        return [kid(d[8:].lstrip("0"), d, st, on(d), now=d == today, ymd=d,
                    wd=("Δε", "Τρ", "Τε", "Πε", "Πα", "Σα", "Κυ")[datetime.strptime(d, "%Y-%m-%d").weekday()])
                for d, st in dd.items()]

    def step(seq, cur, k):  # γειτονική τιμή στη λίστα seq (ή None)
        i = seq.index(cur) + k if cur in seq else -1
        return seq[i] if 0 <= i < len(seq) else None

    crumbs = [("Όλα", None)]
    prev = nxt = None
    is_range = False
    ym_all = list(months)
    lo, sep, hi = fd.partition(":")
    quarter = next(((lo[:4], q) for q in range(1, 5) if sep and lo[:4] == hi[:4] and fd == qr(lo[:4], q)), None)
    day_like = r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
    week = (lo, hi) if sep and re.fullmatch(day_like, lo) and lo[:7] == hi[:7] and (lo, hi) in weeks(lo[:7]) else None

    def month_crumbs(ym):
        y, m = ym[:4], int(ym[5:7])
        q = (m + 2) // 3
        return [(y, y), (f"{_QUARTER_NAMES[q - 1]} τρίμ.", qr(y, q)), (_GREEK_MONTHS[m - 1].rstrip("."), ym)]

    if re.fullmatch(r"\d{4}", fd):
        crumbs.append((fd, fd))
        prev, nxt = step(years, fd, -1), step(years, fd, 1)
        kids = kids_quarters(fd)
    elif quarter:
        y, q = quarter
        crumbs += [(y, y), (f"{_QUARTER_NAMES[q - 1]} τρίμ.", fd)]
        qs = [qr(yy, qq) for yy in years for qq in range(1, 5)]
        prev, nxt = step(qs, fd, -1), step(qs, fd, 1)
        kids = kids_months([f"{y}-{m:02d}" for m in range(q * 3 - 2, q * 3 + 1)])
    elif re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", fd):
        crumbs += month_crumbs(fd)
        prev, nxt = step(ym_all, fd, -1), step(ym_all, fd, 1)
        kids = kids_weeks(fd)
    elif week:
        crumbs += [*month_crumbs(lo[:7]), (wlabel(week), fd)]
        near = [w for ym in (step(ym_all, lo[:7], -1), lo[:7], step(ym_all, lo[:7], 1)) if ym for w in weeks(ym)]
        prev, nxt = (f"{w[0]}:{w[1]}" if w else None for w in (step(near, week, -1), step(near, week, 1)))
        kids = kids_days(week_days(week))
    elif re.fullmatch(day_like, fd) and fd[8:] <= f"{calendar.monthrange(int(fd[:4]), int(fd[5:7]))[1]:02d}":
        w = week_of(fd)
        crumbs += [*month_crumbs(fd[:7]), (wlabel(w), f"{w[0]}:{w[1]}"), (fd[8:].lstrip("0"), fd)]
        dd = days(fd[:7])  # ‹ ›: προηγούμενη/επόμενη ημέρα του μήνα με παραστατικά
        prev, nxt = max((d for d in dd if d < fd), default=None), min((d for d in dd if d > fd), default=None)
        kids = kids_days(week_days(w), lambda d: d == fd)
    elif sep and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", lo) and lo[:7] == hi[:7] and len(lo) == len(hi) == 10:
        is_range = True
        y, m = lo[:4], int(lo[5:7])
        crumbs += [(y, y), (_GREEK_MONTHS[m - 1].rstrip("."), lo[:7]), (f"{lo[8:].lstrip('0')}–{hi[8:].lstrip('0')}", fd)]
        kids = kids_days(days(lo[:7]), lambda d: lo <= d <= hi)
    elif sep and re.fullmatch(r"\d{4}(-\d{2}){0,2}", lo) and lo[:4] == hi[:4]:
        is_range = True
        a, b = lo[:7], hi[:7]
        mname = lambda ym, d: _GREEK_MONTHS[int(ym[5:7] or d) - 1].rstrip(".")  # noqa: E731
        crumbs += [(lo[:4], lo[:4]), (f"{mname(a, 1)}–{mname(b, 12)}", fd)]
        kids = kids_months([f"{lo[:4]}-{m:02d}" for m in range(1, 13)], lambda ym: a <= ym <= (b + "~"))
    elif fd:  # διάστημα σε διαφορετικά έτη ή ελεύθερο κείμενο
        is_range = True
        crumbs.append((fd.replace(":", " – "), fd))
        kids = kids_years(lambda y: bool(sep) and (lo[:4] or "0") <= y <= (hi[:4] or "9"))
    else:
        kids = kids_years()
    crumbs = [(label, f, count(f)) for label, f in crumbs]
    return {"crumbs": crumbs, "prev": prev, "next": nxt, "kids": kids, "range": is_range}


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
    # Άδεια βιβλία → μόνο η καρτέλα «Ανάκτηση», από 1/1 του έτους.
    empty = not cid or not any(db.count_by_status(cid, k) for k in kinds)
    mode = db.get_setting("sync_mode") or "mark"
    tabs = [("main", "📥 Ανάκτηση")] + ([] if empty else (
        [("past", "🗓️ Ανάκτηση προηγούμενων περιόδων")] if mode == "mark" else []) + [("delete", "🗑️ Διαγραφή")])
    tabs.append(("log", "📜 Ιστορικό συνδέσεων"))
    tab = request.args.get("tab") if request.args.get("tab") in dict(tabs) else "main"
    return render_template(
        "sync.html",
        scope=scope,
        noun=_SYNC_NOUNS[scope],
        strip=_month_strip(cid, kinds),
        tabs=tabs,
        tab=tab,
        mode=mode,
        logs=mydata_logs(cid) if tab == "log" else [],
        log_keep=_log_keep(),
        mark_pending=bool(cid) and not all(_mark_ready(cid, k) for k in kinds),
        range_date_from=request.args.get("date_from", "").strip()
        or (f"{now.year - 1}-01-01" if tab == "past" else now.strftime("%Y-01-01" if empty else "%Y-%m-01")),
        range_date_to=request.args.get("date_to", "").strip() or (f"{now.year - 1}-12-31" if tab == "past" else today),
        date_range=min(ranges, key=end) if ranges else None,
        # «Νέα παραστατικά από MARK» ανά βιβλίο: (ετικέτα, Αρχή dd/mm/yyyy ή None, W).
        mark_info=[(("Έσοδα" if k == "income" else "Έξοδα"),
                    _gr(db.get_setting(_start_key(k, cid))) if cid and _mark_ready(cid, k) else None,
                    db.get_setting(_wm_key(k, _MARK_ENDPOINTS[k][0], cid)) if cid else None)
                   for k in kinds],
        # Ανά βιβλίο: τελευταία ανάκτηση (τύπος + πότε), MARK myDATA (W: ως εκεί ελέγχθηκε, και στα δύο endpoints
        # των εξόδων) και MARK τελευταίου παραστατικού (τα MARK του myDATA είναι χρονολογικά).
        # Παλιές εγκαταστάσεις χωρίς last_sync: μόνο το τελευταίο διάστημα.
        last=[(label, (json.loads(db.get_setting(_last_key(k, cid)) or "null") if cid else None)
               or ({"kind": f"Περίοδος {r}", "at": None} if (r := db.get_setting(_range_key(k))) else None),
               min(int(db.get_setting(_wm_key(k, ep, cid))) for ep in _MARK_ENDPOINTS[k])
               if cid and _mark_ready(cid, k) else None,
               max((r["mark"] for r in db.get_documents(cid, k) if (r["mark"] or "").isdigit()), key=int, default=None)
               if cid else None)
              for k, label in (("income", "Έσοδα"), ("expense", "Έξοδα"))],
    )


def _book_list(items: list, args, view: str) -> tuple[list, str, str, dict]:
    """Φίλτρα + ταξινόμηση λίστας βιβλίου εσόδων/εξόδων από τα query args.
    Κοινό για τις λίστες και για το προηγούμενο/επόμενο της αναλυτικής προβολής."""
    sort = args.get("sort", "date")
    direction = args.get("dir", "desc" if view in ("classified", "confirmed") else "asc")
    filters = {k: args.get(f"f_{k}", "").strip() for k in ("mark", "date", "issuer", "type")}
    fm, fi, ft = filters["mark"], filters["issuer"].lower(), filters["type"]

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
        if fi and fi not in ((inv.issuer_name or "") + " " + (inv.issuer_vat or "")).lower():
            return False
        return not (ft and ft not in (inv.invoice_type or ""))

    def sort_key(inv):
        if sort == "name":
            return (inv.issuer_name or inv.issuer_vat or "").lower()
        if sort == "total":
            return inv.total_gross or 0.0
        # default: ημερομηνία (ISO μορφή yyyy-mm-dd, ταξινομείται σωστά ως string)
        return inv.issue_date or ""

    if any(filters.values()):
        items = [i for i in items if _match(i)]
    return sorted(items, key=sort_key, reverse=direction == "desc"), sort, direction, filters


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
    cid = _active_company_id()
    names = db.load_names()  # κοινός χάρτης vat→επωνυμία (μία φορά)
    buckets = {v: [] for v in _EXPENSE_VIEWS}
    for row in db.get_documents(cid, "expense") if cid else []:
        buckets.setdefault(row["status"], []).append(_row_to_invoice(row, names))

    counts = {v: len(buckets[v]) for v in _EXPENSE_VIEWS}
    items, sort, direction, filters = _book_list(buckets.get(view, []), request.args, view)
    # «⏸ Αναμένουν επιβεβαίωση»: χαρακτηρισμένα προμηθευτών που ο αυτόματος κύκλος δεν στέλνει.
    review = db.review_vats(cid)
    review_count = sum(1 for i in buckets["classified"] if i.issuer_vat in review)
    review_only = view == "classified" and request.args.get("review") == "1"
    if review_only:
        items = [i for i in items if i.issuer_vat in review]
    total = len(items)
    page_items, page, total_pages = paginate(items, request.args.get("page"))
    if view == "classified":
        for inv in page_items:
            inv.check_errors = (
                _line_cover_errors(inv, db.get_local_classification(inv.doc_id))
                if inv.local_action == "classify" and not inv.cls_post_mode else []
            )
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
        period=_period_nav(_month_strip(cid, ("expense",)), filters["date"], view, cid, "expense"),
        per_page=PER_PAGE,
        filters=filters,
        review=review,
        review_count=review_count,
        review_only=review_only,
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
    # Κουμπί «Ιστορικό χαρακτηρισμών»: μόνο όταν υπάρχει προηγούμενη εγγραφή (στον κουβά).
    cls_hist = db.cls_history(cid, mark)
    if all(r["active"] for r in cls_hist):
        cls_hist = []
    # URL επιστροφής στη λίστα, διατηρώντας ταξινόμηση/σελίδα/φίλτρα (fallback: view).
    # Έσοδα: ίδια προβολή, μόνο για ανάγνωση (χωρίς ενέργειες χαρακτηρισμού/απόρριψης).
    is_income = row.get("kind") == "income"
    back = request.args.get("back") or url_for("income" if is_income else "invoices", view=row["status"])
    # Προηγούμενο/επόμενο: ίδιο βιβλίο και κατάσταση. Αν ήρθαμε από τη λίστα του βιβλίου,
    # ακολουθεί τα φίλτρα/ταξινόμησή της (από το back URL)· αλλιώς σειρά ημ/νίας.
    names = db.load_names()
    siblings = sorted(
        (_row_to_invoice(r, names) for r in db.get_documents(cid, row["kind"], [row["status"]])),
        key=lambda i: (i.issue_date or "", i.mark),
    )
    bu = urlparse(back)
    if bu.path == url_for("income" if is_income else "invoices"):
        args = dict(parse_qsl(bu.query))
        listed = _book_list(siblings, args, args.get("view") or row["status"])[0]
        if any(i.mark == mark for i in listed):  # αλλιώς (π.χ. άλλαξε κατάσταση) → όλα
            siblings = listed
    pos = next(i for i, r in enumerate(siblings) if r.mark == mark)
    return render_template(
        "document.html",
        inv=inv,
        pay_amount=db.get_setting("pay_amount", "1") == "1",
        is_income=is_income,
        can_cancel=cancel_allowed(),
        status=row["status"],
        local_action=row.get("local_action") or "classify",
        source=row.get("source") or "rest",
        classification_mark=row.get("classification_mark"),
        cls_history=cls_hist,
        type_desc=INVOICE_TYPE_NAMES.get(inv.invoice_type or ""),
        is_credit=inv.invoice_type in CREDIT_INVOICE_TYPES,
        vat_rates=VAT_CATEGORY_RATES,
        tax_types=TAX_TYPES,
        tax_categories=TAX_CATEGORIES,
        back=back,
        prev_mark=siblings[pos - 1].mark if pos > 0 else None,
        next_mark=siblings[pos + 1].mark if pos + 1 < len(siblings) else None,
        pos=pos + 1,
        siblings_total=len(siblings),
        line_cls=line_cls,
        doc_cls=doc_cls,
        flags=flags,
        categories=INCOME_CATEGORIES if is_income else EXPENSE_CATEGORIES,
        types=INCOME_TYPES if is_income else EXPENSE_TYPES,
        vat_types=VAT_TYPES,
        xml_tree=(xml_tree := _xml_tree(row.get("raw_xml") or _local_xml(row))),
        xml_links=_xml_links(xml_tree),
        payments=_xml_payments(xml_tree),
        has_xml=bool(xml_tree),
        local_xml=bool(xml_tree) and not row.get("raw_xml"),
        # {γραμμή: κατηγορία εξαίρεσης ΦΠΑ}· τοπικό draft (χωρίς XML) → από τις γραμμές του draft.
        exemptions=vat_return._parse_exempt(row)[1] or {
            n: ln.get("vat_exemption") for n, ln in
            enumerate(json.loads(row.get("draft_json") or "{}").get("lines") or [], start=1) if ln},
        exemption_labels=XML_EXTRA_CODES["vatExemptionCategory"],
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
        "20": "Μεταφορές - Ταχυμεταφορές",
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
    # Κώδικας ΦΠΑ ν. 5144/2024 (νέα αρίθμηση άρθρων, όπως στις περιγραφές της ΑΑΔΕ).
    "vatExemptionCategory": {
        "1": "Άρθρα 2 και 3 ΚΦΠΑ", "2": "Άρθρο 5 ΚΦΠΑ", "3": "Άρθρο 17 ΚΦΠΑ", "4": "Άρθρο 18 ΚΦΠΑ",
        "5": "Άρθρο 21 ΚΦΠΑ", "6": "Άρθρο 24 ΚΦΠΑ", "7": "Άρθρο 27 ΚΦΠΑ", "8": "Άρθρο 29 ΚΦΠΑ",
        "9": "Άρθρο 30 ΚΦΠΑ", "10": "Άρθρο 31 ΚΦΠΑ", "11": "Άρθρο 32 ΚΦΠΑ",
        "12": "Άρθρο 32 ΚΦΠΑ (πλοία ανοικτής θαλάσσης)", "13": "Άρθρο 32.1.γ ΚΦΠΑ (πλοία ανοικτής θαλάσσης)",
        "14": "Άρθρο 33 ΚΦΠΑ", "15": "Άρθρο 44 ΚΦΠΑ", "16": "Άρθρο 45 ΚΦΠΑ", "17": "Άρθρο 47 ΚΦΠΑ",
        "18": "Άρθρο 48 ΚΦΠΑ", "19": "Άρθρο 54 ΚΦΠΑ", "20": "ΦΠΑ εμπεριεχόμενος — άρθρο 50 ΚΦΠΑ",
        "21": "ΦΠΑ εμπεριεχόμενος — άρθρο 51 ΚΦΠΑ", "22": "ΦΠΑ εμπεριεχόμενος — άρθρο 52 ΚΦΠΑ",
        "23": "ΦΠΑ εμπεριεχόμενος — άρθρο 53 ΚΦΠΑ", "24": "Άρθρο 8 ΚΦΠΑ", "25": "ΠΟΛ 1029/1995",
        "26": "ΠΟΛ 1167/2015", "27": "Λοιπές εξαιρέσεις ΦΠΑ", "28": "Άρθρο 29 περ. β' παρ. 1 ΚΦΠΑ (Tax Free)",
        "29": "Άρθρο 56 ΚΦΠΑ (OSS μη ενωσιακό)", "30": "Άρθρο 57 ΚΦΠΑ (OSS ενωσιακό)",
        "31": "Άρθρο 58 ΚΦΠΑ (IOSS)",
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
        children = [node(c, tag, kids) for c in el]
        # Εκδότης/λήπτης χωρίς επωνυμία στο XML (συνήθως ελληνικό ΑΦΜ): από τους συναλλασσόμενους, ως hint στο ΑΦΜ.
        if tag in ("issuer", "counterpart") and not kids.get("name") and (vat := kids.get("vatNumber")):
            name = db.get_supplier_name(vat)
            own = (get_active_company() or {}) if has_request_context() else {}
            if not name and vat == str(own.get("AADE_VAT_NUMBER") or ""):  # η ίδια η εταιρεία
                name = own.get("company_name")
            for c in children:
                if c["tag"] == "vatNumber" and name:
                    c["hint"] = name
        return {"tag": tag, "label": label, "value": value, "children": children}

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    if root.tag.endswith("}InvoicesDoc") and len(root):  # τοπικό πρόχειρο: το invoice μέσα στο InvoicesDoc
        root = root[0]
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


def _xml_payments(tree: list[dict]) -> list[dict]:
    """Τρόποι πληρωμής του XML ({method, amount}) από το δέντρο του _xml_tree, για το header."""
    out = []
    for pm in (n for n in tree if n["tag"] == "paymentMethods"):
        for d in pm["children"]:
            kids = {c["tag"]: c["value"] for c in d["children"]}
            if kids.get("type"):
                out.append({"method": kids["type"].split(" · ", 1)[-1], "amount": kids.get("amount")})
    return out


def _local_xml(row: dict) -> str:
    """Τοπικό πρόχειρο που δεν έχει σταλεί: το XML που θα στείλει η «Αποστολή στο myDATA»."""
    if row.get("local_action") != "create" or not row.get("draft_json"):
        return ""
    try:
        return draft_xml(json.loads(row["draft_json"]), get_own_vat())
    except Exception:  # χαλασμένο/παλιό draft: η σελίδα φορτώνει χωρίς XML
        return ""


@app.route("/document/<mark>/xml")
def document_xml(mark):
    """Λήψη του XML του παραστατικού όπως το έδωσε το myDATA (ή, για πρόχειρο, όπως θα σταλεί)."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    xml = row and (row.get("raw_xml") or _local_xml(row))
    if not xml:
        flash("Δεν υπάρχει αποθηκευμένο XML — κάνε νέα ανάκτηση του διαστήματος.", "error")
        return redirect(url_for("document", mark=mark))
    return Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{mark}.xml"'})


@app.route("/document/<mark>/pdf")
def document_pdf(mark):
    """Το URL λήψης του παραστατικού: αν είναι PDF, προβολή στον browser (ο πάροχος το στέλνει
    συνήθως ως attachment)· αλλιώς ανακατεύθυνση στο αρχικό URL."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    url = _xml_links(_xml_tree(row.get("raw_xml"))).get("download") if row else None
    if not url:
        return redirect(url_for("document", mark=mark))
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return redirect(url)
    if not r.content.startswith(b"%PDF"):
        return redirect(url)
    return Response(r.content, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{mark}.pdf"'})


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
            no_vat = r.get("category") == NO_VAT_RIGHT or r.get("vat_type") == "none"
            pi_vat[g["vat_category"]] = "" if no_vat else (r.get("vat_type") or "VAT_361")
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
        is_credit=inv.invoice_type in CREDIT_INVOICE_TYPES,
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
    if inv.lines:
        errors = _line_cover_errors(inv, classifications)
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


def _line_cover_errors(inv, classifications: list[dict]) -> list[str]:
    """Χαρακτηρισμός ΑΝΑ ΓΡΑΜΜΗ που θα απέρριπτε το myDATA (303/304/306): γραμμές χωρίς
    χαρακτηρισμό ή ανύπαρκτες, άθροισμα Ε3 εκτός [καθαρή, μικτή]. Χωρίς γραμμές: [] (άγνωστες)."""
    inv_lines = inv.lines or []
    if not inv_lines:
        return []
    by_line: dict[int, list[dict]] = {}
    for c in classifications:
        by_line.setdefault(c["line_number"], []).append(c)
    errors = []
    stray = sorted(set(by_line) - {ln.line_number for ln in inv_lines}, key=str)
    if stray:
        errors.append("γραμμές που δεν υπάρχουν στο παραστατικό: " + ", ".join(map(str, stray)))
    for ln in inv_lines:
        net_i = round(ln.net_value or 0, 2)
        gross_i = round(net_i + (ln.vat_amount or 0), 2)
        entries = by_line.get(ln.line_number, [])
        if not entries:
            errors.append(f"η γραμμή {ln.line_number} (καθαρή {net_i:.2f} €) δεν χαρακτηρίστηκε")
            continue
        e3_sum = round(sum(e["amount"] for e in entries if not (e["classification_type"] or "").startswith("VAT_")), 2)
        # Έλεγχος αθροίσματος μόνο όταν ξέρουμε την αξία της γραμμής (net>0).
        if gross_i > 0 and not (net_i - 0.01 <= e3_sum <= gross_i + 0.01):
            errors.append(
                f"γραμμή {ln.line_number}: άθροισμα E3 {e3_sum:.2f} € εκτός εύρους {net_i:.2f}–{gross_i:.2f} €"
            )
    return errors


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
    χαρακτηρισμός γίνεται με την παράμετρο postPerInvoice). Τρόπος (bulk_post_mode):
    "auto" = συγκεντρωτικά με περισσότερες από μία γραμμές, ανά γραμμή με μία·
    "invoice" = πάντα συγκεντρωτικά· "line" = πάντα ανά γραμμή· "rule" = όπως ο τελευταίος
    χαρακτηρισμός του συναλλασσόμενου (db.get_rule_post_mode).
    """
    marks = request.form.getlist("marks")
    ctype = request.form.get("bulk_type", "")
    ccat = request.form.get("bulk_category", "")
    vat_type = request.form.get("bulk_vat_type", "auto")
    mode = request.form.get("bulk_post_mode", "auto")
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
    if mode not in ("auto", "invoice", "line", "rule"):
        flash("Μη έγκυρος τρόπος χαρακτηρισμού.", "error")
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

        # Συνδυασμός ανά γραμμή —ή ομάδα ΦΠΑ, συγκεντρωτικά—: (type, category, vat). Με «πρόταση
        # ανά συναλλασσόμενο» παίρνει την πρόταση της κατηγορίας ΦΠΑ της, αλλιώς τη βασική του.
        if mode == "rule":
            post_mode = db.get_rule_post_mode(cid, inv.issuer_vat)
        else:
            post_mode = len(inv.lines) > 1 if mode == "auto" else mode == "invoice"
        post_mode = int(bool(inv.lines and post_mode))
        if use_rules:
            default = rules.get(inv.issuer_vat or "") or {}
            pats = _patterns_for(inv.issuer_vat)
        else:
            default, pats = {"type": ctype, "category": ccat, "vat_type": "" if vat_type == "auto" else vat_type}, {}
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

        # Επίσημοι συνδυασμοί ΑΑΔΕ για τον τύπο του παραστατικού (αλλιώς απόρριψη στην αποστολή).
        bad = sorted({(c, t) for t, c in pairs if not _combo_allowed(inv.invoice_type, c, t)})
        if bad:
            failed.append(
                f"{mark}: ο συνδυασμός {' , '.join(f'{c} / {t}' for c, t in bad)} δεν επιτρέπεται "
                f"από την ΑΑΔΕ για τον τύπο {inv.invoice_type}"
            )
            continue

        if post_mode:  # συγκεντρωτικά (postPerInvoice)
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
    """Ακύρωση τελευταίου (τοπικού, μη απεσταλμένου) χαρακτηρισμού: βλ. db.unclassify.
    Οι «Νέες εγγραφές» δεν αγγίζονται — διαγράφονται από τη σελίδα τους."""
    cid = _active_company_id()
    marks = request.form.getlist("marks")
    if not marks:
        flash("Δεν επιλέχθηκε κανένα παραστατικό.", "error")
        return redirect(url_for("invoices", view="classified"))
    n = db.unclassify(cid, marks)
    skipped = len(marks) - n
    flash(f"↩ Ακυρώθηκε ο τελευταίος χαρακτηρισμός σε {n} παραστατικά (τα ήδη απεσταλμένα γύρισαν στον προηγούμενο χαρακτηρισμό τους)."
          + (f" {skipped} δεν άλλαξαν (π.χ. «Νέες εγγραφές»)." if skipped else ""), "ok")
    return redirect(url_for("invoices", view="unclassified" if n else "classified"))


@app.route("/send", methods=["POST"])
def send():
    """Μαζική αποστολή στο myDATA των ΕΠΙΛΕΓΜΕΝΩΝ τοπικά χαρακτηρισμένων (χωρίς επιλογή: τίποτα).
    Επιτυχία ανά παραστατικό → κατάσταση «Απεσταλμένο»."""
    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("invoices"))
    if not send_allowed():
        flash(SEND_DISABLED_MSG, "error")
        return redirect(url_for("invoices", view="classified"))

    selected = set(request.form.getlist("marks"))
    if not selected:
        flash("Επίλεξε τα παραστατικά που θα σταλούν στο myDATA.", "error")
        return redirect(url_for("invoices", view="classified"))
    rows = [r for r in db.get_documents(cid, "expense", ["classified"]) if r["mark"] in selected]
    if not rows:
        flash("Δεν υπάρχουν χαρακτηρισμένα παραστατικά για αποστολή.", "error")
        return redirect(url_for("invoices", view="classified"))

    def _fail_flash(items):
        for mark, text, fixable in items:
            fix = Markup(' <a href="{}">Διόρθωση</a>').format(url_for("classify", mark=mark)) if fixable else ""
            flash(Markup("Αποτυχία - {}: {}").format(mark, text) + fix, "error")

    # 1ο πέρασμα: όλοι οι τοπικοί έλεγχοι. Έστω ένα λάθος → δεν στέλνεται ΚΑΝΕΝΑ.
    # (Απόρριψη από το ίδιο το myDATA στο 2ο πέρασμα δεν αναιρεί όσα στάλθηκαν ήδη.)
    invalid, to_send = [], {}
    for row in rows:
        mark = row["mark"]
        action = row.get("local_action") or "classify"
        if action in ("reject", "cancel") and not cancel_allowed():
            # η άδεια μπορεί να αφαιρέθηκε αφού μπήκε στην ουρά
            invalid.append((mark, CANCEL_DISABLED_MSG, False))
            continue
        flag = {"create": "allow_new", "reject": None, "cancel": None}.get(action, "allow_classify")
        if flag and not get_active_company().get(flag):
            invalid.append((mark, _GATED[flag][1], False))
            continue
        if action not in ("reject", "cancel", "create"):  # classify / correct
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
                invalid.append((mark, "λείπει ο τοπικός χαρακτηρισμός", True))
                continue
            # Ίδιος έλεγχος με την ΑΑΔΕ (ο συγκεντρωτικός ελέγχεται στην αποθήκευση).
            errors = [] if row.get("cls_post_mode") else _line_cover_errors(_row_to_invoice(row), classifications)
            if errors:
                invalid.append((mark, " · ".join(errors), True))
                continue
            to_send[mark] = classifications
    if invalid:
        flash(f"⛔ Η αποστολή ακυρώθηκε: {len(invalid)} από {len(rows)} παραστατικά έχουν λάθη — δεν στάλθηκε "
              "κανένα. Διόρθωσέ τα και ξαναδοκίμασε.", "error")
        _fail_flash(invalid)
        return redirect(url_for("invoices", view="classified"))

    # 2ο πέρασμα: αποστολή. Client ανά ενέργεια: λογιστής ή εταιρεία (Παράμετροι → Λογιστής).
    ok, failed = [], []
    for row in rows:
        mark = row["mark"]
        action = row.get("local_action") or "classify"
        try:
            client = get_client(send=action if action in ("reject", "cancel", "create") else "classify")
            if action == "reject":
                result = client.reject_invoice(mark)
            elif action == "cancel":
                result = client.cancel_invoice(mark)
            elif action == "create":
                result = client.send_invoice(draft_xml(json.loads(row.get("draft_json") or "{}"), get_own_vat()))
            else:  # classify / correct
                classifications = to_send[mark]
                result = client.send_expenses_classification(
                    mark, classifications, post_per_invoice=bool(row.get("cls_post_mode"))
                )
        except MyDataError as e:
            failed.append((mark, str(e), action == "classify"))
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
            failed.append((mark, "; ".join(result["errors"] or [str(result)]), action == "classify"))

    if ok:
        flash(
            f"✔ Στάλθηκαν στο myDATA {len(ok)} παραστατικά. Η επιβεβαίωση γίνεται αυτόματα με την επόμενη "
            "«Ανάκτηση» (το myDATA αργεί έως ~8 ώρες).",
            "ok",
        )
    _fail_flash(failed)

    return redirect(url_for("invoices", view="sent" if ok else "classified"))


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
    cid = _active_company_id()
    names = db.load_names()
    buckets = {v: [] for v in _INCOME_VIEWS}
    for row in db.get_documents(cid, "income") if cid else []:
        buckets.setdefault(row["status"], []).append(_row_to_invoice(row, names))

    counts = {v: len(buckets[v]) for v in _INCOME_VIEWS}
    items, sort, direction, filters = _book_list(buckets.get(view, []), request.args, view)
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
        period=_period_nav(_month_strip(cid, ("income",)), filters["date"], view, cid, "income"),
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
                         "cpn": short_counterparty_name(d["counterparty_name"]) or ""})
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
    ("yearly", "Συνοπτικό βιβλίο", True, "Όπως στο myDATA. Το καθαρό κέρδος μετρά πάντα μόνο τη μεταβολή τους (έναρξης − λήξης)."),
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
    (μηνιαίος) ή ανά τρίμηνο (τριμηνιαίος). partial = περίοδος που δεν έχει κλείσει· href = η δήλωση Φ2 της περιόδου."""
    if (get_active_company() or {}).get("vat_period") == "q":
        return [dict(rows[i]["vat_quarter"], short=f"{_QUARTER_NAMES[i // 3]} τριμ.",
                     title=rows[i]["vat_quarter"]["span"], href=url_for("reports_vat", year=year, period=f"q{i // 3 + 1}"))
                for i in range(0, len(rows), 3)]
    current = year == str(now.year)
    return [dict(r["vat_month"], short=_GREEK_MONTHS[i], title=r["label"],
                 partial=current and i + 1 == now.month, href=url_for("reports_vat", year=year, period=f"m{i + 1}"))
            for i, r in enumerate(rows)]


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
    # Διάγραμμα: σωρευτική πορεία όπως στην πρόβλεψη (υπόλοιπο εσ. − εξ.), με το περσινό ίδιο διάστημα.
    prev_y = str(int(year) - 1)
    prev_t = {k: _yearly_totals(db.yearly_documents(cid, k, _yearly_statuses(k, unc), prev_y), stock_in("yearly"))
              for k in ("income", "expense")}
    prev_bal = [prev_t["income"][m]["net"] - prev_t["expense"][m]["net"] for m in range(1, 13)]
    has_prev = any(prev_t[k][m]["net"] for k in prev_t for m in range(1, 13))
    cum = _forecast_chart([r["income"]["net"] for r in rows], [r["expense"]["net"] for r in rows],
                          [r["balance"] for r in rows], prev_bal, None, len(rows), res_label="Υπόλοιπο")
    # Όλες οι στήλες, πάντα — ίδιες με το συνοπτικό βιβλίο του myDATA.
    return render_template(
        "reports_yearly.html", year=year, years=years, rows=rows, cols=YEARLY_TABLE_COLS, pl=pl,
        stock_chk=_stock_check(cid, year),
        unc=unc, unc_count=unc_count, per_q=per_q, table_rows=table_rows,
        income_total=total("income"), expense_total=total("expense"),
        chart=_yearly_chart(rows), cum=cum, has_prev=has_prev, prev_year=prev_y,
        current_month=now.month if year == str(now.year) else None,
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


def _stock_check(cid: int | None, year: str, exp_docs: list[dict] | None = None) -> dict:
    """Αποθέματα μιας χρήσης για το κόστος πωληθέντων. Έναρξης = η εγγραφή 2.13 της χρήσης ή, αν λείπει, τα
    αποθέματα λήξης (2.14) της προηγούμενης· λήξης = η εγγραφή 2.14 της χρήσης. used = υπάρχουν και τα δύο.
    unsure = η χρήση έχει κόστος πωληθέντων (αγορές 2.1/2.2 ή αποθέματα) χωρίς και τα δύο: το αποτέλεσμα είναι
    επισφαλές (π.χ. απόθεμα που αγοράστηκε για την επόμενη χρήση)· why = τι λείπει."""
    load = lambda y: db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, y)  # noqa: E731
    st, pst = _pl([], load(year) if exp_docs is None else exp_docs), _pl([], load(str(int(year) - 1)))
    s_open, src = ((st["opening_total"], "own") if st["opening"] else
                   (pst["closing_total"], "prev") if pst["closing"] else (0.0, None))
    used = bool(src and st["closing"])
    why = None if used else (
        "χωρίς εγγραφές αποθεμάτων" if not src and not st["closing"] else
        "ανοιχτή χρήση: τα αποθέματα λήξης δηλώνονται στο κλείσιμο" if src and year == str(datetime.now(ATHENS).year) else
        "χωρίς αποθέματα λήξης" if src else f"χωρίς αποθέματα έναρξης (ούτε λήξης στο {int(year) - 1})")
    return {"open": round(s_open, 2), "src": src, "close": st["closing_total"], "has_close": bool(st["closing"]),
            "used": used, "why": why, "unsure": not used and bool(src or st["closing"] or st["purchases"])}


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
    """Σελίδα «Ανάλυση Ε3»: έσοδα και έξοδα του έτους ανά χαρακτηρισμό Ε3."""
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
        "yearly_e3_print.html" if request.args.get("print") else "reports_yearly_e3.html",
        year=year, pl=pl, tax=_year_tax(cid, int(year), pl["profit"], withheld), stock_chk=_stock_check(cid, year, exp_docs),
        years=sorted(set(db.document_years(cid)) | {year}, reverse=True),
        back=_safe_back(request.args.get("back")) or url_for("dashboard"),  # από όπου άνοιξε (συνοπτικό βιβλίο / πίνακας ελέγχου)
        inc_net=net(inc_docs), exp_net=net(exp_docs), now_str=datetime.now().strftime("%d/%m/%Y %H:%M"),
        links=not request.args.get("print"),  # ποσά → modal παραστατικών (όχι στο PDF)
    )


@app.route("/reports/yearly/e3/docs")
def reports_yearly_e3_docs():
    """Τμήμα HTML για το modal της ανάλυσης Ε3: τα παραστατικά ενός λογαριασμού (type, category) του έτους,
    με την καθαρή αξία / ΦΠΑ τους και το ποσό Ε3 που φέρνει το καθένα (ίδιος υπολογισμός με το _e3_breakdown).
    whole = όλο το παραστατικό σε αυτόν τον λογαριασμό (ποσό = καθαρή αξία, ή μικτή όταν ο ΦΠΑ μπαίνει στο Ε3)."""
    year, book = request.args.get("year", ""), request.args.get("book")
    typ, cat = request.args.get("type", ""), request.args.get("category", "")
    if not year.isdigit() or book not in ("income", "expense"):
        return "Μη έγκυρη επιλογή.", 400
    statuses = _INCOME_CLASSIFIED_STATUSES if book == "income" else _EXPENSE_CLASSIFIED_STATUSES
    rows, total = [], {"net": 0.0, "vat": 0.0, "part": 0.0}
    for d in db.period_documents(_active_company_id(), book, f"{year}-01-01", f"{year}-12-31"):
        if d["status"] not in statuses:
            continue
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        hits = [e.get("amount") or 0 for e in json.loads(d["cls_json"] or "[]")
                if not (e.get("type") or "").startswith("VAT_") and (e.get("type") or "") == typ
                and (e.get("category") or "") == cat]
        if not hits:  # και με μηδενικό ποσό: μετρά στο «παραστατικά» της ανάλυσης
            continue
        part = sum(hits)
        net, vat, part = sign * (d["total_net"] or 0.0), sign * (d["total_vat"] or 0.0), sign * part
        rows.append(dict(d, net=net, vat=vat, part=round(part, 2), credit=sign < 0,
                         whole=abs(part - net) < 0.015 or abs(part - net - vat) < 0.015))
        for k, v in (("net", net), ("vat", vat), ("part", part)):
            total[k] += v
    # Σελιδοποίηση της λίστας (τα σύνολα από όλα τα παραστατικά)· q = οι παράμετροι του modal για τον pager.
    page_docs, page, pages = paginate(rows, request.args.get("page"))
    q = {k: v for k, v in request.args.items() if k != "page"}
    return render_template(
        "_e3_docs.html", docs=page_docs, n_docs=len(rows), page=page, pages=pages, q=q, total={k: round(v, 2) for k, v in total.items()}, book=book, year=year,
        typ=typ, cat=cat, type_label=_CLASSIFICATION_NAMES.get(typ, ""),
        category_label=_CLASSIFICATION_CATEGORY_NAMES.get(cat, ""), type_names=INVOICE_TYPE_NAMES,
    )


# ---------------------------------------------------------------------------
# Σύγκριση ετών: το τρέχον έτος απέναντι στα προηγούμενα, στο ίδιο χρονικό σημείο
# (ολοκληρωμένοι μήνες: Ιαν. έως τον προηγούμενο μήνα, κάθε έτους).
# ---------------------------------------------------------------------------
_CMP_YEARS = 6  # τρέχον + έως 5 προηγούμενα (όσα έχουν παραστατικά)
# Μεγέθη του γραφήματος αθροιστικών ποσών (κλειδί → ετικέτα άξονα).
_CMP_TOTALS = {"income": "Έσοδα", "cogs": "Κόστος πωληθέντων", "opex": "Έξοδα χρήσης",
               "expense": "Σύνολο εξόδων", "profit": "Καθαρό κέρδος"}
# Χρήση 2024: αχαρακτήριστα έξοδα κατανέμονται με τις αναλογίες Ε3 (αγορές αποθεμάτων / έξοδα) των 2025–2026.
_CMP_FILL_YEAR, _CMP_FILL_FROM = "2024", ("2025", "2026")


def _cmp_unclassified(cid: int | None) -> list[dict]:
    """Αχαρακτήριστα έξοδα του _CMP_FILL_YEAR με χαρακτηρισμό κατά τα ποσά Ε3 των _CMP_FILL_FROM ανά λογαριασμό
    (αγορές 2.1/2.2 και έξοδα χρήσης· όχι αποθέματα, πάγια, αποσβέσεις). Χωρίς βάση: κανένα."""
    base = [d for y in _CMP_FILL_FROM for d in db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, y)]
    pl = _pl([], base)
    w = [g for g in pl["purchases"] + pl["expense"] if g["amount"] > 0 and not g["depreciation"]]
    total = sum(g["amount"] for g in w)
    if not total:
        return []
    return [dict(d, cls_json=json.dumps([{"type": g["type"], "category": g["category"],
                                          "amount": round((d["total_net"] or 0) * g["amount"] / total, 2)} for g in w]))
            for d in db.yearly_documents(cid, "expense", ["unclassified"], _CMP_FILL_YEAR)]


def _counterparty_totals(docs: list[dict], stock: bool = False) -> dict:
    """{ΑΦΜ: {name, vat, amount, n}}: καθαρή αξία ανά συναλλασσόμενο (πιστωτικά αρνητικά).
    stock=True αφαιρεί τα αποθέματα 2.13/2.14 — δεν είναι αγορά από τον συναλλασσόμενο."""
    out: dict = {}
    for d in docs:
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        net = (d["total_net"] or 0) - (_stock_amount(d["cls_json"]) if stock else 0)
        if not net:  # μόνο αποθέματα: δεν είναι αγορά από τον συναλλασσόμενο
            continue
        t = out.setdefault(d["counterparty_vat"], {"name": d["counterparty_name"] or d["counterparty_vat"] or "—",
                                                   "vat": d["counterparty_vat"], "amount": 0.0, "n": 0})
        t["amount"] += sign * net
        t["n"] += 1
    return out


def _cmp_delta(cur: float, prev: float | None) -> dict | None:
    """Μεταβολή έναντι πέρσι: v = διαφορά, pct = % επί του |πέρσι| (None χωρίς βάση)."""
    if prev is None:
        return None
    return {"v": round(cur - prev, 2), "pct": round((cur - prev) / abs(prev) * 100, 1) if prev else None}


def _e3_groups(rows: list[dict]) -> dict:
    """{λογαριασμός Ε3 (E3_561…) ή κατηγορία χωρίς τύπο: ποσό} από γραμμές του _e3_breakdown."""
    out: dict = {}
    for g in rows:
        # Αποθέματα (2.13 / 2.14) ανά κατηγορία: χωριστά από τυχόν κοινό λογαριασμό με αγορές.
        key = "_".join(g["type"].split("_")[:2]) if g["type"] and not g["stock"] else g["category"]
        out[key] = out.get(key, 0.0) + g["amount"]
    return out


def _cmp_axis(lo: float, hi: float, base: float, top: float):
    """Άξονας y με στρογγυλά βήματα που καλύπτει [lo, hi] (και το 0): (y(v), ticks)."""
    step = _nice_step(max(hi, 0) - min(lo, 0))
    ymin, ymax = (min(lo, 0) // step) * step, -(-max(hi, 0) // step) * step
    ymax = ymax if ymax > ymin else ymin + step
    y = lambda v: round(base - (v - ymin) / (ymax - ymin) * (base - top), 1)  # noqa: E731
    n = int(round((ymax - ymin) / step))
    return y, [{"v": ymin + step * k, "y": y(ymin + step * k)} for k in range(n + 1)]


def _smooth(pts: list[tuple[float, float]]) -> str:
    """Path SVG με ομαλή καμπύλη από σημεία (x, y σε pixels): μονότονη κυβική Hermite (Fritsch–Carlson) ως bezier —
    περνά από όλα τα σημεία χωρίς υπερβάσεις (σωρευτικό που μένει ίδιο δεν «φουσκώνει»)."""
    n = len(pts)
    if n < 3:
        return "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    d = [(pts[i + 1][1] - pts[i][1]) / ((pts[i + 1][0] - pts[i][0]) or 1e-9) for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2 for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        if a * a + b * b > 9:  # περιορισμός κλίσεων: διατηρεί τη μονοτονία
            t = 3 / (a * a + b * b) ** 0.5
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    out = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(n - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        h = (x1 - x0) / 3
        out.append(f"C{x0 + h:.1f},{y0 + m[i] * h:.1f} {x1 - h:.1f},{y1 - m[i + 1] * h:.1f} {x1:.1f},{y1:.1f}")
    return "".join(out)


def _cmp_lines(years: list[str], series: list[list[float]], proj: list[float], ytd: float, done: int, today: float,
               unsure: list[bool] | None = None) -> dict:
    """Γεωμετρία SVG (viewBox 760×160), άξονας 12 μηνών (σημείο i = τέλος του μήνα i+1), ένα σωρευτικό μέγεθος:
    ανά έτος μία καμπύλη (τα προηγούμενα όλο το έτος, το τρέχον στους done ολοκληρωμένους μήνες + σημείο ytd στη
    θέση today) και η προβολή του τρέχοντος (proj, 12 τιμές) από σήμερα έως τον Δεκέμβριο. Ομαλές καμπύλες
    (_smooth). Ετικέτες τέλους απομακρυσμένες ώστε να μη συμπίπτουν. slot = απόσταση από το τρέχον έτος (0 = φέτος).
    unsure = ανά έτος, επισφαλής καμπύλη (διακεκομμένη)."""
    W, H, left, right, top, bottom = 760, 160, 64, 110, 12, 26
    base = H - bottom
    vals = [v for s in series for v in s] + proj + [ytd]
    y, ticks = _cmp_axis(min(vals + [0]), max(vals + [0]), base, top)
    x = lambda i: round(left + i / 11 * (W - left - right), 1)  # noqa: E731
    path = lambda pts: _smooth([(x(i), y(v)) for i, v in pts])  # noqa: E731
    lines = []
    for k, (yr, s) in enumerate(zip(years, series)):
        cur = k == len(years) - 1
        pts = list(enumerate(s)) + ([(today, ytd)] if cur else [])
        end, v = (11, proj[-1]) if cur and proj else pts[-1]
        lines.append({"year": yr, "slot": len(years) - 1 - k, "v": v, "ex": x(end), "ey": y(v), "proj": cur and bool(proj),
                      "d": path(pts), "unsure": bool(unsure and unsure[k])})
    for i, ln in enumerate(sorted(lines, key=lambda ln: ln["ey"])):  # ετικέτες τέλους ≥ 14px μεταξύ τους
        ln["ly"] = ln["ey"] if not i else max(ln["ey"], prev + 14)
        prev = ln["ly"]
    slot = (W - left - right) / 11
    months = []
    for i in range(12):
        vals_i = [(yr, len(years) - 1 - k, s[i], False, None) for k, (yr, s) in enumerate(zip(years, series)) if i < len(s)]
        if proj and i >= done:
            vals_i.append((years[-1], 0, proj[i], True, None))
        months.append({"label": _GREEK_MONTHS[i], "cx": x(i), "x": round(x(i) - slot / 2, 1), "w": round(slot, 1),
                       "vals": vals_i[::-1]})
    return {"w": W, "h": H, "left": left, "right": W - right, "top": top, "base": base, "lines": lines, "months": months,
            "ticks": ticks, "cut_x": x(today), "today": {"x": x(today), "y": y(ytd), "v": ytd},
            "proj": path([(today, ytd)] + [(i, proj[i]) for i in range(done, 12)]) if proj else ""}


def _bar_path(x: float, w: float, by: float, ty: float) -> str:
    """Μπάρα από by (βάση) έως ty, με στρογγυλεμένη την άκρη δεδομένων (4px)."""
    if abs(by - ty) < 0.5:
        return ""
    r = min(4.0, abs(by - ty), w / 2)
    d = -1 if ty < by else 1  # προς τα πάνω (θετικό) ή κάτω (αρνητικό)
    return (f"M{x:.1f},{by:.1f}V{ty - d * r:.1f}Q{x:.1f},{ty:.1f} {x + r:.1f},{ty:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{ty:.1f} {x + w:.1f},{ty - d * r:.1f}V{by:.1f}Z")


def _cmp_bars(years: list[str], series: list[list[float]], labels: list[str]) -> dict:
    """Γεωμετρία SVG (viewBox 760×250): ανά ετικέτα (μέγεθος) ομάδα μπαρών, μία ανά έτος (παλαιότερο αριστερά),
    με 2px κενό ανάμεσα. Αρνητικές τιμές (π.χ. ζημιά) σχεδιάζονται κάτω από τη βάση."""
    W, H, left, right, top, bottom = 760, 250, 64, 8, 16, 30
    base = H - bottom
    vals = [v for s in series for v in s]
    y, ticks = _cmp_axis(min(vals + [0]), max(vals + [0]), base, top)
    n = len(labels)
    slot, k = (W - left - right) / max(n, 1), len(series)
    bw = max(2.0, min(28.0, (slot * 0.8 - 2 * (k - 1)) / k))
    group = k * bw + 2 * (k - 1)
    months = []
    for i in range(n):
        cx = left + slot * (i + 0.5)
        bars = [{"slot": len(years) - 1 - j, "d": _bar_path(cx - group / 2 + j * (bw + 2), bw, y(0), y(s[i]))}
                for j, s in enumerate(series)]
        months.append({"label": labels[i], "cx": round(cx, 1), "x": round(left + slot * i, 1), "w": round(slot, 1),
                       "bars": bars, "vals": [(yr, len(years) - 1 - j, s[i], False, None) for j, (yr, s) in enumerate(zip(years, series))][::-1]})
    return {"w": W, "h": H, "left": left, "right": W - right, "top": top, "base": base, "zero": y(0),
            "months": months, "ticks": ticks}


def _cmp_parties(Y: dict, years: list[str], key: str) -> dict:
    """Πελάτες ή προμηθευτές: κορυφαίοι 10 φέτος με τα ποσά κάθε έτους, νέοι / χαμένοι έναντι πέρσι,
    μεγαλύτερες μεταβολές, πλήθος ενεργών και συγκέντρωση (μερίδιο 1ου / 2ου–5ου / υπολοίπων) ανά έτος."""
    cur = Y[years[-1]][key]
    prev = Y[years[-2]][key] if len(years) > 1 else None
    amount = lambda m, v: m.get(v, {}).get("amount", 0.0)  # noqa: E731
    top = sorted(cur.values(), key=lambda t: -t["amount"])[:10]
    rows = [dict(t, vals=[round(amount(Y[y][key], t["vat"]), 2) for y in years],
                 d=_cmp_delta(t["amount"], amount(prev, t["vat"])) if prev is not None else None) for t in top]
    out = {"rows": rows, "max": max([abs(v) for r in rows for v in r["vals"]] + [1]),
           "active": [len(Y[y][key]) for y in years], "conc": [], "has_prev": prev is not None}
    for y in years:
        amts = sorted((t["amount"] for t in Y[y][key].values() if t["amount"] > 0), reverse=True)
        total = sum(amts) or 1
        top1, top5 = sum(amts[:1]) / total * 100, sum(amts[:5]) / total * 100
        out["conc"].append({"year": y, "top1": round(top1, 1), "next4": round(top5 - top1, 1), "rest": round(100 - top5, 1),
                            "empty": not amts})
    if prev is not None:
        pick = lambda src, other: sorted((t for v, t in src.items() if v not in other and t["amount"] > 0),  # noqa: E731
                                         key=lambda t: -t["amount"])
        new, lost = pick(cur, prev), pick(prev, cur)
        out.update(new=new[:5], new_n=len(new), new_sum=round(sum(t["amount"] for t in new), 2),
                   lost=lost[:5], lost_n=len(lost), lost_sum=round(sum(t["amount"] for t in lost), 2))
        movers = []
        for v in set(cur) | set(prev):
            d = amount(cur, v) - amount(prev, v)
            if abs(d) >= 0.01:
                movers.append({"name": (cur.get(v) or prev.get(v))["name"], "vat": v, "v": round(d, 2)})
        movers = sorted(movers, key=lambda m: -abs(m["v"]))[:8]
        out["movers"], out["movers_max"] = movers, max([abs(m["v"]) for m in movers] + [1])
    return out


def _cmp_e3(Y: dict, years: list[str], key: str) -> dict:
    """Ανάλυση Ε3 (έσοδα ή έξοδα) ανά λογαριασμό: ποσά κάθε έτους, μεταβολή, μεγαλύτερες μεταβολές."""
    groups = sorted({g for y in years for g in Y[y][key]})
    has_prev = len(years) > 1
    rows = []
    for g in groups:
        vals = [round(Y[y][key].get(g, 0.0), 2) for y in years]
        if not any(abs(v) >= 0.01 for v in vals):
            continue
        rows.append({"code": g, "label": _e3_group_label(g) or _CLASSIFICATION_CATEGORY_NAMES.get(g, ""), "vals": vals,
                     "d": _cmp_delta(vals[-1], vals[-2]) if has_prev else None})
    rows.sort(key=lambda r: -abs(r["vals"][-1]))
    movers = sorted((r for r in rows if r["d"] and abs(r["d"]["v"]) >= 0.01), key=lambda r: -abs(r["d"]["v"]))[:8]
    return {"rows": rows, "max": max([abs(v) for r in rows for v in r["vals"]] + [1]),
            "totals": [round(sum(r["vals"][i] for r in rows), 2) for i in range(len(years))],
            "movers": movers, "movers_max": max([abs(r["d"]["v"]) for r in movers] + [1])}


def _compare(cid: int | None, now: datetime) -> dict:
    """Όλα τα στοιχεία της σελίδας «Σύγκριση ετών». Έσοδα, έξοδα, κέρδος και Ε3: ίδιος υπολογισμός με την
    «Ανάλυση Ε3» (_pl)· αποθέματα μόνο σε χρήση με έναρξης και λήξης (βλ. παρακάτω).
    ΦΠΑ, πάγια, παρακρατήσεις: από τα σύνολα (_yearly_totals)."""
    # Year to date: 1/1 έως τη σημερινή ημερομηνία (ΜΜ-ΗΗ) κάθε έτους. done = ολοκληρωμένοι μήνες
    # (καμπύλες ανά μήνα, βάση της προβολής)· ο τρέχων μήνας μετρά έως σήμερα.
    md, done = now.strftime("%m-%d"), now.month - 1
    have = set(db.document_years(cid))
    years = [str(y) for y in range(now.year - _CMP_YEARS + 1, now.year + 1) if str(y) in have or y == now.year]
    with_stock = stock_in("dashboard")
    in_cut = lambda d: "01-01" <= (d["issue_date"] or "")[5:10] <= md  # noqa: E731
    filled = _cmp_unclassified(cid) if _CMP_FILL_YEAR in years else []
    Y = {}
    for y in years:
        all_inc = db.yearly_documents(cid, "income", _INCOME_CLASSIFIED_STATUSES, y)
        inc = [d for d in all_inc if in_cut(d)]
        raw = db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, y) + (filled if y == _CMP_FILL_YEAR else [])
        # Αποθέματα (_stock_check): μετρούν μόνο με έναρξης ΚΑΙ λήξης (αλλιώς το αποτέλεσμα «κρέμεται») και τότε
        # λογίζονται στην αρχή της χρήσης, ώστε η μεταβολή τους να μετρά και στο year to date (η εγγραφή γίνεται
        # οποτεδήποτε, συνήθως στο κλείσιμο).
        sc = _stock_check(cid, y, raw)
        used = sc["used"]
        all_exp = [dict(d, cls_json=json.dumps([e for e in json.loads(d["cls_json"] or "[]")
                                                 if e.get("category") not in STOCK_CATEGORIES])) for d in raw]
        exp = [d for d in all_exp if in_cut(d)]
        # opening = οι εγγραφές αποθεμάτων που μετρούν (στο year to date και στον Ιανουάριο της καμπύλης)·
        # late = μετά το σημείο σύγκρισης, χωρίς αποθέματα (καμπύλη όλου του έτους, βάση της προβολής).
        opening = [{"issue_date": f"{y}-01-01", "invoice_type": "", "counterparty_vat": "", "counterparty_name": None,
                    "total_net": amt, "total_vat": 0, "lines_json": "[]", **dict.fromkeys(db.EXTRA_TOTALS, 0),
                    "cls_json": json.dumps([{"type": "", "category": cat, "amount": amt}])}
                   for cat, amt in ((OPENING_STOCK, sc["open"]), (CLOSING_STOCK, sc["close"]))] if used else []
        late = [d for d in all_exp if not in_cut(d)]
        it, et = _yearly_totals(inc), _yearly_totals(exp)
        s = lambda t, c: round(sum(t[m][c] for m in range(1, 13)), 2)  # noqa: E731  (inc/exp ήδη έως σήμερα)
        # Έσοδα / έξοδα / κέρδος όπως η «Ανάλυση Ε3»: έξοδα = κόστος πωληθέντων (με αποθέματα έναρξης/λήξης)
        # + έξοδα χρήσης, χωρίς αγορές παγίων. Ανά μήνα το ίδιο _pl (γραμμικό: οι μήνες αθροίζουν στο σύνολο).
        pl = _pl(inc, exp + opening)
        # Όλοι οι μήνες του έτους (οι ολοκληρωμένοι ταυτίζονται με τη σύγκριση): καμπύλη + βάση της προβολής.
        of = lambda docs, m: [d for d in docs if (d["issue_date"] or "")[5:7] == f"{m:02d}"]  # noqa: E731
        mpl = [_pl(of(all_inc, m), of(exp + opening + late, m)) for m in range(1, 13)]
        # Σωρευτικά όλου του έτους: αποτέλεσμα / έσοδα / έξοδα Ε3 (κόστος πωληθέντων + έξοδα χρήσης).
        cum_full, cum_in_full, cum_out_full, acc, acc_in, acc_out = [], [], [], 0.0, 0.0, 0.0
        for p in mpl:
            acc, acc_in, acc_out = acc + p["profit"], acc_in + p["inc_total"], acc_out + p["cogs"] + p["exp_total"]
            cum_full.append(round(acc, 2))
            cum_in_full.append(round(acc_in, 2))
            cum_out_full.append(round(acc_out, 2))
        cum = cum_full[:done]
        # Μετά τη σημερινή ημερομηνία, ανά μήνα (έσοδα / έξοδα Ε3): βάση της αναλογικής προβολής.
        rest = [_pl(of([d for d in all_inc if not in_cut(d)], m), of(late, m)) for m in range(1, 13)]
        r = Y[y] = {
            "income": pl["inc_total"], "expense": round(pl["cogs"] + pl["exp_total"], 2), "opex": pl["exp_total"],
            "profit": pl["profit"], "gross": pl["gross"], "cogs": pl["cogs"],
            "vat_out": s(it, "vat"), "vat_in": s(et, "vat"), "withheld": s(it, "withheld"),
            "assets": s(et, "assets"), "depreciation": s(et, "depreciation"),
            # Όλο το έτος (για τα προηγούμενα): αγορές / αποσβέσεις συχνά καταχωρούνται μετά τη σημερινή ημερομηνία.
            **{k + "_full": round(sum(v[k] for v in _yearly_totals(all_exp).values()), 2) for k in ("assets", "depreciation")},
            "cum": cum, "cum_full": cum_full, "cum_in_full": cum_in_full, "cum_out_full": cum_out_full,
            "r_in": [p["inc_total"] for p in rest], "r_out": [p["cogs"] + p["exp_total"] for p in rest],
            "n_in": len(inc), "n_out": len(exp),
            "credits": sum(d["invoice_type"] in CREDIT_INVOICE_TYPES for d in inc + exp),
            "customers": _counterparty_totals([d for d in inc if d["counterparty_vat"]]),  # λιανική: όχι πελάτης
            "suppliers": _counterparty_totals([d for d in raw if in_cut(d)], not with_stock),
            # Έξοδα Ε3 όπως στο κέρδος: αποθέματα λήξης αφαιρούνται, αγορές παγίων εκτός (ενότητα «Πάγια»).
            "e3_in": _e3_groups(pl["income"]),
            "e3_out": _e3_groups(pl["opening"] + pl["purchases"] + [dict(g, amount=-g["amount"]) for g in pl["closing"]]
                                 + pl["expense"]),
            "exp_docs": exp,
            # Ανάλυση κόστους πωληθέντων (year to date): έναρξης + αγορές − λήξης.
            "s_open": pl["opening_total"], "purchases": pl["purchases_total"], "s_close": pl["closing_total"],
            # Αποθέματα όλης της χρήσης (ενότητα «Αποθέματα»).
            "stock_open": sc["open"], "stock_src": sc["src"], "stock_close": sc["close"],
            "has_close": sc["has_close"], "stock_used": used, "stock_why": sc["why"],
            "exp_ns": round(pl["purchases_total"] + pl["exp_total"], 2),  # χωρίς αποθέματα: βάση της προβολής
            "purch_full": round(sum(p["purchases_total"] for p in mpl), 2),
        }
        r["vat_net"] = round(r["vat_out"] - r["vat_in"], 2)
        r["margin"] = round(r["profit"] / r["income"] * 100, 1) if r["income"] else None
        r["avg_in"] = round(r["income"] / r["n_in"], 2) if r["n_in"] else 0.0
        r["n_cust"], r["n_supp"] = len(r["customers"]), len(r["suppliers"])
    cur = Y[years[-1]]
    prev = Y[years[-2]] if len(years) > 1 else None
    # Αναλογική προβολή έως 31/12, χωριστά για έσοδα και έξοδα Ε3: φετινό YTD + περσινά ποσά ΜΕΤΑ τη σημερινή
    # ημερομηνία × (φετινό YTD / περσινό YTD της ίδιας ημερομηνίας). Χωρίς περσινή βάση: ημερήσιος ρυθμός φετινού YTD.
    # Ό,τι έχει ήδη καταχωρηθεί φέτος μετά από σήμερα είναι το ελάχιστο του μήνα του.
    dim = [calendar.monthrange(now.year, m)[1] for m in range(1, 13)]
    left = [0] * done + [dim[done] - now.day] + dim[done + 1:]  # μέρες που απομένουν ανά μήνα
    elapsed = now.timetuple().tm_yday

    def rest_of(key, side, name):
        ytd = cur[key]
        if prev and prev[key] > 0:
            k = ytd / prev[key]
            est = [v * k for v in prev["r_" + side]]
            how = f"{name} ×{k:.2f}".replace(".", ",") + f" επί των περσινών μετά τις {now.day}/{now.month}"
        else:
            est, how = [ytd / elapsed * n for n in left], f"{name}: ημερήσιος ρυθμός φέτος (χωρίς περσινή βάση)"
        return [max(e, f) if i >= done else 0.0 for i, (e, f) in enumerate(zip(est, cur["r_" + side]))], how

    r_in, m_in = rest_of("income", "in", "έσοδα")
    r_out, m_out = rest_of("exp_ns", "out", "έξοδα")  # λόγος χωρίς αποθέματα: η μεταβολή τους δεν είναι ρυθμός
    proj, proj_in, proj_out = [], [], []
    acc, acc_in, acc_out = cur["profit"], cur["income"], cur["expense"]
    for i in range(12):
        acc, acc_in, acc_out = acc + r_in[i] - r_out[i], acc_in + r_in[i], acc_out + r_out[i]
        proj.append(round(acc if i >= done else cur["profit"], 2))
        proj_in.append(round(acc_in if i >= done else cur["income"], 2))
        proj_out.append(round(acc_out if i >= done else cur["expense"], 2))
    today = max(done - 1 + now.day / calendar.monthrange(now.year, now.month)[1], 0)  # θέση της σημερινής ημέρας

    # Εταιρεία με κόστος πωληθέντων (αγορές αποθεμάτων ή αποθέματα): σε χρήση χωρίς αποθέματα έναρξης και λήξης
    # το αποτέλεσμα είναι επισφαλές σε όλη τη διάρκεια του έτους → διακεκομμένη καμπύλη.
    trade = any(Y[y]["purch_full"] or Y[y]["stock_src"] or Y[y]["has_close"] for y in years)
    unsure = [trade and not Y[y]["stock_used"] for y in years]

    def cum_chart(key, series_key, title, good_up, pr, ytd, uns=None):
        """Ένα από τα τρία σωρευτικά γραφήματα + η προβολή του έναντι όλου του περσινού έτους."""
        prev_full = prev[series_key][-1] if prev else None
        return {"key": key, "title": title, "good_up": good_up, "proj_v": pr[-1], "prev_full": prev_full,
                "d": _cmp_delta(pr[-1], prev_full) if prev else None,
                "chart": _cmp_lines(years, [Y[y][series_key] for y in years[:-1]] + [cur[series_key][:done]],
                                    pr, ytd, done, today, uns)}
    proj_method = f"{m_in} · {m_out}"

    def kpi(key, label, good_up):
        vals = [Y[y][key] for y in years]
        return {"key": key, "label": label, "v": vals[-1], "prev": vals[-2] if prev else None,
                "d": _cmp_delta(vals[-1], vals[-2]) if prev else None, "good_up": good_up,
                "spark": _spark(vals) if len(vals) > 1 else ""}

    kpis = [kpi("income", "Έσοδα", True), kpi("expense", "Έξοδα", False), kpi("profit", "Καθαρό κέρδος Ε3", True),
            kpi("vat_net", "Καθαρός ΦΠΑ", None), kpi("assets", "Αγορές παγίων", None)]
    # Αναλυτικός πίνακας μεγεθών: (κλειδί, τίτλος, μορφή, αύξηση = καλό;)
    metrics = [(k, label, fmt, good, [Y[y][k] for y in years]) for k, label, fmt, good in (
        ("income", "Έσοδα Ε3", "amt", True), ("s_open", "Αποθέματα έναρξης", "amt", None),
        ("purchases", "+ Αγορές εμπορευμάτων / πρώτων υλών (2.1, 2.2)", "amt", False),
        ("s_close", "− Αποθέματα λήξης", "amt", None), ("cogs", "= Κόστος πωληθέντων", "amt", False),
        ("gross", "Μικτό κέρδος", "amt", True), ("opex", "Έξοδα χρήσης", "amt", False),
        ("expense", "Σύνολο εξόδων Ε3", "amt", False),
        ("profit", "Καθαρό κέρδος Ε3", "amt", True), ("margin", "Περιθώριο καθαρού κέρδους", "pct", True),
        ("vat_out", "ΦΠΑ εκροών", "amt", None), ("vat_in", "ΦΠΑ εισροών", "amt", None),
        ("vat_net", "Καθαρός ΦΠΑ (εκροών − εισροών)", "amt", None), ("withheld", "Παρακρατήσεις εσόδων", "amt", None),
        ("assets", "Αγορές παγίων (2.7)", "amt", None), ("depreciation", "Αποσβέσεις (E3_587)", "amt", None),
        ("n_in", "Παραστατικά εσόδων", "int", True), ("n_out", "Παραστατικά εξόδων", "int", None),
        ("credits", "Πιστωτικά", "int", None), ("avg_in", "Μέση αξία παραστατικού εσόδου", "amt", True),
        ("n_cust", "Ενεργοί πελάτες (με ΑΦΜ)", "int", True), ("n_supp", "Ενεργοί προμηθευτές", "int", None))]
    metrics = [{"label": label, "fmt": fmt, "good_up": good, "vals": vals,
                "d": _cmp_delta(vals[-1], vals[-2]) if prev and None not in vals[-2:] else None}
               for _, label, fmt, good, vals in metrics]

    customers, suppliers = _cmp_parties(Y, years, "customers"), _cmp_parties(Y, years, "suppliers")
    e3_in, e3_out = _cmp_e3(Y, years, "e3_in"), _cmp_e3(Y, years, "e3_out")

    # Πάγια: αγορές / αποσβέσεις ανά έτος + οι αγορές του τρέχοντος έτους ανά παραστατικό.
    asset_docs = []
    for d in cur["exp_docs"]:
        amt = sum(e.get("amount") or 0 for e in json.loads(d["cls_json"] or "[]") if e.get("category") == ASSET_CATEGORY)
        if amt:
            sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
            asset_docs.append({"date": d["issue_date"], "name": d["counterparty_name"] or d["counterparty_vat"] or "—",
                               "amount": round(sign * amt, 2)})
    asset_docs.sort(key=lambda a: -abs(a["amount"]))

    # Σημεία-κλειδιά: σύντομες προτάσεις από τα παραπάνω (μόνο με σύγκριση έναντι πέρσι).
    insights = []
    # Αποθέματα εκτός υπολογισμού (χρήση χωρίς έναρξης ή χωρίς λήξης) — μόνο αν υπάρχει κάποια εγγραφή αποθεμάτων.
    skip = [f"{y} ({Y[y]['stock_why']})" for y in years if Y[y]["stock_why"] and Y[y]["stock_why"] != "χωρίς εγγραφές αποθεμάτων"]
    if prev and skip:
        insights.append(("flat", None, "Αποθέματα εκτός υπολογισμού: " + " · ".join(skip)))
    if filled:
        insights.append(("flat", None, f"{_CMP_FILL_YEAR}: {len(filled)} αχαρακτήριστα έξοδα κατανεμήθηκαν σε αγορές "
                                       f"αποθεμάτων και έξοδα με τις αναλογίες {'–'.join(_CMP_FILL_FROM)}"))
    if prev:
        amt = lambda v: format_el_amount(abs(v)) + " €"  # noqa: E731
        pct = lambda d: f" ({'+' if d['pct'] > 0 else ''}{d['pct']:.1f}%)".replace(".", ",") if d["pct"] is not None else ""  # noqa: E731
        for k in kpis[:3]:
            d = k["d"]
            if abs(d["v"]) >= 0.01:
                good = (d["v"] > 0) == k["good_up"]
                insights.append(("up" if d["v"] > 0 else "down", good,
                                 f"{k['label']} {'αυξήθηκαν' if k['key'] != 'profit' else 'αυξήθηκε'} κατά {amt(d['v'])}{pct(d)}"
                                 if d["v"] > 0 else
                                 f"{k['label']} {'μειώθηκαν' if k['key'] != 'profit' else 'μειώθηκε'} κατά {amt(d['v'])}{pct(d)}"))
        dv = kpis[0]["d"]["v"]
        best = max(customers.get("movers", []), key=lambda m: m["v"] * (1 if dv >= 0 else -1), default=None)
        if best and abs(dv) >= 0.01 and (best["v"] > 0) == (dv > 0):
            insights.append(("up" if dv > 0 else "down", dv > 0,
                             f"Ο πελάτης «{best['name']}» εξηγεί το {min(abs(best['v'] / dv) * 100, 999):.0f}% της μεταβολής των εσόδων "
                             f"({'+' if best['v'] > 0 else '−'}{amt(best['v'])})"))
        up = next((r for r in e3_out["movers"] if r["d"]["v"] > 0), None)
        if up:
            insights.append(("up", False, f"Μεγαλύτερη αύξηση εξόδου: {up['label'] or up['code']} +{amt(up['d']['v'])}{pct(up['d'])}"))
        down = next((r for r in e3_out["movers"] if r["d"]["v"] < 0), None)
        if down:
            insights.append(("down", True, f"Μεγαλύτερη μείωση εξόδου: {down['label'] or down['code']} −{amt(down['d']['v'])}{pct(down['d'])}"))
        n = customers.get("new_n")
        if n:
            insights.append(("up", True, f"{n} {'νέος πελάτης έφερε' if n == 1 else 'νέοι πελάτες έφεραν'} {amt(customers['new_sum'])}"))
        n = customers.get("lost_n")
        if n:
            insights.append(("down", False, f"{n} {'περσινός πελάτης' if n == 1 else 'περσινοί πελάτες'} χωρίς κίνηση φέτος "
                                            f"({amt(customers['lost_sum'])} πέρσι)"))
        c1 = customers["conc"][-1]
        if not c1["empty"] and c1["top1"] + c1["next4"] >= 60:
            insights.append(("flat", None, f"Συγκέντρωση: οι 5 μεγαλύτεροι πελάτες φέρνουν το {c1['top1'] + c1['next4']:.0f}% των εσόδων"))
        m0, m1 = prev["margin"], cur["margin"]
        if m0 is not None and m1 is not None and abs(m1 - m0) >= 1:
            insights.append(("up" if m1 > m0 else "down", m1 > m0,
                             f"Περιθώριο καθαρού κέρδους {m1:.1f}% από {m0:.1f}% πέρσι".replace(".", ",")))

    return {
        "years": years, "span": f"1/1 – {now.day}/{now.month}", "Y": Y, "cur": cur, "prev": prev, "kpis": kpis, "metrics": metrics,
        # Τρία σωρευτικά γραφήματα το ένα κάτω από το άλλο (ίδια χρώματα ετών με όλη την αναφορά).
        "cums": [cum_chart("in", "cum_in_full", "Σωρευτικά έσοδα", True, proj_in, cur["income"]),
                 cum_chart("out", "cum_out_full", "Σωρευτικά έξοδα", False, proj_out, cur["expense"]),
                 cum_chart("res", "cum_full", "Σωρευτικό αποτέλεσμα", True, proj, cur["profit"], unsure)],
        "unsure": [y for y, u in zip(years, unsure) if u],
        "proj_method": proj_method,
        # Αθροιστικά της περιόδου (όχι ανά μήνα: ίδια έσοδα/έξοδα καταχωρούνται σε άλλο μήνα κάθε χρόνο).
        "totals": _cmp_bars(years, [[Y[y][k] for k in _CMP_TOTALS] for y in years], list(_CMP_TOTALS.values())),
        "customers": customers, "suppliers": suppliers, "e3_in": e3_in, "e3_out": e3_out,
        "assets_max": max([abs(Y[y][k + s]) for y in years for k in ("assets", "depreciation") for s in ("", "_full")] + [1]),
        "vat_max": max([abs(Y[y][k]) for y in years for k in ("vat_out", "vat_in")] + [1]),
        "asset_docs": asset_docs[:10], "asset_docs_n": len(asset_docs), "insights": insights,
    }


@app.route("/reports/compare")
def reports_compare():
    """Σύγκριση ετών: τρέχον έτος απέναντι στα προηγούμενα, στους ίδιους ολοκληρωμένους μήνες."""
    now = datetime.now(ATHENS)
    data = _compare(_active_company_id(), now)
    return render_template("reports_compare.html", c=data, months=_GREEK_MONTHS,
                           now_str=now.strftime("%d/%m/%Y"))


def _flow_docs(cid: int | None, year: str) -> tuple[list[dict], list[dict]]:
    """Χαρακτηρισμένα έσοδα/έξοδα ενός έτους για τις Ροές (ίδιες καταστάσεις με τις αναφορές)."""
    docs = lambda kind, st: [d for d in db.period_documents(cid, kind, f"{year}-01-01", f"{year}-12-31")  # noqa: E731
                             if d["status"] in st]
    return docs("income", _INCOME_CLASSIFIED_STATUSES), docs("expense", _EXPENSE_CLASSIFIED_STATUSES)


def _flow_opts() -> dict:
    """Διακόπτες των Ροών από τη διεύθυνση: αποθέματα / πάγια (προεπιλογή: ενεργοί· «0» = εκτός)."""
    return {"stock": request.args.get("stock") != "0", "assets": request.args.get("assets") != "0"}


def _flow_year(cid: int | None) -> tuple[str, list[str]]:
    """(επιλεγμένο έτος, διαθέσιμα έτη): όσα έχουν παραστατικά + το τρέχον, νεότερο πρώτο."""
    now = str(datetime.now(ATHENS).year)
    years = sorted(set(db.document_years(cid)) | {now}, reverse=True)
    year = request.args.get("year", "")
    return (year if year in years else now), years


@app.route("/reports/flows")
def reports_flows():
    """Ροές: διάγραμμα Sankey από πού έρχονται τα έσοδα και πού πάνε, ανά έτος."""
    cid = _active_company_id()
    year, years = _flow_year(cid)
    inc, exp = _flow_docs(cid, year)
    # Μηνιαίος «καθρέφτης» της κεφαλίδας: πλήθος παραστατικών εσόδων/εξόδων ανά μήνα του έτους (όλες οι καταστάσεις,
    # όπως στον πίνακα ελέγχου).
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    monthly: dict = {}
    for kind in ("income", "expense"):
        for ym, st in (db.month_counts(cid, kind, f"{year}-01-01") if cid else {}).items():
            if ym in months:
                monthly.setdefault(ym, {})[kind] = sum(st.values())
    opts = _flow_opts()
    return render_template("reports_flows.html", year=year, years=years, sankey=_sankey(inc, exp, **opts), opts=opts,
                           months=months, monthly=monthly, this_month=datetime.now(ATHENS).strftime("%Y-%m"))


@app.route("/reports/flows/docs")
def reports_flows_docs():
    """Τμήμα HTML για το modal των Ροών: τα παραστατικά ενός κόμβου (side, key, label) του έτους,
    με το ποσό που φέρνει το καθένα στη ροή (ίδιος υπολογισμός με το _sankey)."""
    side, key, label = request.args.get("side"), request.args.get("key", ""), request.args.get("label", "")
    if side not in ("src", "dst"):
        return "Μη έγκυρη επιλογή.", 400
    cid = _active_company_id()
    year, _years = _flow_year(cid)
    inc, exp = _flow_docs(cid, year)
    # Ένα παραστατικό μπορεί να φέρνει πολλά μερίδια στον ίδιο κόμβο (π.χ. δύο χαρακτηρισμοί ίδιου λογαριασμού).
    rows: dict = {}
    opts = _flow_opts()
    for d, part in _sankey(inc, exp, pick=(side, key, label), **opts):
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        r = rows.setdefault(d["mark"], dict(d, net=sign * (d["total_net"] or 0), vat=sign * (d["total_vat"] or 0),
                                             part=0.0, credit=sign < 0))
        r["part"] += part
    # Ταξινόμηση: συναλλασσόμενος (χωρίς τόνους/πεζά-κεφαλαία· χωρίς όνομα στο τέλος) → ημερομηνία → αριθμός
    # (σειρά, μετά Α/Α αριθμητικά όταν είναι αριθμός: 2 πριν από το 10).
    def order(r):
        name, aa = r["counterparty_name"] or "", str(r["aa"] or "")
        return (not name, _fold(name), r["issue_date"] or "", r["series"] or "",
                (0, int(aa), "") if aa.isdigit() else (1, 0, aa))
    docs = sorted(rows.values(), key=order)
    for r in docs:
        r["part"] = round(r["part"], 2)
    total = {k: round(sum(r[k] for r in docs), 2) for k in ("net", "vat", "part")}
    page_docs, page, pages = paginate(docs, request.args.get("page"))
    q = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("_flow_docs.html", docs=page_docs, n_docs=len(docs), page=page, pages=pages, q=q,
                           total=total, label=label, is_in=side == "src", year=year, type_names=INVOICE_TYPE_NAMES, opts=opts)

# ------------------------------------------------------------------ #
# MYDATA ΛΟΙΠΑ: ο κουβάς (db.mydata_archive) — ό,τι του myDATA δεν είναι ενεργό στα βιβλία — ανά κατηγορία,
# με drill down ως τα στοιχεία της εγγραφής. myDATA = documents + κουβάς (βλ. db.archive_sync).
# ------------------------------------------------------------------ #
HISTORY_CATEGORIES = {
    "cancelled": ("Ακυρωμένα παραστατικά", "Παραστατικά που ακυρώθηκαν στο myDATA, το καθένα με την πράξη ακύρωσής του"),
    "duplicate": ("Διπλές διαβιβάσεις (ίδιο UID)",
                  "Το ίδιο παραστατικό διαβιβάστηκε ξανά με νέο MARK· στα βιβλία μετρά μόνο το νεότερο"),
    "foreign": ("Παραστατικά άλλων ληπτών", "Με εκδότη εμάς, διαβιβασμένα από τον λήπτη για δικό του έξοδο (π.χ. 13.1)"),
    "movement": ("Παραστατικά διακίνησης", "Δελτία αποστολής — δεν χαρακτηρίζονται· με χρώμα όσα είναι άλλων ληπτών"),
    "pos": ("Αποδείξεις είσπραξης / επιστροφής POS",
            "Αποδεικτικά πληρωμής με κάρτα (8.4, 8.5) — δεν είναι έσοδο ή έξοδο, δεν χαρακτηρίζονται"),
    "other": ("Λοιπά παραστατικά εκτός βιβλίων", "Παραστατικά του myDATA που δεν είναι στα βιβλία της εφαρμογής"),
    "rejection": ("Απορρίψεις / αποκλίσεις", "Χαρακτηρισμοί με απόρριψη ή απόκλιση (transactionMode)"),
    "expenses_cls": ("Προηγούμενοι χαρακτηρισμοί εξόδων", "Χαρακτηρισμοί εξόδων που αντικαταστάθηκαν από νεότερους"),
    "income_cls": ("Προηγούμενοι χαρακτηρισμοί εσόδων", "Χαρακτηρισμοί εσόδων που αντικαταστάθηκαν από νεότερους"),
    "active_cls": ("Ισχύοντες χαρακτηρισμοί εκτός βιβλίων",
                   "Τελευταίος χαρακτηρισμός παραστατικών που δεν είναι στα βιβλία — συνήθως προηγούμενης περιόδου "
                   "που δεν έχει ανακτηθεί· με την ανάκτησή της περνούν στα βιβλία"),
}


def _history_category(row: dict, own_vat: str, cancelled: set) -> str:
    """Κατηγορία μίας εγγραφής του κουβά (κλειδί του HISTORY_CATEGORIES)."""
    from mydata_client import MOVEMENT_INVOICE_TYPES, POS_RECEIPT_TYPES

    kind = row["kind"]
    if kind == "cancel":  # μαζί με το παραστατικό που ακυρώνει
        return "cancelled"
    if kind in _CLS_KINDS and row.get("invoice_mark") in cancelled:  # χαρακτηρισμός ακυρωμένου: μαζί του, όχι «ισχύων»
        return "cancelled"
    if kind in _CLS_KINDS:
        # παραστατικό που δεν ανακτήθηκε: όλες οι υποβολές του (και απορρίψεις) μαζί με την ισχύουσα (Ιστορικό),
        # όχι χωρίς link στις άλλες κατηγορίες
        if not (row.get("ref_in_books") or row.get("ref_in_archive")):
            return "active_cls"
        if kind == "expenses_cls" and row.get("has_transaction_mode"):
            return "rejection"
        return kind if row.get("superseded") else "active_cls"
    if row.get("has_cancelled_by") or row["mark"] in cancelled:
        return "cancelled"
    if row.get("dup_of"):
        return "duplicate"
    if (row.get("invoice_type") or "") in POS_RECEIPT_TYPES:
        return "pos"
    if (row.get("invoice_type") or "") in MOVEMENT_INVOICE_TYPES:  # και όσα είναι άλλων ληπτών (row["foreign"])
        return "movement"
    if _history_foreign(row, own_vat):
        return "foreign"
    return "other"


def _history_foreign(row: dict, own_vat: str) -> bool:
    """Εκδότης εμείς, λήπτης άλλος: παραστατικό άλλου λήπτη."""
    return bool(own_vat and row.get("issuer_vat") == own_vat and row.get("counterpart_vat") not in (None, "", own_vat))


def _history_rows(cid) -> tuple[list[dict], list[int]]:
    """(εγγραφές του κουβά με «cat», διαθέσιμα έτη) — φίλτρο έτους από το ?year=."""
    rows = db.archive_rows(cid)
    own_vat = str((get_active_company() or {}).get("AADE_VAT_NUMBER") or "").strip()
    cancelled = ({r["invoice_mark"] for r in rows if r["kind"] == "cancel"}
                 | {r["mark"] for r in rows if r["kind"] == "invoice" and r.get("has_cancelled_by")})
    for r in rows:
        r["cat"] = _history_category(r, own_vat, cancelled)
        r["foreign"] = r["kind"] == "invoice" and _history_foreign(r, own_vat)
    years = sorted({int(r["date"][:4]) for r in rows if (r["date"] or "")[:4].isdigit()}, reverse=True)
    year = request.args.get("year", type=int)
    if year:
        rows = [r for r in rows if (r["date"] or "").startswith(str(year))]
    return rows, years


@app.route("/reports/history")
def reports_history():
    """MYDATA ΛΟΙΠΑ: ο κουβάς ανά κατηγορία (πλήθος, ποσό, εύρος ημερομηνιών)."""
    rows, years = _history_rows(_active_company_id())
    cats = []
    for key, (label, desc) in HISTORY_CATEGORIES.items():
        rs = [r for r in rows if r["cat"] == key]
        dates = sorted(r["date"][:10] for r in rs if r["date"])
        grouped = key in _HISTORY_GROUPED
        cats.append({"key": key, "label": label, "desc": desc, "n": len(rs),
                     "n_docs": len({r["invoice_mark"] for r in rs}) if grouped
                     else len({r["invoice_mark"] or r["mark"] for r in rs}) if key == "cancelled" else None,
                     # ποσά χαρακτηρισμών διαδοχικών υποβολών δεν αθροίζονται
                     "amount": 0 if grouped else _history_total(rs),
                     "first": dates[0] if dates else None, "last": dates[-1] if dates else None})
    return render_template("reports_history.html", cats=cats, total=len(rows), years=years,
                           year=request.args.get("year", type=int))


@app.route("/reports/history/docs")
def reports_history_docs():
    """Τμήμα HTML για το modal: οι εγγραφές μίας κατηγορίας."""
    cat = request.args.get("cat", "")
    if cat not in HISTORY_CATEGORIES:
        return "Μη έγκυρη κατηγορία.", 400
    rows, _years = _history_rows(_active_company_id())
    rows = [r for r in rows if r["cat"] == cat]
    grouped = cat in _HISTORY_GROUPED
    if grouped:  # χαρακτηρισμοί: μία γραμμή ανά παραστατικό (rows: νεότερο πρώτα → ο πρώτος = τελευταίος)
        groups: dict = {}
        for r in rows:
            g_ = groups.setdefault(r["invoice_mark"], dict(r, n=0))
            g_["n"] += 1
        rows = list(groups.values())
    total = 0 if grouped else _history_total(rows)
    if cat == "cancelled":  # δέντρο: ρίζα το παραστατικό που ακυρώνεται, παιδιά οι ακυρώσεις και οι χαρακτηρισμοί του
        trees: dict = {}
        for r in rows:
            key = r["mark"] if r["kind"] == "invoice" else r["invoice_mark"]
            t = trees.setdefault(key, {"mark": key, "root": None, "children": []})
            if r["kind"] == "invoice":
                t["root"] = r
            else:
                t["children"].append(r)
        for t in trees.values():
            t["children"].sort(key=lambda c: (c["kind"] != "cancel", c["mark"]))
            t["date"] = (t["root"] or t["children"][0])["date"]
        rows = list(trees.values())
    page_rows, page, pages = paginate(rows, request.args.get("page"))
    vats = ("ref_vat",) if grouped else ("issuer_vat", "counterpart_vat")
    flat = [r for t in page_rows for r in [t["root"], *t["children"]] if r] if cat == "cancelled" else page_rows
    names = {r[k]: db.get_supplier_name(r[k]) for r in flat for k in vats if r[k]}
    q = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("_history_docs.html", rows=page_rows, n=len(rows), page=page, pages=pages, q=q,
                           cat=cat, label=HISTORY_CATEGORIES[cat][0], names=names, grouped=grouped,
                           total=total, type_names=INVOICE_TYPE_NAMES)


_HISTORY_GROUPED = ("rejection", "expenses_cls", "income_cls", "active_cls")
_CLS_KINDS = ("expenses_cls", "income_cls")


def _history_total(rows: list[dict]) -> float:
    """Σύνολο ποσών κατηγορίας· οι χαρακτηρισμοί (π.χ. ακυρωμένων) δεν αθροίζονται — το ποσό είναι του παραστατικού."""
    return sum(r["amount"] or 0 for r in rows if r["kind"] not in _CLS_KINDS)


def _local_dt(iso: str) -> str:
    """ISO (UTC, όπως τα γράφει το db) → «yyyy-mm-dd HH:MM» σε ώρα Ελλάδας."""
    try:
        return datetime.fromisoformat(iso).astimezone(ATHENS).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


@app.route("/history/cls/<mark>")
def cls_history(mark):
    """Τμήμα HTML (modal): το ιστορικό χαρακτηρισμών ενός παραστατικού, από τον ισχύοντα προς τον παλαιότερο —
    από την καρτέλα του παραστατικού και από το «myData λοιπά»."""
    from itertools import groupby

    from mydata_client import cls_record

    cid = _active_company_id()
    recs = [dict(r, **cls_record(r["xml"])) for r in db.cls_history(cid, mark)]
    # Πίνακας σύγκρισης: στήλη ανά υποβολή (νεότερη αριστερά), σειρά ανά γραμμή του παραστατικού
    # (None = χαρακτηρισμός ανά παραστατικό). Σε κάθε κελί, «new» ό,τι δεν υπήρχε στην αμέσως παλαιότερη
    # υποβολή και «gone» ό,τι δεν κράτησε η αμέσως νεότερη — δηλαδή τι άλλαξε από υποβολή σε υποβολή.
    def key(e):  # στον χαρακτηρισμό ΦΠΑ (VAT_xxx) η κατηγορία δεν έχει ουσία: ίδιος τύπος + ποσό = ίδιος
        t = e.get("type") or ""
        return t, None if t.startswith("VAT_") else e.get("category"), round(e.get("amount") or 0, 2)

    for r in recs:
        r["cells"] = {ln: list(es) for ln, es in groupby(r["entries"], key=lambda e: e.get("line"))}
        # Ημερομηνία: το myDATA δεν δίνει· ακριβής μόνο όταν τον στείλαμε από την εφαρμογή.
        r["sent"] = _local_dt(r["sent_at"]) if r.get("sent_at") else None
    lines = sorted({ln for r in recs for ln in r["cells"]}, key=lambda ln: (ln is not None, ln or 0))
    for i, r in enumerate(recs):
        newer, older = (recs[i - 1] if i else None), (recs[i + 1] if i + 1 < len(recs) else None)
        r["diff"] = {}
        for ln in lines:
            mine = r["cells"].get(ln, [])
            o_keys = {key(e) for e in older["cells"].get(ln, [])} if older else None
            n_keys = {key(e) for e in newer["cells"].get(ln, [])} if newer else None
            r["diff"][ln] = [("new" if o_keys is not None and key(e) not in o_keys else "")
                             + (" gone" if n_keys is not None and key(e) not in n_keys else "") for e in mine]
    if not recs:
        return "Δεν υπάρχει ιστορικό χαρακτηρισμών.", 404
    row = db.get_document(cid, mark)
    # Ισχύει στο myDATA η ΝΕΟΤΕΡΗ υποβολή (recs[0])· το βιβλίο μπορεί να δείχνει παλαιότερη, αν η νέα ήρθε
    # (RequestTransmittedDocs έως σήμερα) χωρίς ανάκτηση της περιόδου του παραστατικού.
    stale = next((r for r in recs[1:] if r["active"]), None)
    return render_template("_cls_history.html", mark=mark, recs=recs, lines=lines, stale=stale,
                           back=request.args.get("back"),
                           doc_url=url_for("document", mark=mark) if row else None,
                           categories={**EXPENSE_CATEGORIES, **INCOME_CATEGORIES},
                           types={**EXPENSE_TYPES, **INCOME_TYPES}, vat_types=VAT_TYPES)


@app.route("/reports/history/item")
def reports_history_item():
    """Τμήμα HTML για το modal: όλα τα στοιχεία μίας εγγραφής του κουβά, όπως τα έδωσε το myDATA."""
    cid = _active_company_id()
    kind, mark = request.args.get("kind", ""), request.args.get("mark", "")
    item = db.archive_item(cid, kind, mark)
    if not item and kind == "invoice" and mark.isdigit() and cid:
        # Παραστατικό εκτός διαστήματος ανάκτησης (π.χ. παλιό που ακυρώθηκε τώρα): λήψη από το myDATA → κουβάς.
        try:
            recs = get_client().request_mark(mark)
        except (MyDataError, requests.RequestException) as e:
            return f"Το παραστατικό {mark} δεν έχει ανακτηθεί και η λήψη του από το myDATA απέτυχε: {_fetch_error(e)}", 502
        if recs:
            db.archive_sync(cid, recs)
            item = db.archive_item(cid, kind, mark)
    if not item:
        return "Η εγγραφή δεν βρέθηκε (ούτε στο myDATA).", 404
    ref = item.get("invoice_mark")
    ref_url = None
    if ref and db.get_document(cid, ref):
        ref_url = url_for("document", mark=ref, back=url_for("reports_history"))
    elif ref and db.archive_item(cid, "invoice", ref):
        ref_url = url_for("reports_history_item", kind="invoice", mark=ref)
    tree = _xml_tree(item["xml"])
    # Διπλή διαβίβαση (?dup=1 από την κατηγορία «Διπλές διαβιβάσεις»): δίπλα-δίπλα με την έγκυρη.
    valid = request.args.get("dup") and db.uid_current(cid, item.get("uid"), item["mark"])
    if valid:
        valid["tree"] = _xml_tree(valid["xml"])
        _xml_diff(valid["tree"], tree)
        _xml_diff(tree, valid["tree"])
    return render_template("_history_item.html", item=item, xml_tree=tree, ref_url=ref_url, valid=valid,
                           back=request.args.get("back"), kind_labels=_HISTORY_KIND_LABELS)


def _xml_diff(nodes: list[dict], other: list[dict]) -> None:
    """Σημειώνει (diff=True) κάθε τιμή του nodes που δεν υπάρχει, στην ίδια θέση, στο other (δέντρα του _xml_tree)."""
    def leaves(ns, path=""):
        for n in ns:
            p = f"{path}/{n['label']}"
            yield from leaves(n["children"], p) if n["children"] else [(p, n["value"])]

    have = set(leaves(other))

    def walk(ns, path=""):
        for n in ns:
            p = f"{path}/{n['label']}"
            if n["children"]:
                walk(n["children"], p)
            else:
                n["diff"] = (p, n["value"]) not in have

    walk(nodes)


_HISTORY_KIND_LABELS = {"invoice": "Παραστατικό", "expenses_cls": "Χαρακτηρισμός εξόδων",
                        "income_cls": "Χαρακτηρισμός εσόδων", "cancel": "Ακύρωση"}


# ------------------------------------------------------------------ #
# ΑΝΑΖΗΤΗΣΗ MARK: ό,τι κι αν είναι (παραστατικό, χαρακτηρισμός, ακύρωση) — κατάσταση, ανάλυση, διασυνδέσεις.
# Πηγή: db.mark_records (mydata_all = documents + κουβάς)· ό,τι λείπει, από το myDATA (request_mark).
# ------------------------------------------------------------------ #
_DOC_STATUS_WHY = {
    "unclassified": "Αχαρακτήριστο — περιμένει χαρακτηρισμό",
    "classified": "Χαρακτηρισμένο τοπικά — δεν έχει σταλεί ακόμα στο myDATA",
    "sent": "Ο χαρακτηρισμός στάλθηκε — αναμένεται επιβεβαίωση από την επόμενη ανάκτηση",
    "confirmed": "Ολοκληρωμένο — χαρακτηρισμένο στο myDATA",
}


def _invoice_facts(xml: str | None) -> dict:
    """Βασικά στοιχεία παραστατικού από το XML του myDATA (χωρίς namespaces)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml or "")
    except ET.ParseError:
        return {}

    def text(*path):
        el = root
        for name in path:
            el = next((c for c in el if c.tag.split("}")[-1] == name), None)
            if el is None:
                return None
        return (el.text or "").strip() or None

    num = lambda *p: float(text(*p) or 0)  # noqa: E731
    return {
        "uid": text("uid"), "cancelled_by": text("cancelledByMark"),
        "issue_date": text("invoiceHeader", "issueDate"), "invoice_type": text("invoiceHeader", "invoiceType"),
        "series": text("invoiceHeader", "series"), "aa": text("invoiceHeader", "aa"),
        "issuer_vat": text("issuer", "vatNumber"), "counterpart_vat": text("counterpart", "vatNumber"),
        "net": num("invoiceSummary", "totalNetValue"), "vat": num("invoiceSummary", "totalVatAmount"),
        "gross": num("invoiceSummary", "totalGrossValue"),
        "correlated": sorted({(el.text or "").strip() for el in root.iter()
                              if el.tag.split("}")[-1] in ("correlatedInvoices", "multipleConnectedMarks") and el.text},
                             key=db._mark_no),
    }


def _mark_status(cid: int, rec: dict, own_vat: str) -> dict:
    """Κατάσταση μίας εγγραφής του myDATA: {label, tone (ok/info/warn/err/muted), why}."""
    kind, mark = rec["kind"], rec["mark"]
    if kind == "cancel":
        return {"label": "Πράξη ακύρωσης", "tone": "err",
                "why": f"Ακυρώνει το παραστατικό {rec['invoice_mark']}"
                       + (f" από {'/'.join(rec['event_date'][:10].split('-')[::-1])}" if rec.get("event_date") else "")}
    if kind == "invoice" and rec["src"] == "documents":
        doc = db.get_document(cid, mark) or {}
        book = "εσόδων" if doc.get("kind") == "income" else "εξόδων"
        why = ("Χαρακτηρισμένο" if doc.get("kind") == "income" and doc.get("status") == "classified"
               else _DOC_STATUS_WHY.get(doc.get("status"), ""))
        return {"label": f"Ενεργό στο βιβλίο {book}", "tone": "ok", "why": why}
    if kind != "invoice":
        doc = db.get_document(cid, rec["invoice_mark"])
        if doc and doc.get("classification_mark") == mark:
            return {"label": "Ισχύει — στα βιβλία", "tone": "ok",
                    "why": "Ενσωματωμένος στο ίδιο το παραστατικό" if mark == rec["invoice_mark"]
                    else "Ο τρέχων χαρακτηρισμός του παραστατικού στα βιβλία"}
    row = next(iter(db.archive_rows(cid, mark)), None)
    if not row:
        return {"label": "Άγνωστο", "tone": "muted", "why": ""}
    inv_mark = mark if kind == "invoice" else rec["invoice_mark"]
    cancelled = {inv_mark} if db.cancellations(cid, inv_mark) else set()
    if kind != "invoice" and (rec2 := next((r for r in db.mark_records(cid, inv_mark) if r["kind"] == "invoice"), None)):
        if _invoice_facts(rec2["xml"]).get("cancelled_by"):
            cancelled.add(inv_mark)
    cat = _history_category(row, own_vat, cancelled)
    if kind == "invoice":
        return {
            "cancelled": {"label": "Ακυρωμένο", "tone": "err", "why": "Ακυρώθηκε στο myDATA — δεν μετρά πουθενά"},
            "duplicate": {"label": "Αντικαταστάθηκε", "tone": "warn",
                          "why": f"Ίδιο UID με το MARK {row['dup_of']}: κατά τις προδιαγραφές του myDATA ισχύει η "
                                 "τελευταία διαβίβαση και η προηγούμενη ακυρώνεται"},
            "movement": {"label": "Παραστατικό διακίνησης", "tone": "info", "why": "Δελτίο αποστολής — δεν χαρακτηρίζεται, εκτός βιβλίων"},
            "foreign": {"label": "Άλλου λήπτη", "tone": "info", "why": "Εκδότης εμείς, διαβιβάστηκε από τον λήπτη για δικό του έξοδο"},
        }.get(cat, {"label": "Εκτός βιβλίων", "tone": "warn",
                    "why": "Ήρθε από το myDATA αλλά δεν είναι στα βιβλία — π.χ. εκτός διαστήματος ανάκτησης"})
    return {
        "cancelled": {"label": "Χαρακτηρισμός ακυρωμένου", "tone": "err", "why": "Το παραστατικό του ακυρώθηκε"},
        "rejection": {"label": "Απόρριψη / απόκλιση", "tone": "warn", "why": "Υποβολή με transactionMode"},
        "active_cls": {"label": "Ισχύει — εκτός βιβλίων", "tone": "info",
                       "why": "Τελευταίος χαρακτηρισμός παραστατικού που δεν είναι στα βιβλία"},
    }.get(cat, {"label": "Αντικαταστάθηκε", "tone": "muted", "why": "Υπάρχει νεότερος χαρακτηρισμός του παραστατικού"})


def _mark_node(cid: int, mark: str, rel: str, own_vat: str, current: str) -> dict:
    """Κόμβος του γράφου διασυνδέσεων: {mark, kind, rel, status, here, missing}."""
    rec = next(iter(db.mark_records(cid, mark)), None)
    return {"mark": mark, "rel": rel, "here": mark == current, "missing": rec is None,
            "kind": rec["kind"] if rec else "invoice",
            "status": _mark_status(cid, rec, own_vat) if rec else
            {"label": "Δεν έχει ανακτηθεί", "tone": "muted", "why": "Κλικ για λήψη από το myDATA"}}


def _mark_analysis(cid: int, mark: str, recs: list[dict]) -> dict:
    from mydata_client import cls_record

    own_vat = str((get_active_company() or {}).get("AADE_VAT_NUMBER") or "").strip()
    rec = recs[0]
    kind = rec["kind"]
    inv_mark = mark if kind == "invoice" else rec["invoice_mark"]
    inv_rec = rec if kind == "invoice" else next(
        (r for r in db.mark_records(cid, inv_mark) if r["kind"] == "invoice"), None)
    facts = _invoice_facts(inv_rec["xml"]) if inv_rec else {}
    doc = db.get_document(cid, inv_mark) if inv_mark else None
    names = {v: (db.get_supplier_name(v) or ("Εμείς" if v == own_vat else None))
             for v in (facts.get("issuer_vat"), facts.get("counterpart_vat")) if v}

    cls = None
    if kind in _CLS_KINDS:
        if rec["xml"]:
            cls = cls_record(rec["xml"])
        else:  # χαρακτηρισμός των βιβλίων χωρίς XML: ό,τι κρατά το documents
            cls = {"entries": json.loads((doc or {}).get("cls_json") or "[]"), "transaction_mode": None}
        cls["lines"] = sorted({e.get("line") for e in cls["entries"]}, key=lambda ln: (ln is not None, ln or 0))

    # Διασυνδέσεις: παραστατικό, χαρακτηρισμοί, ακυρώσεις, ίδιο UID, συσχετιζόμενα (και προς τις δύο κατευθύνσεις).
    rels: dict = {}

    def add(m, rel):
        if m and m not in rels:
            rels[m] = rel

    add(inv_mark, "Παραστατικό")
    for r in db.cls_history(cid, inv_mark) if inv_mark else []:
        add(r["mark"], "Χαρακτηρισμός")
    if doc and doc.get("classification_mark") and doc["classification_mark"] != inv_mark:
        add(doc["classification_mark"], "Χαρακτηρισμός")
    for c in db.cancellations(cid, inv_mark) if inv_mark else []:
        add(c["mark"], "Ακύρωση")
    add(facts.get("cancelled_by"), "Ακύρωση")
    for m in db.uid_marks(cid, facts.get("uid")):
        add(m, "Ίδιο UID")
    for m in facts.get("correlated", []):
        add(m, "Συσχετιζόμενο")
    for m in db.mark_refs(cid, inv_mark) if inv_mark else []:
        add(m, "Το αναφέρει")
    add(mark, _HISTORY_KIND_LABELS.get(kind, kind))
    nodes = sorted((_mark_node(cid, m, rel, own_vat, mark) for m, rel in rels.items()), key=lambda n: db._mark_no(n["mark"]))

    xml = rec["xml"] or (inv_rec or {}).get("xml")
    tree = _xml_tree(xml)
    return {
        "rec": rec, "kind": kind, "status": _mark_status(cid, rec, own_vat), "inv_mark": inv_mark,
        "inv_status": _mark_status(cid, inv_rec, own_vat) if inv_rec and kind != "invoice" else None,
        "inv_missing": bool(inv_mark) and inv_rec is None, "facts": facts, "names": names, "doc": doc,
        "embedded": kind == "invoice" and any(r["kind"] in _CLS_KINDS for r in recs[1:]),
        "cls": cls, "nodes": nodes, "xml_tree": tree, "xml_is_invoice": not rec["xml"] and bool(inv_rec),
        "payments": _xml_payments(tree) if kind == "invoice" else [],
        "doc_cls": json.loads(doc.get("cls_json") or "[]") if doc and kind == "invoice" else [],
    }


@app.route("/mark")
def mark_lookup():
    """Αναζήτηση MARK: τι είναι, πού στέκεται, με τι συνδέεται. ?live=1 → λήψη από το myDATA ό,τι λείπει."""
    cid = _active_company_id()
    m = (request.args.get("m") or "").strip()
    # back: η οθόνη που κάλεσε την αναζήτηση — μένει ίδια σε όλη την πλοήγηση μέσα στη σελίδα (κόμβοι, νέα αναζήτηση).
    back = _safe_back(request.args.get("back")) or None
    ctx = {"m": m, "error": None, "live_none": False, "back": back}
    if m and not m.isdigit():
        ctx["error"] = "Το MARK είναι αριθμός (π.χ. 400001910473429)."
    elif m and cid and request.args.get("live"):
        try:
            recs = get_client().request_mark(m)
        except (MyDataError, requests.RequestException) as e:
            ctx["error"] = f"Η λήψη από το myDATA απέτυχε: {_fetch_error(e)}"
        else:
            if recs:
                db.archive_sync(cid, recs)
                return redirect(url_for("mark_lookup", m=m, back=back))
            ctx["live_none"] = True
    if m and not ctx["error"] and cid and (recs := db.mark_records(cid, m)):
        ctx.update(_mark_analysis(cid, m, recs))
    return render_template("mark.html", **ctx, kind_labels=_HISTORY_KIND_LABELS, type_names=INVOICE_TYPE_NAMES,
                           categories={**EXPENSE_CATEGORIES, **INCOME_CATEGORIES},
                           types={**EXPENSE_TYPES, **INCOME_TYPES}, vat_types=VAT_TYPES)


@app.route("/mark/<m>/xml")
def mark_xml(m):
    """Λήψη του XML μίας εγγραφής του myDATA (οποιουδήποτε είδους), όπως ήρθε."""
    rec = next((r for r in db.mark_records(_active_company_id(), m) if r["xml"]), None)
    if not rec:
        abort(404)
    return Response(rec["xml"], mimetype="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{m}.xml"'})


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


_VAT_CARRY_CACHE: dict = {}  # (εταιρεία, έτος, τύπος, περίοδος) → (έκδοση παραστατικών, (401, 483))


def _vat_auto_carry(cid: int | None, year: int, kind: str, n: int) -> tuple[float, float]:
    """(401, 483) αυτόματα από την προηγούμενη περίοδο ίδιου τύπου: το «ποσό για έκπτωση» (502)
    και το χρεωστικό έως 30 € που δεν αποδόθηκε. Επειδή κάθε περίοδος εξαρτάται από τη
    μεταφορά της προηγούμενης, η αλυσίδα ξεκινά από την πρώτη περίοδο με δεδομένα.
    Cache: ξαναϋπολογίζεται μόνο όταν αλλάξει κάποιο παραστατικό της εταιρείας."""
    end = _vat_bounds(year, kind, n)[0]
    key, digest = (cid, year, kind, n), db.documents_rev(cid)
    hit = _VAT_CARRY_CACHE.get(key)
    if hit and hit[0] == digest:
        return hit[1]
    _VAT_CARRY_CACHE[key] = (digest, out := _vat_carry_chain(cid, year, kind, n, end))
    return out


def _vat_carry_chain(cid: int | None, year: int, kind: str, n: int, end: str) -> tuple[float, float]:
    years = [int(y) for y in db.document_years(cid) if y.isdigit()]
    if not years:
        return 0.0, 0.0
    per_year = 12 if kind == "m" else 4
    credit = debit = 0.0
    # Ένα query ανά είδος για όλη την αλυσίδα (όχι 2 ανά περίοδο), μοιρασμένο μετά στις περιόδους.
    start = f"{min(years)}-01-01"
    # χωρίς raw_xml: οι κωδικοί εξαίρεσης δεν επηρεάζουν το 502/483 που μεταφέρεται
    docs = {k: db.period_documents(cid, k, start, end, xml=False) for k in ("income", "expense")}
    for y in range(min(years), year + 1):
        for i in range(1, per_year + 1):
            if (y, i) >= (year, n):
                return credit, debit
            date_from, date_to = _vat_bounds(y, kind, i)
            pick = lambda k: [d for d in docs[k] if date_from <= (d["issue_date"] or "") <= date_to]  # noqa: E731
            codes = vat_return.compute(pick("income"), pick("expense"), credit, debit)["codes"]
            credit, debit = codes["502"], codes["carry"]
    return credit, debit


# ------------------------------------------------------------------ #
# Πρόβλεψη αποτελέσματος τρέχοντος έτους
# ------------------------------------------------------------------ #
_FORECAST_STATUSES = ["unclassified", "classified", "sent", "confirmed"]


def _blend(prevs: list[list[float]], start: int, done: int) -> tuple[list[float], int]:
    """Σταθμισμένη εποχικότητα προηγούμενων ετών (prevs: πιο πρόσφατο πρώτο), βάρη 1, ½, ¼, …
    Κάθε έτος κανονικοποιείται στο δικό του διάστημα start..done-1 και το μείγμα κλιμακώνεται στο πιο πρόσφατο
    έτος με κίνηση σε αυτό: ο λόγος του _forecast μένει «φέτος / πέρσι». Έτη χωρίς κίνηση στο διάστημα δεν μετρούν·
    κανένα → prevs[0] αυτούσιο. Επιστρέφει (σειρά 12 μηνών, πλήθος ετών που μέτρησαν)."""
    ok = [(0.5 ** i, p, s) for i, p in enumerate(prevs) if (s := sum(p[start:done])) > 0]
    if not ok:
        return prevs[0], 0
    scale, w_sum = ok[0][2], sum(w for w, _, _ in ok)
    return [scale * sum(w * p[m] / s for w, p, s in ok) / w_sum for m in range(12)], len(ok)


def _forecast(cur: list[float], prev: list[float], done: int, start: int = 0, years: int = 1) -> tuple[list[float], str]:
    """Σημειακή πρόβλεψη 12 μηνών (λίστες 0–11). Οι μήνες < done είναι πραγματικοί· οι υπόλοιποι:
    περσινός μήνας × (φετινό / περσινό ίδιου διαστήματος), αλλιώς μέσος όρος μήνα, αλλιώς πέρσι.
    Βάση = μήνες start..done-1 (start = πρώτος μήνας με κίνηση: νέα εταιρεία ≠ μηδενικοί μήνες).
    prev μπορεί να είναι μείγμα ετών (_blend)· years = πόσα έτη μέτρησαν, μόνο για την ετικέτα."""
    actual, prev_same, n = sum(cur[start:done]), sum(prev[start:done]), done - start
    if n > 0 and prev_same > 0:
        ratio = actual / prev_same
        src = f"{years} ετών (σταθμισμένη, βαρύτερο το πιο πρόσφατο)" if years > 1 else "προηγούμενου έτους"
        rest, label = [p * ratio for p in prev[done:]], f"εποχικότητα {src} ×{ratio:.2f}".replace(".", ",")
    elif n > 0:
        rest, label = [actual / n] * (12 - done), "μέσος όρος ολοκληρωμένων μηνών"
    else:
        rest, label = prev[done:], "περσινά ποσά (δεν υπάρχει ακόμα ολοκληρωμένος μήνας με κίνηση)"
    rest = [max(r, c) for r, c in zip(rest, cur[done:])]  # ήδη καταχωρημένα: η πρόβλεψη δεν πέφτει κάτω απ' αυτά
    return [round(v, 2) for v in cur[:done] + rest], label


def _scenarios(cur_in, cur_out, prev_in, prev_out, done: int, seed: int, start: int = 0, fixed: list[float] | None = None,
               known_in: list[float] | None = None, known_out: list[float] | None = None, runs: int = 5000,
               vol_in: float = 1.0, vol_out: float = 1.0, shift_in: float = 1.0, shift_out: float = 1.0) -> dict | None:
    """Bootstrap: κάθε υπόλοιπος μήνας παίρνει την απόκλιση (πραγματικό − αναμενόμενο) ενός τυχαίου ολοκληρωμένου μήνα
    (ίδιου για έσοδα/έξοδα — κρατά τη συσχέτιση). fixed = ντετερμινιστικές εκροές ανά μήνα (σταθερά
    έξοδα, κόστος παγίων) που αφαιρούνται χωρίς τύχη· known_in / known_out = προγραμματισμένα ποσά
    ανά μήνα: πιάνονται ή χάνονται, ποτέ δεν ξεπερνιούνται — ο μήνας k απέχει από το αναμενόμενο
    όσο ο ολοκληρωμένος μήνας k, αλλά μόνο προς το χειρότερο (έσοδα ≤, έξοδα ≥ πρόγραμμα). Επιστρέφει P10/P50/P90 του αποτελέσματος
    έτους και τα σωρευτικά P10/P90 ανά μήνα (μόνο οι μήνες ≥ done), το ίδιο για έσοδα (in) και έξοδα (out)·
    vol_in / vol_out = πολλαπλασιαστής των αποκλίσεων (0 = χωρίς αβεβαιότητα, 2 = διπλάσια)· shift_in / shift_out =
    πολλαπλασιαστής του αναμενόμενου (τα known_* έρχονται ήδη μετατοπισμένα)· None αν < 2 μήνες.
    ponytail: με λίγους μήνες το εύρος υποεκτιμάται — ιστορικό περισσότερων ετών αν χρειαστεί."""
    if done - start < 2:
        return None
    fixed = fixed or [0.0] * 12
    rng = random.Random(seed)
    base = sum(cur_in[:done]) - sum(cur_out[:done]) - sum(fixed[:done])

    def residuals(cur, prev, vol):  # (αναμενόμενο μήνα m, {k: (πραγματικό − αναμενόμενο) × vol})
        # Διαφορά, όχι λόγος: περσινός μήνας ≈ 0 με φετινό μεγάλο δεν βγάζει συντελεστή ×60.
        prev_same = sum(prev[start:done])
        ratio = sum(cur[start:done]) / prev_same if prev_same > 0 else 0
        mean = sum(cur[start:done]) / (done - start)
        exp = lambda m: prev[m] * ratio if prev_same > 0 else mean  # noqa: E731
        return exp, {k: (cur[k] - exp(k)) * vol for k in range(start, done)}

    (e_in, r_in), (e_out, r_out) = residuals(cur_in, prev_in, vol_in), residuals(cur_out, prev_out, vol_out)
    s_in = lambda m, k: max(e_in(m) * shift_in + r_in[k], 0.0)  # noqa: E731
    s_out = lambda m, k: max(e_out(m) * shift_out + r_out[k], 0.0)  # noqa: E731
    if known_in:
        s_in = lambda m, k: max(known_in[m] + min(r_in[k], 0.0), 0.0)  # noqa: E731
    if known_out:
        s_out = lambda m, k: known_out[m] + max(r_out[k], 0.0)  # noqa: E731
    paths, in_paths, out_paths = [], [], []
    for _ in range(runs):
        a_in, a_out = sum(cur_in[:done]), sum(cur_out[:done]) + sum(fixed[:done])
        path, p_in, p_out = [], [], []
        for m in range(done, 12):
            k = rng.randrange(start, done)
            a_in += max(s_in(m, k), cur_in[m])  # ≥ ήδη καταχωρημένα
            a_out += max(s_out(m, k), cur_out[m]) + fixed[m]
            path.append(a_in - a_out)
            p_in.append(a_in)
            p_out.append(a_out)
        paths.append(path)
        in_paths.append(p_in)
        out_paths.append(p_out)
    q = lambda xs: statistics.quantiles(xs, n=10)  # noqa: E731

    def band(ps):  # σωρευτικά P10/P90 ανά μήνα + τελικά P10/P50/P90
        per_month = [q([p[i] for p in ps]) for i in range(12 - done)]
        final = per_month[-1]
        return {"p10": round(final[0], 2), "p50": round(final[4], 2), "p90": round(final[8], 2),
                "lo": [round(m[0], 2) for m in per_month], "hi": [round(m[8], 2) for m in per_month]}

    # in / out: εύρος σωρευτικών εσόδων / εξόδων (έξοδα με τα fixed, δηλ. σταθερά + αποσβέσεις)
    return band(paths) | {"in": band(in_paths), "out": band(out_paths)}


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
                    done: int, dep: float = 0.0, res_label: str = "Αποτέλεσμα") -> dict:
    """Γεωμετρία SVG (viewBox 760×280): σωρευτικές γραμμές εσόδων/εξόδων/αποτελέσματος — συνεχείς
    στους πραγματικούς μήνες, διακεκομμένες στην πρόβλεψη — με βεντάλια P10–P90 και περσινή αναφορά.
    out = σύνολο εξόδων ανά μήνα και res_m = αποτέλεσμα ανά μήνα, και τα δύο με τις αποσβέσεις dep τον Δεκέμβριο.
    Μήνες = len(inc) (≤ 12· π.χ. συνοπτικό βιβλίο τρέχοντος έτους έως τον τρέχοντα μήνα)."""
    from itertools import accumulate

    W, H, left, right, top, bottom = 760, 280, 58, 118, 16, 30
    base = H - bottom
    n = len(inc)
    ci, co, res, pr = (list(accumulate(v)) for v in (inc, out, res_m, prev_res[:n]))
    bands = {}  # key → (εύρος σεναρίων, σωρευτική γραμμή)
    if sc:
        bands = {"in": (sc["in"], ci), "out": (sc["out"], co), "res": (sc, res)}
    values = ci + co + res + pr + [v for b, _ in bands.values() for v in b["lo"] + b["hi"]] + [0]
    step = _nice_step(max(values) - min(min(values), 0))
    lo, hi = step * (min(values) // step), step * -(-max(values) // step)
    hi = hi if hi > lo else lo + step
    slot = (W - left - right) / n
    x = lambda i: round(left + slot * (i + 0.5), 1)  # noqa: E731
    y = lambda v: round(base - (v - lo) / (hi - lo) * (base - top), 1)  # noqa: E731
    pts = lambda vals, idx: " ".join(f"{x(i)},{y(vals[i])}" for i in idx)  # noqa: E731
    split = max(done - 1, 0)
    actual, future = range(done), range(split, n)

    def line(vals):
        return {"actual": "M" + pts(vals, actual) if done > 1 else "", "future": "M" + pts(vals, future),
                "area": f"M{x(0)},{y(max(lo, 0))} L" + pts(vals, range(n)) + f" L{x(n - 1)},{y(max(lo, 0))}Z"}

    def fan(b, cum):  # βεντάλια από το τελευταίο πραγματικό σημείο: πάνω όριο → πίσω από το κάτω
        return ("M" + f"{x(split)},{y(cum[done - 1])} " + " ".join(f"{x(done + i)},{y(v)}" for i, v in enumerate(b["hi"]))
                + " L" + " ".join(f"{x(done + i)},{y(v)}" for i, v in reversed(list(enumerate(b["lo"])))) + "Z")

    fans = {key: fan(b, cum) for key, (b, cum) in bands.items()}
    # Ετικέτες τέλους: ελάχιστη απόσταση 15px ώστε να μη συγκρούονται.
    ends = sorted([{"key": "in", "label": "Έσοδα", "v": ci[-1]}, {"key": "out", "label": "Έξοδα", "v": co[-1]},
                   {"key": "res", "label": res_label, "v": res[-1]}], key=lambda e: y(e["v"]))
    last = -99
    for e in ends:
        e["y"], e["ly"] = y(e["v"]), max(y(e["v"]), last + 15)
        last = e["ly"]
    bw = min(12.0, slot * 0.2)

    def bar(i: int, dx: float, v: float) -> dict:  # μηνιαία μπάρα από το 0 έως το v (αρνητικό: προς τα κάτω)
        return {"x": round(x(i) + dx, 1), "y": min(y(0), y(v)), "h": abs(y(v) - y(0))}

    months = [{"label": _GREEK_MONTHS[i], "cx": x(i), "x": round(left + slot * i, 1), "w": round(slot, 1),
               "bar_in": bar(i, -bw - 1, inc[i]), "bar_out": bar(i, 1, out[i]), "bw": round(bw, 1),
               "in": round(ci[i], 2), "out": round(co[i], 2), "res": round(res[i], 2), "prev": round(pr[i], 2),
               "forecast": i >= done, "dep": dep if i == n - 1 else 0,
               **{f"{key}_{k}": bands[key][0][k][i - done] if key in bands and i >= done else None
                  for key in ("in", "out", "res") for k in ("lo", "hi")}}
              for i in range(n)]
    ticks = [{"v": lo + step * k, "y": y(lo + step * k)} for k in range(int(round((hi - lo) / step)) + 1)]
    return {"w": W, "h": H, "left": left, "right": W - right, "top": top, "base": base, "zero": y(0),
            "split_x": round(left + slot * done, 1), "done": done, "months": months, "ticks": ticks, "ends": ends,
            "in": line(ci), "out": line(co), "res": line(res), "prev": "M" + pts(pr, range(n)), "fans": fans,
            "end_x": x(n - 1)}


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
    vol = {k: min(max(request.args.get(k, 100, type=int), 0), 500) for k in ("vol_in", "vol_out")}  # μεταβλητότητα %
    shift = {k: min(max(request.args.get(k, 0, type=int), -100), hi)
             for k, hi in (("shift_in", 200), ("shift_out", 500))}  # μετατόπιση %

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
    cur_fixed = fixed_m(year)
    cur_var = [o - f for o, f in zip(cur_op, cur_fixed)]

    # Εποχικότητα: έως 5 προηγούμενα έτη (όσα και η «Σύγκριση ετών»), σταθμισμένα υπέρ των πιο πρόσφατων.
    hist = [y for y in reversed(years) if y < year][:_CMP_YEARS - 1]
    var_of = lambda y: [o - f for o, f in zip(operating(y), fixed_m(y))]  # noqa: E731
    base_in, n_in = _blend([col(("income", y), "net") for y in hist], start, done)
    base_var, n_var = _blend([var_of(y) for y in hist], start, done)
    f_in, label_in = _forecast(cur_in, base_in, done, start, n_in)
    f_var, label_var = _forecast(cur_var, base_var, done, start, n_var)
    dep = _depreciation(asset_lines, year, small, large, months=12 - done)  # αγορά μέσα στον τρέχοντα μήνα
    dep_m = [0.0] * 11 + [dep["total"]]  # απόσβεση: εγγραφή τέλους χρήσης

    # Βάση όλης της σελίδας: μέσος όρος (σύνολα έτους) της στατιστικής πρόβλεψης και της προβολής της «Σύγκρισης ετών».
    # Η διαφορά μοιράζεται στους μήνες πρόβλεψης αναλογικά (σχήμα της εποχικότητας)· στα έξοδα μόνο στα μεταβλητά
    # (σταθερά και αποσβέσεις μένουν). Ποτέ κάτω από τα ήδη καταχωρημένα. Πρόγραμμα / sliders εφαρμόζονται μετά.
    # ponytail: ξαναϋπολογίζει όλη τη σύγκριση (6 έτη παραστατικών)· cache αν η σελίδα αργήσει.
    cmp = {c["key"]: c["proj_v"] for c in _compare(cid, now)["cums"]}
    stat_in = round(sum(f_in), 2)
    stat_out = round(sum(f_var) + sum(cur_fixed[:done]) + sum(fixed.values()) * (12 - done) + dep["total"], 2)
    avg = {"income": {"stat": stat_in, "cmp": cmp["in"]}, "expense": {"stat": stat_out, "cmp": cmp["out"]},
           "result": {"stat": round(stat_in - stat_out, 2), "cmp": cmp["res"]}}

    def to_avg(f, cur, stat, other):
        rest = sum(f[done:])
        k = max(rest + (other - stat) / 2, 0.0) / rest if rest > 0 else 1.0
        f[done:] = [round(max(v * k, c), 2) for v, c in zip(f[done:], cur[done:])]
        return k

    k_avg_in, k_avg_out = to_avg(f_in, cur_in, stat_in, cmp["in"]), to_avg(f_var, cur_var, stat_out, cmp["out"])
    label_in, label_var = (lb + " · μέσος όρος με τη Σύγκριση ετών" for lb in (label_in, label_var))
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
    # Μετατόπιση «τι θα γίνει αν» (sliders): ±% στους μήνες πρόβλεψης, ποτέ κάτω από τα ήδη καταχωρημένα.
    k_in, k_out = 1 + shift["shift_in"] / 100, 1 + shift["shift_out"] / 100
    for f, cur, k in ((f_in, cur_in, k_in), (f_var, cur_var, k_out), (f_fixed, cur_fixed, k_out)):
        f[done:] = [round(max(v * k, c), 2) for v, c in zip(f[done:], cur[done:])]

    f_op = [round(a + b, 2) for a, b in zip(f_var, f_fixed)]
    res_m = [round(i - o - d, 2) for i, o, d in zip(f_in, f_op, dep_m)]
    sc = _scenarios(cur_in, cur_var, base_in, base_var, done, seed=year, start=start,
                    fixed=[a + b for a, b in zip(f_fixed, dep_m)],
                    known_in=f_in if planned_in is not None else None, known_out=f_var if planned_out is not None else None,
                    vol_in=vol["vol_in"] / 100, vol_out=vol["vol_out"] / 100, shift_in=k_in * k_avg_in, shift_out=k_out * k_avg_out)
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
        stock_chk=_stock_check(cid, str(year)),
        history=history, sc=sc, start=start, expected=expected, tax=tax, plan_result=total["result"] if planned else None, labels={"income": label_in, "expense": label_var},
        has_prev=any(prev_in) or any(prev_op), prev_in_total=tot(prev_in), errors=errors,
        fixed=[{"label": EXPENSE_TYPES.get(t, t), "code": t, "amount": v} for t, v in fixed.items()],
        dep=dep, form={k: request.args.get(k, "") for k in ("assets_small", "assets_large", "planned_income", "planned_expense")},
        forecast_in=forecast_in, forecast_var=forecast_var, vol=vol, shift=shift, avg=avg,
        depr_limit=_DEPR_FULL_LIMIT, depr_rate=round(_DEPR_RATE * 100),
        chart=_forecast_chart(f_in, [o + d for o, d in zip(f_op, dep_m)], res_m, prev_res, sc, done, dep["total"]),
    )

def _vat_period_args(cid):
    """Έτος/περίοδος ΦΠΑ από το URL (προεπιλογή: η περίοδος που δηλώνεται τώρα) — κοινό για τη
    Δήλωση ΦΠΑ και τη Συμφωνία Φ2."""
    kind = (get_active_company() or {}).get("vat_period") or "m"
    now = datetime.now(ATHENS)
    years = sorted(set(db.document_years(cid)) | {str(now.year)}, reverse=True)
    year = request.args.get("year", "")
    year = year if year in years else str(now.year)
    period = request.args.get("period", "")
    if not (len(period) >= 2 and period[0] == kind and period[1:].isdigit()
            and 1 <= int(period[1:]) <= (12 if kind == "m" else 4)):
        due_y, due_n = _vat_due_period(kind, now)
        period = f"{kind}{due_n}"
        if "year" not in request.args:
            year = str(due_y)
    return years, year, period, kind


@app.route("/reports/vat/reconcile")
def reports_vat_reconcile():
    """Συμφωνία Φ2: από ποιους λογαριασμούς γεμίζει το έντυπο και τι μένει εκτός, ανά βιβλίο."""
    cid = _active_company_id()
    years, year, period, kind = _vat_period_args(cid)
    date_from, date_to = _vat_bounds(int(year), kind, int(period[1:]))
    return render_template(
        "reports_vat_reconcile.html", years=years, year=year, period=period,
        date_from=date_from, date_to=date_to, months=_GREEK_MONTHS, quarters=_QUARTER_NAMES,
        r=vat_return.reconcile(db.period_documents(cid, "income", date_from, date_to),
                               db.period_documents(cid, "expense", date_from, date_to)),
        accounts={**EXPENSE_TYPES, **INCOME_TYPES, **{k: v for k, v in VAT_TYPES.items() if k}},
    )


@app.route("/reports/vat/reconcile/docs")
def reports_vat_reconcile_docs():
    """Τμήμα HTML για το modal της συμφωνίας Φ2: τα παραστατικά ενός κελιού (book, e3, col). Ανά παραστατικό
    η καθαρή αξία / ΦΠΑ του και η αξία / ΦΠΑ που συμμετέχει στο κελί: κωδικός βάσης (30x/36x/34x, εκτός) μαζί
    με τον φόρο του (33x/38x) και αντίστροφα· nd = μόνο ΦΠΑ. Χωρίς e3 = όλες οι γραμμές (σύνολο στήλης)·
    col=net = όλες οι βάσεις + εκτός (αξία) και όλοι οι φόροι + μη εκπιπτόμενος (ΦΠΑ) της γραμμής."""
    cid = _active_company_id()
    years, year, period, kind = _vat_period_args(cid)
    book, col = request.args.get("book"), request.args.get("col", "")
    e3 = request.args.get("e3")  # None = σύνολο στήλης· "" = χωρίς λογαριασμό Ε3
    if book not in ("income", "expense"):
        return "Μη έγκυρη επιλογή.", 400
    date_from, date_to = _vat_bounds(int(year), kind, int(period[1:]))
    docs = db.period_documents(cid, book, date_from, date_to)
    m = vat_return.reconcile(docs if book == "income" else [], docs if book == "expense" else [])[book]
    pairs = [p[:2] for p in vat_return.OUTPUT_RATES.values()] + list(vat_return.INPUT_CODES.values())
    tax_of, base_of = {b: t for b, t in pairs}, {t: b for b, t in pairs}
    if col == "net":
        base_cols, tax_cols = m["base_cols"], [c for c in m["cols"] if c not in m["base_cols"]]
    elif col == vat_return.ND:
        base_cols, tax_cols = [], [col]
    elif col in base_of:
        base_cols, tax_cols = [base_of[col]], [col]
    else:
        base_cols, tax_cols = [col], [tax_of[col]] if col in tax_of else []

    def collect(cols):
        out: dict = {}
        for (row, c), cell in m["docs"].items():
            if c in cols and (e3 is None or row == e3):
                for mark, v in cell.items():
                    out[mark] = out.get(mark, 0.0) + v
        return out

    def cell(c):  # το ποσό του κελιού όπως στον πίνακα (ο πίνακας αθροίζει στρογγυλεμένες γραμμές)
        if e3 is None:
            return m["totals"].get(c, 0.0)
        return next((r["vals"].get(c, 0.0) for r in m["rows"] if r["e3"] == e3), 0.0)

    part, part_vat = collect(base_cols), collect(tax_cols)
    rows = []
    for d in docs:
        p, pv = part.get(d["mark"], 0.0), part_vat.get(d["mark"], 0.0)
        if abs(p) >= 0.005 or abs(pv) >= 0.005:
            sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
            net, vat = sign * (d["total_net"] or 0.0), sign * (d["total_vat"] or 0.0)
            # Ολόκληρο = η αξία και ο ΦΠΑ του παραστατικού πέφτουν όλα σε αυτό το κελί (nd: μόνο ο ΦΠΑ).
            whole = abs(pv - vat) < 0.015 and (not base_cols or abs(p - net) < 0.015)
            rows.append(dict(d, part=round(p, 2), part_vat=round(pv, 2), credit=sign < 0, net=net, vat=vat, whole=whole))
    total = {"net": round(sum(r["net"] for r in rows), 2), "vat": round(sum(r["vat"] for r in rows), 2),
             "part": round(sum(cell(c) for c in base_cols), 2), "part_vat": round(sum(cell(c) for c in tax_cols), 2)}
    # Σελιδοποίηση της λίστας (τα σύνολα από όλα τα παραστατικά)· q = οι παράμετροι του modal για τον pager.
    page_docs, page, pages = paginate(rows, request.args.get("page"))
    q = {k: v for k, v in request.args.items() if k != "page"}
    accounts = {**EXPENSE_TYPES, **INCOME_TYPES}
    return render_template(
        "_reconcile_docs.html", docs=page_docs, n_docs=len(rows), page=page, pages=pages, q=q, total=total, book=book, col=col, clicked_tax=col in tax_cols and col != "net",
        e3=e3, e3_label=accounts.get(e3 or "", ""), type_names=INVOICE_TYPE_NAMES, year=year, period=period,
    )


@app.route("/reports/vat")
def reports_vat():
    """Δήλωση ΦΠΑ (Φ2) για μήνα (period=m1…m12) ή τρίμηνο (q1…q4) ενός έτους, ανάλογα με την
    περίοδο ΦΠΑ της εταιρείας (vat_period). Το 401 υπολογίζεται αυτόματα από την προηγούμενη
    περίοδο· τιμή στο prev_credit υπερισχύει (κενό = αυτόματα)."""
    cid = _active_company_id()
    years, year, period, kind = _vat_period_args(cid)
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



def _assets_card(cid: int, year: int, month: int) -> dict:
    """Πάγια για τον πίνακα ελέγχου: αγορές (κατηγορία 2.7) φέτος / πέρσι, καταχωρημένες αποσβέσεις (E3_587) φέτος / πέρσι,
    αποσβέσεις έτους κατά τον κανόνα (_depreciation) και αναπόσβεστη αξία 31/12 των παγίων ≥ 1.500 € της 5ετίας.
    Διάγραμμα (viewBox 760×220): ανά μήνα δύο μπάρες — αγορές φέτος (έως τον τρέχοντα μήνα month) και πέρσι."""
    docs = {y: db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, str(y)) for y in range(year - 4, year + 1)}
    lines = {y: _expense_lines(d)[1] for y, d in docs.items()}
    dep = _depreciation(lines, year)
    # Αναπόσβεστη αξία: 20% τον χρόνο από τη χρήση αγοράς (μαζί με αυτήν), όπως στο _depreciation.
    residual = sum(v * max(0.0, 1 - _DEPR_RATE * (year - y + 1))
                   for y, vs in lines.items() for v in vs if abs(v) >= _DEPR_FULL_LIMIT)
    prev_t, cur_t = _yearly_totals(docs[year - 1]), _yearly_totals(docs[year])
    rows = [{"expense": cur_t[m]} for m in range(1, month + 1)]
    buy = [r["expense"]["assets"] for r in rows] + [None] * (12 - len(rows))
    pbuy = [prev_t[m]["assets"] for m in range(1, 13)]

    W, H, left, right, top, bottom = 760, 220, 58, 8, 14, 30
    base = H - bottom
    step = _nice_step(max([v or 0 for v in buy + pbuy] + [0]))
    n_ticks = max(1, -(-max([v or 0 for v in buy + pbuy] + [0]) // step))
    ymax, slot = step * n_ticks, (W - left - right) / 12
    bar_w = min(20.0, slot * 0.3)
    y = lambda v: round(base - max(v, 0) / ymax * (base - top), 1)  # noqa: E731
    bar = lambda x, v: (round(x, 1), y(v), round(bar_w, 1), round(base - y(v), 1)) if v and base - y(v) >= 0.5 else None  # noqa: E731
    months = []
    for i in range(12):
        cx = left + slot * (i + 0.5)
        months.append({"label": _GREEK_MONTHS[i], "cx": round(cx, 1), "x": round(left + slot * i, 1), "w": round(slot, 1),
                       "buy": buy[i], "pbuy": pbuy[i],
                       "bar": bar(cx + 1, buy[i]), "pbar": bar(cx - bar_w - 1, pbuy[i])})  # πέρσι αριστερά, φέτος δεξιά
    return {
        "dep": dep, "residual": round(residual, 2),
        "bought": round(sum(v or 0 for v in buy), 2), "p_bought": round(sum(pbuy), 2),
        "booked": round(sum(r["expense"]["depreciation"] for r in rows), 2),
        "p_booked": round(sum(prev_t[m]["depreciation"] for m in range(1, 13)), 2),
        "empty": not any(lines.values()) and not any(r["expense"]["depreciation"] for r in rows),
        "chart": {"w": W, "h": H, "left": left, "right": W - right, "top": top, "base": base, "months": months,
                  "ticks": [{"v": step * k, "y": y(step * k)} for k in range(int(n_ticks) + 1)]},
    }



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
    inc_y = db.yearly_documents(cid, "income", _INCOME_CLASSIFIED_STATUSES, year)  # μία φορά: και για _pl/_stock_check
    exp_y = db.yearly_documents(cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, year)
    income, expense = _yearly_totals(inc_y, with_stock), _yearly_totals(exp_y, with_stock)
    rows = [{"label": f"{_GREEK_MONTHS[m - 1]} {year}", "income": income[m], "expense": expense[m],
             "balance": round(income[m]["net"] - expense[m]["net"], 2)} for m in range(1, now.month + 1)]
    ytd = lambda part, col: round(sum(r[part][col] for r in rows), 2)  # noqa: E731
    kpi = {
        "income": ytd("income", "net"), "expense": ytd("expense", "net"),
        "vat": round(ytd("income", "vat") - ytd("expense", "vat"), 2),
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
    codes = vat_return.compute(  # χωρίς raw_xml: η κάρτα δείχνει μόνο 511/502/337/430
        db.period_documents(cid, "income", date_from, date_to, xml=False),
        db.period_documents(cid, "expense", date_from, date_to, xml=False),
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

    def todo(n, icon, title, sub, href, tone, post=None):
        if n:  # post: πεδία φόρμας → POST στο href αντί για σύνδεσμο
            todos.append({"n": n, "icon": icon, "title": title, "sub": sub, "href": href, "tone": tone, "post": post})

    todo(exp_st.get("unclassified", 0), "🧾", "Έξοδα προς χαρακτηρισμό", "Αχαρακτήριστα παραστατικά εξόδων",
         url_for("invoices", view="unclassified"), "warn")
    todo(exp_st.get("classified", 0), "📤", "Χαρακτηρισμοί προς αποστολή", "Έτοιμοι τοπικά — αποστολή στο myDATA",
         url_for("invoices", view="classified"), "accent")
    todo(exp_st.get("sent", 0), "⏳", "Αναμένουν επιβεβαίωση", "Απεσταλμένα — επιβεβαιώνονται στην επόμενη ανάκτηση",
         url_for("invoices", view="sent"), "info")
    todo(inc_st.get("unclassified", 0), "💶", "Έσοδα προς χαρακτηρισμό", "Αχαρακτήριστα παραστατικά εσόδων",
         url_for("income", view="unclassified"), "warn")
    due_monthly = [t for t in _recurring_pending(cid, today.strftime("%Y-%m")) if t["monthly_day"] <= today.day]
    todo(len(due_monthly), "🔁", "Προγραμματισμένες εγγραφές προς δημιουργία", ", ".join(t["name"] for t in due_monthly),
         url_for("recurring"), "accent")
    # «Νέα από MARK» ενεργό: ανάκτηση όταν πέρασε το διάστημα ελέγχου της εταιρείας από την τελευταία.
    # ponytail: μετράει κάθε ανάκτηση (και διαστήματος), όχι μόνο «Νέα από MARK»
    ready = [k for k in ("income", "expense") if _mark_ready(cid, k)]
    ats = [datetime.strptime(a, "%d/%m/%Y %H:%M").replace(tzinfo=ATHENS) for k in ready
           if (a := json.loads(db.get_setting(_last_key(k, cid)) or "{}").get("at"))]
    due = min(ats) + timedelta(hours=company["mark_check_hours"]) if ats else None
    fetch_due_in = None  # δευτερόλεπτα ως τη λήξη: η σελίδα ανανεώνει τότε το «Τι εκκρεμεί»
    if ready and (len(ats) < len(ready) or now >= due):
        seen = len(ats) == len(ready)
        mins = int((now - min(ats)).total_seconds() // 60) if seen else 0
        todo((f"{mins // 60}ώ" if mins >= 60 else f"{mins}λ") if seen else "—", "🔄", "Ανάκτηση παραστατικών",
             (f"Τελευταία ανάκτηση {min(ats):%d/%m/%Y %H:%M}" if seen else "Καμία ανάκτηση ακόμα") + " · Νέα από MARK",
             url_for("sync_run"), "info",
             {"scope": "both" if len(ready) == 2 else ready[0], "action": "new", "back": "dashboard",
              "date_from": today.isoformat(), "date_to": today.isoformat()})  # ημερομηνίες: μόνο για την επικύρωση
    elif ready:
        fetch_due_in = int((due - now).total_seconds()) + 1

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
    exp_docs = db.period_documents(cid, "expense", f"{year}-01-01", f"{year}-12-31", xml=False)
    inc_docs = db.period_documents(cid, "income", f"{year}-01-01", f"{year}-12-31", xml=False)

    def top_of(docs, stock):
        return sorted(_counterparty_totals(docs, stock).values(), key=lambda t: -t["amount"])[:5]

    top = top_of(exp_docs, not with_stock)
    # Λιανική (χωρίς ΑΦΜ) δεν είναι πελάτης.
    top_in = top_of([d for d in inc_docs if d["counterparty_vat"]], False)
    recent = sorted([dict(d, kind="expense") for d in exp_docs] + [dict(d, kind="income") for d in inc_docs],
                    key=lambda d: (d["issue_date"] or "", d["mark"] or ""), reverse=True)[:12]
    for d in recent:
        d["credit"] = d["invoice_type"] in CREDIT_INVOICE_TYPES

    months = _last_12_months(now)  # μηνιαίος «καθρέφτης» στην κεφαλίδα
    return render_template(
        "dashboard.html", company=company, year=year, kpi=kpi, chart=_yearly_chart(rows), vat=vat,
        pl=_pl(inc_y, exp_y),  # όπως στο συνοπτικό βιβλίο
        stock_chk=_stock_check(cid, year, exp_y),
        vat_periods=_yearly_vat_periods(rows, year, now),
        todos=todos, fetch_due_in=fetch_due_in, segments=segments,
        exp_total=exp_total, done_pct=done_pct, inc_st=inc_st,
        top=top, top_max=max([t["amount"] for t in top] + [1]),
        top_in=top_in, top_in_max=max([t["amount"] for t in top_in] + [1]), recent=recent,
        type_names=INVOICE_TYPE_NAMES,
        months=months, this_month=months[-1], monthly=db.company_month_counts(months[0] + "-01").get(cid, {}),
        strip=_month_strip(cid, _SYNC_SCOPES["both"]),
        # Κλικ σε μήνα/τρίμηνο της λωρίδας → Ανάκτηση με αυτό το διάστημα (σε τρόπο MARK: καρτέλα προηγούμενων περιόδων).
        strip_href=url_for("sync", tab="past" if (db.get_setting("sync_mode") or "mark") == "mark" else "main"),
    )


@app.route("/dashboard/assets")
def dashboard_assets():
    """Κάρτα «Πάγια» του πίνακα ελέγχου (τμήμα HTML): φορτώνεται μετά τη σελίδα, γιατί διαβάζει 5 χρόνια εξόδων."""
    now = datetime.now(ATHENS)
    return render_template("_dashboard_assets.html", assets=_assets_card(_active_company_id(), now.year, now.month),
                           year=str(now.year))


@app.route("/reports")
def reports():
    """OLAP: κύβος ανάλυσης εσόδων-εξόδων (pivot, slice, drill-down στον browser)."""
    return render_template("reports.html", cube=_olap_cube(_active_company_id()))


def _copy_lines(inv, local: list[dict], exemptions: dict | None = None) -> list[dict]:
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
                    "vat_type": (r.get("vat_type") or "VAT_361") if needs_vat_cls(ln.vat_category) else "",
                    "vat_exemption": (exemptions or {}).get(ln.line_number, ""),
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
            "lines": _copy_lines(inv, db.get_local_classification(row["id"]), vat_return._parse_exempt(row)[1]),
        }
    draft["issue_date"] = datetime.now(ATHENS).strftime("%Y-%m-%d")
    draft["aa"] = ""  # ο Α/Α πληκτρολογείται από τον χρήστη
    return draft


@app.route("/new_expense")
def new_expense():
    """Η παλιά «Νέα εγγραφή»: όλα πλέον από τη Φόρμα καταχώρησης."""
    return redirect(url_for("entry", **request.args))


@app.route("/draft/<mark>/delete", methods=["POST"])
def draft_delete(mark):
    """Διαγραφή τοπικού draft (δεν έχει διαβιβαστεί στο myDATA)."""
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None or row.get("local_action") != "create":
        flash("Η εγγραφή δεν βρέθηκε ή δεν είναι τοπικό πρόχειρο.", "error")
        return redirect(url_for("invoices", view="classified"))
    db.delete_document(cid, mark)
    flash(f"✔ Η τοπική εγγραφή {mark} διαγράφηκε.", "ok")
    return redirect(url_for("invoices", view="classified"))


# ------------------------------------------------------------------ #
# ΦΟΡΜΑ ΚΑΤΑΧΩΡΗΣΗΣ — κάθε τύπος παραστατικού (όπως η Ειδική Φόρμα της ΑΑΔΕ).
# Έξοδα → τοπικό πρόχειρο στα «Χαρακτηρισμένα» (μαζική «Αποστολή»)· έσοδα → άμεση αποστολή.
# ------------------------------------------------------------------ #
# Είδη και τύποι παραστατικού, με τα ονόματα και τη σειρά της Ειδικής Φόρμας της ΑΑΔΕ (aade_form.py).
_ENTRY_FAMILIES = {code: label for code, label in aade_form.OPTIONS["family"]}
_ENTRY_TYPES = {code: label for code, label in aade_form.OPTIONS["invoice_type"]}


# Έτοιμα πρότυπα: τύπος, εκδότης, γραμμές και Ε3 (επιτρεπτοί συνδυασμοί ΑΑΔΕ)· ποσά, σειρά και Α/Α ο χρήστης.
_ENTRY_PRESETS = {
    "efka": ("ΕΦΚΑ", {"invoice_type": "14.5", "issuer": {"country": "GR", "vat": "997072577"},
                      "lines": [{"vat_category": "8", "cls": [{"category": "category2_5", "type": "E3_585_007"}]}]}),
    "rent": ("Ενοίκιο", {"invoice_type": "16.1",
                         "lines": [{"vat_category": "8", "cls": [{"category": "category2_5", "type": "E3_585_014"}]}],
                         "taxes": [{"type": "4", "category": "3"}]}),
    "payroll": ("Μισθοδοσία", {"invoice_type": "17.1",
                               "lines": [{"vat_category": "8", "cls": [{"category": "category2_6", "type": "E3_581_001"}]},
                                         {"vat_category": "8", "cls": [{"category": "category2_6", "type": "E3_581_002"}]}],
                               "taxes": [{"type": "1", "category": "11"}, {"type": "5"}],
                               "payments": [{"type": "1"}]}),  # Επαγ. Λογαριασμός Πληρωμών Ημεδαπής
    "stock": ("Απογραφή", {"invoice_type": "17.5",
                           "lines": [{"vat_category": "8", "cls": [{"category": "category2_13", "type": "E3_101"}]},
                                     {"vat_category": "8", "cls": [{"category": "category2_14", "type": "E3_104"}]}]}),
}


def _as_entry(d: dict) -> dict:
    """Draft της Φόρμας καταχώρησης· τα παλιά της «Νέας εγγραφής» (πρότυπα, πρόχειρα) μετατρέπονται."""
    from mydata_client import OMISSION_TYPES, type_rules

    if not d or d.get("form") == "entry":
        return d
    t = d.get("invoice_type", "")
    lines = []
    for ln in d.get("lines") or []:
        if not ln:
            continue
        net = ln.get("amount")
        cls = [{"category": ln.get("classification_category", ""), "type": ln.get("classification_type", ""), "amount": net}]
        if ln.get("vat_type"):
            cls.append({"category": "", "type": ln["vat_type"], "amount": net})
        lines.append({"net": net, "vat_category": ln.get("vat_category") or "8", "vat_amount": ln.get("vat_amount") or 0.0,
                      "vat_exemption": ln.get("vat_exemption", ""), "cls": cls})
    withheld, deductions = d.get("withheld_amount") or 0.0, d.get("deductions_amount") or 0.0
    taxes = ([{"type": "1", "category": "11", "base": d.get("withheld_base"), "amount": withheld}] if withheld else []) \
        + ([{"type": "5", "category": "", "base": None, "amount": deductions}] if deductions else [])
    gross = round(sum((ln["net"] or 0) + ln["vat_amount"] for ln in lines) - withheld - deductions, 2)
    return {
        "form": "entry",
        "invoice_type": t,
        **{k: d.get(k) or "" for k in ("series", "aa", "issue_date")},
        "omission": t in OMISSION_TYPES,
        "issuer": {"country": d.get("issuer_country") or "GR", "vat": d.get("issuer_vat") or "",
                   **{k: d.get("issuer_" + k) or "" for k in ("name", "street", "postal_code", "city")}},
        "payments": [{"type": d.get("payment_method") or "5", "amount": gross}] if type_rules(t)["payment"] else [],
        "taxes": taxes,
        "lines": lines,
    }


def _self_counterparty(d: dict) -> str:
    """Ο συναλλασσόμενος (counterparty_vat) που θα αποθηκευτεί για το draft (εκδότης· εμείς όπου εκδίδουμε)."""
    from mydata_client import entry_roles

    role = entry_roles(d.get("invoice_type", ""), d.get("self_pricing"), d.get("omission"))["issuer"]
    return get_own_vat() if role == "own" else "" if role == "none" else ((d.get("issuer") or {}).get("vat") or "").strip()


@app.route("/entry")
def entry():
    from mydata_client import SELF_EXPENSE_TYPES

    cid = _active_company_id()
    today = datetime.now(ATHENS).strftime("%Y-%m-%d")
    edit_mark = request.args.get("edit", "").strip()
    draft, template_id = None, ""
    if edit_mark:
        row = db.get_document(cid, edit_mark) if cid else None
        if row and row.get("local_action") == "create":
            draft = _as_entry(json.loads(row.get("draft_json") or "{}"))
        else:
            flash("Η εγγραφή προς επεξεργασία δεν βρέθηκε.", "error")
            edit_mark = ""
    elif request.args.get("copy"):  # αντίγραφο ολοκληρωμένης εγγραφής: σημερινή ημ/νία, κενός Α/Α
        row = db.get_document(cid, request.args["copy"]) if cid else None
        if row and (json.loads(row.get("draft_json") or "{}").get("form") == "entry"
                    or (row.get("status") == "confirmed" and row.get("invoice_type") in SELF_EXPENSE_TYPES)):
            draft = dict(_as_entry(_copy_draft(row)), issue_date=today, aa="")
        else:
            flash("Αντιγραφή γίνεται μόνο από δική μας εγγραφή εξόδου (13.x–17.x).", "error")
    elif request.args.get("template"):  # πρότυπο: σημερινή ημ/νία, Α/Α συνέχεια της σειράς του
        template = db.get_template(cid, request.args["template"])
        if template:
            d = _as_entry(template["draft"])
            draft = dict(d, issue_date=today, aa=str(db.next_aa(cid, _self_counterparty(d), d.get("series", ""))))
            template_id = str(template["id"])
        else:
            flash("Το πρότυπο δεν βρέθηκε.", "error")
    elif request.args.get("preset") in _ENTRY_PRESETS:
        draft = _ENTRY_PRESETS[request.args["preset"]][1]
    return _render_entry(draft, edit_mark, template_id=template_id)


def _render_entry(draft: dict | None, edit_mark: str = "", status: int = 200, template_id: str = ""):
    from mydata_client import ENTRY_OPTIONS, ENTRY_RATES, ENTRY_RULES, SELF_TYPES_ISSUER_OPTIONAL, entry_roles

    company = get_active_company() or {}
    opt = aade_form.OPTIONS  # [κωδικός, ετικέτα(, συντελεστής)] με τη σειρά της ΑΑΔΕ
    # Ε3: οι ετικέτες της ΑΑΔΕ, αλλά μόνο όσα δέχεται το XSD (π.χ. το E3_585_017 η φόρμα της ΑΑΔΕ το έχει, το XSD όχι).
    inc_ok, exp_ok = set(xsd_enum("IncomeClassificationValueType")), set(xsd_enum("ExpensesClassificationValueType"))
    e3 = {"income": [x for x in opt["income_e3"] if x[0] in inc_ok],
          "expense": [x for x in opt["expense_e3"] if x[0] in exp_ok and x[0].startswith("E3_")]}
    return render_template(
        "entry.html",
        draft=draft,
        edit_mark=edit_mark,
        types=_ENTRY_TYPES,
        families=_ENTRY_FAMILIES,
        # ρόλοι ανά τύπο × (αυτοτιμολόγηση, παράλειψη εκδότη): ίδια πηγή με τον server
        roles={t: {f"{int(sp)}{int(om)}": entry_roles(t, sp, om) for sp in (False, True) for om in (False, True)}
               for t in _ENTRY_TYPES},
        issuer_optional=sorted(SELF_TYPES_ISSUER_OPTIONAL),
        rules=ENTRY_RULES,
        eu_countries=sorted(EU_COUNTRIES),
        combos={t: {c: [e for e in es if e in ("*", "-") or e in inc_ok | exp_ok]
                    for c, es in db.combos_for_type(t).items()} for t in _ENTRY_TYPES},
        opt=ENTRY_OPTIONS,  # για τα select του template (τα dict κρατούν τη σειρά της ΑΑΔΕ)
        js_opt={"income_category": opt["income_category"], "expense_category": opt["expense_category"],
                "e3": e3, "vat_cls": [x for x in opt["expense_e3"] if x[0] in exp_ok and not x[0].startswith("E3_")],
                "tax": {tt: [x[:2] for x in opt[k]] for tt, k in (("1", "withheld"), ("2", "fees"), ("3", "other"), ("4", "stamp"))},
                "vat": [x[:2] for x in opt["vat"]]},
        rates=ENTRY_RATES,
        tax_types=dict(aade_form.TAX_TYPES),
        # όλες οι χώρες του CountryType· τα ελληνικά ονόματα τα συμπληρώνει ο browser (Intl)
        countries={c: XML_EXTRA_CODES["country"].get(c, c) for c in xsd_enum("CountryType")},
        own={"vat": get_own_vat(), "name": company.get("company_name", "")},
        suppliers=db.list_suppliers(_active_company_id()),
        today=datetime.now(ATHENS).strftime("%Y-%m-%d"),
        templates=db.list_templates(_active_company_id()),
        presets={k: name for k, (name, _) in _ENTRY_PRESETS.items()},
        template_id=template_id,
        back=_safe_back(request.values.get("back")) or "",
    ), status


def _is_vat_cls(ctype: str) -> bool:
    """Χαρακτηρισμός ΦΠΑ της Φόρμας καταχώρησης: VAT_36x ή NOT_VAT_295 (όχι Ε3)."""
    return ctype.startswith(("VAT_", "NOT_VAT_"))


def _entry_clean(raw: dict) -> tuple[dict, list[str]]:
    """JSON της Φόρμας καταχώρησης → καθαρό draft (αριθμοί, εκδότης/αντισυμβαλλόμενος) και λίστα λαθών."""
    from mydata_client import (ENTRY_OPTIONS, ENTRY_RULES, ENTRY_TAX_KEYS, REC_NEGATIVE_TYPES,
                               SELF_TYPES_ISSUER_OPTIONAL, VAT_EXPENSE_TYPE, entry_country_rule, entry_roles,
                               entry_totals)

    O = ENTRY_OPTIONS  # οι επιλογές της Ειδικής Φόρμας της ΑΑΔΕ: μόνο αυτές γίνονται δεκτές
    tax_label = dict(aade_form.TAX_TYPES)

    errors: list[str] = []

    def s(v) -> str:
        return str(v if v is not None else "").strip()

    def num(v) -> float:
        try:
            return round(float(s(v).replace(",", ".")), 2) if s(v) else 0.0
        except ValueError:
            errors.append(f"Μη έγκυρο ποσό «{s(v)}».")
            return 0.0

    def party(p) -> dict:
        p = p if isinstance(p, dict) else {}
        # known_name: επωνυμία από τον κατάλογο / VIES (για αποθήκευση· στην Ελλάδα δεν στέλνεται στο XML)
        return {k: s(p.get(k)) for k in ("country", "vat", "name", "branch", "postal_code", "street", "number", "city",
                                         "known_name")}

    t = s(raw.get("invoice_type"))
    d = {
        "form": "entry",
        "invoice_type": t,
        **{k: s(raw.get(k)) for k in ("series", "aa", "issue_date", "special_category", "currency", "exchange_rate",
                                      "dispatch_date",
                                      "dispatch_time", "vehicle", "move_purpose", "correlated")},
        **{k: bool(raw.get(k)) for k in ("vat_suspension", "self_pricing", "omission", "dispatch")},
        "tax_level": "line" if raw.get("tax_level") == "line" else "doc",
        "issuer": party(raw.get("issuer")),
        "counterpart": party(raw.get("counterpart")),
    }
    d["currency"] = d["currency"] or "EUR"
    for p in (d["issuer"], d["counterpart"]):
        if p["country"] and p["country"] not in xsd_enum("CountryType"):
            errors.append(f"Μη έγκυρη χώρα «{p['country']}».")
    if d["currency"] not in O["currency"]:
        errors.append(f"Μη έγκυρο νόμισμα «{d['currency']}».")
    for k, opts, label in (("special_category", "special", "ειδική κατηγορία"), ("move_purpose", "move_purpose", "σκοπός διακίνησης")):
        if d[k] and d[k] not in O[opts]:
            errors.append(f"Μη έγκυρη τιμή «{d[k]}» στο πεδίο {label}.")

    if d["currency"] == "EUR":
        d["exchange_rate"] = ""  # ΑΑΔΕ: ισοτιμία μόνο όταν το νόμισμα δεν είναι EUR
    else:
        try:
            ok = float(d["exchange_rate"].replace(",", ".")) > 0
        except ValueError:
            ok = False
        if not ok:
            errors.append(f"Συμπλήρωσε την ισοτιμία {d['currency']} προς ευρώ.")
        else:
            d["exchange_rate"] = d["exchange_rate"].replace(",", ".")
    d["payments"] = [{"type": s(p.get("type")), "amount": num(p.get("amount")), "info": s(p.get("info"))}
                     for p in raw.get("payments") or [] if s(p.get("type"))]
    line_taxes = d["tax_level"] == "line"
    d["lines"] = [
        {
            "rec_type": s(ln.get("rec_type")),
            "not_vat195": bool(ln.get("not_vat195")),
            "unit": s(ln.get("unit")),
            "quantity": s(ln.get("quantity")),
            "net": num(ln.get("net")),
            "discount_option": bool(ln.get("discount_option")),
            "vat_category": s(ln.get("vat_category")),
            "vat_amount": num(ln.get("vat_amount")),
            "vat_exemption": s(ln.get("vat_exemption")),
            "comments": s(ln.get("comments")),
            **{k: num(ln.get(k)) if line_taxes else 0.0 for k in ENTRY_TAX_KEYS.values()},
            **{k + "_cat": s(ln.get(k + "_cat")) if line_taxes else "" for k in ("withheld", "fees", "other", "stamp")},
            "cls": [{"category": s(c.get("category")), "type": s(c.get("type")), "amount": num(c.get("amount"))}
                    for c in ln.get("cls") or [] if s(c.get("category")) or s(c.get("type"))],
        }
        for ln in raw.get("lines") or []
    ]
    d["taxes"] = [] if line_taxes else [
        {"type": s(x.get("type")), "category": s(x.get("category")),
         "base": num(x.get("base")) if s(x.get("base")) else None, "amount": num(x.get("amount"))}
        for x in raw.get("taxes") or [] if s(x.get("type"))
    ]

    if t not in _ENTRY_TYPES:
        return d, ["Επίλεξε τύπο παραστατικού."]
    # Πεδία που δεν ισχύουν για τον τύπο (η φόρμα τα κρύβει, όπως η Ειδική Φόρμα της ΑΑΔΕ): δεν στέλνονται.
    fam = t.split(".")[0]
    hidden = set(ENTRY_RULES["hidden"].get(t, []))
    no_vat = t in ENTRY_RULES["no_vat"]
    vat_ok = ({"8"} if no_vat else {"7"} if t in ENTRY_RULES["vat_zero"]
              else set(O["vat"]) - {"7", "8"} if t in ENTRY_RULES["vat_mandatory"] else set(O["vat"]) - {"8"})
    if "self_pricing" in hidden:
        d["self_pricing"] = False
    if not d["dispatch"] or "dispatch" in hidden:
        d.update(dispatch=False, dispatch_date="", dispatch_time="", vehicle="", move_purpose="")
    if t not in ENTRY_RULES["correlated"]:
        d["correlated"] = ""
    if no_vat or entry_roles(t, d["self_pricing"], d["omission"])["side"] != "income":
        d["vat_suspension"] = False  # αναστολή καταβολής ΦΠΑ: μόνο σε πωλήσεις με ΦΠΑ
    if "payments" in hidden:
        d["payments"] = []
    elif fam == "17" and not d["payments"]:  # ΑΑΔΕ: στα 17.x (όπου δηλώνεται) ο τρόπος πληρωμής είναι υποχρεωτικός
        errors.append(f"Ο τύπος {t} θέλει τρόπο εξόφλησης.")
    if any(p["type"] not in O["payment"] for p in d["payments"]):
        errors.append("Μη έγκυρος τρόπος εξόφλησης.")
    excluded = ENTRY_RULES["excluded_tax_options"].get(t, {})
    for tx in d["taxes"]:
        if ENTRY_TAX_KEYS.get(tx["type"]) in hidden:
            errors.append(f"Ο τύπος {t} δεν δέχεται «{tax_label[tx['type']]}».")
        elif tx["category"] in excluded.get(tx["type"], []):
            errors.append(f"Ο τύπος {t} δεν δέχεται την κατηγορία {tx['category']} στο «{tax_label[tx['type']]}».")
    if t not in ENTRY_RULES["special_only"].get(d["special_category"], [t]):
        errors.append(f"Η ειδική κατηγορία {d['special_category']} επιτρέπεται μόνο στα "
                      f"{', '.join(ENTRY_RULES['special_only'][d['special_category']])}.")
    if not (d["series"] and d["aa"] and d["issue_date"]):
        errors.append("Συμπλήρωσε σειρά, Α/Α και ημερομηνία έκδοσης.")
    if d["omission"] and t not in ENTRY_RULES["omission"]:
        errors.append(f"Διαβίβαση λόγω παράλειψης εκδότη επιτρέπεται μόνο στα {', '.join(ENTRY_RULES['omission'])}.")
    roles = entry_roles(t, d["self_pricing"], d["omission"])
    side_prefix = "category1_" if roles["side"] == "income" else "category2_"

    # Εκδότης / αντισυμβαλλόμενος: όπου είμαστε εμείς, το ΑΦΜ μπαίνει από τον server.
    own_vat = get_own_vat()
    if not own_vat:
        errors.append("Λείπει το ΑΦΜ της εταιρείας — συμπλήρωσέ το στη Διαχείριση εταιρειών.")
    for key, label, role in (("issuer", "εκδότη", roles["issuer"]), ("counterpart", "αντισυμβαλλόμενου", roles["counterpart"])):
        p = d[key]
        if role == "own":
            d[key] = dict(party({}), country="GR", vat=own_vat, branch=p["branch"] or "0")
        elif role == "none":
            d[key] = party({})
        else:
            rule = entry_country_rule(key, t)
            p["country"] = "GR" if rule == "gr" else (p["country"] or "").upper()
            if rule == "eu" and p["country"] not in EU_COUNTRIES:
                errors.append(f"Ο τύπος {t} θέλει {label} από χώρα της ΕΕ (εκτός Ελλάδας).")
            elif rule == "third" and (p["country"] in EU_COUNTRIES or p["country"] in ("", "GR")):
                errors.append(f"Ο τύπος {t} θέλει {label} από χώρα εκτός ΕΕ.")
            if not p["vat"]:
                if key == "issuer" and t not in SELF_TYPES_ISSUER_OPTIONAL:
                    errors.append(f"Συμπλήρωσε το ΑΦΜ του {label}.")
            elif p["country"] != "GR" and not (p["name"] and p["postal_code"] and p["city"]):
                errors.append(f"Για {label} εκτός Ελλάδας συμπλήρωσε επωνυμία, Τ.Κ. και πόλη.")

    if not d["lines"]:
        errors.append("Συμπλήρωσε τουλάχιστον μία γραμμή.")
    for n, ln in enumerate(d["lines"], start=1):
        if no_vat:  # «Εγγραφές χωρίς ΦΠΑ»
            ln.update(vat_category="8", vat_amount=0.0, vat_exemption="")
        # Φ2 – μη συμμετοχή: μόνο έσοδα 1.1–11.5, όχι με αυτοτιμολόγηση (ΑΑΔΕ)
        if roles["side"] != "income" or fam not in ENTRY_RULES["not_vat195_families"] or d["self_pricing"]:
            ln["not_vat195"] = False
        if "unit" in hidden:
            ln.update(unit="", quantity="")
        for k in ("withheld", "fees", "other", "stamp", "deductions"):  # φόροι γραμμής που κρύβει η ΑΑΔΕ
            if k in hidden:
                ln[k] = 0.0
                ln[k + "_cat"] = ""
        for tt, cats in excluded.items():
            k = ENTRY_TAX_KEYS[tt]
            if ln[k] and ln[k + "_cat"] in cats:
                errors.append(f"Γραμμή {n}: ο τύπος {t} δεν δέχεται την κατηγορία {ln[k + '_cat']} στο «{tax_label[tt]}».")
        if ln["unit"] and ln["unit"] not in O["unit"]:
            errors.append(f"Γραμμή {n}: μη έγκυρη μονάδα μέτρησης.")
        if not ln["unit"]:
            ln["quantity"] = ""
        rec = ln["rec_type"]
        if rec and rec not in O["rec_type"]:
            errors.append(f"Γραμμή {n}: μη έγκυρη κατηγορία γραμμής.")
        # ΑΑΔΕ: «Τέλος με ΦΠΑ» μόνο όπου ο τύπος δέχεται τέλη, «Λοιποί Φόροι με ΦΠΑ» όπου δέχεται λοιπούς φόρους.
        elif rec in ("2", "3") and {"2": "fees", "3": "other"}[rec] in hidden:
            errors.append(f"Γραμμή {n}: ο τύπος {t} δεν δέχεται «{O['rec_type'][rec]}».")
        elif rec == "7" and t not in REC_NEGATIVE_TYPES:
            errors.append(f"Γραμμή {n}: «{O['rec_type']['7']}» επιτρέπεται μόνο στα 17.3, 17.4, 17.5 και 17.6.")
        if ln["rec_type"] in ("2", "3") and any(ln[k] for k in ENTRY_TAX_KEYS.values()):
            errors.append(f"Γραμμή {n}: σε γραμμή τέλους/λοιπών φόρων με ΦΠΑ το ποσό μπαίνει στην καθαρή αξία "
                          "και δεν επιτρέπονται φόροι γραμμής (ΑΑΔΕ).")
        if ln["vat_category"] not in vat_ok:
            errors.append(f"Γραμμή {n}: επίλεξε συντελεστή ΦΠΑ"
                          + (f" — ο τύπος {t} δέχεται μόνο {', '.join(O['vat'][v] for v in sorted(vat_ok))}." if ln["vat_category"] else "."))
        if ln["vat_category"] != "7":
            ln["vat_exemption"] = ""
        elif ln["vat_exemption"] not in O["exemption"]:
            errors.append(f"Γραμμή {n}: με ΦΠΑ 0% συμπλήρωσε την κατηγορία εξαίρεσης ΦΠΑ.")
        if ln["net"] < 0 or ln["vat_amount"] < 0:
            errors.append(f"Γραμμή {n}: τα ποσά δεν μπορεί να είναι αρνητικά — για αρνητικές αξίες στα 17.3–17.6 "
                          "διάλεξε «Αρνητικό πρόσημο αξιών» και γράψε τα ποσά θετικά.")
        e3 = [c for c in ln["cls"] if not _is_vat_cls(c["type"])]
        if any(not c["category"].startswith(side_prefix) for c in e3):
            errors.append(f"Γραμμή {n}: ο τύπος {t} θέλει χαρακτηρισμούς "
                          f"{'εσόδων (category1_x)' if roles['side'] == 'income' else 'εξόδων (category2_x)'}.")
        if t != "9.3" and abs(sum(c["amount"] for c in e3) - ln["net"]) > 0.005:
            errors.append(f"Γραμμή {n}: οι χαρακτηρισμοί Ε3 ({sum(c['amount'] for c in e3):.2f}) πρέπει να "
                          f"αθροίζουν στην καθαρή αξία ({ln['net']:.2f}).")
        if roles["side"] == "income":
            ln["cls"] = e3  # χαρακτηρισμός ΦΠΑ μόνο στα έξοδα
        elif needs_vat_cls(ln["vat_category"]) and not any(_is_vat_cls(c["type"]) for c in ln["cls"]):
            ln["cls"].append({"category": "", "type": VAT_EXPENSE_TYPE, "amount": ln["net"]})  # σφάλμα 230
        for k in ("withheld", "fees", "other", "stamp"):
            if ln[k] and ln[k + "_cat"] not in O[k]:
                errors.append(f"Γραμμή {n}: επίλεξε κατηγορία για το ποσό {ln[k]:.2f}.")
    for n, tx in enumerate(d["taxes"], start=1):
        if tx["type"] not in tax_label:
            errors.append(f"Φόρος {n}: μη έγκυρος τύπος φόρου.")
        elif tx["type"] == "5":
            tx["category"] = ""
        elif tx["category"] not in O[ENTRY_TAX_KEYS[tx["type"]]]:
            errors.append(f"Φόρος {n}: επίλεξε κατηγορία φόρου.")
        if tx["amount"] <= 0:
            errors.append(f"Φόρος {n}: συμπλήρωσε ποσό.")
    if t == "17.1" and d["lines"]:  # μισθοδοσία: παρακράτηση και κρατήσεις μέσα στις αποδοχές
        tot = entry_totals(d)
        if tot["withheld"] + tot["deductions"] > tot["net"] + 0.005:
            errors.append("Παρακράτηση φόρου και κρατήσεις ΕΦΚΑ: όχι πάνω από το σύνολο αποδοχών.")
        if any(tx["type"] == "1" and not (tx["base"] and tx["amount"] <= tx["base"]) for tx in d["taxes"]):
            errors.append("Συμπλήρωσε την υποκείμενη αξία της παρακράτησης φόρου (όχι μικρότερη από τον φόρο).")
    if d["lines"] and d["payments"]:
        gross = entry_totals(d)["gross"]
        paid = round(sum(p["amount"] for p in d["payments"]), 2)
        if abs(paid - gross) > 0.005:
            errors.append(f"Οι τρόποι εξόφλησης ({paid:.2f}) πρέπει να αθροίζουν στη συνολική αξία ({gross:.2f}).")
    return d, errors


@app.route("/entry", methods=["POST"])
def entry_submit():
    from mydata_client import entry_roles

    edit_mark = request.form.get("edit_mark", "").strip()
    template_id = request.form.get("template_id", "").strip()
    draft, errors = _entry_clean(_posted_draft())
    if not errors:  # και το XML που θα σταλεί, με το XSD της ΑΑΔΕ
        errors = [f"Έλεγχος XSD ΑΑΔΕ: {e}" for e in xsd_errors(draft_xml(draft))]
    if errors:
        for e in errors:
            flash(e, "error")
        return _render_entry(draft, edit_mark, 422, template_id)
    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("companies"))

    if entry_roles(draft["invoice_type"], draft["self_pricing"], draft["omission"])["side"] == "income":
        # Τα έσοδα δεν έχουν μαζική «Αποστολή» (όπως η ακύρωση εσόδου): SendInvoices τώρα.
        if not send_allowed():
            flash(SEND_DISABLED_MSG, "error")
            return _render_entry(draft, edit_mark, 422)
        try:
            result = get_client(send="create").send_invoice(draft_xml(draft))
        except MyDataError as e:
            flash(f"Η αποστολή απέτυχε: {e}", "error")
            return _render_entry(draft, edit_mark, 422)
        if (result.get("status") or "").lower() != "success":
            flash(f"Η αποστολή απέτυχε: {'; '.join(result['errors']) or result.get('status')}", "error")
            return _render_entry(draft, edit_mark, 422)
        _save_entry_parties(cid, draft)
        flash(f"✔ Το παραστατικό {draft['invoice_type']} στάλθηκε στο myDATA (MARK {result.get('invoice_mark')}). "
              "Θα εμφανιστεί στο βιβλίο εσόδων με την επόμενη «Ανάκτηση».", "ok")
        return redirect(url_for("income"))

    existing = db.get_document(cid, edit_mark) if edit_mark else None
    if existing and existing.get("local_action") != "create":
        existing = None
    # Μηνιαίο πρότυπο: μία εγγραφή ανά μήνα. Στην επεξεργασία κρατιέται το πρότυπο της παλιάς.
    tid = (existing or {}).get("template_id") or template_id
    origin = (int(tid), draft["issue_date"][:7]) if str(tid).isdigit() else None
    if origin:
        taken = db.template_periods(cid, origin[1]).get(origin[0])
        if taken and taken["mark"] != edit_mark:
            flash(f"Υπάρχει ήδη εγγραφή του προτύπου για τον μήνα {origin[1]} ({taken['mark']}).", "error")
            return _render_entry(draft, edit_mark, 422, template_id)
    if existing:
        db.delete_document(cid, edit_mark)
    _store_entry(cid, draft, origin)
    _save_entry_parties(cid, draft)
    flash(f"✔ Η εγγραφή {draft['invoice_type']} {'ενημερώθηκε' if existing else 'αποθηκεύτηκε'} τοπικά στα "
          "«Χαρακτηρισμένα». Στείλ' την με «Αποστολή στο myDATA».", "ok")
    return redirect(_safe_back(request.form.get("back")) or url_for("invoices", view="classified"))


def _posted_draft() -> dict:
    """Το JSON draft που στέλνει η Φόρμα καταχώρησης (κρυφό πεδίο «draft»)."""
    try:
        raw = json.loads(request.form.get("draft") or "{}")
    except ValueError:
        raw = {}
    return raw if isinstance(raw, dict) else {}


@app.route("/entry/template", methods=["POST"])
def expense_template_save():
    """Η τρέχουσα φόρμα ως πρότυπο εξόδου (χωρίς ημ/νία και Α/Α)."""
    from mydata_client import entry_roles

    cid = _active_company_id()
    name = " ".join(request.form.get("template_name", "").split())[:80]
    draft, _ = _entry_clean(_posted_draft())  # λάθη ποσών δεν μετρούν: τα ποσά ελέγχονται σε κάθε χρήση
    edit_mark = request.form.get("edit_mark", "").strip()
    series = draft.get("series", "")
    other = db.template_with_series(cid, series, name) if series else None
    error = (
        "Δώσε όνομα προτύπου (π.χ. «ΕΦΚΑ εργοδότη»)." if not cid or not name
        else "Πρότυπα γίνονται μόνο για παραστατικά εξόδων." if draft["invoice_type"] not in _ENTRY_TYPES
        or entry_roles(draft["invoice_type"], draft["self_pricing"], draft["omission"])["side"] != "expense"
        else f"Η σειρά «{series}» χρησιμοποιείται ήδη από το πρότυπο «{other}» — κάθε πρότυπο θέλει δική του σειρά."
        if other else "Δώσε σειρά στο πρότυπο — ο Α/Α του συνεχίζει μέσα στη σειρά." if not series else ""
    )
    if error:
        flash(error, "error")
        return _render_entry(draft, edit_mark, 422, request.form.get("template_id", "").strip())
    draft.pop("issue_date", None)
    draft.pop("aa", None)
    tid = db.save_template(cid, name, draft)
    flash(f"✔ Αποθηκεύτηκε το πρότυπο «{name}».", "ok")
    return redirect(url_for("entry", template=tid))


@app.route("/entry/template/<int:tid>/delete", methods=["POST"])
def expense_template_delete(tid):
    db.delete_template(_active_company_id(), tid)
    flash("✔ Το πρότυπο διαγράφηκε.", "ok")
    return redirect(url_for("entry"))


def _save_entry_parties(cid: int, d: dict) -> None:
    """Εκδότης / αντισυμβαλλόμενος (όχι εμείς): συνδέεται με την εταιρεία, ώστε να φαίνεται αμέσως στους
    «Συναλλασσόμενους» της· αν δεν υπάρχει στον κατάλογο, αποθηκεύεται με χώρα και διεύθυνση."""
    own = get_own_vat()
    for p in (d["issuer"], d["counterpart"]):
        vat, name = p.get("vat"), p.get("name") or p.get("known_name")
        if not vat or vat == own:
            continue
        db.link_supplier(cid, vat)
        known = db.get_supplier(vat) or {}
        has_address = bool(p.get("city") or p.get("street"))
        # υπάρχει ήδη· συμπληρώνονται χώρα/διεύθυνση αν λείπουν (π.χ. μόνο επωνυμία από παλιότερο VIES)
        if known.get("name") and not (has_address and not known.get("city")):
            continue
        if has_address or p.get("country", "GR") != "GR":
            street = " ".join(x for x in (p.get("street"), p.get("number")) if x)
            db.save_supplier_address(vat, name, p.get("country") or "GR", street, p.get("postal_code"), p.get("city"))
        elif name:
            db.upsert_supplier(vat, name)


def _store_entry(cid: int, d: dict, origin: tuple | None = None) -> int | None:
    """Καθαρό draft εξόδου της Φόρμας καταχώρησης → τοπική εγγραφή στα «Χαρακτηρισμένα».
    None αν υπάρχει ήδη εγγραφή του ίδιου προτύπου για τον μήνα (origin)."""
    from mydata_client import ENTRY_TAX_KEYS, entry_totals

    tot = entry_totals(d)
    lines = [
        {
            "line_number": i,
            "net_value": ln["net"],
            "vat_amount": ln["vat_amount"],
            "vat_category": ln["vat_category"],
            "has_expenses_classification": True,
            "classifications": [{"type": c["type"], "category": c["category"], "amount": c["amount"]} for c in ln["cls"]],
        }
        for i, ln in enumerate(d["lines"], start=1)
    ]
    taxes = d["taxes"] if d["tax_level"] != "line" else [
        {"type": tt, "category": ln.get(k + "_cat", ""), "base": ln["net"], "amount": ln[k]}
        for ln in d["lines"] for tt, k in ENTRY_TAX_KEYS.items() if ln[k]
    ]
    doc = {
        "issue_date": d["issue_date"],
        "issuer_vat": d["issuer"]["vat"],
        "issuer_name": d["issuer"]["name"] or None,
        "invoice_type": d["invoice_type"],
        "series": d["series"],
        "aa": d["aa"],
        "total_net": tot["net"],
        "total_vat": tot["vat"],
        "total_gross": tot["gross"],
        "lines": lines,
        "extra_totals": {
            "total_withheld": tot["withheld"],
            "total_other_taxes": tot["other"],
            "total_stamp_duty": tot["stamp"],
            "total_fees": tot["fees"],
            "total_deductions": tot["deductions"],
            "taxes_json": json.dumps(taxes),
        },
    }
    return db.create_local_expense(cid, doc, d, [c for ln in lines for c in ln["classifications"]], origin)


# Συχνότητες προγραμματισμένων εγγραφών: κάθε πόσους μήνες.
_EVERY = {1: "Μηνιαία", 2: "Διμηνιαία", 3: "Τριμηνιαία", 6: "Εξαμηνιαία", 12: "Ετήσια"}


def _is_due(t: dict, period: str) -> bool:
    """Το πρότυπο γράφεται τον μήνα; (ημέρα ορισμένη και μήνας στη σειρά: έναρξη, έναρξη + κάθε, …)."""
    every, start = t.get("every_months") or 1, t.get("start_month") or 1
    return bool(t.get("monthly_day")) and (int(period[5:]) - start) % every == 0


def _next_due(t: dict, period: str) -> str:
    """Ο επόμενος μήνας (YYYY-MM) μετά το period που γράφεται το πρότυπο."""
    y, m = map(int, period.split("-"))
    for _ in range(12):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if _is_due(t, f"{y}-{m:02d}"):
            return f"{y}-{m:02d}"
    return ""


def _recurring_pending(cid: int | None, period: str) -> list[dict]:
    """Προγραμματισμένα πρότυπα που γράφονται τον μήνα και δεν έχουν ακόμα εγγραφή."""
    done = db.template_periods(cid, period)
    return [t for t in db.list_templates(cid) if _is_due(t, period) and t["id"] not in done]


def _draft_for_month(cid: int, template: dict, period: str) -> dict | None:
    """Το πρότυπο ως έγκυρο draft για τον μήνα: ημ/νία = ημέρα προτύπου (ή τελευταία του μήνα),
    Α/Α συνέχεια της σειράς. None αν το πρότυπο δεν έχει γραμμές με ποσό."""
    d = _as_entry(template["draft"])
    y, m = map(int, period.split("-"))
    day = min(template["monthly_day"], calendar.monthrange(y, m)[1])
    lines = [ln for ln in d.get("lines") or [] if ln and ln.get("net")]
    if not lines:
        return None
    return dict(d, lines=lines, issue_date=f"{period}-{day:02d}",
                aa=str(db.next_aa(cid, _self_counterparty(d), d.get("series", ""))))


def _period_arg(value: str | None) -> str:
    return value if value and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) else datetime.now(ATHENS).strftime("%Y-%m")


@app.route("/recurring")
def recurring():
    """Προγραμματισμένες εγγραφές από πρότυπα (μηνιαίες έως ετήσιες): πρόγραμμα ανά πρότυπο + δημιουργία για έναν μήνα."""
    cid = _active_company_id()
    period = _period_arg(request.args.get("period"))
    y, m = map(int, period.split("-"))
    now = datetime.now(ATHENS)
    cards = []
    for t in db.list_templates(cid):  # ό,τι δείχνει η κάρτα: τύπος, συναλλασσόμενος, ποσό, αν άργησε
        d = _as_entry(db.get_template(cid, t["id"])["draft"])
        vat = ((d.get("issuer") or {}).get("vat") or "").strip()
        due = _is_due(t, period)
        day = min(t["monthly_day"], calendar.monthrange(y, m)[1]) if due else None
        cards.append(dict(t, type=d.get("invoice_type", ""), type_name=_ENTRY_TYPES.get(d.get("invoice_type", ""), ""),
                          series=d.get("series", ""), vat=vat, party=db.get_supplier_name(vat) if vat else "",
                          net=round(sum((ln or {}).get("net") or 0 for ln in d.get("lines") or []), 2), day=day,
                          late=bool(day) and (period, day) <= (now.strftime("%Y-%m"), now.day),
                          next_due="" if due or not t["monthly_day"] else _next_due(t, period)))
    done = db.template_periods(cid, period)
    # Πρώτα ό,τι θέλει δουλειά: άργησε → εκκρεμεί → έγινε → όχι αυτόν τον μήνα → χωρίς πρόγραμμα.
    cards.sort(key=lambda t: (2 if t["id"] in done else 0 if t["late"] else 1 if t["day"] else 3 if t["next_due"] else 4,
                              t["day"] or 0, t["next_due"]))
    return render_template("recurring.html", period=period, templates=cards, done=done, every=_EVERY,
                           prev=f"{y - (m == 1)}-{12 if m == 1 else m - 1:02d}",
                           next=f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}")


@app.route("/recurring/days", methods=["POST"])
def recurring_days():
    cid = _active_company_id()
    def num(key, default, lo, hi):
        v = request.form.get(key, "").strip()
        return min(max(int(v), lo), hi) if v.isdigit() else default

    for t in db.list_templates(cid):
        every = num(f"every_{t['id']}", 1, 1, 12)
        db.set_template_monthly_day(cid, t["id"], num(f"day_{t['id']}", None, 1, 31),
                                    every if every in _EVERY else 1, num(f"start_{t['id']}", 1, 1, 12))
    flash("✔ Αποθηκεύτηκε το πρόγραμμα των εγγραφών.", "ok")
    return redirect(url_for("recurring", period=request.form.get("period")))


@app.route("/recurring/create", methods=["POST"])
def recurring_create():
    cid = _active_company_id()
    period = _period_arg(request.form.get("period"))
    if not get_own_vat():  # ίδιος έλεγχος με τη Φόρμα καταχώρησης (εκδότης 17.x / λήπτης)
        flash("Λείπει το ΑΦΜ της εταιρείας - συμπλήρωσέ το στη Διαχείριση εταιρειών.", "error")
        return redirect(url_for("recurring", period=period))
    ids = set(request.form.getlist("template_ids"))
    made, skipped, empty, skipped_xsd = [], [], [], []
    for t in _recurring_pending(cid, period) if cid else []:
        if str(t["id"]) not in ids:
            continue
        template = db.get_template(cid, t["id"])
        draft = _draft_for_month(cid, template, period)
        if draft is None:
            empty.append(t["name"])
            continue
        draft, errors = _entry_clean(draft)
        errors = errors or [f"Έλεγχος XSD ΑΑΔΕ: {e}" for e in xsd_errors(draft_xml(draft))]
        for e in errors:
            flash(f"{t['name']}: {e}", "error")
        if errors:
            skipped_xsd.append(t["name"])
        elif _store_entry(cid, draft, (t["id"], period)):
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
    if skipped_xsd:
        flash(f"Δεν δημιουργήθηκαν (λάθη — διόρθωσε το πρότυπο): {', '.join(skipped_xsd)}.", "error")
    if not (made or skipped or empty or skipped_xsd):
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
        result = get_client(send="income_cancel").cancel_invoice(mark)
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
    from mydata_client import XSD_VERSION

    return render_template("help.html", xsd_version=XSD_VERSION)


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
        acc_actions=accountant_actions(),
        accountant_action_list=ACCOUNTANT_ACTIONS,
        combinations_count=db.combos_count(),
        combinations_invoice_types=sorted(
            db.combos_invoice_types(), key=lambda t: [int(x) if x.isdigit() else 0 for x in t.split(".")]
        ),
        type_names=INVOICE_TYPE_NAMES,
        acc_companies=[c["company_name"] for c in load_companies() if c.get("use_accountant")],
        combinations_version=db.get_setting("combos_version"),
        stock_reports=[(k, title, desc, stock_in(k)) for k, title, _, desc in STOCK_REPORTS],
        auto_classify=db.get_setting("auto_classify") == "1",
        pay_amount=db.get_setting("pay_amount", "1") == "1",
        sync_mode=db.get_setting("sync_mode") or "mark",
        strip_from_year=_strip_from_year(), this_year=datetime.now(ATHENS).year,
        # Όλα τα tabs αποδίδονται μαζί· η εναλλαγή γίνεται στον browser χωρίς reload.
        safety_backups=db.list_safety_backups(),
        keep_safety=db.keep_safety(),
        keep_logs=_log_keep(),
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
    db.set_setting("pay_amount", "1" if request.form.get("pay_amount") else "0")
    db.set_setting("sync_mode", "dates" if request.form.get("sync_mode") == "dates" else "mark")
    y, this_year = request.form.get("strip_from_year", "").strip(), datetime.now(ATHENS).year
    if y:
        if not (y.isdigit() and 2000 <= int(y) <= this_year):
            flash(f"Μη έγκυρο έτος λωρίδας: {y} (2000 – {this_year}).", "error")
            return redirect(url_for("parameters", tab="reports"))
        db.set_setting("strip_from_year", y)
    flash("✔ Αποθηκεύτηκαν οι ρυθμίσεις ανάκτησης και αυτόματου χαρακτηρισμού.", "ok")
    return redirect(url_for("parameters", tab="reports"))


@app.route("/companies")
def companies():
    comps = load_companies()
    months = _last_12_months(datetime.now(ATHENS))
    return render_template(
        "companies.html",
        months=months,
        this_month=months[-1],
        monthly=db.company_month_counts(months[0] + "-01"),
        companies=comps,
        # Σειρά εμφάνισης στο rack: αλφαβητικά. Οι διαδρομές (select/update/delete) μένουν με τη θέση στη λίστα.
        order=sorted(range(len(comps)), key=lambda i: _fold(comps[i].get("company_name") or "")),
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
    acc_req = bool(request.form.get("accountant_required"))
    if _sole_without_birth_year():
        return redirect(url_for("companies"))
    if not name or (not (use_acc or acc_req) and not (user_id and sub_key)):
        flash(
            "Επωνυμία υποχρεωτική. User ID/Subscription Key απαιτούνται εκτός αν "
            "χρησιμοποιούνται credentials λογιστή.",
            "error",
        )
        return redirect(url_for("companies"))
    data = {
        "company_name": name,
        "AADE_USER_ID": user_id,
        "AADE_SUBSCRIPTION_KEY": sub_key,
        "AADE_VAT_NUMBER": vat,
        "MYDATA_ENV": env,
        "use_accountant": use_acc,
        "accountant_required": acc_req,
        "allow_cancel": bool(request.form.get("allow_cancel")),
        "allow_send": bool(request.form.get("allow_send")),
        "allow_classify": bool(request.form.get("allow_classify")),
        "allow_new": bool(request.form.get("allow_new")),
        "vat_period": request.form.get("vat_period", "m"),
        "mark_check_hours": request.form.get("mark_check_hours", 1),
        **_company_tax_fields(),
    }
    if _accountant_missing(data):
        data["allow_send"] = False
    new_id = db.add_company(data)
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
        not (request.form.get("use_accountant") or request.form.get("accountant_required")) and not (user_id and sub_key)
    ):
        flash(
            "Επωνυμία υποχρεωτική. User ID/Subscription Key απαιτούνται εκτός αν "
            "χρησιμοποιούνται credentials λογιστή.",
            "error",
        )
        return redirect(url_for("companies"))
    data = {
        "company_name": name,
        "AADE_USER_ID": user_id,
        "AADE_SUBSCRIPTION_KEY": sub_key,
        "AADE_VAT_NUMBER": request.form.get("aade_vat_number", "").strip(),
        "MYDATA_ENV": request.form.get("mydata_env", "prod"),
        "use_accountant": bool(request.form.get("use_accountant")),
        "accountant_required": bool(request.form.get("accountant_required")),
        "allow_cancel": bool(request.form.get("allow_cancel")),
        "allow_send": bool(request.form.get("allow_send")),
        "allow_classify": bool(request.form.get("allow_classify")),
        "allow_new": bool(request.form.get("allow_new")),
        "vat_period": request.form.get("vat_period", "m"),
        "mark_check_hours": request.form.get("mark_check_hours", 1),
        **_company_tax_fields(),
    }
    if _accountant_missing(data):
        data["allow_send"] = False
    db.update_company(comps[idx]["id"], data)
    flash(f"✔ Ενημερώθηκε η εταιρεία «{name}».", "ok")
    back = request.form.get("back", "")  # modal ενεργής εταιρείας (base.html) → πίσω στη σελίδα του
    return redirect(back if back.startswith("/") and not back.startswith(("//", "/\\")) else url_for("companies"))


_TOGGLES = {"MYDATA_ENV", "use_accountant", "allow_send", "allow_classify", "allow_new", "allow_cancel"}


@app.route("/companies/toggle/<int:idx>/<flag>", methods=["POST"])
def companies_toggle(idx, flag):
    """Tag της κάρτας: αλλάζει μία ρύθμιση (dev⇄prod ή μία άδεια) χωρίς το modal."""
    comps = load_companies()
    if not (0 <= idx < len(comps)) or flag not in _TOGGLES:
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("companies"))
    c = comps[idx]
    c[flag] = ("prod" if c[flag] == "dev" else "dev") if flag == "MYDATA_ENV" else not c[flag]
    coupled = c["accountant_required"] and flag in ("use_accountant", "allow_send")
    if coupled:  # υποχρεωτικός λογιστής: λογιστής και αποστολή ανοίγουν/κλείνουν μαζί
        c["use_accountant"] = c["allow_send"] = c[flag]
    if c["locked"] and (flag.startswith("allow_") or coupled):
        flash("🔐 Κλειδωμένη εταιρεία — οι άδειες δεν αλλάζουν.", "error")
    elif flag == "allow_cancel" and c["allow_cancel"] and not c["allow_send"]:
        flash("Οι ακυρώσεις/απορρίψεις θέλουν ενεργή την «Αποστολή στο myDATA» — ενεργοποίησέ την πρώτα.", "error")
    elif flag in ("use_accountant", "allow_send") and _accountant_missing(c):
        pass
    elif not (c["use_accountant"] or c["accountant_required"]) and not (c["AADE_USER_ID"] and c["AADE_SUBSCRIPTION_KEY"]):
        flash("Χωρίς credentials λογιστή χρειάζονται User ID/Subscription Key (Ρυθμίσεις εταιρείας).", "error")
    else:
        db.update_company(c["id"], c)
        flash(f"✔ Ενημερώθηκε η εταιρεία «{c['company_name']}».", "ok")
    return redirect(url_for("companies"))


@app.route("/companies/delete/<int:idx>", methods=["POST"])
def companies_delete(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("companies"))
    removed = comps[idx]
    # Επιβεβαίωση: ο χρήστης πληκτρολογεί το ΑΦΜ (ή την επωνυμία, αν δεν έχει ΑΦΜ).
    expected = removed.get("AADE_VAT_NUMBER") or removed.get("company_name", "")
    if request.form.get("confirm", "").strip() != expected.strip():
        flash("Η διαγραφή ακυρώθηκε: το ΑΦΜ επιβεβαίωσης δεν ταιριάζει.", "error")
        return redirect(url_for("companies"))
    was_active = db.get_active_company_id() == removed["id"]
    db.delete_company(
        removed["id"]
    )  # cascade: παραστατικά της εταιρείας (οι προμηθευτές είναι κοινοί)
    shutil.rmtree(_log_dir(removed["id"]), ignore_errors=True)
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
    """Τοπική αναζήτηση ΑΦΜ στον (κοινό) πίνακα προμηθευτών· με χώρα και διεύθυνση, αν υπάρχουν."""
    s = db.get_supplier((vat or "").strip()) or {}
    return {"found": bool(s.get("name")), "name": s.get("name") or "", "country": s.get("country") or "",
            "street": s.get("street") or "", "postal_code": s.get("postal_code") or "", "city": s.get("city") or ""}


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
        title = result["name"].partition("||")[2].strip()  # διακριτικός τίτλος, π.χ. «ΟΤΕ»
        msg += f" · {vies.legal_name(result['name'])}" + (f" ({title})" if title else "")
        # αποθήκευση στην cache επωνυμιών της ενεργής εταιρείας
        names = load_names()
        _, num = vies.normalize(country, afm)
        if names.get(num) != result["name"]:
            names[num] = result["name"]
            save_names(names)
    # έτοιμα πεδία για τις φόρμες: επίσημη επωνυμία (χωρίς τον διακριτικό τίτλο «||…») και η διεύθυνση σε κομμάτια
    return {"ok": True, "valid": True, "name": result["name"], "address": result.get("address") or "",
            "legal_name": vies.legal_name(result["name"]), **vies.split_address(result.get("address")), "message": msg}


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
        db.set_setting("accountant_actions", ",".join(k for k, _ in ACCOUNTANT_ACTIONS if request.form.get("act_" + k)))
        flash("✔ Αποθηκεύτηκαν τα credentials λογιστή.", "ok")
        return redirect(url_for("parameters", tab="accountant"))
    return redirect(url_for("parameters", tab="accountant"))


# ------------------------------------------------------------------ #
# Συναλλασσόμενοι — πελάτες & προμηθευτές (κοινοί για όλες τις εταιρείες)
# ------------------------------------------------------------------ #
def _fold(s: str) -> str:
    """Για σύγκριση/ταξινόμηση χωρίς διάκριση πεζών/κεφαλαίων και τόνων («Άλφα» = «αλφα»)."""
    return "".join(c for c in unicodedata.normalize("NFD", s.casefold()) if not unicodedata.combining(c))


def _filter_suppliers(items: list[dict], q: str) -> list[dict]:
    """Φίλτρο: substring σε ΑΦΜ ή επωνυμία (χωρίς διάκριση πεζών/κεφαλαίων και τόνων)."""
    return [s for s in items if _fold(q) in _fold(f"{s['vat']} {s['name'] or ''}")]


@app.route("/suppliers")
def suppliers():
    """Κατάλογος συναλλασσόμενων (πελάτες & προμηθευτές), με σελιδοποίηση."""
    all_suppliers = db.list_suppliers(_active_company_id() or 0)  # μόνο της ενεργής εταιρείας
    q = request.args.get("q", "").strip()
    items = _filter_suppliers(all_suppliers, q)
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
        shown_with_rule=sum(1 for s in items if s["vat"] in patterns or s["vat"] in rules),
        with_docs=db.vats_with_documents(),
        filtered_total=len(items),
        q=q,
        page=page,
        total_pages=total_pages,
        patterns=patterns,
        rules=rules,
        review=db.review_vats(cid),
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


@app.route("/suppliers/rules-delete", methods=["POST"])
def suppliers_rules_delete():
    """Διαγραφή προτάσεων χαρακτηρισμού στην ενεργή εταιρεία: ενός συναλλασσόμενου (vat) ή όλων
    όσων φαίνονται με την τρέχουσα αναζήτηση (q). Μετά ξαναγίνονται από την τελευταία εγγραφή
    («Αντιγραφή χαρακτηρισμών») ή μαθαίνονται από τον επόμενο χαρακτηρισμό."""
    cid = _active_company_id()
    vat = request.form.get("vat", "").strip()
    q = request.form.get("q", "").strip()
    vats = [vat] if vat else [s["vat"] for s in _filter_suppliers(db.list_suppliers(cid or 0), q)]
    n = db.delete_rules(cid, vats)
    flash(f"✔ Διαγράφηκαν οι προτάσεις χαρακτηρισμού {'του ' + vat if vat else f'{n} συναλλασσόμενων'}." if n
          else "Δεν υπήρχαν προτάσεις για διαγραφή.", "ok")
    return redirect(_safe_back(request.form.get("back")) or url_for("suppliers", q=q or None))


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
        db.set_review_vat(cid, vat, request.form.get("review") == "1")
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
        review=vat in db.review_vats(cid),
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
    return {"ok": True, "name": name, "short": short_counterparty_name(name)}


@app.route("/suppliers/delete/<vat>", methods=["POST"])
def suppliers_delete(vat):
    if db.delete_supplier(vat):
        flash(f"✔ Διαγράφηκε ο συναλλασσόμενος {vat}.", "ok")
    else:
        flash(f"Ο συναλλασσόμενος {vat} έχει κινήσεις (παραστατικά) και δεν διαγράφεται.", "error")
    return redirect(_safe_back(request.form.get("back")) or url_for("suppliers"))


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
    "accountant_required",
    "vat_period",
    "entity_type",
    "birth_year",
    "mark_check_hours",
)


@app.route("/companies/export")
def companies_export():
    """Χωρίς παραμέτρους: όλα. Από τον διάλογο (sel=1): μόνο οι επιλεγμένες εταιρείες (c=δείκτης)
    και ο λογιστής μόνο με acc=1."""
    comps = load_companies()
    active = get_active_index()
    picked = range(len(comps))
    if request.args.get("sel"):
        picked = sorted({int(i) for i in request.args.getlist("c") if i.isdigit() and int(i) < len(comps)})
    payload = {
        "active": picked.index(active) if active in picked else 0,
        "companies": [{k: comps[i].get(k) for k in _COMPANY_KEYS} for i in picked],
    }
    if not request.args.get("sel") or request.args.get("acc"):
        payload["accountant"] = get_accountant()
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
    company = {k: c.get(k) for k in _COMPANY_KEYS} | {"use_accountant": False, "accountant_required": False}
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
        payload = {k: company.get(k) for k in _COMPANY_KEYS} | {"use_accountant": False, "accountant_required": False}
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


@app.route("/backup/vacuum", methods=["POST"])
def backup_vacuum():
    """Συμπίεση βάσης (VACUUM) από Παράμετροι → Αντίγραφα ασφαλείας."""
    before = _backup_info()["size"]
    try:
        db.vacuum()
    except sqlite3.OperationalError as e:  # π.χ. άλλη ενέργεια κρατά τη βάση εκείνη τη στιγμή
        flash(f"Η συμπίεση δεν έγινε ({e}). Δοκίμασε ξανά σε λίγο.", "error")
        return redirect(url_for("parameters", tab="backup"))
    after = _backup_info()["size"]
    mb = lambda n: f"{n / 1048576:.1f} MB"  # noqa: E731
    flash(f"✔ Η βάση συμπιέστηκε: {mb(before)} → {mb(after)}.", "ok")
    return redirect(url_for("parameters", tab="backup"))


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
    # Μία από τις δύο φόρμες της καρτέλας: αντίγραφα πριν από επαναφορά ή logs myDATA.
    logs = "keep_mydata_logs" in request.form
    key, top = ("keep_mydata_logs", 500) if logs else ("keep_safety_backups", 50)
    try:
        n = int(request.form.get(key, ""))
    except ValueError:
        n = 0
    if not 1 <= n <= top:
        flash(f"Το πλήθος πρέπει να είναι από 1 έως {top}.", "error")
        return redirect(url_for("parameters", tab="backup"))
    db.set_setting(key, n)
    if not logs:
        db.prune_safety_backups()
    flash(f"✔ Θα κρατούνται τα {n} τελευταία {'logs' if logs else 'αντίγραφα'}.", "ok")
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
with open(os.path.join(_BASE_DIR, "pyproject.toml"), "rb") as fh:
    APP_VERSION = tomllib.load(fh)["project"]["version"]
app.jinja_env.globals["app_version"] = APP_VERSION


def _check_update():
    global _update
    try:
        current = APP_VERSION
        rel = requests.get("https://api.github.com/repos/tedlaz/mydata-classifier/releases/latest", timeout=5).json()
        latest = rel["tag_name"].lstrip("v")
        if tuple(map(int, latest.split("."))) > tuple(map(int, current.split("."))):
            url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"].endswith(".exe")), rel["html_url"])
            _update = {"version": latest, "url": url}
    except Exception:
        pass


if getattr(sys, "frozen", False):
    threading.Thread(target=_check_update, daemon=True).start()


# ------------------------------------------------------------------ #
# ΑΥΤΟΜΑΤΟΣ ΚΥΚΛΟΣ: κάθε auto_cycle_minutes, για τις επιλεγμένες εταιρείες μία-μία: «Νέα παραστατικά από MARK»
# (έξοδα) → χαρακτηρισμός όσων έχουν πρόταση συναλλασσόμενου → αποστολή τους στο myDATA. Όσα δεν έχουν πρόταση
# δεν αγγίζονται, μόνο καταγράφονται. Καλούνται τα ίδια views με τα κουμπιά (sync_run, bulk_classify, send) σε
# ψεύτικο request με g.auto_cid: ίδιοι έλεγχοι, και η ενεργή εταιρεία του χρήστη δεν αλλάζει.
# Τρέχει μόνο με ξεκλείδωτη εφαρμογή (τα κλειδιά myDATA αποκρυπτογραφούνται μόνο μετά το login).
# Ένα log ανά κύκλο στο <data>/mydata_logs/auto_cycle· η κατάσταση (σελίδα /auto-cycle) ζει στη μνήμη.
# ponytail: ένα νήμα, ένας κύκλος κάθε φορά· χωρίς lock απέναντι σε ταυτόχρονη χειροκίνητη ενέργεια του
# χρήστη στην ίδια εταιρεία — lock ανά εταιρεία αν φανεί πρόβλημα.
# ------------------------------------------------------------------ #
_CYCLE_DIR = os.path.join(_LOG_DIR, "auto_cycle")
CYCLE_LOG_KEEP = 200  # προεπιλογή· η ρύθμιση keep_cycle_logs υπερισχύει
_CYCLE_LIVE = 400  # γραμμές log του τρέχοντος κύκλου στη μνήμη (για τη ζωντανή προβολή)
_cycle_lock = threading.Lock()
_cycle_wake, _cycle_stop = threading.Event(), threading.Event()
_cycle = {"running": False, "trigger": "", "company": "", "step": "", "done": 0, "total": 0,
          "index": 0, "count": 0, "level": "fetch", "cidx": 0, "companies": [], "started": None, "summary": None, "lines": [], "seq": 0}
_cycle_buf: list[str] = []  # όλες οι γραμμές του τρέχοντος κύκλου (για το αρχείο .log)
# Ο ίδιος κύκλος δομημένα (αρχείο .json δίπλα στο .log): κάρτα κύκλου → υποκάρτα ανά εταιρεία με τις γραμμές της.
_cycle_rec: dict = {"lines": [], "companies": []}
_COUNT_KEYS = ("new", "classified", "sent", "failed", "no_rule", "skipped")


class _CycleStopped(Exception):
    pass


# Τι κάνει ο κύκλος ανά εταιρεία. Προεπιλογή και ό,τι άγνωστο/χαλασμένο = «fetch» (μόνο ανάγνωση).
_CYCLE_LEVELS = {"fetch": "Μόνο ανάκτηση", "classify": "Ανάκτηση + χαρακτηρισμός (τοπικά)",
                 "send": "Ανάκτηση + χαρακτηρισμός + αποστολή"}
_LEVEL_RANK = {"fetch": 0, "classify": 1, "send": 2}


def _cycle_allowed(c: dict | None) -> str:
    """Ανώτατο επίπεδο που επιτρέπουν οι άδειες της εταιρείας (ίδιες σημαίες με τα κουμπιά: _GATED, send)."""
    if not c or not c.get("allow_classify"):
        return "fetch"
    return "send" if c.get("allow_send") else "classify"


def _cycle_effective(chosen: str, c: dict | None) -> str:
    return min(chosen if chosen in _LEVEL_RANK else "fetch", _cycle_allowed(c), key=_LEVEL_RANK.get)


def _fresh_company(cid: int) -> dict | None:
    """Η εταιρεία όπως είναι ΤΩΡΑ στη βάση (οι άδειες μπορεί να άλλαξαν στη μέση του κύκλου)."""
    return next((c for c in load_companies() if c["id"] == cid), None)


def _cycle_settings() -> tuple[bool, int, dict[int, str]]:
    """(χρονοπρογραμματισμός ενεργός, λεπτά, {id εταιρείας: επίπεδο})."""
    try:
        minutes = max(5, int(db.get_setting("auto_cycle_minutes") or 60))
    except ValueError:
        minutes = 60
    try:
        raw = json.loads(db.get_setting("auto_cycle_companies") or "{}")
        raw = dict.fromkeys(raw, "fetch") if isinstance(raw, list) else raw
        sel = {int(k): v if v in _LEVEL_RANK else "fetch" for k, v in raw.items()}
    except (ValueError, TypeError, AttributeError):
        sel = {}
    return db.get_setting("auto_cycle_on") == "1", minutes, sel


def _cycle_sync_levels() -> list[str]:
    """Ευθυγραμμίζει τις αποθηκευμένες επιλογές του κύκλου με τις ΤΩΡΙΝΕΣ άδειες των εταιρειών: ό,τι δεν επιτρέπεται
    πια κατεβαίνει στο επιτρεπόμενο, εταιρείες που διαγράφηκαν φεύγουν. Ποτέ αναβάθμιση (άνοιγμα άδειας ≠ συναίνεση
    για αυτόματη αποστολή). Επιστρέφει μηνύματα για ό,τι άλλαξε."""
    sel = _cycle_settings()[2]
    by_id = {c["id"]: c for c in load_companies()}
    new, msgs = {}, []
    for cid, level in sel.items():
        if cid not in by_id:
            msgs.append(f"Αυτόματος κύκλος: η εταιρεία με id {cid} δεν υπάρχει πια — αφαιρέθηκε από τον κύκλο.")
            continue
        new[cid] = _cycle_effective(level, by_id[cid])
        if new[cid] != level:
            msgs.append(f"Αυτόματος κύκλος — {by_id[cid]['company_name']}: «{_CYCLE_LEVELS[level]}» → "
                        f"«{_CYCLE_LEVELS[new[cid]]}», γιατί άλλαξαν οι άδειες της εταιρείας.")
    if new != sel:
        db.set_setting("auto_cycle_companies", json.dumps(new))
    return msgs


_COMPANY_EDITS = {"companies_add", "companies_update", "companies_toggle", "companies_delete",
                  "companies_import", "companies_import_share"}


@app.after_request
def _cycle_after_company_edit(response):
    """Μετά από κάθε αλλαγή εταιρειών (άδειες, κλείδωμα, εισαγωγή, διαγραφή): ενημέρωση των επιλογών του κύκλου."""
    if request.method == "POST" and request.endpoint in _COMPANY_EDITS and auth.unlocked():
        try:
            for msg in _cycle_sync_levels():
                flash(msg, "error")
        except Exception:  # noqa: BLE001 — δεν χαλάει ποτέ την ενέργεια· ο κύκλος ελέγχει ούτως ή άλλως ανά βήμα
            app.logger.exception("auto cycle sync")
    return response


def _cycle_due() -> datetime:
    """Πότε τρέχει ο επόμενος προγραμματισμένος κύκλος (μετρά από την έναρξη του προηγούμενου)."""
    last = db.get_setting("auto_cycle_last")
    now = datetime.now(ATHENS)
    return datetime.fromisoformat(last) + timedelta(minutes=_cycle_settings()[1]) if last else now


def _cycle_set(**kw) -> None:
    with _cycle_lock:
        _cycle.update(kw)


def _clog(text: str, kind: str = "msg") -> None:
    """Γραμμή log του κύκλου (με ώρα) στο αρχείο, στη δομημένη εγγραφή (στην κάρτα της τρέχουσας εταιρείας, ή του
    κύκλου) και στη ζωντανή προβολή. kind: msg | call | ok | warn | err | head | sep (sep: μόνο στο .log — στις
    κάρτες τα λέει ήδη η κεφαλίδα)."""
    for i, part in enumerate(str(text).split("\n")):
        line = f"{datetime.now(ATHENS):%H:%M:%S}  {part}" if i == 0 else f"          {part}"
        _cycle_buf.append(line)
        if kind == "sep":
            continue
        cidx = _cycle["cidx"]
        (_cycle_rec["companies"][cidx - 1]["lines"] if cidx else _cycle_rec["lines"]).append({"t": line, "k": kind})
        with _cycle_lock:
            _cycle["seq"] += 1
            _cycle["lines"].append({"n": _cycle["seq"], "t": line, "k": kind, "c": cidx})
            del _cycle["lines"][:-_CYCLE_LIVE]


def _cycle_check() -> None:
    if _cycle_stop.is_set():
        raise _CycleStopped


@contextmanager
def _cycle_ctx(cid: int, data: dict | None = None, level: str = "fetch"):
    """Ψεύτικο POST request στην εταιρεία cid: ό,τι μίλησε με το myDATA και ό,τι μήνυμα (flash) έβγαλε το view
    γράφονται στο log του κύκλου· ό,τι ανακτήθηκε πάει και στον κουβά (όπως στο _write_mydata_archive).
    level: τι επιτρέπεται μέσα στο request — κάτω από «send» ο client του myDATA κάνει ΜΟΝΟ GET (get_client)."""
    with app.test_request_context(method="POST", data=data or {}):
        g.auto_cid, g.auto_level = cid, level
        try:
            yield
        finally:
            for call in g.pop("mydata_log", []):
                _clog("  ↔ " + call.partition("  ")[2], "err" if " ✗ " in call else "call")  # χωρίς τη 2η ώρα του _log_line
            _write_mydata_archive(None)
            for cat, msg in session.pop("_flashes", []):
                text = msg.striptags() if isinstance(msg, Markup) else str(msg)
                _clog(("  ✗ " if cat == "error" else "  • ") + text, "err" if cat == "error" else "msg")


def _keep_errors() -> None:
    session["_flashes"] = [f for f in session.get("_flashes", []) if f[0] == "error"]


def _cls_text(entries: list[dict]) -> str:
    """Σύνοψη τοπικού χαρακτηρισμού: «category2_1/E3_102_001 100.00 · VAT_361 100.00»."""
    return " · ".join(
        f"{e['classification_category'] + '/' if e['classification_category'] else ''}{e['classification_type']} "
        f"{e['amount'] or 0:.2f}" for e in entries)


def _doc_text(inv) -> str:
    return (f"{inv.mark} · {(inv.issue_date or '—')[:10]} · {' '.join(filter(None, (inv.issuer_vat or '—', inv.issuer_name)))} · "
            f"{inv.invoice_type or '—'} · {inv.total_gross or 0:.2f} €")


def _cycle_level(cid: int, chosen: str, need: str) -> str | None:
    """Επίπεδο για το επόμενο βήμα, με τις άδειες της εταιρείας όπως είναι ΤΩΡΑ· None αν δεν φτάνει για το need."""
    level = _cycle_effective(chosen, _fresh_company(cid))
    return level if _LEVEL_RANK[level] >= _LEVEL_RANK[need] else None


def _cycle_company(c: dict, chosen: str, tot: dict) -> None:
    """Ένας γύρος για μία εταιρεία. Σφάλμα ανάκτησης → η εταιρεία σταματά εδώ (τίποτα δεν χαρακτηρίζεται).
    Κάνει μόνο ό,τι επιτρέπουν ΚΑΙ η επιλογή του κύκλου (chosen) ΚΑΙ οι άδειες της εταιρείας, που ξαναδιαβάζονται
    πριν από κάθε βήμα. Επιπλέον, ο client του myDATA μπλοκάρει κάθε μη-GET κλήση εκτός από το βήμα αποστολής."""
    cid = c["id"]
    today = datetime.now(ATHENS).date().isoformat()
    eff = _cycle_effective(chosen, c)
    _cycle_set(level=eff)
    _cycle_rec["companies"][-1]["level"] = eff
    _clog(f"  Ενέργειες: {_CYCLE_LEVELS[eff]}" + (
        f" — επιλέχθηκε «{_CYCLE_LEVELS.get(chosen, chosen)}» αλλά οι άδειες της εταιρείας δεν το επιτρέπουν"
        if eff != chosen else ""), "warn" if eff != chosen else "msg")
    if not _mark_ready(cid, "expense"):
        _clog("⏭ Παράλειψη: δεν έχει γίνει η αρχική ανάκτηση «Νέα παραστατικά από MARK» των εξόδων "
              "(γίνεται μία φορά χειροκίνητα από την Ανάκτηση).", "warn")
        tot["skipped"] += 1
        return

    # 1) Ανάκτηση
    _cycle_set(step="Ανάκτηση από myDATA", done=0, total=0)
    _clog("① Ανάκτηση: Νέα παραστατικά εξόδων από MARK", "head")
    before = {r["mark"] for r in db.get_documents(cid, "expense")}
    with _cycle_ctx(cid, {"scope": "expense", "action": "new", "date_from": today, "date_to": today}):
        sync_run()
        failed = any(cat == "error" for cat, _ in session.get("_flashes", []))
    if failed:
        _clog("✗ Η ανάκτηση απέτυχε — η εταιρεία παραλείπεται σε αυτόν τον κύκλο.", "err")
        tot["failed"] += 1
        return
    new = [r["mark"] for r in db.get_documents(cid, "expense") if r["mark"] not in before]
    tot["new"] += len(new)
    _clog(f"  Νέα παραστατικά εξόδων: {len(new)}" + (f" ({', '.join(new)})" if new else ""))
    _cycle_check()

    # 2) Χαρακτηρισμός όσων έχουν πρόταση (ένα-ένα: πρόοδος και διακοπή ανά παραστατικό)
    with _cycle_ctx(cid):
        cands = _auto_classify_candidates(cid)
        names = db.load_names()
        no_rule = [_row_to_invoice(r, names) for r in db.get_documents(cid, "expense", ["unclassified"])
                   if r["mark"] not in {i.mark for i in cands}]
    classified = []
    if not _cycle_level(cid, chosen, "classify"):
        _clog(f"⏭ Χαρακτηρισμός: εκτός ενεργειών της εταιρείας — {len(cands)} αχαρακτήριστα με πρόταση "
              "περιμένουν χειροκίνητο χαρακτηρισμό.", "warn")
    else:
        _clog(f"② Χαρακτηρισμός με πρόταση συναλλασσόμενου: {len(cands)} αχαρακτήριστα με πρόταση", "head")
        for k, inv in enumerate(cands, 1):
            _cycle_check()
            _cycle_set(step="Χαρακτηρισμός", done=k - 1, total=len(cands))
            if not (level := _cycle_level(cid, chosen, "classify")):
                raise PermissionError("οι άδειες χαρακτηρισμού της εταιρείας άλλαξαν στη μέση του κύκλου")
            with _cycle_ctx(cid, {"marks": [inv.mark], "action": "rules"}, level):
                bulk_classify()
                _keep_errors()  # το «Χαρακτηρίστηκαν τοπικά… Στείλ' τα…» δεν ισχύει εδώ· μένουν μόνο οι αιτίες αποτυχίας
                row = db.get_document(cid, inv.mark)
            if row and row["status"] == "classified":
                classified.append(inv.mark)
                _clog(f"  ✓ {_doc_text(inv)} → {_cls_text(db.get_local_classification(row['id']))}", "ok")
            else:
                tot["failed"] += 1
                _clog(f"  ✗ {_doc_text(inv)} → δεν χαρακτηρίστηκε (βλ. αιτία παραπάνω)", "err")
        tot["classified"] += len(classified)
        _cycle_set(done=len(cands))

    # 3) Αποστολή μόνο όσων χαρακτηρίστηκαν τώρα (όχι όσα άφησε ο χρήστης χαρακτηρισμένα),
    # εκτός από προμηθευτές με «Επιβεβαίωση πριν την αποστολή» — αυτά μένουν για τον χρήστη.
    review = db.review_vats(cid)
    held = [i for i in cands if i.mark in classified and i.issuer_vat in review]
    if held:
        classified = [m for m in classified if m not in {i.mark for i in held}]
        _clog(f"⏸ Αναμένουν επιβεβαίωση ({len(held)}) — έμειναν στα «Χαρακτηρισμένα»: "
              + ", ".join(i.mark for i in held), "warn")
    if classified and not _cycle_level(cid, chosen, "send"):
        _clog(f"⏭ Αποστολή: εκτός ενεργειών της εταιρείας — {len(classified)} έμειναν στα «Χαρακτηρισμένα».", "warn")
    elif classified:
        _clog(f"③ Αποστολή στο myDATA: {len(classified)} παραστατικά", "head")
        for k, mark in enumerate(classified, 1):
            _cycle_check()
            _cycle_set(step="Αποστολή στο myDATA", done=k - 1, total=len(classified))
            if not _cycle_level(cid, chosen, "send"):
                raise PermissionError("οι άδειες αποστολής της εταιρείας άλλαξαν στη μέση του κύκλου")
            with _cycle_ctx(cid, {"marks": [mark]}, "send"):
                send()
                _keep_errors()  # το γενικό «Στάλθηκαν…» αντικαθίσταται από τη γραμμή ✓
            row = db.get_document(cid, mark)
            if row and row["status"] == "sent":
                tot["sent"] += 1
                _clog(f"  ✓ {mark} στάλθηκε · MARK χαρακτηρισμού {row.get('classification_mark') or '—'}", "ok")
            else:
                tot["failed"] += 1
                _clog(f"  ✗ {mark} δεν στάλθηκε — παραμένει στα «Χαρακτηρισμένα»", "err")
        _cycle_set(done=len(classified))

    # 4) Χωρίς πρόταση: δεν αγγίζονται, μόνο αναφέρονται.
    tot["no_rule"] += len(no_rule)
    if no_rule:
        _clog(f"⚠ Χωρίς πρόταση — δεν χαρακτηρίστηκαν ({len(no_rule)}):", "warn")
        for inv in sorted(no_rule, key=lambda i: i.issue_date or ""):
            _clog(f"    {_doc_text(inv)}" + (" · τύπος 1.5: μόνο χειροκίνητα" if inv.invoice_type == "1.5" else ""), "warn")


def _cycle_write(started: datetime, title: str) -> None:
    """Γράφει (ξανά) τα αρχεία του κύκλου: .log (κείμενο, για λήψη) και .json (κάρτες της σελίδας)· καλείται μετά
    από κάθε εταιρεία, ώστε να μη χαθεί τίποτα σε crash."""
    try:
        os.makedirs(_CYCLE_DIR, exist_ok=True)
        base = os.path.join(_CYCLE_DIR, f"{started:%Y%m%d-%H%M%S-%f}")
        with open(base + ".log", "w", encoding="utf-8") as f:
            f.write(title + "\n" + "\n".join(_cycle_buf) + "\n")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(_cycle_rec, f, ensure_ascii=False)
        try:
            keep = max(1, int(db.get_setting("keep_cycle_logs") or CYCLE_LOG_KEEP))
        except ValueError:
            keep = CYCLE_LOG_KEEP
        for old in sorted(n for n in os.listdir(_CYCLE_DIR) if n.endswith(".log"))[:-keep]:
            for ext in (".log", ".json"):
                if os.path.exists(path := os.path.join(_CYCLE_DIR, old[:-4] + ext)):
                    os.remove(path)
    except OSError:
        app.logger.exception("auto cycle log")  # το log δεν σταματά ποτέ τον κύκλο


def _cycle_history(limit: int = 30) -> list[dict]:
    """Οι τελευταίοι κύκλοι για τη σελίδα, νεότερος πρώτα: η δομημένη εγγραφή (.json) + name για τη λήψη του .log.
    Κύκλοι χωρίς .json (παλαιότερη έκδοση) ανασυνθέτονται από το κείμενο του .log (_cycle_from_log)."""
    out = []
    for lg in mydata_logs(None, _CYCLE_DIR)[:limit]:
        try:
            with open(os.path.join(_CYCLE_DIR, lg["name"][:-4] + ".json"), encoding="utf-8") as f:
                out.append(dict(json.load(f), name=lg["name"]))
        except (OSError, ValueError):
            out.append(dict(_cycle_from_log(lg), name=lg["name"]))
    return out


_LOG_CO = re.compile(r"^\d\d:\d\d:\d\d  ━━ \d+/\d+ · (.+?) · ΑΦΜ (\S+) · (prod|dev)")
_LOG_TOTALS = {"νέα": "new", "χαρακτηρίστηκαν": "classified", "στάλθηκαν": "sent", "αποτυχίες": "failed",
               "χωρίς πρόταση": "no_rule", "παραλείφθηκαν": "skipped"}


def _log_kind(t: str) -> str:
    """Το είδος μιας γραμμής .log (για τα χρώματα), όπως θα το είχε δώσει το _clog."""
    if "↔" in t:
        return "err" if " ✗ " in t else "call"
    body = t[10:].lstrip()
    if body.startswith("✗"):
        return "err"
    if body.startswith("✓"):
        return "ok"
    if body[:1] in ("⚠", "⏭", "⏹") or body[:1].isdigit() and "€" in body:  # «χωρίς πρόταση» παραστατικά
        return "warn"
    return "head" if body[:1] in ("①", "②", "③") else "msg"


def _cycle_from_log(lg: dict) -> dict:
    """Κύκλος από .log χωρίς .json (πριν από τις κάρτες): ίδια δομή με το _cycle_rec, ώστε να φαίνεται με
    κάρτα ανά εταιρεία. Τα σύνολα του κύκλου από τον τίτλο· της εταιρείας μετρημένα από τις γραμμές της.
    ponytail: στηρίζεται στη μορφή των γραμμών του _clog· άγνωστες γραμμές απλώς μένουν «msg»."""
    title, name = lg["title"], lg["name"]
    totals = {key: int(m.group(1)) for label, key in _LOG_TOTALS.items()
              if (m := re.search(rf"{label} (\d+)", title))}
    rec = {"started": f"{name[:4]}-{name[4:6]}-{name[6:8]}T{name[9:11]}:{name[11:13]}:{name[13:15]}",
           "trigger": (m.group(1) if (m := re.search(r"\((.*?)\)", title)) else ""), "lines": [], "companies": [],
           "stopped": "διακόπηκε" in title, "ended": "σε εξέλιξη" not in title, "totals": totals,
           "secs": int(m.group(1)) if (m := re.search(r"Τέλος.*? κύκλου σε (\d+)s", lg["body"])) else 0}
    co = None
    for t in lg["body"].split("\n"):
        if m := _LOG_CO.match(t):
            co = {"name": m.group(1), "vat": "" if m.group(2) == "—" else m.group(2), "env": m.group(3),
                  "level": "", "secs": 0, "lines": [], "counts": dict.fromkeys(_COUNT_KEYS, 0)}
            rec["companies"].append(co)
            continue
        if (m := re.match(r"^\d\d:\d\d:\d\d    \((\d+(?:\.\d+)?)s\)$", t)) and co:
            co["secs"] = float(m.group(1))
            continue
        if "▶ Έναρξη κύκλου" in t or "■ Τέλος" in t or "⏹ Τέλος" in t or not t.strip():
            continue
        line = {"t": t, "k": _log_kind(t)}
        if not co:
            rec["lines"].append(line)
            continue
        co["lines"].append(line)
        n = co["counts"]
        if m := re.search(r"Ενέργειες: (.+?)(?: —|$)", t):
            # startswith: και τα παλιά logs με «… αποστολή στο myDATA»
            co["level"] = next((k for k, v in _CYCLE_LEVELS.items() if m.group(1).startswith(v)), "")
        elif m := re.search(r"Νέα παραστατικά εξόδων: (\d+)", t):
            n["new"] += int(m.group(1))
        elif m := re.search(r"Χωρίς πρόταση — δεν χαρακτηρίστηκαν \((\d+)\)", t):
            n["no_rule"] += int(m.group(1))
        elif "⏭ Παράλειψη" in t:
            n["skipped"] += 1
        elif line["k"] == "ok":
            n["sent" if "στάλθηκε" in t else "classified"] += 1
        elif line["k"] == "err" and "↔" not in t and not t.startswith("          "):
            n["failed"] += 1
    for co in rec["companies"]:
        text = "\n".join(ln["t"] for ln in co["lines"])
        co["status"] = ("stopped" if "⏹ Διακόπηκε" in text else "error" if co["counts"]["failed"]
                        else "skipped" if co["counts"]["skipped"] else "ok")
    return rec


def _cycle_summary(tot: dict) -> str:
    return (f"νέα {tot['new']} · χαρακτηρίστηκαν {tot['classified']} · στάλθηκαν {tot['sent']} · "
            f"αποτυχίες {tot['failed']} · χωρίς πρόταση {tot['no_rule']} · παραλείφθηκαν {tot['skipped']}")


def _cycle_cards() -> list[dict]:
    """Οι υποκάρτες εταιρειών του τρέχοντος κύκλου χωρίς τις γραμμές (για τη ζωντανή προβολή)."""
    return [{k: v for k, v in co.items() if k != "lines"} for co in _cycle_rec["companies"]]


def _run_cycle(trigger: str) -> dict:
    """Ένας πλήρης κύκλος σε όλες τις επιλεγμένες εταιρείες. Επιστρέφει τα σύνολα."""
    synced = _cycle_sync_levels()  # γράφονται στο log μόλις ξεκινήσει (παρακάτω)
    _, minutes, sel = _cycle_settings()
    ids = list(sel)
    started = datetime.now(ATHENS)
    db.set_setting("auto_cycle_last", started.isoformat())
    _cycle_stop.clear()
    _cycle_buf.clear()
    _cycle_rec.clear()
    _cycle_rec.update(started=started.isoformat(), trigger=trigger, minutes=minutes, lines=[], companies=[],
                      ended=None, secs=0, stopped=False, totals={})
    _cycle_set(running=True, trigger=trigger, company="", step="Έναρξη", done=0, total=0,
               started=started.isoformat(), lines=[], cidx=0, companies=[])
    tot = dict.fromkeys(_COUNT_KEYS, 0)
    by_id = {c["id"]: c for c in load_companies()}
    comps = [by_id[i] for i in ids if i in by_id]
    title = lambda end="": (f"{started:%d/%m/%Y %H:%M:%S} · Αυτόματος κύκλος ({trigger}) · {len(comps)} εταιρείες"  # noqa: E731
                            + (f" · {end}" if end else " · σε εξέλιξη"))
    _clog(f"▶ Έναρξη κύκλου ({trigger}) · διάστημα {minutes}′ · εταιρείες: "
          + (", ".join(c["company_name"] for c in comps) or "καμία"), "sep")
    for msg in synced:
        _clog("⚠ " + msg, "warn")
    if not comps:
        _clog("⚠ Καμία εταιρεία στον κύκλο — επίλεξε εταιρείες στις Ρυθμίσεις.", "warn")
    stopped = False
    for n, c in enumerate(comps, 1):
        if _cycle_stop.is_set():
            stopped = True
            _clog("⏹ Διακόπηκε από τον χρήστη. Δεν εκτελέστηκαν: "
                  + ", ".join(x["company_name"] for x in comps[n - 1:]), "warn")
            break
        rec = {"name": c["company_name"], "vat": c.get("AADE_VAT_NUMBER") or "",
               "env": "dev" if c.get("MYDATA_ENV") == "dev" else "prod", "chosen": sel[c["id"]],
               "level": _cycle_effective(sel[c["id"]], c), "status": "running", "secs": 0,
               "counts": dict.fromkeys(_COUNT_KEYS, 0), "lines": []}
        _cycle_rec["companies"].append(rec)
        _clog(f"━━ {n}/{len(comps)} · {c['company_name']} · ΑΦΜ {rec['vat'] or '—'} · {rec['env']}", "sep")
        _cycle_set(company=c["company_name"], step="", done=0, total=0, index=n, count=len(comps), cidx=n,
                   companies=_cycle_cards())
        t0, before, error = time.monotonic(), dict(tot), False
        try:
            _cycle_company(c, sel[c["id"]], tot)
        except _CycleStopped:
            stopped = True
            rest = ", ".join(x["company_name"] for x in comps[n:])
            _clog("⏹ Διακόπηκε από τον χρήστη στη μέση της εταιρείας (μετά την τρέχουσα κλήση). Ό,τι έγινε ως εδώ "
                  "ισχύει· τα υπόλοιπα γίνονται στον επόμενο κύκλο." + (f" Δεν εκτελέστηκαν: {rest}" if rest else ""),
                  "warn")
        except Exception:  # noqa: BLE001 — μία εταιρεία δεν σταματά τον κύκλο
            tot["failed"] += 1
            error = True
            _clog("✗ Απρόσμενο σφάλμα — συνέχεια στην επόμενη εταιρεία:\n" + traceback.format_exc().rstrip(), "err")
        rec["secs"] = round(time.monotonic() - t0, 1)
        rec["counts"] = {k: tot[k] - before[k] for k in _COUNT_KEYS}
        rec["status"] = ("stopped" if stopped else "error" if error or rec["counts"]["failed"]
                         else "skipped" if rec["counts"]["skipped"] else "ok")
        _clog(f"  ({rec['secs']:.1f}s)", "sep")
        _cycle_set(cidx=0, companies=_cycle_cards())
        _cycle_write(started, title())
        if stopped:
            break
    secs = (datetime.now(ATHENS) - started).total_seconds()
    summary = _cycle_summary(tot)
    _clog(f"{'⏹ Τέλος (διακόπηκε)' if stopped else '■ Τέλος'} κύκλου σε {secs:.0f}s · {summary}", "sep")
    _cycle_rec.update(ended=datetime.now(ATHENS).isoformat(), secs=round(secs), stopped=stopped, totals=dict(tot))
    _cycle_write(started, title(("διακόπηκε · " if stopped else "") + summary))
    app.logger.info("auto cycle (%s): %s", trigger, summary)
    _cycle_stop.clear()
    _cycle_set(running=False, company="", step="", done=0, total=0,
               summary={"at": f"{started:%d/%m/%Y %H:%M}", "text": summary, "stopped": stopped, **tot})
    return tot


def _auto_cycle_loop() -> None:
    while True:
        try:  # ξύπνημα ακριβώς στη λήξη της αντίστροφης μέτρησης (αλλιώς κάθε 30″ για αλλαγές ρυθμίσεων)
            wait = min(30.0, max(0.5, (_cycle_due() - datetime.now(ATHENS)).total_seconds()))
        except Exception:  # noqa: BLE001 — π.χ. βάση που δεν έχει αρχικοποιηθεί ακόμα
            wait = 30.0
        manual = _cycle_wake.wait(timeout=wait)
        _cycle_wake.clear()
        try:
            on = _cycle_settings()[0]
            if (manual or (on and datetime.now(ATHENS) >= _cycle_due())) and auth.unlocked():
                _run_cycle("χειροκίνητα" if manual else "προγραμματισμένα")
        except Exception:  # noqa: BLE001 — το νήμα δεν πεθαίνει ποτέ
            app.logger.exception("auto cycle")
            _cycle_set(running=False)


_cycle_thread: threading.Thread | None = None


@app.before_request
def _start_auto_cycle():
    """Το νήμα ξεκινά στο πρώτο request (όχι στο import: εκεί θα έτρεχε παράλληλα με imports άλλων modules)."""
    global _cycle_thread
    if _cycle_thread is None and not app.testing and not os.getenv("MYDATA_NO_AUTO_CYCLE"):
        with _cycle_lock:
            if _cycle_thread is None:
                _cycle_thread = threading.Thread(target=_auto_cycle_loop, daemon=True, name="auto-cycle")
                _cycle_thread.start()


def _cycle_phase() -> str:
    on = _cycle_settings()[0]
    if _cycle["running"]:
        return "stopping" if _cycle_stop.is_set() else "running"
    return "locked" if not auth.unlocked() else "idle" if on else "disabled"


@app.route("/auto-cycle")
def auto_cycle():
    for msg in _cycle_sync_levels():  # π.χ. μετά από επαναφορά αντιγράφου ή άλλη αλλαγή εκτός σελίδας Εταιρειών
        flash(msg, "error")
    on, minutes, sel = _cycle_settings()
    companies = [dict(c, ready=_mark_ready(c["id"], "expense"), max_level=_cycle_allowed(c),
                      level=sel.get(c["id"], "")) for c in load_companies()]
    return render_template("auto_cycle.html", on=on, minutes=minutes, ids=sel, companies=companies,
                           levels=_CYCLE_LEVELS, rank=_LEVEL_RANK,
                           history=_cycle_history(), running_started=_cycle["started"] if _cycle["running"] else None)


@app.route("/auto-cycle/status")
def auto_cycle_status():
    """Ζωντανή κατάσταση για τη σελίδα· since = τελευταίος αριθμός γραμμής που έχει ήδη η σελίδα."""
    try:
        since = int(request.args.get("since") or 0)
    except ValueError:
        since = 0
    notes = _cycle_sync_levels() if auth.unlocked() else []  # άδειες που άλλαξαν αλλού (άλλη καρτέλα, επαναφορά…)
    on, minutes, sel = _cycle_settings()
    with _cycle_lock:
        state = {k: v for k, v in _cycle.items() if k != "lines"}
        lines = [ln for ln in _cycle["lines"] if ln["n"] > since]  # νέος κύκλος = νέο started → η σελίδα καθαρίζει
    phase = _cycle_phase()
    # Υπογραφή των ρυθμίσεων: αν αλλάξει (επιλογή ή άδειες εταιρείας), η σελίδα ξαναφέρνει τις Ρυθμίσεις.
    settings_sig = json.dumps([minutes, on] + [[c["id"], c["company_name"], sel.get(c["id"], ""), _cycle_allowed(c)]
                                               for c in load_companies()], ensure_ascii=False)
    return {**state, "phase": phase, "on": on, "minutes": minutes, "lines": lines, "notes": notes,
            "settings_sig": settings_sig,
            "next_run": _cycle_due().isoformat() if on and phase == "idle" else None,
            "now": datetime.now(ATHENS).isoformat()}


@app.route("/auto-cycle/start", methods=["POST"])
def auto_cycle_start():
    if not _cycle["running"]:
        _cycle_wake.set()
    return {"ok": True}


@app.route("/auto-cycle/stop", methods=["POST"])
def auto_cycle_stop():
    if _cycle["running"]:
        _cycle_stop.set()
    return {"ok": True}


@app.route("/auto-cycle/toggle", methods=["POST"])
def auto_cycle_toggle():
    """Παύση / συνέχιση του χρονοπρογραμματισμού. Η παύση διακόπτει και τον κύκλο που τρέχει.
    Η συνέχιση μετρά το διάστημα από τώρα (όχι κύκλος αμέσως επειδή «χρωστάει» από την παύση)."""
    on = not _cycle_settings()[0]
    db.set_setting("auto_cycle_on", "1" if on else "0")
    if on:
        db.set_setting("auto_cycle_last", datetime.now(ATHENS).isoformat())
    elif _cycle["running"]:
        _cycle_stop.set()
    _cycle_wake.clear()
    return {"ok": True, "on": on}


@app.route("/auto-cycle/settings", methods=["POST"])
def auto_cycle_settings():
    minutes = request.form.get("minutes", "").strip()
    if not (minutes.isdigit() and 5 <= int(minutes) <= 1440):
        flash("Το διάστημα του κύκλου πρέπει να είναι από 5 έως 1440 λεπτά.", "error")
        return redirect(url_for("auto_cycle"))
    sel = {}
    for c in load_companies():  # level_<id>: "" = εκτός κύκλου· ποτέ πάνω από ό,τι επιτρέπουν οι άδειές της
        chosen = request.form.get(f"level_{c['id']}", "")
        if chosen not in _LEVEL_RANK:
            continue
        sel[c["id"]] = _cycle_effective(chosen, c)
        if sel[c["id"]] != chosen:
            flash(f"{c['company_name']}: οι άδειες της εταιρείας επιτρέπουν μόνο «{_CYCLE_LEVELS[sel[c['id']]]}».", "error")
    db.set_setting("auto_cycle_minutes", minutes)
    db.set_setting("auto_cycle_on", "1" if request.form.get("on") else "0")
    db.set_setting("auto_cycle_companies", json.dumps(sel))
    flash("✔ Αποθηκεύτηκαν οι ρυθμίσεις του αυτόματου κύκλου" + (" (ισχύουν από τον επόμενο κύκλο)."
                                                               if _cycle["running"] else "."), "ok")
    return redirect(url_for("auto_cycle"))


@app.route("/auto-cycle/log/<name>")
def auto_cycle_log(name):
    if not re.fullmatch(r"\d{8}-\d{6}-\d{6}\.log", name) or not os.path.isfile(os.path.join(_CYCLE_DIR, name)):
        abort(404)
    return send_file(os.path.join(_CYCLE_DIR, name), as_attachment=True, download_name=f"auto-cycle-{name}")


@app.context_processor
def inject_company():
    # self_types: τύποι που εκδίδουμε εμείς (13.x–17.x) → επιτρέπεται «Αντιγραφή».
    if request.endpoint in _PUBLIC:
        return {}  # login/setup: κλειδωμένη, τα κλειδιά των εταιρειών δεν αποκρυπτογραφούνται
    idx, comps = get_active_index(), load_companies()
    return {"active_company": comps[idx] if comps else None, "active_index": idx, "self_types": SELF_EXPENSE_TYPES, "update": _update}


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
