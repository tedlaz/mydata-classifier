"""
Έλεγχος εγκυρότητας ΑΦΜ και εύρεση επωνυμίας μέσω VIES (European Commission).

REST API: https://ec.europa.eu/taxation_customs/vies/#/technical-information
GET https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}

Πλεονεκτήματα έναντι GSIS/RgWsPublic2: δεν απαιτεί credentials και καλύπτει
όλα τα κράτη-μέλη της ΕΕ (χρήσιμο και για εκδότες των τύπων 14.1-14.4).
Περιορισμός: επιστρέφει στοιχεία μόνο για ΑΦΜ/VAT ενεργά στο σύστημα VIES
(στην Ελλάδα: όσα έχουν δηλωθεί για ενδοκοινοτικές συναλλαγές).
"""

import re
import unicodedata

import requests

BASE_URL = (
    "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}"
)

# Η Ελλάδα στο VIES είναι "EL", όχι "GR"
_COUNTRY_ALIASES = {"GR": "EL"}


def normalize(country: str, number: str) -> tuple[str, str]:
    country = (country or "EL").strip().upper()
    country = _COUNTRY_ALIASES.get(country, country)
    number = re.sub(r"[^A-Za-z0-9]", "", number or "")
    # αν το νούμερο ξεκινά με τον κωδικό χώρας (π.χ. EL123...), αφαίρεσέ τον
    if number.upper().startswith(country):
        number = number[len(country) :]
    return country, number


def check_vat(country: str, number: str, timeout: int = 15) -> dict | None:
    """
    Επιστρέφει {"valid": bool, "name": str|None, "address": str|None}
    ή None αν η υπηρεσία δεν απάντησε (σφάλμα δικτύου / μη διαθέσιμη).
    Δεν σηκώνει exceptions - ο έλεγχος είναι βοηθητικός.
    """
    country, number = normalize(country, number)
    if not number:
        return {"valid": False, "name": None, "address": None}
    try:
        resp = requests.get(
            BASE_URL.format(country=country, number=number),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()

    except requests.RequestException:
        return None

    except ValueError:
        return None

    valid = bool(data.get("isValid") if "isValid" in data else data.get("valid"))
    name = (data.get("name") or "").strip()
    address = (data.get("address") or "").strip()
    # το VIES επιστρέφει "---" όταν το κράτος-μέλος δεν κοινοποιεί το στοιχείο
    if name in ("", "---"):
        name = None
    if address in ("", "---"):
        address = None
    return {"valid": valid, "name": name, "address": address}


# Συντομεύσεις για την εμφάνιση επωνυμιών (η αποθήκευση μένει όπως έρχεται από
# VIES/ΑΑΔΕ). Οι λέξεις συγκρίνονται χωρίς τόνους και με ανοχή σε ορθογραφικά
# λάθη (_close), π.χ. ΕΤΑΡΕΙΑ, ΠΕΡΙΩΡΙΣΜΕΝΗΣ. Νέα συντόμευση/εξαίρεση = μία γραμμή.
_W = r"(?<!\w){}(?!\w)"
# Νομικές μορφές με τελείες, με λατινικά γράμματα (VIVABANK AE) ή με κενά στο τέλος (NOVA Μ Α Ε).
_A, _E, _O, _I, _K, _B = "[ΑA]", "[ΕE]", "[ΟO]", "[ΙI]", "[ΚK]", "[ΒB]"
_DOTTED = [
    (_W.format(rf"{_A}\.?\s*{_E}\.\s*{_B}\.?\s*{_E}\.?|AEBE"), "ΑΕΒΕ"),
    (_W.format(rf"{_E}\.\s*Π\.\s*{_E}\.?|EPE"), "ΕΠΕ"),
    (_W.format(rf"{_I}\.\s*{_K}\.\s*{_E}\.?|IKE"), "ΙΚΕ"),
    (_W.format(rf"{_A}\.\s*{_E}\.?|AE"), "ΑΕ"),
    (_W.format(rf"{_O}\.\s*{_E}\.?|OE"), "ΟΕ"),
    (_W.format(rf"{_E}\.\s*{_E}\.?|EE"), "ΕΕ"),
    (r"(?<!\w)(?:Μ\s+)?([ΑΟΕ])\s+Ε\s*$", r"\1Ε"),
]
_DOTTED = [(re.compile(p, re.I), r) for p, r in _DOTTED]
_MONO_M = re.compile(r"(?<!\w)Μ(?:ΟΝ)?\.?\s*(?=(?:ΑΕ|ΙΚΕ|ΕΠΕ)(?!\w))", re.I)  # Μ.ΙΚΕ / Μ.ΑΕ (μονοπρόσωπη)

_ET = ("ΕΤΑΙΡΕΙΑ", "ΕΤΑΙΡΙΑ")
# (λέξεις, αντικατάσταση) — κάθε λέξη είναι πλειάδα αποδεκτών γραφών· "" = αφαίρεση.
# Οι πολυλεκτικές πρώτα. Η «ΑΝΩΝΥΜΗ» γίνεται ΑΕ και η «ΕΤΑΙΡΕΙΑ» φεύγει χωριστά, ώστε
# να πιάνεται και η «ΑΝΩΝΥΜΗ ΕΜΠΟΡΙΚΗ ΕΤΑΙΡΕΙΑ».
_PHRASES = [
    ((_ET, ("ΠΕΡΙΟΡΙΣΜΕΝΗΣ",), ("ΕΥΘΥΝΗΣ",)), "ΕΠΕ"),
    ((("ΙΔΙΩΤΙΚΗ",), ("ΚΕΦΑΛΑΙΟΥΧΙΚΗ",), _ET), "ΙΚΕ"),
    ((("ΕΙΔΙΚΟΣ",), ("ΛΟΓΑΡΙΑΣΜΟΣ",), ("ΚΟΝΔΥΛΙΩΝ",), ("ΕΡΕΥΝΑΣ",)), "ΕΛΚΕ"),
    ((("ΔΗΜΟΣΙΑ",), ("ΕΠΙΧΕΙΡΗΣΗ",), ("ΗΛΕΚΤΡΙΣΜΟΥ",)), "ΔΕΗ"),
    ((("ΗΛΕΚΤΡΟΝΙΚΟΣ",), ("ΕΘΝΙΚΟΣ",), ("ΦΟΡΕΑΣ",), ("ΚΟΙΝΩΝΙΚΗΣ",), ("ΑΣΦΑΛΙΣΗΣ",)), "ΕΦΚΑ"),
    ((("ΕΘΝΙΚΟΣ",), ("ΦΟΡΕΑΣ",), ("ΚΟΙΝΩΝΙΚΗΣ",), ("ΑΣΦΑΛΙΣΗΣ",)), "ΕΦΚΑ"),
    ((("ΑΝΩΝΥΜΗ", "ΑΝΩΝΥΜΟΣ"),), "ΑΕ"),
    ((("ΟΜΟΡΡΥΘΜΗ", "ΟΜΟΡΥΘΜΗ"),), "ΟΕ"),
    ((("ΕΤΕΡΟΡΡΥΘΜΗ", "ΕΤΕΡΟΡΥΘΜΗ"),), "ΕΕ"),
    ((("ΜΟΝΟΠΡΟΣΩΠΗ", "ΜΟΝΟΠΡΟΣΩΠΟΣ"),), ""),
    ((_ET,), ""),
    ((("ΕΜΠΟΡΙΑ",),), ""),
    ((("ΕΙΣΑΓΩΓΑΙ", "ΕΙΣΑΓΩΓΕΣ"),), ""),
    ((("ΕΞΑΓΩΓΑΙ", "ΕΞΑΓΩΓΕΣ"),), ""),
    ((("TELECOMMUNICATIONS",), ("AND", "ΑND", ""), ("MEDIA",)), ""),  # «ΑND» με ελληνικό Α, «&»
]
_CONNECTORS = {"ΚΑΙ", ""}  # «ΚΑΙ», «-», «&» δίπλα σε λέξη που αφαιρέθηκε φεύγουν κι αυτά
_LEGAL_FORM = re.compile(_W.format(r"(ΑΕΒΕ|ΑΕ|ΟΕ|ΕΕ|ΕΠΕ|ΙΚΕ)"), re.I)
SHORT_NAME_MIN = 20  # επωνυμίες μικρότερες από αυτό εμφανίζονται αυτούσιες
SHORT_NAME_MAX = 50  # πάνω από αυτό (μετά τις συντομεύσεις): 2 πρώτες λέξεις + νομική μορφή


def _key(word: str) -> str:
    """Λέξη για σύγκριση: κεφαλαία, χωρίς τόνους/διαλυτικά και σημεία στίξης."""
    word = unicodedata.normalize("NFD", word.upper())
    return re.sub(r"[\W_]", "", "".join(c for c in word if not unicodedata.combining(c)))


def _distance(a: str, b: str) -> int:
    """Απόσταση Levenshtein (λέξεις λίγων γραμμάτων → απλός πίνακας αρκεί)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _close(key: str, variants) -> bool:
    """Ταιριάζει με κάποια γραφή, επιτρέποντας 1 λάθος (6+ γράμματα) ή 2 (10+)."""
    for v in variants:
        tol = 2 if len(v) >= 10 else 1 if len(v) >= 6 else 0
        if key == v or (abs(len(key) - len(v)) <= tol and _distance(key, v) <= tol):
            return True
    return False


def _abbreviate(part: str) -> str:
    for pattern, repl in _DOTTED:
        part = pattern.sub(repl, part)
    toks = part.split()
    keys = [_key(t) for t in toks]
    out = []  # (λέξη, αφαιρέθηκε)
    i = 0
    while i < len(toks):
        for words, repl in _PHRASES:
            n = len(words)
            if i + n <= len(toks) and all(_close(keys[i + j], words[j]) for j in range(n)):
                out.append((repl, not repl))
                i += n
                break
        else:
            out.append((toks[i], False))
            i += 1
    kept = [
        t for k, (t, removed) in enumerate(out)
        if not removed and not (
            _key(t) in _CONNECTORS
            and ((k > 0 and out[k - 1][1]) or (k + 1 < len(out) and out[k + 1][1]))
        )
    ]
    words = _MONO_M.sub("", " ".join(kept)).split()
    # «ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ Α.Ε.» → μία φορά ΑΕ
    words = [w for k, w in enumerate(words) if not (k and w == words[k - 1] and _LEGAL_FORM.fullmatch(w))]
    return " ".join(words).strip(" ,-&")


def short_name(name):
    """Σύντομη επωνυμία για εμφάνιση: από το «αριστερό || δεξί» του VIES κρατά το
    αριστερό, ή το δεξί (διακριτικό τίτλο) αν είναι μικρότερο και χωρίς νομική μορφή,
    συμπληρωμένο με τη νομική μορφή του αριστερού· συντομεύει «ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ» → ΑΕ κλπ."""
    if not name or len(name.strip()) < SHORT_NAME_MIN:
        return name
    left, _, right = (_abbreviate(p) for p in name.partition("||"))
    forms = _LEGAL_FORM.findall(left)
    form = forms[-1].upper() if forms else ""
    if right and len(right) < len(left) and not _LEGAL_FORM.search(right):
        result = f"{right} {form}".strip()
    else:
        result = left or right
    if len(result) > SHORT_NAME_MAX:
        form = (_LEGAL_FORM.findall(result) or [form])[-1].upper()
        head = []
        for w in result.split():
            if _LEGAL_FORM.fullmatch(w) or len(head) == 2:
                break
            head.append(w)
        result = " ".join(head + [form]).strip()
    return result


def legal_name(name: str | None) -> str:
    """Η επίσημη επωνυμία: από το «επωνυμία||διακριτικός τίτλος» του VIES το αριστερό μέρος."""
    return (name or "").partition("||")[0].strip()


# Τ.Κ. + πόλη στην τελευταία γραμμή: «15124 - ΜΑΡΟΥΣΙ», «20122 MILANO MI», «2611ET DELFT», «AT-5330 Fuschl/See».
_POSTAL_CITY = re.compile(r"^(?:[A-Z]{1,3}-)?(\d{3,6}(?:\s?[A-Z]{2}(?=\s))?)\s*(?:-\s*)?(.+)$", re.I)
_NUMBER_LAST = re.compile(r"^(.*?\D)\s+(\d+[A-Za-zΑ-Ωα-ω]?(?:-\d+)?)$")   # «ΚΗΦΙΣΙΑΣ 99», «RICHINI 6»
_NUMBER_FIRST = re.compile(r"^(\d+[A-Za-z]?(?:-\d+)?),?\s+(\D.*)$")        # «12 RUE DE LA PAIX»


def split_address(address: str | None) -> dict:
    """Η διεύθυνση του VIES (ελεύθερο κείμενο, διαφορετικό ανά χώρα) σε οδό / αριθμό / Τ.Κ. / πόλη.

    Οι γραμμές χωρίζονται με αλλαγή γραμμής ή με πολλά κενά (Ελλάδα: «ΑΙΟΛΟΥ 86        10559 - ΑΘΗΝΑ»).
    ponytail: κανόνες για τις συνήθεις μορφές της ΕΕ· ό,τι δεν ταιριάζει μένει ολόκληρο στην οδό, για διόρθωση."""
    parts = [p.strip(" ,") for p in re.split(r"\n|\s{3,}", address or "") if p.strip(" ,")]
    out = {"street": "", "number": "", "postal_code": "", "city": ""}
    if not parts:
        return out
    if len(parts) > 1 and (m := _POSTAL_CITY.match(parts[-1])):
        out["postal_code"], out["city"] = m.group(1).upper().replace(" ", ""), m.group(2).strip()
        parts = parts[:-1]
    street = ", ".join(parts)
    if m := _NUMBER_LAST.match(street):
        street, out["number"] = m.group(1).strip(" ,"), m.group(2).lstrip("0") or "0"
    elif m := _NUMBER_FIRST.match(street):
        out["number"], street = m.group(1), m.group(2).strip()
    out["street"] = street
    return out


def lookup_name(afm: str, country: str = "EL", timeout: int = 15) -> str | None:
    """Επωνυμία για έγκυρο ΑΦΜ, αλλιώς None."""
    result = check_vat(country, afm, timeout=timeout)
    if result and result["valid"]:
        return result["name"]
    return None
