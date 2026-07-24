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
    except requests.RequestException, ValueError:
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


def lookup_name(afm: str, country: str = "EL", timeout: int = 15) -> str | None:
    """Επωνυμία για έγκυρο ΑΦΜ, αλλιώς None."""
    result = check_vat(country, afm, timeout=timeout)
    if result and result["valid"]:
        return result["name"]
    return None
