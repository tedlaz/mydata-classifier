"""
Κεντρικός χρήστης που ξεκλειδώνει την εφαρμογή + κρυπτογράφηση των κλειδιών myDATA.

- Ένα τυχαίο data key (DEK, Fernet) κρυπτογραφεί τα κλειδιά myDATA στη βάση («enc:...»).
- Το DEK αποθηκεύεται κρυπτογραφημένο με κλειδί που προκύπτει από τον κωδικό (scrypt),
  άρα το κλειδί δεν υπάρχει πουθενά στον δίσκο. Σωστός κωδικός = το DEK αποκρυπτογραφείται
  (το Fernet έχει HMAC, οπότε λάθος κωδικός → InvalidToken· δεν χρειάζεται ξεχωριστό hash).
- Μετά το login το DEK ζει μόνο στη μνήμη της διεργασίας· restart = νέο login.
"""

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:"
_dek: Fernet | None = None
_raw: bytes | None = None  # το DEK σε bytes, για re-wrap στην αλλαγή κωδικού
nonce: str | None = None  # τιμή του session["auth"] για το τρέχον ξεκλείδωμα


def _kek(password: str, salt: bytes) -> Fernet:
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 2**20, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _set(dek: bytes) -> None:
    global _dek, _raw, nonce
    _dek, _raw, nonce = Fernet(dek), dek, secrets.token_urlsafe(32)


def create(username: str, password: str) -> dict:
    """Νέος χρήστης: επιστρέφει τις ρυθμίσεις auth_* προς αποθήκευση και ξεκλειδώνει."""
    dek = Fernet.generate_key()
    _set(dek)
    return _wrap(username, password, dek)


def _wrap(username: str, password: str, dek: bytes) -> dict:
    salt = os.urandom(16)
    return {
        "auth_user": username,
        "auth_salt": salt.hex(),
        "auth_dek": _kek(password, salt).encrypt(dek).decode(),
    }


def unlock(username: str, password: str, rec: dict) -> bool:
    """Έλεγχος username/password με τις αποθηκευμένες auth_* ρυθμίσεις· αν σωστά, ξεκλειδώνει."""
    try:
        dek = _kek(password, bytes.fromhex(rec["auth_salt"])).decrypt(rec["auth_dek"].encode())
    except (InvalidToken, KeyError, ValueError):
        return False
    if not hmac.compare_digest(username.encode(), (rec.get("auth_user") or "").encode()):
        return False
    _set(dek)
    return True


def rewrap(username: str, password: str) -> dict:
    """Αλλαγή κωδικού/username: ίδιο DEK, νέο τύλιγμα (τα πεδία δεν ξανακρυπτογραφούνται)."""
    if _raw is None:
        raise RuntimeError("Η εφαρμογή είναι κλειδωμένη.")
    return _wrap(username, password, _raw)


def lock() -> None:
    global _dek, _raw, nonce
    _dek = _raw = nonce = None


def unlocked() -> bool:
    return _dek is not None


def enc(value: str) -> str:
    """Κρυπτογράφηση όταν είναι ξεκλείδωτη· αλλιώς (πριν το πρώτο setup / tests) ως έχει."""
    if not value or _dek is None or value.startswith(_PREFIX):
        return value
    return _PREFIX + _dek.encrypt(value.encode()).decode()


def dec(value: str) -> str:
    if not value or not value.startswith(_PREFIX):
        return value or ""
    if _dek is None:
        raise RuntimeError("Η εφαρμογή είναι κλειδωμένη.")
    return _dek.decrypt(value[len(_PREFIX):].encode()).decode()
