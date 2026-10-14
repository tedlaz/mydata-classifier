"""Υποχρεωτικός λογιστής: λογιστής ⇄ αποστολή μαζί, λήψη με κλειδιά εταιρείας, αποστολή με του λογιστή.
Εκτέλεση: python -m tests.test_accountant_required"""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import app as A  # noqa: E402
import db  # noqa: E402
from mydata_client import MyDataError  # noqa: E402

A.app.testing = True
db.init_db()
cid = db.add_company({"company_name": "Δοκιμή ΑΕ", "AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k",
                      "AADE_VAT_NUMBER": "123456789", "accountant_required": True})
db.set_active_company_id(cid)
c = A.app.test_client()
co = lambda: db.list_companies()[0]  # noqa: E731
user = lambda cl: cl.session.headers["aade-user-id"]  # noqa: E731

# Χωρίς credentials λογιστή: η αποστολή δεν ανοίγει, και ο λογιστής μαζί της.
c.post("/companies/toggle/0/allow_send")
assert not co()["allow_send"] and not co()["use_accountant"]
c.post("/companies/toggle/0/use_accountant")
assert not co()["allow_send"] and not co()["use_accountant"]
c.post("/companies/toggle/0/MYDATA_ENV")  # οι άλλες ρυθμίσεις αλλάζουν κανονικά
assert co()["MYDATA_ENV"] == "dev"

# Με credentials λογιστή: ανοίγουν/κλείνουν μαζί, από όποιον διακόπτη κι αν πατηθεί.
db.save_accountant({"user_id": "acc", "subscription_key": "acck", "env": "dev"})
c.post("/companies/toggle/0/allow_send")
assert co()["allow_send"] and co()["use_accountant"]
c.post("/companies/toggle/0/use_accountant")
assert not co()["allow_send"] and not co()["use_accountant"]
c.post("/companies/toggle/0/use_accountant")
assert co()["allow_send"] and co()["use_accountant"]
db.update_company(cid, co() | {"use_accountant": False})  # και απευθείας στη βάση: ακολουθεί την αποστολή
assert co()["use_accountant"]

# Λήψη με τα κλειδιά της εταιρείας, αποστολή με του λογιστή (+ entityVatNumber).
read, send = A._make_client(), A._make_client(send="classify")
assert user(read) == "u" and not read.entity_vat
assert user(send) == "acc" and send.entity_vat == "123456789"

# Ανά ενέργεια (Παράμετροι → Λογιστής). Default: όλα μέσω λογιστή εκτός από την ακύρωση εσόδου.
via = lambda action: user(A._make_client(send=action))  # noqa: E731
assert [via(a) for a in ("reject", "create", "cancel", "income_cancel")] == ["acc", "acc", "acc", "u"]
db.set_setting("accountant_actions", "")  # μόνο ο χαρακτηρισμός, πάντα
assert via("classify") == "acc" and [via(a) for a in ("reject", "create", "cancel")] == ["u", "u", "u"]
assert user(A._make_client()) == "u"  # κενή ρύθμιση: η λήψη δεν περνά στον λογιστή
db.set_setting("accountant_actions", "income_cancel,bogus")
assert via("income_cancel") == "acc" and via("reject") == "u" and via("bogus") == "u"
db.delete_setting("accountant_actions")

# Παράμετροι → Λογιστής: αποθήκευση των επιλογών (ο χαρακτηρισμός μένει πάντα).
page = c.get("/parameters?tab=accountant").get_data(as_text=True)
assert 'name="act_reject" value="1" checked' in page and 'name="act_income_cancel" value="1" >' in page
c.post("/accountant", data={"user_id": "acc", "env": "dev", "act_create": "1", "act_income_cancel": "1"})
assert db.get_setting("accountant_actions") == "create,income_cancel"
assert A.accountant_actions() == {"classify", "create", "income_cancel"}
assert db.get_accountant()["subscription_key"] == "acck"  # κενό κλειδί = κρατά το αποθηκευμένο
db.delete_setting("accountant_actions")

# Χωρίς δικά της κλειδιά: η λήψη γίνεται μέσω λογιστή.
db.update_company(cid, co() | {"AADE_USER_ID": "", "AADE_SUBSCRIPTION_KEY": ""})
assert user(A._make_client()) == "acc"

# Μη υποχρεωτικός, χωρίς λογιστή: αποστολή με τα κλειδιά της εταιρείας.
db.update_company(cid, co() | {"AADE_USER_ID": "u", "AADE_SUBSCRIPTION_KEY": "k",
                               "accountant_required": False, "use_accountant": False})
assert user(A._make_client(send="classify")) == "u" and co()["allow_send"]

# Υποχρεωτικός αλλά χάθηκαν τα credentials λογιστή: καμία αποστολή, ποτέ με του πελάτη.
db.update_company(cid, co() | {"accountant_required": True})
db.save_accountant({"user_id": "", "subscription_key": "", "env": "dev"})
try:
    A._make_client(send="classify")
    raise AssertionError("έπρεπε να αποτύχει")
except MyDataError:
    pass
assert user(A._make_client()) == "u"  # η λήψη συνεχίζει με τα κλειδιά της εταιρείας

# Αποθήκευση από τη φόρμα χωρίς credentials λογιστή: η αποστολή κλείνει.
c.post("/companies/update/0", data={"company_name": "Δοκιμή ΑΕ", "aade_user_id": "u",
                                    "accountant_required": "1", "allow_send": "1", "entity_type": "legal"})
assert not co()["allow_send"] and not co()["use_accountant"] and co()["accountant_required"]

print("OK")

# ------------------------------------------------------------------ #
# ΛΗΨΗ: όλοι οι συνδυασμοί. Με πλήρη κλειδιά εταιρείας ΠΑΝΤΑ με αυτά· του λογιστή μόνο αν δεν υπάρχουν.
# ------------------------------------------------------------------ #
import itertools  # noqa: E402
import re  # noqa: E402

KEYS = {"full": ("u", "k"), "user_only": ("u", ""), "none": ("", "")}
for use_acc, req, keys, acc_ok, acts in itertools.product(
        (False, True), (False, True), KEYS, (False, True), (None, "", "reject,create,cancel,income_cancel")):
    uid, key = KEYS[keys]
    db.save_accountant({"user_id": "acc" if acc_ok else "", "subscription_key": "acck" if acc_ok else "", "env": "dev"})
    db.update_company(cid, co() | {"use_accountant": use_acc, "accountant_required": req, "allow_send": use_acc,
                                   "AADE_USER_ID": uid, "AADE_SUBSCRIPTION_KEY": key, "AADE_VAT_NUMBER": "123456789"})
    db.delete_setting("accountant_actions") if acts is None else db.set_setting("accountant_actions", acts)
    case = (use_acc, req, keys, acc_ok, acts)
    if keys == "full":
        cl = A._make_client()
        assert user(cl) == "u" and not cl.entity_vat and "entityVatNumber" not in cl._params(), case
    elif (use_acc or req) and acc_ok:
        cl = A._make_client()
        assert user(cl) == "acc" and cl.entity_vat == "123456789", case
    else:
        try:
            A._make_client()
            raise AssertionError(f"έπρεπε να αποτύχει: {case}")
        except MyDataError:
            pass

# Κλειδιά του λογιστή περασμένα ως της εταιρείας: με λογιστή → μέσω λογιστή με entityVatNumber· χωρίς → μπλοκάρισμα.
db.save_accountant({"user_id": "acc", "subscription_key": "acck", "env": "dev"})
db.delete_setting("accountant_actions")
db.update_company(cid, co() | {"use_accountant": True, "AADE_USER_ID": "acc", "AADE_SUBSCRIPTION_KEY": "acck"})
assert A._make_client().entity_vat == "123456789"
db.update_company(cid, co() | {"use_accountant": False, "accountant_required": False})
try:
    A._make_client()
    raise AssertionError("έπρεπε να αποτύχει")
except MyDataError:
    pass

# Κανείς δεν παρακάμπτει τον κανόνα: client μόνο από το _make_client, αποστολή μόνο στα 2 γνωστά σημεία.
src = open(os.path.join(os.path.dirname(A.__file__), "app.py"), encoding="utf-8").read()
assert len(re.findall(r"MyDataClient\(", src)) == 3, "νέος MyDataClient εκτός _make_client"
assert len(re.findall(r"get_client\(send=", src)) == 2, "νέο σημείο αποστολής: έλεγξε τα κλειδιά του"
assert not re.search(r"_make_client\(send=", src.replace("_make_client(send=send)", "")), "client αποστολής εκτός get_client"
print("OK λήψη")
