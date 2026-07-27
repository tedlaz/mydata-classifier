"""
myDATA Expense Classifier - Flask UI
Εκτέλεση:  python app.py  →  http://127.0.0.1:5000
"""

import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db
from classifications import (
    CREDIT_INVOICE_TYPES,
    EXPENSE_CATEGORIES,
    EXPENSE_TYPES,
    INCOME_CATEGORIES,
    INCOME_TYPES,
    INVOICE_TYPE_NAMES,
    VAT_TYPES,
)
from ensure_env import ensure_env, env_path
from mydata_client import ExpenseInvoice, InvoiceLine, MyDataClient, MyDataError

ensure_env()  # δημιουργεί .env με ασφαλές FLASK_SECRET στην πρώτη εκκίνηση
load_dotenv(env_path())

# Ώρα Ελλάδας: όλες οι «σήμερα/τώρα» τιμές να είναι ανεξάρτητες από τη ζώνη του server
ATHENS = ZoneInfo("Europe/Athens")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")


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


def get_own_vat() -> str:
    c = get_active_company()
    if c and c.get("AADE_VAT_NUMBER"):
        return str(c["AADE_VAT_NUMBER"]).strip()
    return (os.getenv("AADE_VAT_NUMBER") or "").strip()


# ------------------------------------------------------------------ #
# Προμηθευτές: κανόνας χαρακτηρισμού + επωνυμία ανά ΑΦΜ, ΚΟΙΝΟΙ για όλες τις
# εταιρείες (πίνακας suppliers). Λεπτά wrappers γύρω από το db.
# ------------------------------------------------------------------ #
def load_rules() -> dict:
    return db.load_rules()


def save_rule(issuer_vat: str | None, ctype: str, ccat: str, vat_type: str = ""):
    db.save_rule(issuer_vat, ctype, ccat, vat_type)


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
        return MyDataClient(user_id, sub_key, env)
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
        "lines": [
            {
                "line_number": ln.line_number,
                "net_value": ln.net_value,
                "vat_amount": ln.vat_amount,
                "vat_category": ln.vat_category,
                "has_expenses_classification": ln.has_expenses_classification,
                "classifications": ln.classifications,
            }
            for ln in inv.lines
        ],
        "cls_info": getattr(inv, "cls_info", []) or [],
        # MARK χαρακτηρισμού από το myDATA (για όσα ήταν ήδη χαρακτηρισμένα εκεί).
        "classification_mark": getattr(inv, "classification_mark", "") or "",
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
    return inv


@app.route("/")
def index():
    return redirect(url_for("invoices"))


@app.route("/fetch", methods=["POST"])
def fetch():
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    try:
        # HTML date input δίνει yyyy-mm-dd → μετατροπή σε dd/MM/yyyy
        df = date.fromisoformat(date_from).strftime("%d/%m/%Y")
        dt = date.fromisoformat(date_to).strftime("%d/%m/%Y")
    except ValueError:
        flash("Μη έγκυρες ημερομηνίες.", "error")
        return redirect(url_for("invoices"))

    cid = _active_company_id()
    if not cid:
        flash(
            "Δεν έχει οριστεί ενεργή εταιρεία. Πρόσθεσε μία στη σελίδα «Εταιρείες».",
            "error",
        )
        return redirect(url_for("companies"))

    try:
        client = get_client()
        unclassified = client.request_unclassified_expenses(df, dt)
        classified = client.request_classified_expenses(df, dt)
    except MyDataError as e:
        flash(str(e), "error")
        return redirect(url_for("invoices"))
    except Exception as e:  # noqa: BLE001 — network κ.λπ., δεν θέλουμε 500 στο route
        flash(f"Σφάλμα επικοινωνίας: {e}", "error")
        return redirect(url_for("invoices"))

    enrich_issuer_names(unclassified)
    enrich_issuer_names(classified)

    # Αποθήκευση στο μόνιμο ledger (SQLite). Το upsert ΔΕΝ χαμηλώνει την τοπική
    # πρόοδο: αχαρακτήριστα που τοπικά είναι classified/sent μένουν ως έχουν,
    # ενώ όσα το myDATA δείχνει χαρακτηρισμένα περνούν σε «Επιβεβαιωμένα».
    for inv in unclassified:
        db.upsert_document(cid, "expense", _invoice_to_doc(inv), "unclassified")
    for inv in classified:
        db.upsert_document(cid, "expense", _invoice_to_doc(inv), "confirmed")

    db.set_setting("last_range", f"{df} – {dt}")
    flash(
        f"✔ Ανακτήθηκαν και αποθηκεύτηκαν {len(unclassified) + len(classified)} "
        "παραστατικά.",
        "ok",
    )
    return redirect(url_for("invoices", date_from=date_from, date_to=date_to))


@app.route("/documents/delete-range", methods=["POST"])
def delete_documents_range():
    kind = request.form.get("kind", "")
    destination = "income" if kind == "income" else "invoices"
    if kind not in ("expense", "income"):
        flash("Μη έγκυρο είδος βιβλίου.", "error")
        return redirect(url_for("invoices"))

    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        flash("Μη έγκυρες ημερομηνίες.", "error")
        return redirect(url_for(destination))
    if start > end:
        flash("Η ημερομηνία «Από» πρέπει να προηγείται της «Έως».", "error")
        return redirect(url_for(destination))

    cid = _active_company_id()
    if not cid:
        flash(
            "Δεν έχει οριστεί ενεργή εταιρεία. Πρόσθεσε μία στη σελίδα «Εταιρείες».",
            "error",
        )
        return redirect(url_for("companies"))

    deleted = db.delete_documents_range(cid, kind, date_from, date_to)
    book = "εσόδων" if kind == "income" else "εξόδων"
    flash(
        f"Διαγράφηκαν {deleted} παραστατικά {book} για το διάστημα "
        f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}.",
        "ok",
    )
    return redirect(url_for(destination, date_from=date_from, date_to=date_to))


# Καρτέλες κατάστασης του βιβλίου εξόδων (σειρά ροής).
_EXPENSE_VIEWS = ("unclassified", "classified", "sent", "confirmed")


PER_PAGE = 50


@app.route("/invoices")
def invoices():
    today = datetime.now(ATHENS).strftime("%Y-%m-%d")
    range_date_from = request.args.get("date_from", "").strip() or today
    range_date_to = request.args.get("date_to", "").strip() or today
    view = request.args.get("view", "unclassified")
    if view not in _EXPENSE_VIEWS:
        view = "unclassified"
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "asc")
    reverse = direction == "desc"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
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
    marks_int = []
    for row in db.get_documents(cid, "expense") if cid else []:
        buckets.setdefault(row["status"], []).append(_row_to_invoice(row, names))
        m = row["mark"] or ""
        if m.isdigit():
            marks_int.append(int(m))

    counts = {v: len(buckets[v]) for v in _EXPENSE_VIEWS}
    last_mark = str(max(marks_int)) if marks_int else None

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
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    page_items = items[start : start + PER_PAGE]

    return render_template(
        "invoices.html",
        view=view,
        invoices=page_items,
        counts=counts,
        date_range=db.get_setting("last_range"),
        last_mark=last_mark,
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
        per_page=PER_PAGE,
        filters=filters,
        range_date_from=range_date_from,
        range_date_to=range_date_to,
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
    # Χαρακτηρισμοί ΑΝΑ ΓΡΑΜΜΗ (από τον πίνακα classifications, που κρατά το
    # line_number) → εμφανίζονται δίπλα σε κάθε γραμμή στην προβολή.
    cls_by_line: dict[int | None, list[dict]] = {}
    for c in db.get_local_classification(row["id"]):
        cls_by_line.setdefault(c.get("line_number"), []).append(
            {
                "type": c.get("classification_type"),
                "category": c.get("classification_category"),
                "amount": c.get("amount"),
            }
        )
    # Ενέργειες σε επίπεδο παραστατικού (απόρριψη/ακύρωση/απόκλιση) - δεν ανήκουν
    # σε γραμμή, μένουν στο κάτω μπλοκ.
    flags = [c for c in (inv.cls_info or []) if c.get("transaction_mode")]
    has_line_cls = bool(cls_by_line) or any(ln.classifications for ln in inv.lines)
    # URL επιστροφής στη λίστα, διατηρώντας ταξινόμηση/σελίδα/φίλτρα (fallback: view).
    back = request.args.get("back") or url_for("invoices", view=row["status"])
    return render_template(
        "document.html",
        inv=inv,
        status=row["status"],
        local_action=row.get("local_action") or "classify",
        source=row.get("source") or "rest",
        classification_mark=row.get("classification_mark"),
        type_desc=INVOICE_TYPE_NAMES.get(inv.invoice_type or ""),
        back=back,
        cls_by_line=cls_by_line,
        flags=flags,
        has_line_cls=has_line_cls,
        categories=EXPENSE_CATEGORIES,
        types=EXPENSE_TYPES,
        vat_types=VAT_TYPES,
    )


def _pair_cls_rows(entries: list[dict]) -> list[dict]:
    """Ζευγαρώνει κάθε E3 χαρακτηρισμό με γραμμή ΦΠΑ (VAT_xxx) ίδιου ποσού και
    επιστρέφει UI rows {category, type, amount, vat_type}."""
    e3s = [e for e in entries if e.get("type") and not e["type"].startswith("VAT_")]
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
    return rows


def _line_classification_rows(inv, rule: dict) -> list[dict]:
    """Ομάδες χαρακτηρισμού ΑΝΑ ΓΡΑΜΜΗ του αρχικού παραστατικού. Το myDATA απαιτεί
    χαρακτηρισμό ΚΑΘΕ γραμμής, με άθροισμα E3 ίσο με την αξία της (σφάλματα
    303/304/306 όταν λείπουν γραμμές ή δεν κλείνουν τα ποσά). Κάθε ομάδα =
    {line_number, net, vat, rows:[{category, type, amount, vat_type}]}."""
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
        src = ln.classifications or (inv.cls_info if single else []) or []
        rows = _pair_cls_rows(src)
        if not rows:
            rows = [
                {
                    "category": rule.get("category", ""),
                    "type": rule.get("type", ""),
                    "amount": net,
                    "vat_type": rule.get("vat_type", "")
                    or ("VAT_361" if vat > 0 else ""),
                }
            ]
        groups.append(
            {
                "line_number": ln.line_number or (len(groups) + 1),
                "net": net,
                "vat": vat,
                "rows": rows,
            }
        )
    return groups


@app.route("/classify/<mark>")
def classify(mark):
    cid = _active_company_id()
    row = db.get_document(cid, mark) if cid else None
    if row is None:
        flash("Το παραστατικό δεν βρέθηκε. Κάνε νέα αναζήτηση.", "error")
        return redirect(url_for("invoices"))
    inv = _row_to_invoice(row)
    rule = load_rules().get(inv.issuer_vat or "", {})
    return render_template(
        "classify.html",
        inv=inv,
        line_groups=_line_classification_rows(inv, rule),
        has_existing=bool(inv.cls_info),
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
            return redirect(url_for("classify", mark=mark))
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
        return redirect(url_for("classify", mark=mark))

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
            return redirect(url_for("classify", mark=mark))

    # «Χειροκίνητο»: το παραστατικό είναι ήδη χαρακτηρισμένο στην πύλη myDATA - το
    # καταγράφουμε ΜΟΝΟ τοπικά (→ «Επιβεβαιωμένα», tag Manually), χωρίς αποστολή.
    manual = request.form.get("action") == "manual"
    db.save_local_classification(cid, mark, classifications, manual=manual)

    # αποθήκευση κανόνα ανά προμηθευτή από την πρώτη γραμμή χαρακτηρισμού
    if rule_from:
        save_rule(inv.issuer_vat, rule_from[0], rule_from[1], rule_from[2])
    if manual:
        flash(
            "✔ Καταγράφηκε τοπικά ως ήδη χαρακτηρισμένο στο myDATA (χειροκίνητο). "
            "Θα το βρεις στα «Επιβεβαιωμένα».",
            "ok",
        )
        return redirect(url_for("invoices", view="confirmed"))
    flash(
        "✔ Ο χαρακτηρισμός αποθηκεύτηκε τοπικά. Στείλ' τον στο myDATA από την "
        "καρτέλα «Χαρακτηρισμένα» με το κουμπί «Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(url_for("invoices", view="classified"))


@app.route("/bulk_classify", methods=["POST"])
def bulk_classify():
    """
    Συνολικός χαρακτηρισμός: ο ίδιος συνδυασμός εφαρμόζεται σε κάθε γραμμή
    ενός ή περισσότερων επιλεγμένων παραστατικών (ανά γραμμή - το myDATA δεν
    δέχεται πλέον classificationPostMode, σφάλμα 340).
    """
    marks = request.form.getlist("marks")
    ctype = request.form.get("bulk_type", "")
    ccat = request.form.get("bulk_category", "")
    vat_type = request.form.get("bulk_vat_type", "")
    use_rules = (
        request.form.get("action") == "rules"
    )  # "με βάση την πρόταση ανά προμηθευτή"
    rules = load_rules()

    if not marks:
        flash("Δεν επιλέχθηκε κανένα παραστατικό.", "error")
        return redirect(url_for("invoices"))
    if not use_rules and not (ctype and ccat):
        flash("Επίλεξε κατηγορία και τύπο χαρακτηρισμού.", "error")
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

        # επιλογή συνδυασμού: κανόνας προμηθευτή ή κοινή επιλογή από τη φόρμα
        if use_rules:
            rule = rules.get(inv.issuer_vat or "")
            if not rule or not (rule.get("type") and rule.get("category")):
                failed.append(
                    f"{mark}: δεν υπάρχει αποθηκευμένη πρόταση για τον προμηθευτή "
                    f"{inv.issuer_vat or '—'}"
                )
                continue
            use_type, use_cat, use_vat = (
                rule["type"],
                rule["category"],
                rule.get("vat_type", ""),
            )
        else:
            use_type, use_cat, use_vat = ctype, ccat, vat_type

        # Ο ίδιος συνδυασμός (type/category) εφαρμόζεται σε ΚΑΘΕ γραμμή του
        # παραστατικού, με ποσό την καθαρή αξία της γραμμής. Το myDATA πλέον δεν
        # δέχεται classificationPostMode=1 (σφάλμα 340) - ο χαρακτηρισμός γίνεται
        # ανά γραμμή. Fallback σε συμβολική γραμμή 1 αν λείπουν γραμμές.
        src_lines = inv.lines or [
            InvoiceLine(
                line_number=1,
                net_value=inv.total_net or 0.0,
                vat_amount=inv.total_vat or 0.0,
                vat_category=None,
                has_expenses_classification=False,
            )
        ]
        classifications = []
        for line in src_lines:
            classifications.append(
                {
                    "line_number": line.line_number,
                    "classification_type": use_type,
                    "classification_category": use_cat,
                    "amount": line.net_value,
                }
            )
            # Γραμμή με ΦΠΑ ΠΡΕΠΕΙ να έχει χαρακτηρισμό ΦΠΑ (σφάλμα 306). Αν δεν
            # δόθηκε, προεπιλέγεται VAT_361 (εγχώριες αγορές/δαπάνες). Το ποσό της
            # γραμμής VAT_xxx ισούται με την ΚΑΘΑΡΗ ΑΞΙΑ (σφάλμα 306), vatAmount null (337).
            if (line.vat_amount or 0) > 0:
                classifications.append(
                    {
                        "line_number": line.line_number,
                        "classification_type": use_vat or "VAT_361",
                        "classification_category": "",
                        "amount": line.net_value,
                    }
                )

        # Αποθήκευση ΤΟΠΙΚΑ (χωρίς αποστολή στο myDATA).
        db.save_local_classification(cid, mark, classifications)
        save_rule(inv.issuer_vat, use_type, use_cat, use_vat)
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


@app.route("/send", methods=["POST"])
def send():
    """Μαζική αποστολή στο myDATA των τοπικά χαρακτηρισμένων (όλων ή επιλεγμένων).
    Επιτυχία ανά παραστατικό → κατάσταση «Απεσταλμένο»."""
    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("invoices"))

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
                    own_vat=get_own_vat(),
                    payment_method=draft.get("payment_method", "5"),
                )
            else:  # classify / correct
                classifications = [
                    {
                        "line_number": e["line_number"],
                        "classification_type": e["classification_type"],
                        "classification_category": e["classification_category"] or "",
                        "amount": e["amount"],
                    }
                    for e in db.get_local_classification(row["id"])
                ]
                if not classifications:
                    failed.append(f"{mark}: λείπει ο τοπικός χαρακτηρισμός")
                    continue
                result = client.send_expenses_classification(mark, classifications)
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
    με τον ίδιο χαρακτηρισμό περνούν σε «Επιβεβαιωμένα»."""
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


@app.route("/income/fetch", methods=["POST"])
def income_fetch():
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    try:
        df = date.fromisoformat(date_from).strftime("%d/%m/%Y")
        dt = date.fromisoformat(date_to).strftime("%d/%m/%Y")
    except ValueError:
        flash("Μη έγκυρες ημερομηνίες.", "error")
        return redirect(url_for("income"))

    cid = _active_company_id()
    if not cid:
        flash(
            "Δεν έχει οριστεί ενεργή εταιρεία. Πρόσθεσε μία στη σελίδα «Εταιρείες».",
            "error",
        )
        return redirect(url_for("companies"))

    try:
        unclassified, classified, cancelled_marks = get_client().request_income(df, dt)
    except MyDataError as e:
        flash(str(e), "error")
        return redirect(url_for("income"))
    except Exception as e:  # noqa: BLE001 — network κ.λπ.
        flash(f"Σφάλμα επικοινωνίας: {e}", "error")
        return redirect(url_for("income"))

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

    db.set_setting("last_income_range", f"{df} – {dt}")
    flash(
        f"✔ Ανακτήθηκαν {len(unclassified) + len(classified)} παραστατικά εσόδων "
        f"({len(unclassified)} αχαρακτήριστα, {len(classified)} χαρακτηρισμένα)"
        + (f" · αφαιρέθηκαν {removed} ακυρωμένα." if removed else "."),
        "ok",
    )
    return redirect(url_for("income", date_from=date_from, date_to=date_to))


@app.route("/income")
def income():
    today = datetime.now(ATHENS).strftime("%Y-%m-%d")
    range_date_from = request.args.get("date_from", "").strip() or today
    range_date_to = request.args.get("date_to", "").strip() or today
    view = request.args.get("view", "unclassified")
    if view not in _INCOME_VIEWS:
        view = "unclassified"
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "asc")
    reverse = direction == "desc"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
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
    marks_int = []
    for row in db.get_documents(cid, "income") if cid else []:
        buckets.setdefault(row["status"], []).append(_row_to_invoice(row, names))
        m = row["mark"] or ""
        if m.isdigit():
            marks_int.append(int(m))

    counts = {v: len(buckets[v]) for v in _INCOME_VIEWS}
    last_mark = str(max(marks_int)) if marks_int else None

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
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    page_items = items[start : start + PER_PAGE]

    return render_template(
        "income.html",
        view=view,
        invoices=page_items,
        counts=counts,
        date_range=db.get_setting("last_income_range"),
        last_mark=last_mark,
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
        per_page=PER_PAGE,
        filters=filters,
        range_date_from=range_date_from,
        range_date_to=range_date_to,
    )


# ------------------------------------------------------------------ #
# ΑΝΑΦΟΡΕΣ
# ------------------------------------------------------------------ #
# Χαρακτηρισμένα έσοδα = status 'classified'. Χαρακτηρισμένα έξοδα = οποιαδήποτε
# κατάσταση πέρα από 'unclassified' (τοπικά χαρακτηρισμένα, απεσταλμένα ή
# επιβεβαιωμένα — όλα έχουν χαρακτηρισμό).
_INCOME_CLASSIFIED_STATUSES = ["classified"]
_EXPENSE_CLASSIFIED_STATUSES = ["classified", "sent", "confirmed"]
_REPORT_GROUPS = ("counterparty", "invoice_type", "classification", "vat")
_REPORT_GROUP_LABELS = {
    "period": "Περίοδος",
    "counterparty": "Πελάτης ή προμηθευτής",
    "invoice_type": "Τύπος παραστατικού",
    "classification": "Χαρακτηρισμός",
    "vat": "Κατηγορία ΦΠΑ",
}
_VAT_CATEGORY_LABELS = {
    "1": "24%",
    "2": "13%",
    "3": "6%",
    "4": "17%",
    "5": "9%",
    "6": "4%",
    "7": "0%",
    "8": "Χωρίς ΦΠΑ",
}
_CLASSIFICATION_NAMES = {**INCOME_TYPES, **EXPENSE_TYPES}
_CLASSIFICATION_CATEGORY_NAMES = {**INCOME_CATEGORIES, **EXPENSE_CATEGORIES}


def _report_number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _report_period(issue_date: str | None, period: str) -> str:
    if not issue_date:
        return ""
    try:
        issued = date.fromisoformat(issue_date)
    except ValueError:
        return ""
    if period == "day":
        return issued.isoformat()
    if period == "month":
        return issued.strftime("%Y-%m")
    quarter = (issued.month - 1) // 3 + 1
    return f"{issued.year}-Q{quarter}"


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


def _report_group_totals(
    documents: list[dict], period: str, group_by: list[str]
) -> dict:
    grouped: dict = {}
    by_classification = "classification" in group_by
    by_vat = "vat" in group_by

    for doc in documents:
        classifications = _report_classifications(doc) if by_classification else []
        for part in _report_financial_parts(doc, by_vat):
            allocations = [{"key": ("", ""), "weight": 1.0}]
            if by_classification and classifications:
                exact = [
                    item
                    for item in classifications
                    if part["line"] is not None and item["line"] == part["line"]
                ]
                allocations = exact or classifications
            weight_total = sum(item["weight"] for item in allocations)

            for allocation in allocations:
                share = (
                    allocation["weight"] / weight_total
                    if weight_total
                    else 1 / len(allocations)
                )
                key_parts = []
                if period:
                    key_parts.append(_report_period(doc.get("issue_date"), period))
                if "counterparty" in group_by:
                    key_parts.append(
                        (
                            str(doc.get("counterparty_name") or ""),
                            str(doc.get("counterparty_vat") or ""),
                        )
                    )
                if "invoice_type" in group_by:
                    key_parts.append(str(doc.get("invoice_type") or ""))
                if by_classification:
                    key_parts.append(allocation["key"])
                if by_vat:
                    key_parts.append(part["vat_category"])
                key = tuple(key_parts)
                bucket = grouped.setdefault(
                    key, {"net": 0.0, "vat": 0.0, "gross": 0.0, "_ids": set()}
                )
                for amount_name in ("net", "vat", "gross"):
                    bucket[amount_name] += part[amount_name] * share
                bucket["_ids"].add(doc["id"])

    for bucket in grouped.values():
        bucket["count"] = len(bucket.pop("_ids"))
        for amount_name in ("net", "vat", "gross"):
            bucket[amount_name] = round(bucket[amount_name], 2)
    return grouped


def _report_group_label(dimension: str, value) -> str:
    if dimension == "period":
        if not value:
            return "Χωρίς ημερομηνία"
        if "-Q" in value:
            year, quarter = value.split("-Q", 1)
            return f"{quarter}ο τρίμηνο {year}"
        if len(value) == 7:
            year, month = value.split("-", 1)
            return f"{month}/{year}"
        try:
            return date.fromisoformat(value).strftime("%d/%m/%Y")
        except ValueError:
            return value
    if dimension == "invoice_type":
        if not value:
            return "Χωρίς τύπο"
        description = INVOICE_TYPE_NAMES.get(value)
        return f"{value} — {description}" if description else value
    if dimension == "counterparty":
        name, vat = value
        if name and vat:
            return f"{name} — {vat}"
        if name:
            return name
        if vat:
            return vat
        return "Χωρίς στοιχεία πελάτη / προμηθευτή"
    if dimension == "classification":
        category, classification_type = value
        if not (category or classification_type):
            return "Χωρίς χαρακτηρισμό"
        parts = []
        if classification_type:
            description = _CLASSIFICATION_NAMES.get(classification_type)
            parts.append(
                f"{classification_type} — {description}"
                if description
                else classification_type
            )
        if category:
            description = _CLASSIFICATION_CATEGORY_NAMES.get(category)
            parts.append(f"{category} — {description}" if description else category)
        return " · ".join(parts)
    if not value:
        return "Άγνωστη κατηγορία ΦΠΑ"
    return f"{_VAT_CATEGORY_LABELS.get(value, value)} (κατηγορία {value})"


def _empty_report_totals() -> dict:
    return {"net": 0.0, "vat": 0.0, "gross": 0.0, "count": 0}


@app.route("/reports")
def reports():
    # Αν δεν δόθηκαν ρητά στο URL, χρησιμοποίησε τις τελευταίες τιμές της συνεδρίας.
    if "date_from" in request.args or "date_to" in request.args:
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()
    else:
        date_from = session.get("reports_date_from", "")
        date_to = session.get("reports_date_to", "")

    # Επικύρωση (αν δόθηκαν): ISO yyyy-mm-dd. Κενά = χωρίς όριο.
    valid = True
    for d in (date_from, date_to):
        if d:
            try:
                date.fromisoformat(d)
            except ValueError:
                valid = False
    if valid and date_from and date_to and date_from > date_to:
        valid = False
    if not valid:
        flash("Μη έγκυρες ημερομηνίες.", "error")
        date_from = date_to = ""

    # Απομνημόνευση των τελευταίων έγκυρων τιμών για την επόμενη επίσκεψη.
    session["reports_date_from"] = date_from
    session["reports_date_to"] = date_to

    cid = _active_company_id()
    submitted = bool(date_from or date_to)
    period = request.args.get("period", "")
    if period not in ("", "day", "month", "quarter"):
        period = ""
    requested_groups = set(request.args.getlist("group_by"))
    group_by = [group for group in _REPORT_GROUPS if group in requested_groups]
    dimensions = (["period"] if period else []) + group_by
    detailed = bool(dimensions)

    income = db.sum_totals(
        cid, "income", _INCOME_CLASSIFIED_STATUSES, date_from or None, date_to or None
    )
    expense = db.sum_totals(
        cid, "expense", _EXPENSE_CLASSIFIED_STATUSES, date_from or None, date_to or None
    )
    profit = {
        "net": round(income["net"] - expense["net"], 2),
        "vat": round(income["vat"] - expense["vat"], 2),
        "gross": round(income["gross"] - expense["gross"], 2),
    }
    breakdown = []
    period_breakdowns = []
    if detailed:
        income_documents = db.report_documents(
            cid,
            "income",
            _INCOME_CLASSIFIED_STATUSES,
            date_from or None,
            date_to or None,
        )
        expense_documents = db.report_documents(
            cid,
            "expense",
            _EXPENSE_CLASSIFIED_STATUSES,
            date_from or None,
            date_to or None,
        )
        income_groups = _report_group_totals(
            income_documents, period, group_by
        )
        expense_groups = _report_group_totals(
            expense_documents, period, group_by
        )
        empty = _empty_report_totals()

        def group_sort_key(values):
            def sortable(value):
                if isinstance(value, tuple):
                    # Στον αντισυμβαλλόμενο το πρώτο στοιχείο είναι η επωνυμία.
                    # Αν λείπει, χρησιμοποίησε το ΑΦΜ ως εφεδρικό κλειδί σειράς.
                    if len(value) == 2 and not value[0]:
                        return str(value[1]).casefold()
                    return " ".join(str(part) for part in value).casefold()
                return str(value).casefold()

            return tuple(sortable(value) for value in values)

        if period:
            income_periods = _report_group_totals(income_documents, period, [])
            expense_periods = _report_group_totals(expense_documents, period, [])
            period_rows: dict = {}
            if group_by:
                for key in sorted(
                    income_groups.keys() | expense_groups.keys(), key=group_sort_key
                ):
                    period_key, detail_key = key[0], key[1:]
                    period_rows.setdefault(period_key, []).append(
                        {
                            "labels": [
                                _report_group_label(dimension, value)
                                for dimension, value in zip(group_by, detail_key)
                            ],
                            "income": income_groups.get(key, empty),
                            "expense": expense_groups.get(key, empty),
                        }
                    )
            period_keys = income_periods.keys() | expense_periods.keys()
            for (period_key,) in sorted(period_keys, key=group_sort_key):
                period_income = income_periods.get((period_key,), empty)
                period_expense = expense_periods.get((period_key,), empty)
                period_breakdowns.append(
                    {
                        "label": _report_group_label("period", period_key),
                        "rows": period_rows.get(period_key, []),
                        "income": period_income,
                        "expense": period_expense,
                        "profit": {
                            "net": round(
                                period_income["net"] - period_expense["net"], 2
                            ),
                            "vat": round(
                                period_income["vat"] - period_expense["vat"], 2
                            ),
                            "gross": round(
                                period_income["gross"] - period_expense["gross"], 2
                            ),
                        },
                    }
                )
        else:
            for key in sorted(
                income_groups.keys() | expense_groups.keys(), key=group_sort_key
            ):
                breakdown.append(
                    {
                        "labels": [
                            _report_group_label(dimension, value)
                            for dimension, value in zip(dimensions, key)
                        ],
                        "income": income_groups.get(key, empty),
                        "expense": expense_groups.get(key, empty),
                    }
                )
    return render_template(
        "reports.html",
        date_from=date_from,
        date_to=date_to,
        submitted=submitted,
        period=period,
        group_by=group_by,
        dimensions=[_REPORT_GROUP_LABELS[name] for name in dimensions],
        detail_dimensions=[_REPORT_GROUP_LABELS[name] for name in group_by],
        detailed=detailed,
        breakdown=breakdown,
        period_breakdowns=period_breakdowns,
        income=income,
        expense=expense,
        profit=profit,
    )


@app.route("/new_expense")
def new_expense():
    from mydata_client import PAYMENT_METHODS, SELF_EXPENSE_TYPES, VAT_CATEGORIES

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

    # Επιτρεπόμενοι συνδυασμοί ΕΞΟΔΩΝ ανά self-expense τύπο (για αλυσιδωτό φιλτράρισμα).
    combos_all = {
        t: {k: v for k, v in db.combos_for_type(t).items() if k.startswith("category2")}
        for t in SELF_EXPENSE_TYPES
    }
    return render_template(
        "new_expense.html",
        self_types=SELF_EXPENSE_TYPES,
        categories=EXPENSE_CATEGORIES,
        types=EXPENSE_TYPES,
        vat_categories=VAT_CATEGORIES,
        payment_methods=PAYMENT_METHODS,
        today=datetime.now(ATHENS).strftime("%Y-%m-%d"),
        draft=draft,
        edit_mark=edit_mark if draft else "",
        combos_all=combos_all,
    )


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
    from mydata_client import SELF_EXPENSE_TYPES

    invoice_type = request.form.get("invoice_type", "")
    series = request.form.get("series", "0").strip()
    aa = request.form.get("aa", "1").strip()
    issue_date = request.form.get("issue_date", "")

    if invoice_type not in SELF_EXPENSE_TYPES:
        flash("Μη έγκυρος τύπος εγγραφής.", "error")
        return redirect(url_for("new_expense"))

    # γραμμές: παράλληλες λίστες από τη φόρμα
    amounts = request.form.getlist("line_amount")
    cats = request.form.getlist("line_category")
    typs = request.form.getlist("line_type")
    vat_cats = request.form.getlist("line_vat_category")
    vat_amts = request.form.getlist("line_vat_amount")
    lines = []
    for i, (amt, cat, typ) in enumerate(zip(amounts, cats, typs)):
        if not amt.strip():
            continue
        if not (cat and typ):
            flash("Κάθε γραμμή με ποσό πρέπει να έχει κατηγορία και τύπο.", "error")
            return redirect(url_for("new_expense"))
        try:
            lines.append(
                {
                    "amount": float(amt),
                    "classification_category": cat,
                    "classification_type": typ,
                    "vat_category": (vat_cats[i] if i < len(vat_cats) else "8") or "8",
                    "vat_amount": float(vat_amts[i])
                    if i < len(vat_amts) and vat_amts[i].strip()
                    else 0.0,
                }
            )
        except ValueError:
            flash(f"Μη έγκυρο ποσό στη γραμμή {i + 1}.", "error")
            return redirect(url_for("new_expense"))

    if not lines:
        flash("Συμπλήρωσε τουλάχιστον μία γραμμή.", "error")
        return redirect(url_for("new_expense"))

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
        return redirect(url_for("new_expense"))

    if invoice_type.startswith("17."):
        issuer_vat = own_vat
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
            return redirect(url_for("new_expense"))

    cid = _active_company_id()
    if not cid:
        flash("Δεν έχει οριστεί ενεργή εταιρεία.", "error")
        return redirect(url_for("companies"))

    # Αποθήκευση ΤΟΠΙΚΑ στα «Χαρακτηρισμένα» (χωρίς διαβίβαση). Η μαζική «Αποστολή
    # στο myDATA» θα εκτελέσει το SendInvoices (δημιουργία+διαβίβαση+χαρακτηρισμός).
    total_net = round(sum(line["amount"] for line in lines), 2)
    total_vat = round(sum(line["vat_amount"] for line in lines), 2)
    disp_lines = [
        {
            "line_number": i + 1,
            "net_value": line["amount"],
            "vat_amount": line["vat_amount"],
            "vat_category": line["vat_category"],
            "has_expenses_classification": True,
            "classifications": [],
        }
        for i, line in enumerate(lines)
    ]
    cls_info = [
        {
            "type": line["classification_type"],
            "category": line["classification_category"],
            "amount": line["amount"],
        }
        for line in lines
    ]
    draft = {
        "invoice_type": invoice_type,
        "series": series,
        "aa": aa,
        "issue_date": issue_date,
        "lines": lines,
        "issuer_vat": issuer_vat,
        "issuer_country": issuer_country,
        "payment_method": payment_method,
    }
    doc = {
        "issue_date": issue_date,
        "issuer_vat": issuer_vat,
        "issuer_name": None,
        "invoice_type": invoice_type,
        "series": series,
        "aa": aa,
        "total_net": total_net,
        "total_vat": total_vat,
        "total_gross": round(total_net + total_vat, 2),
        "lines": disp_lines,
    }
    # Επεξεργασία: αντικατάσταση του υπάρχοντος τοπικού draft.
    edit_mark = request.form.get("edit_mark", "").strip()
    if edit_mark:
        existing = db.get_document(cid, edit_mark)
        if existing and existing.get("local_action") == "create":
            db.delete_document(cid, edit_mark)
    db.create_local_expense(cid, doc, draft, cls_info)
    verb = "ενημερώθηκε" if edit_mark else "αποθηκεύτηκε"
    flash(
        f"✔ Η εγγραφή {invoice_type} ({SELF_EXPENSE_TYPES[invoice_type]}) {verb} "
        "τοπικά στα «Χαρακτηρισμένα». Μάζεψε κι άλλες και στείλ' τες μαζικά με "
        "«Αποστολή στο myDATA».",
        "ok",
    )
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
            "Για παραστατικά προμηθευτών χρησιμοποίησε την Απόρριψη.",
            "error",
        )
        return redirect(url_for("invoices"))
    db.stage_action(cid, mark, "cancel")
    flash(
        f"✔ Το παραστατικό {mark} μπήκε στα «Χαρακτηρισμένα» ως ΑΚΥΡΩΣΗ. "
        "Θα εκτελεστεί με το κουμπί «Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(url_for("invoices", view="classified"))


@app.route("/reject/<mark>", methods=["POST"])
def reject(mark):
    """Απόρριψη παραστατικού τρίτου (transactionMode=1): στήνεται ΤΟΠΙΚΑ στα
    «Χαρακτηρισμένα» και διαβιβάζεται με τη μαζική «Αποστολή στο myDATA»."""
    cid = _active_company_id()
    if not db.stage_action(cid, mark, "reject"):
        flash("Το παραστατικό δεν βρέθηκε. Κάνε νέα αναζήτηση.", "error")
        return redirect(url_for("invoices"))
    flash(
        f"✔ Το παραστατικό {mark} μπήκε στα «Χαρακτηρισμένα» ως ΑΠΟΡΡΙΨΗ. "
        "Στείλ' το με το κουμπί «Αποστολή στο myDATA».",
        "ok",
    )
    return redirect(url_for("invoices", view="classified"))


@app.route("/parameters")
def parameters():
    tab = request.args.get("tab", "companies")
    if tab not in {"suppliers", "companies", "accountant", "combinations"}:
        tab = "companies"
    return render_template(
        "parameters.html",
        active_tab=tab,
        suppliers=db.list_suppliers(),
        companies=load_companies(),
        active=get_active_index(),
        acc=get_accountant(),
        combinations_count=db.combos_count(),
        combinations_invoice_types=sorted(db.combos_invoice_types()),
    )


@app.route("/companies")
def companies():
    return redirect(url_for("parameters", tab="companies"))


@app.route("/companies/add", methods=["POST"])
def companies_add():
    name = request.form.get("company_name", "").strip()
    user_id = request.form.get("aade_user_id", "").strip()
    sub_key = request.form.get("aade_subscription_key", "").strip()
    vat = request.form.get("aade_vat_number", "").strip()
    env = request.form.get("mydata_env", "prod")
    use_acc = bool(request.form.get("use_accountant"))
    if not name or (not use_acc and not (user_id and sub_key)):
        flash(
            "Επωνυμία υποχρεωτική. User ID/Subscription Key απαιτούνται εκτός αν "
            "χρησιμοποιούνται credentials λογιστή.",
            "error",
        )
        return redirect(url_for("parameters", tab="companies"))
    new_id = db.add_company(
        {
            "company_name": name,
            "AADE_USER_ID": user_id,
            "AADE_SUBSCRIPTION_KEY": sub_key,
            "AADE_VAT_NUMBER": vat,
            "MYDATA_ENV": env,
            "use_accountant": use_acc,
        }
    )
    if db.get_active_company_id() is None and new_id is not None:
        db.set_active_company_id(new_id)
    flash(f"✔ Προστέθηκε η εταιρεία «{name}».", "ok")
    return redirect(url_for("parameters", tab="companies"))


@app.route("/companies/update/<int:idx>", methods=["POST"])
def companies_update(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("parameters", tab="companies"))
    name = request.form.get("company_name", "").strip()
    user_id = request.form.get("aade_user_id", "").strip()
    sub_key = request.form.get("aade_subscription_key", "").strip()
    if not name or (
        not bool(request.form.get("use_accountant")) and not (user_id and sub_key)
    ):
        flash(
            "Επωνυμία υποχρεωτική. User ID/Subscription Key απαιτούνται εκτός αν "
            "χρησιμοποιούνται credentials λογιστή.",
            "error",
        )
        return redirect(url_for("parameters", tab="companies"))
    db.update_company(
        comps[idx]["id"],
        {
            "company_name": name,
            "AADE_USER_ID": user_id,
            "AADE_SUBSCRIPTION_KEY": sub_key,
            "AADE_VAT_NUMBER": request.form.get("aade_vat_number", "").strip(),
            "MYDATA_ENV": request.form.get("mydata_env", "prod"),
            "use_accountant": bool(request.form.get("use_accountant")),
        },
    )
    flash(f"✔ Ενημερώθηκε η εταιρεία «{name}».", "ok")
    return redirect(url_for("parameters", tab="companies"))


@app.route("/companies/delete/<int:idx>", methods=["POST"])
def companies_delete(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("parameters", tab="companies"))
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
    return redirect(url_for("parameters", tab="companies"))


@app.route("/companies/select/<int:idx>", methods=["POST"])
def companies_select(idx):
    comps = load_companies()
    if not (0 <= idx < len(comps)):
        flash("Η εταιρεία δεν βρέθηκε.", "error")
        return redirect(url_for("parameters", tab="companies"))
    db.set_active_company_id(comps[idx]["id"])
    flash(f"✔ Ενεργή εταιρεία: «{comps[idx].get('company_name', '')}».", "ok")
    return redirect(url_for("parameters", tab="companies"))


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
                "subscription_key": request.form.get("subscription_key", "").strip(),
                "env": request.form.get("env", "prod"),
            }
        )
        flash("✔ Αποθηκεύτηκαν τα credentials λογιστή.", "ok")
        return redirect(url_for("parameters", tab="accountant"))
    return redirect(url_for("parameters", tab="accountant"))


# ------------------------------------------------------------------ #
# Προμηθευτές (κοινοί για όλες τις εταιρείες)
# ------------------------------------------------------------------ #
@app.route("/suppliers")
def suppliers():
    return redirect(url_for("parameters", tab="suppliers"))


@app.route("/suppliers/save", methods=["POST"])
def suppliers_save():
    vat = request.form.get("vat", "").strip()
    name = request.form.get("name", "").strip()
    if not vat:
        flash("Το ΑΦΜ είναι υποχρεωτικό.", "error")
        return redirect(url_for("parameters", tab="suppliers"))
    db.upsert_supplier(vat, name)
    flash(f"✔ Αποθηκεύτηκε ο προμηθευτής {vat}.", "ok")
    return redirect(url_for("parameters", tab="suppliers"))


@app.route("/suppliers/delete/<vat>", methods=["POST"])
def suppliers_delete(vat):
    db.delete_supplier(vat)
    flash(f"✔ Διαγράφηκε ο προμηθευτής {vat}.", "ok")
    return redirect(url_for("parameters", tab="suppliers"))


@app.route("/suppliers/import", methods=["POST"])
def suppliers_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash(
            "Επίλεξε αρχείο .txt (μία γραμμή ανά προμηθευτή: ΑΦΜ<κενό>Επωνυμία).",
            "error",
        )
        return redirect(url_for("parameters", tab="suppliers"))
    data = f.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    n = db.import_suppliers_txt(text)
    flash(f"✔ Εισήχθησαν/ενημερώθηκαν {n} προμηθευτές.", "ok")
    return redirect(url_for("parameters", tab="suppliers"))


@app.route("/suppliers/export")
def suppliers_export():
    return Response(
        db.export_suppliers_txt(),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=suppliers.txt"},
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
        return redirect(url_for("parameters", tab="companies"))
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        flash(f"Μη έγκυρο companies.json: {e}", "error")
        return redirect(url_for("parameters", tab="companies"))

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
    return redirect(url_for("parameters", tab="companies"))


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


@app.context_processor
def inject_company():
    return {"active_company": get_active_company()}


if __name__ == "__main__":
    app.run(debug=True)
