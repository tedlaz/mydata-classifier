"""Δήλωση ΦΠΑ (έντυπο Φ2) από τα παραστατικά του myDATA για ένα διάστημα (μήνας/τρίμηνο).

Εκροές: γραμμές παραστατικών ΕΣΟΔΩΝ ανά κατηγορία ΦΠΑ (βάση → κωδ. 301–309, φόρος = βάση ×
συντελεστής → 331–339). Εισροές: χαρακτηρισμοί ΦΠΑ των ΕΞΟΔΩΝ (VAT_361…VAT_366 → 361–366,
φόρος → 381–386). Πιστωτικά αφαιρούνται. Η διαφορά του φόρου κατά συντελεστή από τον ΦΠΑ που
χρεώθηκε πράγματι στα τιμολόγια (στρογγυλοποιήσεις ανά γραμμή) μπαίνει στο 422 (ή στο 402),
όπως κάνει ο λογιστής, ώστε το προς καταβολή να ισούται με τον ΦΠΑ των τιμολογίων.
Ό,τι δεν έχει ΦΠΑ (0%, χωρίς ΦΠΑ, έξοδα χωρίς χαρακτηρισμό ΦΠΑ) μένει εκτός.
"""

import json
import xml.etree.ElementTree as ET

from classifications import CREDIT_INVOICE_TYPES, EU_COUNTRIES

# Κατηγορία ΦΠΑ myDATA → (κωδ. βάσης, κωδ. φόρου, συντελεστής).
# ponytail: οι νησιωτικοί 4% (κατηγ. 6 και 10) πάνε και οι δύο στο 305· το 308 (4% ηπειρωτική)
# μένει κενό — χωρίστε τα αν εμφανιστεί πράξη που ανήκει εκεί.
OUTPUT_RATES = {
    "2": ("301", "331", 0.13),
    "3": ("302", "332", 0.06),
    "1": ("303", "333", 0.24),
    "5": ("304", "334", 0.09),
    "6": ("305", "335", 0.04),
    "10": ("305", "335", 0.04),
    "4": ("306", "336", 0.17),
    "9": ("309", "339", 0.03),
}

# Εκροές χωρίς ΦΠΑ που δηλώνονται στο Φ2: κατηγορία εξαίρεσης ΦΠΑ της γραμμής → κωδικός
# (άρθρα ΚΦΠΑ ν. 5144/2024, σε παρένθεση ο ν. 2859/2000· κωδικοί κατά τις οδηγίες του εντύπου 050).
# 349 = λοιπές εκροές χωρίς ΦΠΑ με δικαίωμα έκπτωσης, 310 = απαλλασσόμενες χωρίς δικαίωμα (ΠΟΛ.1082/2015).
# ponytail: μόνο οι σαφείς αντιστοιχίσεις· οι υπόλοιπες κατηγορίες μένουν εκτός Φ2 — εκτός πεδίου ή
# ειδικά καθεστώτα (1, 2, 5, 6, 15, 17, 18), καθεστώτα περιθωρίου / χρυσός / ακίνητα (19, 20, 22–24) και
# OSS/IOSS (29–31) ώσπου να επιβεβαιωθούν από λογιστή — προστίθενται εδώ.
EXEMPT_CODES = {
    "14": "342",                                # άρθρο 33 (28): ενδοκοινοτικές παραδόσεις αγαθών
    "4": "345",                                 # άρθρο 18 (14): υπηρεσίες με τόπο άλλο κράτος-μέλος (14.2.α)· τρίτη χώρα → 349 (_exempt_code)
    "8": "348", "28": "348",                    # άρθρο 29 (24): εξαγωγές, και Tax Free
    "11": "348", "12": "348", "13": "348",      # άρθρο 32 (27): πλοία / αεροσκάφη
    "3": "349",                                 # άρθρο 13: τόπος παράδοσης αγαθών εκτός Ελλάδας
    "9": "349", "10": "349", "25": "349",       # άρθρα 30, 31 (25, 26), ΠΟΛ.1029/1995: duty free, αποθήκες
    "16": "349",                                # άρθρο 45 (39α): αντίστροφη επιβάρυνση εσωτερικού
    "26": "349",                                # ΠΟΛ.1167/2015: ειδική βεβαίωση απαλλαγής εξαγωγέων
    "27": "349",                                # λοιπές εξαιρέσεις ΦΠΑ
    "7": "310",                                 # άρθρο 27 (22): απαλλαγές χωρίς δικαίωμα έκπτωσης
    "21": "310",                                # άρθρο 51 (44): βιομηχανοποιημένα καπνά
}


def _parse_exempt(d: dict) -> tuple:
    """(xml, {γραμμή: κατηγορία εξαίρεσης ΦΠΑ}, χώρα αντισυμβαλλόμενου) από το XML του παραστατικού όπως
    το έδωσε το myDATA (raw_xml). Το XML διαβάζεται μία φορά ανά παραστατικό."""
    xml = d.get("raw_xml") or ""
    if d.get("_exempt", (None,))[0] is not xml:  # μνήμη ανά παραστατικό· ξανά αν άλλαξε το XML
        found, country = {}, ""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            root = None
        for det in root.iter() if root is not None else ():
            tag = det.tag.split("}")[-1]
            if tag in ("invoiceDetails", "counterpart"):
                kids = {c.tag.split("}")[-1]: (c.text or "").strip() for c in det}
                if tag == "counterpart":
                    country = kids.get("country", "")
                elif kids.get("vatExemptionCategory") and kids.get("lineNumber", "").isdigit():
                    found[int(kids["lineNumber"])] = kids["vatExemptionCategory"]
        d["_exempt"] = (xml, found, country)
    return d["_exempt"]


def _exemption(d: dict, ln: dict) -> str:
    """Κατηγορία εξαίρεσης ΦΠΑ της γραμμής· "" αν δεν υπάρχει."""
    return _parse_exempt(d)[1].get(ln.get("line_number"), "")


def _exempt_code(d: dict, ln: dict) -> str:
    """Κωδικός Φ2 της γραμμής χωρίς ΦΠΑ (EXEMPT_CODES)· "" = εκτός Φ2. Υπηρεσίες εκτός Ελλάδας (4) σε
    αντισυμβαλλόμενο τρίτης χώρας → 349 (το 345 είναι μόνο για άλλο κράτος-μέλος)."""
    cat, country = _exemption(d, ln), _parse_exempt(d)[2]
    if cat == "4" and country and country != "GR" and country not in EU_COUNTRIES:
        return "349"
    return EXEMPT_CODES.get(cat, "")


# Σύνολο εκροών (311): φορολογητέες (307) + εκροές χωρίς ΦΠΑ που δηλώνονται.
_UNTAXED_OUTPUTS = ("342", "345", "348", "349", "310")

# Χαρακτηρισμός ΦΠΑ εξόδου → (κωδ. βάσης, κωδ. φόρου).
INPUT_CODES = {f"VAT_36{i}": (f"36{i}", f"38{i}") for i in range(1, 7)}
# Πράξεις λήπτη (αντίστροφη επιβάρυνση): ο φόρος δηλώνεται ΚΑΙ στις εκροές ΚΑΙ στις εισροές, με τον
# συντελεστή της κατηγορίας ΦΠΑ κάθε γραμμής· 24% (303/333) όταν οι γραμμές δεν έχουν ΦΠΑ/κατηγορία.
REVERSE_CHARGE = {"VAT_364", "VAT_365", "VAT_366"}
REVERSE_RATE = 0.24
# Χρεωστικό υπόλοιπο έως 30 € δεν αποδίδεται: μεταφέρεται στον κωδ. 483 της επόμενης περιόδου.
MIN_PAYMENT = 30.0


def _sent(d: dict) -> bool:
    """Διαβιβασμένο στο myDATA (όχι τοπικό draft «Νέας εγγραφής»)."""
    return (d.get("mark") or "").isdigit()


def cls_lines(entries: list[dict], lines: dict) -> list[list | None]:
    """Οι πραγματικές γραμμές που καλύπτει κάθε χαρακτηρισμός (entries: ίδιας οικογένειας, Ε3 ή ΦΠΑ·
    lines: αριθμός γραμμής → γραμμή). Το «line» του myDATA δεν είναι πάντα η γραμμή: στον χαρακτηρισμό
    ανά παραστατικό είναι αύξων ομάδας ανά κατηγορία ΦΠΑ (π.χ. line 1 = όλες οι γραμμές 6%, line 2 =
    όλες οι 0%). Κανόνας: οι χαρακτηρισμοί της γραμμής αθροίζουν στην καθαρή της → η γραμμή· αλλιώς ποσό
    = καθαρή μιας κατηγορίας ΦΠΑ → οι γραμμές της· αλλιώς None (όλο το παραστατικό, αναλογικά)."""
    near = lambda a, b: abs(a - b) <= 0.02  # noqa: E731 — στρογγυλοποιήσεις του myDATA
    net = lambda ns: sum(lines[n].get("net_value") or 0.0 for n in ns)  # noqa: E731
    on_line: dict = {}
    for e in entries:
        on_line[e.get("line")] = on_line.get(e.get("line"), 0.0) + (e.get("amount") or 0.0)
    groups: dict = {}
    for n, ln in lines.items():
        groups.setdefault(str(ln.get("vat_category") or ""), []).append(n)
    out = []
    for e in entries:
        n, amount = e.get("line"), e.get("amount") or 0.0
        if n in lines and near(on_line[n], lines[n].get("net_value") or 0.0):
            out.append([n])
        else:
            out.append(next((ns for ns in groups.values() if amount and near(net(ns), amount)), None))
    return out


def _input_vat(d: dict) -> list[tuple[str, float, float, list | None]]:
    """(χαρακτηρισμός VAT_36x, βάση, φόρος, γραμμές ή None = όλο το παραστατικό) — βλ. input_vat."""
    sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
    vat_cls = [e for e in json.loads(d["cls_json"] or "[]") if (e.get("type") or "") in INPUT_CODES]
    lines = {ln.get("line_number"): ln for ln in json.loads(d["lines_json"] or "[]")}
    total = sum(e.get("amount") or 0.0 for e in vat_cls) or 1.0
    out = []
    for e, ns in zip(vat_cls, cls_lines(vat_cls, lines)):
        amount = e.get("amount") or 0.0
        net = sum(lines[n].get("net_value") or 0.0 for n in ns or ())
        rc_rate = e["type"] in REVERSE_CHARGE and not (net and any(lines[n].get("vat_amount") for n in ns))
        if rc_rate:  # χωρίς ΦΠΑ στις γραμμές (π.χ. παραστατικό του εκδότη) → 24%
            tax = round(sign * amount * REVERSE_RATE, 2)
        elif net:  # μερίδιο του χαρακτηρισμού στον ΦΠΑ των γραμμών του
            tax = sign * sum(lines[n].get("vat_amount") or 0.0 for n in ns) * amount / net
        else:  # όλο το παραστατικό → αναλογικά στον ΦΠΑ του
            tax = sign * (d["total_vat"] or 0.0) * amount / total
        out.append((e["type"], sign * amount, tax, ns))
    return out


def input_vat(d: dict) -> list[tuple[str, float, float]]:
    """(χαρακτηρισμός VAT_36x, βάση, φόρος) ενός εξόδου, με πρόσημο (πιστωτικά αρνητικά). Φόρος = ο ΦΠΑ
    των γραμμών που καλύπτει ο χαρακτηρισμός (cls_lines) κατά το μερίδιό του, ή αναλογικά του
    παραστατικού· στις πράξεις λήπτη βάση × 24%. Χωρίς χαρακτηρισμό ΦΠΑ → [] (ο ΦΠΑ του δεν εκπίπτει)."""
    return [(t, b, x) for t, b, x, _ in _input_vat(d)]


def deductible_by_line(d: dict) -> dict:
    """Εκπιπτόμενος ΦΠΑ ανά γραμμή (με πρόσημο, χωρίς πράξεις λήπτη) — για τον κύβο OLAP. Ο φόρος
    ενός χαρακτηρισμού μοιράζεται στις γραμμές του κατά τον ΦΠΑ τους (None = σε όλες).
    Παραστατικό χωρίς γραμμές → {None: φόρος}."""
    lines = {ln.get("line_number"): ln for ln in json.loads(d["lines_json"] or "[]")}
    out: dict = {}
    for typ, _, tax, ns in _input_vat(d):
        if typ in REVERSE_CHARGE:
            continue
        if not lines:
            out[None] = out.get(None, 0.0) + tax
            continue
        ns = ns or list(lines)
        vats = [abs(lines[n].get("vat_amount") or 0.0) for n in ns]
        for n, v in zip(ns, vats):
            out[n] = out.get(n, 0.0) + (tax * v / sum(vats) if sum(vats) else tax / len(ns))
    return out


def compute(income: list[dict], expense: list[dict], prev_credit: float = 0.0, prev_debit: float = 0.0) -> dict:
    """Κωδικοί Φ2 → ποσά. income/expense: γραμμές του db.period_documents. prev_credit = 401
    (πιστωτικό προηγ. περιόδου), prev_debit = 483 (χρεωστικό έως 30 € προηγ. περιόδου).
    codes["carry"]: χρεωστικό έως 30 € αυτής της περιόδου → 483 της επόμενης."""
    c: dict[str, float] = {}
    add = lambda k, v: c.__setitem__(k, c.get(k, 0.0) + v)  # noqa: E731
    invoiced_vat = 0.0  # ΦΠΑ εκροών όπως χρεώθηκε στα τιμολόγια

    for d in filter(_sent, income):
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        for ln in json.loads(d["lines_json"] or "[]"):
            cat = str(ln.get("vat_category") or "")
            if cat in OUTPUT_RATES:
                add(OUTPUT_RATES[cat][0], sign * (ln.get("net_value") or 0.0))
                invoiced_vat += sign * (ln.get("vat_amount") or 0.0)
            elif _exempt_code(d, ln):  # π.χ. 0% με εξαίρεση 16 → 349
                add(_exempt_code(d, ln), sign * (ln.get("net_value") or 0.0))
            # λοιπά 0% (7) και «χωρίς ΦΠΑ» (8) μένουν εκτός

    for d in filter(_sent, expense):
        if d.get("local_action") in ("reject", "cancel"):
            continue
        lines = {ln.get("line_number"): ln for ln in json.loads(d["lines_json"] or "[]")}
        for typ, base, tax, ns in _input_vat(d):  # χωρίς χαρακτηρισμό ΦΠΑ → εκτός δήλωσης
            base_code, tax_code = INPUT_CODES[typ]
            if typ in REVERSE_CHARGE:
                # Εκροή στη γραμμή του συντελεστή κάθε γραμμής (ο φόρος = βάση × συντελεστής).
                net = sum(lines[n].get("net_value") or 0.0 for n in ns or ())
                for n in (ns if net else [None]):
                    cat = str(lines[n].get("vat_category") or "") if n is not None else ""
                    share = base * (lines[n].get("net_value") or 0.0) / net if n is not None else base
                    add(OUTPUT_RATES.get(cat, OUTPUT_RATES["1"])[0], share)
                add("313", base)  # πλασματική εκροή: δεν συμμετέχει στον κύκλο εργασιών ΦΠΑ
            if not tax:  # χαρακτηρισμός ΦΠΑ χωρίς φόρο → εκτός δήλωσης
                continue
            add(base_code, base)
            add(tax_code, tax)

    c = {k: round(v, 2) for k, v in c.items()}
    for cat, (base_code, tax_code, rate) in OUTPUT_RATES.items():
        if base_code in c:
            c[tax_code] = round(c[base_code] * rate, 2)
    reverse_vat = round(sum(c.get(f"38{i}", 0.0) for i in (4, 5, 6)), 2)
    invoiced_vat = round(invoiced_vat + reverse_vat, 2)

    c["307"] = round(sum(c.get(k, 0.0) for k in ("301", "302", "303", "304", "305", "306", "308", "309")), 2)
    c["337"] = round(sum(c.get(k, 0.0) for k in ("331", "332", "333", "334", "335", "336", "338", "339")), 2)
    # Φ2 (Α.1058/2024): 312 = 311 − 313 − 314 − 315. Τα 314/315 αφορούν μόνο όσους δεν έχουν
    # δικαίωμα έκπτωσης για τις αποκτήσεις — εδώ πάντα μηδέν.
    c["311"] = round(c["307"] + sum(c.get(k, 0.0) for k in _UNTAXED_OUTPUTS), 2)
    c["313"] = c.get("313", 0.0)
    c["312"] = round(c["311"] - c["313"], 2)
    c["367"] = round(sum(c.get(f"36{i}", 0.0) for i in range(1, 7)), 2)
    c["387"] = round(sum(c.get(f"38{i}", 0.0) for i in range(1, 7)), 2)

    # Στρογγυλοποίηση: φόρος κατά συντελεστή ≠ ΦΠΑ τιμολογίων → 422 (αν χρεώθηκαν περισσότερα) ή 402.
    diff = round(invoiced_vat - c["337"], 2)
    c["422"] = diff if diff > 0 else 0.0
    c["402"] = -diff if diff < 0 else 0.0
    c["410"] = c["402"]
    c["428"] = c["422"]
    c["430"] = round(c["387"] + c["410"] - c["428"], 2)

    balance = round(c["337"] - c["430"], 2)
    c["480"] = balance if balance > 0 else 0.0
    c["470"] = -balance if balance < 0 else 0.0
    c["401"] = round(prev_credit or 0.0, 2)
    c["483"] = round(prev_debit or 0.0, 2)
    final = round(c["480"] - c["470"] - c["401"] + c["483"], 2)
    c["carry"] = final if 0 < final <= MIN_PAYMENT else 0.0  # δεν αποδίδεται — πάει στο 483 της επόμενης
    c["511"] = final if final > MIN_PAYMENT else 0.0         # ποσό προς καταβολή
    c["502"] = -final if final < 0 else 0.0                  # πιστωτικό προς έκπτωση (μεταφέρεται)
    return {"codes": c, "invoiced_vat": invoiced_vat}


OUT, ND = "out", "nd"  # στήλες «Εκτός Φ2» και «ΦΠΑ μη εκπιπτόμενος» του πίνακα συμφωνίας


def _e3(d: dict) -> list[dict]:
    """Χαρακτηρισμοί Ε3 (όχι ΦΠΑ, όχι σημάδια απόρριψης/απόκλισης χωρίς τύπο)."""
    return [e for e in json.loads(d["cls_json"] or "[]") if e.get("type") and not e["type"].startswith("VAT_")]


def _matrix(rows: dict, cols_order: list[str], total: float) -> dict:
    """{e3: {στήλη: ποσό}} → γραμμές/στήλες για προβολή, με σύνολα και έλεγχο συμφωνίας."""
    used = [c for c in cols_order if any(abs(r.get(c, 0.0)) >= 0.005 for r in rows.values())]
    bases = {b for b, _, _ in OUTPUT_RATES.values()} | {b for b, _ in INPUT_CODES.values()} | set(EXEMPT_CODES.values()) | {OUT}
    base_cols = [c for c in used if c in bases]  # βάσεις (30x/36x) + εκτός = καθαρή αξία· όχι οι φόροι
    out_rows = []
    for e3 in sorted(rows, key=lambda k: (k == "", k)):
        vals = {c: round(rows[e3].get(c, 0.0), 2) for c in used}
        out_rows.append({"e3": e3, "vals": vals, "net": round(sum(vals[c] for c in base_cols), 2)})
    totals = {c: round(sum(r["vals"][c] for r in out_rows), 2) for c in used}
    net = round(sum(r["net"] for r in out_rows), 2)
    return {"cols": used, "rows": out_rows, "totals": totals, "net": net, "total": round(total, 2),
            "ok": abs(net - total) < 0.015}


def reconcile(income: list[dict], expense: list[dict]) -> dict:
    """Συμφωνία Φ2 ως πίνακας: γραμμές οι λογαριασμοί Ε3, στήλες οι κωδικοί του Φ2 (βάση/φόρος) που
    γεμίζουν, «Εκτός Φ2» και (έξοδα) «ΦΠΑ μη εκπιπτόμενος». Ίδιοι κανόνες με το compute· ανά γραμμή
    Ε3: βάσεις + εκτός = καθαρή αξία, και το σύνολο = καθαρή αξία της περιόδου. Ε3 «» = αχαρακτήριστα."""
    def add(rows, e3, col, v):
        rows.setdefault(e3, {})
        rows[e3][col] = rows[e3].get(col, 0.0) + v

    # ---- Έσοδα: κάθε χαρακτηρισμός Ε3 → οι γραμμές του → κατηγορία ΦΠΑ → 30x/33x ή εκτός ----
    rows, total = {}, 0.0
    for d in income:
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        net_doc = sign * (d["total_net"] or 0.0)
        total += net_doc
        lines = {ln.get("line_number"): ln for ln in json.loads(d["lines_json"] or "[]")}
        e3 = _e3(d)
        if not lines:
            add(rows, e3[0]["type"] if e3 else "", OUT, net_doc)
            continue
        # Κάθε γραμμή μοιράζεται στους Ε3 που την καλύπτουν (χωρίς Ε3 → «»).
        share = {n: [] for n in lines}
        for e, ns in zip(e3, cls_lines(e3, lines)):
            for n in ns or lines:
                share[n].append(e)
        for n, ln in lines.items():
            cat, net = str(ln.get("vat_category") or ""), sign * (ln.get("net_value") or 0.0)
            owners = share[n] or [{"type": "", "amount": 1.0}]
            weight = sum(abs(o.get("amount") or 0.0) for o in owners) or 1.0
            for o in owners:
                k = abs(o.get("amount") or 0.0) / weight
                exempt = _exempt_code(d, ln)
                if _sent(d) and cat not in OUTPUT_RATES and exempt:
                    add(rows, o.get("type") or "", exempt, net * k)
                elif _sent(d) and cat in OUTPUT_RATES:
                    add(rows, o.get("type") or "", OUTPUT_RATES[cat][0], net * k)
                    # Φόρος όπως στο Φ2: βάση × συντελεστής (η στρογγυλοποίηση των τιμολογίων πάει στο 422/402).
                    add(rows, o.get("type") or "", OUTPUT_RATES[cat][1], net * k * OUTPUT_RATES[cat][2])
                else:
                    add(rows, o.get("type") or "", OUT, net * k)
        # Γραμμές που δεν αθροίζουν στη σύνοψη (στρογγυλοποιήσεις): στο εκτός, για να συμφωνεί.
        diff = net_doc - sign * sum(ln.get("net_value") or 0.0 for ln in lines.values())
        if abs(diff) >= 0.005:
            add(rows, e3[0]["type"] if e3 else "", OUT, diff)
    inc_cols = [c for cat in sorted(OUTPUT_RATES, key=lambda c: OUTPUT_RATES[c][0]) for c in OUTPUT_RATES[cat][:2]]
    inc_cols = list(dict.fromkeys(inc_cols)) + sorted(set(EXEMPT_CODES.values())) + [OUT]
    out = {"income": _matrix(rows, inc_cols, total)}

    # ---- Έξοδα: χαρακτηρισμοί ΦΠΑ → 36x/38x, στον Ε3 της ίδιας γραμμής/ποσού· υπόλοιπο → εκτός ----
    rows, total = {}, 0.0
    for d in expense:
        sign = -1 if d["invoice_type"] in CREDIT_INVOICE_TYPES else 1
        net_doc = sign * (d["total_net"] or 0.0)
        total += net_doc
        e3 = _e3(d)
        owners = e3 or [{"type": "", "amount": 1.0}]
        # Καθαρή αξία ανά Ε3: κατά το ποσό του (κλιμάκωση όταν τα Ε3 αθροίζουν σε μικτή, π.χ. 2.5).
        weight = sum(sign * (o.get("amount") or 0.0) for o in owners)
        nets = ([net_doc * sign * (o.get("amount") or 0.0) / weight for o in owners] if weight
                else [net_doc / len(owners)] * len(owners))  # Ε3 με μηδενικά ποσά: ισόποσα
        left = list(nets)
        taxed = 0.0
        if _sent(d) and d.get("local_action") not in ("reject", "cancel"):
            vat_entries = [e for e in json.loads(d["cls_json"] or "[]") if (e.get("type") or "").startswith("VAT_")]
            free = list(range(len(owners)))
            for (typ, base, tax, _), ve in zip(_input_vat(d), vat_entries):
                if not tax:  # χωρίς φόρο → εκτός δήλωσης (όπως στο compute)
                    continue
                # Ίδια γραμμή και ποσό → αυτός ο Ε3· αλλιώς αναλογικά στους Ε3 που απομένουν.
                pair = next((i for i in free if owners[i].get("line") == ve.get("line")
                             and abs((owners[i].get("amount") or 0.0) - (ve.get("amount") or 0.0)) < 0.01), None)
                targets = [pair] if pair is not None else (free or list(range(len(owners))))
                if pair is not None:
                    free.remove(pair)
                w = sum(abs(left[i]) for i in targets) or len(targets)
                for i in targets:
                    k = (abs(left[i]) / w) if sum(abs(left[j]) for j in targets) else 1 / len(targets)
                    add(rows, owners[i].get("type") or "", INPUT_CODES[typ][0], base * k)
                    add(rows, owners[i].get("type") or "", INPUT_CODES[typ][1], tax * k)
                    left[i] -= base * k
                taxed += tax
            nd = sign * (d["total_vat"] or 0.0) - taxed  # ΦΠΑ του παραστατικού που δεν εκπίπτει
        else:
            nd = 0.0  # μη διαβιβασμένα / απορριφθέντα: εκτός Φ2 συνολικά
        for i, o in enumerate(owners):
            add(rows, o.get("type") or "", OUT, left[i])
        if abs(nd) >= 0.005:
            w = sum(abs(x) for x in left) or sum(abs(x) for x in nets) or 1.0
            src = left if sum(abs(x) for x in left) else nets
            for i, o in enumerate(owners):
                add(rows, o.get("type") or "", ND, nd * abs(src[i]) / w)
    exp_cols = [c for t in sorted(INPUT_CODES) for c in INPUT_CODES[t]] + [OUT, ND]
    out["expense"] = _matrix(rows, exp_cols, total)
    return out


if __name__ == "__main__":
    # Αύγουστος 2026 (KOSTAS): δύο τιμολόγια 24% — ίδια ποσά με το docs/2026-08.FPA.pdf.
    inc = [{"mark": "1", "invoice_type": "2.1", "lines_json": json.dumps([
               {"net_value": 1500.00, "vat_amount": 360.00, "vat_category": "1"}])},
           {"mark": "2", "invoice_type": "2.1", "lines_json": json.dumps([
               {"net_value": 637.09, "vat_amount": 152.91, "vat_category": "1"}])}]
    exp = [{"mark": "3", "invoice_type": "14.5", "total_vat": 0, "cls_json": "[]", "lines_json": "[]"}]
    r = compute(inc, exp)["codes"]
    assert (r["303"], r["333"], r["307"], r["337"], r["311"], r["312"]) == (2137.09, 512.90, 2137.09, 512.90, 2137.09, 2137.09)
    assert (r["422"], r["428"], r["430"], r["480"], r["511"]) == (0.01, 0.01, -0.01, 512.91, 512.91), r
    # Εισροές: 361 ανά γραμμή, 362 ανά παραστατικό (αναλογικά), πιστωτικό αφαιρείται, ενδοκ. απόκτηση.
    exp = [{"mark": "4", "invoice_type": "1.1", "total_vat": 48.0, "lines_json": json.dumps(
                [{"line_number": 1, "vat_amount": 24.0}, {"line_number": 2, "vat_amount": 24.0}]),
            "cls_json": json.dumps([{"type": "VAT_361", "amount": 100, "line": 1}, {"type": "VAT_362", "amount": 100, "line": None}])},
           {"mark": "5", "invoice_type": "5.1", "total_vat": 12.0, "lines_json": "[]",
            "cls_json": json.dumps([{"type": "VAT_361", "amount": 50}])},
           {"mark": "6", "invoice_type": "14.1", "total_vat": 0, "lines_json": "[]",
            "cls_json": json.dumps([{"type": "VAT_364", "amount": 1000}])},
           {"mark": "NEW-x", "invoice_type": "1.1", "total_vat": 99, "lines_json": "[]", "cls_json": "[]"},
           {"mark": "7", "invoice_type": "1.1", "total_vat": 24, "lines_json": "[]", "cls_json": "[]"},  # χωρίς χαρ. ΦΠΑ
           {"mark": "8", "invoice_type": "1.1", "total_vat": 0, "lines_json": "[]",
            "cls_json": json.dumps([{"type": "VAT_361", "amount": 500}])}]  # χωρίς ΦΠΑ
    assert compute([{"mark": "9", "invoice_type": "2.1", "lines_json": json.dumps(
        [{"net_value": 300, "vat_amount": 0, "vat_category": "7"}])}], [])["codes"]["311"] == 0  # 0% εκτός
    r = compute([], exp, prev_credit=10)["codes"]
    assert (r["361"], r["381"], r["362"], r["382"], r["364"], r["384"]) == (50, 12, 100, 24, 1000, 240), r
    assert (r["303"], r["333"], r["387"], r["430"]) == (1000, 240, 276, 276), r
    assert (r["311"], r["313"], r["312"]) == (1000, 1000, 0), r  # αυτοπαράδοση: εκτός κύκλου εργασιών
    # Ενδοκοινοτική με 13% και 24%: κάθε γραμμή στον συντελεστή της, φόρος εισροών = ΦΠΑ γραμμών.
    rc = {"mark": "400001972149566", "invoice_type": "14.1", "total_vat": 3.53, "lines_json": json.dumps([
        {"line_number": 1, "net_value": 5.0, "vat_amount": 0.65, "vat_category": "2"},
        {"line_number": 2, "net_value": 12.0, "vat_amount": 2.88, "vat_category": "1"}]),
        "cls_json": json.dumps([{"type": "VAT_364", "amount": 5.0}, {"type": "VAT_364", "amount": 12.0}])}
    r2 = compute([], [rc])["codes"]
    assert (r2["301"], r2["331"], r2["303"], r2["333"], r2["337"]) == (5, 0.65, 12, 2.88, 3.53), r2
    assert (r2["364"], r2["384"], r2["313"], r2["312"], r2["511"], r2["502"]) == (17, 3.53, 17, 0, 0, 0), r2
    # Συμφωνία (πίνακας Ε3 × κωδικοί Φ2): γραμμές = καθαρή αξία, σύνολο = περίοδος, ποσά = compute.
    buy = {"mark": "4002", "invoice_type": "1.1", "total_net": 80.0, "total_vat": 19.2, "lines_json": json.dumps(
        [{"line_number": 1, "net_value": 50.0, "vat_amount": 12.0, "vat_category": "1"},
         {"line_number": 2, "net_value": 30.0, "vat_amount": 7.2, "vat_category": "1"}]),
        "cls_json": json.dumps([{"type": "E3_102_001", "amount": 50.0, "line": 1}, {"type": "VAT_361", "amount": 50.0, "line": 1},
                                {"type": "E3_585_016", "amount": 30.0, "line": 2}])}
    draft = dict(buy, mark="NEW-1", total_net=8.0, total_vat=0.0)
    sale = {"mark": "4003", "invoice_type": "2.1", "total_net": 150.0, "lines_json": json.dumps([
        {"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"},
        {"line_number": 2, "net_value": 50.0, "vat_amount": 0.0, "vat_category": "7"}]),
        "cls_json": json.dumps([{"type": "E3_561_001", "amount": 100.0, "line": 1}, {"type": "E3_561_003", "amount": 50.0, "line": 2}])}
    # 0% με εξαίρεση 16 → 349, μέσα στο σύνολο εκροών (311) και στον κύκλο εργασιών (312).
    s39 = {"mark": "4004", "invoice_type": "2.1", "total_net": 40.0, "lines_json": json.dumps(
        [{"line_number": 1, "net_value": 40.0, "vat_amount": 0.0, "vat_category": "7"}]),
        "cls_json": json.dumps([{"type": "E3_561_001", "amount": 40.0, "line": 1}]),
        "raw_xml": '<invoice xmlns="http://www.aade.gr/myDATA/invoice/v1.0"><invoiceDetails><lineNumber>1</lineNumber>'
                   '<netValue>40.00</netValue><vatCategory>7</vatCategory><vatExemptionCategory>16</vatExemptionCategory>'
                   '</invoiceDetails></invoice>'}
    r3 = compute([s39], [])["codes"]
    assert (r3["349"], r3["311"], r3["312"], r3["337"]) == (40, 40, 40, 0), r3
    m3 = reconcile([s39], [])["income"]
    assert m3["ok"] and m3["totals"] == {"349": 40}, m3
    # Ενδοκοινοτική παράδοση (14) → 342, απαλλασσόμενη χωρίς έκπτωση (7) → 310, εκτός πεδίου (1) → εκτός.
    xml = lambda cat: s39["raw_xml"].replace(">16<", f">{cat}<")  # noqa: E731
    r4 = compute([dict(s39, raw_xml=xml("14")), dict(s39, mark="4005", raw_xml=xml("7")),
                  dict(s39, mark="4006", raw_xml=xml("1")), dict(s39, mark="4007", raw_xml=xml("27"))], [])["codes"]
    assert (r4["342"], r4["310"], r4["349"], r4["311"]) == (40, 40, 40, 120), r4  # λοιπές (27) → 349
    # Υπηρεσίες εκτός Ελλάδας (4): κράτος-μέλος (ή άγνωστη χώρα) → 345, τρίτη χώρα → 349.
    cp = lambda cc: xml("4").replace("<invoiceDetails>", f"<counterpart><country>{cc}</country></counterpart><invoiceDetails>")  # noqa: E731
    r5 = compute([dict(s39, raw_xml=cp("DE")), dict(s39, mark="4008", raw_xml=cp("US")),
                  dict(s39, mark="4009", raw_xml=xml("4"))], [])["codes"]
    assert (r5["345"], r5["349"]) == (80, 40), r5
    assert reconcile([dict(s39, raw_xml=cp("US"))], [])["income"]["totals"] == {"349": 40}
    m = reconcile([sale], [dict(rc, total_net=17.0), buy, draft])
    assert m["income"]["ok"] and m["expense"]["ok"], m
    m_inc = {r["e3"]: r["vals"] for r in m["income"]["rows"]}
    assert m_inc == {"E3_561_001": {"303": 100, "333": 24, "out": 0}, "E3_561_003": {"303": 0, "333": 0, "out": 50}}, m_inc
    m_exp = {r["e3"]: r["vals"] for r in m["expense"]["rows"]}
    assert m_exp["E3_102_001"]["361"] == 50 and m_exp["E3_102_001"]["381"] == 12 and m_exp["E3_102_001"]["out"] == 5, m_exp
    assert m_exp["E3_585_016"]["361"] == 0 and m_exp["E3_585_016"]["out"] == 33 and m_exp["E3_585_016"]["nd"] == 7.2, m_exp
    assert m["expense"]["totals"]["364"] == 17 and m["expense"]["totals"]["384"] == 3.53 and m["expense"]["net"] == 105, m["expense"]
    assert (r["470"], r["502"], r["511"]) == (36, 46, 0), r
    # Χρεωστικό έως 30 € → δεν αποδίδεται, μεταφέρεται· το 483 της επόμενης προστίθεται στο προς καταβολή.
    small = [{"mark": "10", "invoice_type": "2.1", "lines_json": json.dumps(
        [{"net_value": 100, "vat_amount": 24, "vat_category": "1"}])}]
    r = compute(small, [])["codes"]
    assert (r["480"], r["511"], r["carry"]) == (24, 0, 24), r
    r = compute(small, [], prev_debit=24)["codes"]
    assert (r["483"], r["511"], r["carry"]) == (24, 48, 0), r
    r = compute([], exp, prev_debit=20)["codes"]  # πιστωτική περίοδος: το 483 μειώνει το πιστωτικό
    assert (r["470"], r["502"], r["511"]) == (36, 16, 0), r
    r = compute([{"mark": "11", "invoice_type": "2.1", "lines_json": json.dumps(
        [{"net_value": 125, "vat_amount": 30, "vat_category": "1"}])}], [])["codes"]
    assert (r["511"], r["carry"]) == (0, 30), r  # ακριβώς 30 € → μεταφέρεται
    # Χαρακτηρισμός όλου του παραστατικού δεμένος στη γραμμή 1 (400012184930639): ΦΠΑ = 90,24, όχι 59,04.
    d = {"mark": "12", "invoice_type": "1.1", "total_vat": 90.24, "lines_json": json.dumps(
        [{"line_number": 1, "net_value": 246.0, "vat_amount": 59.04}, {"line_number": 2, "net_value": 130.0, "vat_amount": 31.2}]),
        "cls_json": json.dumps([{"line": 1, "type": "VAT_361", "amount": 376.0}])}
    assert [round(t, 2) for _, _, t in input_vat(d)] == [90.24] and compute([], [d])["codes"]["381"] == 90.24
    # ...και με στρογγυλοποιήσεις ανά γραμμή (400012397574828): ακριβώς ο ΦΠΑ του παραστατικού.
    d = {"mark": "13", "invoice_type": "1.1", "total_vat": 51.9, "lines_json": json.dumps(
        [{"line_number": 1, "net_value": 111.6, "vat_amount": 26.78}, {"line_number": 2, "net_value": 104.65, "vat_amount": 25.12}]),
        "cls_json": json.dumps([{"line": 1, "type": "VAT_361", "amount": 216.25}])}
    assert round(input_vat(d)[0][2], 2) == 51.9, input_vat(d)
    # Ανά παραστατικό με δύο συντελεστές (400013280656874): το «line» είναι ομάδα ανά κατηγορία ΦΠΑ.
    d = {"mark": "14", "invoice_type": "1.1", "total_vat": 15.36, "lines_json": json.dumps(
        [{"line_number": 1, "net_value": 249.54, "vat_amount": 14.97, "vat_category": "3"},
         {"line_number": 2, "net_value": 6.53, "vat_amount": 0.39, "vat_category": "3"},
         {"line_number": 3, "net_value": 893.0, "vat_amount": 0.0, "vat_category": "7"}]),
        "cls_json": json.dumps([{"line": 1, "type": "E3_585_011", "category": "category2_4", "amount": 256.07},
                                {"line": 1, "type": "VAT_361", "amount": 256.07},
                                {"line": 2, "type": "E3_585_011", "category": "category2_5", "amount": 893.0}])}
    e3 = [e for e in json.loads(d["cls_json"]) if e["type"].startswith("E3")]
    assert cls_lines(e3, {ln["line_number"]: ln for ln in json.loads(d["lines_json"])}) == [[1, 2], [3]]
    assert [round(x, 2) for x in deductible_by_line(d).values()] == [14.97, 0.39]
    # Δύο συντελεστές, χαρακτηρισμοί ΦΠΑ ανά ομάδα: ο φόρος κάθε ομάδας, όχι αναλογικά στη βάση.
    d = {"mark": "15", "invoice_type": "1.1", "total_vat": 30.5, "lines_json": json.dumps(
        [{"line_number": 1, "net_value": 100.0, "vat_amount": 24.0, "vat_category": "1"},
         {"line_number": 2, "net_value": 50.0, "vat_amount": 6.5, "vat_category": "2"}]),
        "cls_json": json.dumps([{"line": 1, "type": "VAT_361", "amount": 50.0}, {"line": 2, "type": "VAT_362", "amount": 100.0}])}
    assert [round(x, 2) for _, _, x in input_vat(d)] == [6.5, 24.0], input_vat(d)
    print("OK")
