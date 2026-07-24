"""
Δημιουργεί αυτόματα το αρχείο .env (δίπλα σε αυτό το script) αν δεν υπάρχει,
με ένα ασφαλές τυχαίο FLASK_SECRET.

Χρήση:
    python ensure_env.py        # τρέξε το χειροκίνητα
    from ensure_env import ensure_env; ensure_env()   # ή κάλεσέ το από τον κώδικα
"""

import os
import secrets

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_BASE_DIR, ".env")

_TEMPLATE = """\
# Δημιουργήθηκε αυτόματα από το ensure_env.py.
# ΠΡΟΣΟΧΗ: το .env δεν πρέπει να μπει σε git.

# Υποχρεωτικό - τυχαία μυστική τιμή για τα Flask sessions.
FLASK_SECRET={secret}
"""


def ensure_env(path: str = _ENV_PATH) -> bool:
    """Δημιουργεί το .env με ασφαλές FLASK_SECRET αν δεν υπάρχει ήδη.
    Επιστρέφει True αν δημιουργήθηκε τώρα, False αν υπήρχε ήδη.

    Αν το FLASK_SECRET δίνεται ήδη από το περιβάλλον (π.χ. σε Docker/compose),
    δεν χρειάζεται αρχείο .env και δεν επιχειρείται εγγραφή - έτσι αποφεύγεται
    το «Permission denied» όταν ο φάκελος του κώδικα δεν είναι εγγράψιμος."""
    if os.getenv("FLASK_SECRET"):
        return False
    if os.path.exists(path):
        return False
    try:
        secret = secrets.token_hex(32)  # 256-bit ασφαλές κλειδί
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_TEMPLATE.format(secret=secret))
        return True
    except OSError:
        # Ο φάκελος δεν είναι εγγράψιμος (π.χ. read-only /app σε container).
        # Δεν είναι κρίσιμο: το FLASK_SECRET μπορεί να δοθεί από το περιβάλλον.
        return False


if __name__ == "__main__":
    created = ensure_env()
    if created:
        print(f"✔ Δημιουργήθηκε το {_ENV_PATH} με νέο FLASK_SECRET.")
    else:
        print(f"• Το {_ENV_PATH} υπάρχει ήδη - δεν άλλαξε τίποτα.")
