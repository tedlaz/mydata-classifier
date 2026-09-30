"""Αυτόματη φόρτωση συνδυασμών ΑΑΔΕ στην εκκίνηση (python test_combos_bundled.py)."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402

A.app.testing = True  # χωρίς πύλη login (βλ. _require_login)
import db  # noqa: E402

# 1) Πρώτη εκκίνηση: φορτώθηκε το αρχείο του docs/.
n = db.combos_count()
assert n > 0 and db.get_setting("combos_version") == "1.0.8", (n, db.get_setting("combos_version"))

# 2) Καθαρισμός → νέα εκκίνηση δεν ξαναφορτώνει (ίδια έκδοση).
db.clear_combos()
A._load_bundled_combos()
assert db.combos_count() == 0

# 3) Παλαιότερη αποθηκευμένη έκδοση → νεότερο αρχείο της εφαρμογής την αντικαθιστά.
db.set_setting("combos_version", "1.0.7")
A._load_bundled_combos()
assert db.combos_count() == n and db.get_setting("combos_version") == "1.0.8"

# 4) Χειροκίνητη εισαγωγή νεότερου αρχείου → κρατά την έκδοσή του, το bundled δεν την πατάει.
import io  # noqa: E402
with open(A._bundled_combos()[0], "rb") as fh:
    data = fh.read()
A.app.test_client().post("/combinations/import", data={"file": (io.BytesIO(data), "syndiasmoi_xaraktirismwn_v1.1.0.xlsx")})
assert db.get_setting("combos_version") == "1.1.0"
A._load_bundled_combos()
assert db.get_setting("combos_version") == "1.1.0"
print("OK")
