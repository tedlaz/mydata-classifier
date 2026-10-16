"""Οι λίστες τιμών της εφαρμογής ταιριάζουν με το XSD της ΑΑΔΕ (python -m tests.test_xsd_lists).

Μετά από νέα έκδοση XSD (Βοήθεια → Αλλαγές ΑΑΔΕ), ό,τι αποτύχει εδώ δείχνει ποια λίστα θέλει ενημέρωση."""
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
from lxml import etree  # noqa: E402

import app as A  # noqa: E402
import classifications as C  # noqa: E402
import aade_form  # noqa: E402
import mydata_client as M  # noqa: E402

X = {"xs": "http://www.w3.org/2001/XMLSchema"}
inv = etree.parse(os.path.join(M._XSD_DIR, f"InvoicesDoc-v{M.XSD_VERSION}.xsd"))


def element_values(name: str) -> set[str]:
    """Επιτρεπτές τιμές ενός στοιχείου του InvoicesDoc (εύρος minInclusive–maxInclusive ή simpleType)."""
    el = inv.xpath(f"//xs:element[@name='{name}']", namespaces=X)[0]
    lo = el.xpath(".//xs:minInclusive/@value", namespaces=X)
    if lo:
        return {str(i) for i in range(int(lo[0]), int(el.xpath(".//xs:maxInclusive/@value", namespaces=X)[0]) + 1)}
    return type_values(el.get("type").split(":")[-1])


def type_values(name: str) -> set[str]:
    enum = set(M.xsd_enum(name))
    if enum:
        return enum
    st = etree.parse(os.path.join(M._XSD_DIR, f"SimpleTypes-v{M.XSD_VERSION}.xsd"))
    t = st.xpath(f"//xs:simpleType[@name='{name}']", namespaces=X)[0]
    lo, hi = (int(t.xpath(f".//xs:{k}/@value", namespaces=X)[0]) for k in ("minInclusive", "maxInclusive"))
    return {str(i) for i in range(lo, hi + 1)}


problems = []


def same(label: str, ours, spec: set[str], subset: bool = False) -> None:
    ours = set(ours)
    extra, missing = sorted(ours - spec), sorted(spec - ours)
    if extra or (missing and not subset):
        problems.append(f"{label}: εκτός σχήματος {extra} · λείπουν {missing}")


# Πίνακες τιμών (API ERP §8) — ακριβώς οι τιμές του σχήματος.
same("Εξαίρεση ΦΠΑ (§8.3)", A.XML_EXTRA_CODES["vatExemptionCategory"], element_values("vatExemptionCategory"))
same("Παρακράτηση (§8.4)", C.WITHHELD_CATEGORIES, element_values("withheldPercentCategory"))
same("Λοιποί φόροι (§8.5)", C.OTHER_TAX_CATEGORIES, element_values("otherTaxesPercentCategory"))
same("Ψηφιακό τέλος (§8.6)", C.STAMP_DUTY_CATEGORIES, element_values("stampDutyPercentCategory"))
same("Τέλη (§8.7)", C.FEES_CATEGORIES, element_values("feesPercentCategory"))
same("Είδος φόρου", C.TAX_TYPES, element_values("taxType"))
same("Τρόπος πληρωμής (§8.12)", M.PAYMENT_METHODS, element_values("type"))  # το πρώτο «type»: paymentMethodDetails
same("Μονάδα μέτρησης (§8.13)", A.XML_EXTRA_CODES["measurementUnit"], element_values("measurementUnit"))
same("Σκοπός διακίνησης (§8.14)", A.XML_EXTRA_CODES["movePurpose"], element_values("movePurpose"))
same("Κατηγορίες εσόδων (§8.8)", C.INCOME_CATEGORIES, type_values("IncomeClassificationCategoryType") - {"category3"})
same("Κατηγορίες εξόδων (§8.10)", C.EXPENSE_CATEGORIES, type_values("ExpensesClassificationCategoryType"))
# Η ΑΑΔΕ δεν δείχνει όλες τις τιμές σε αυτά (ανενεργές / μόνο για ανάγνωση): αρκεί να είναι έγκυρες.
# Επιλογές της Γενικής φόρμας (από την Ειδική Φόρμα της ΑΑΔΕ, aade_form.py): η ΑΑΔΕ δείχνει λιγότερες
# από όσες επιτρέπει το σχήμα (π.χ. 3 μονάδες μέτρησης), αλλά ποτέ τιμή εκτός σχήματος.
O = M.ENTRY_OPTIONS
for label, key, spec in (("ΦΠΑ", "vat", element_values("vatCategory")),
                         ("Εξαίρεση ΦΠΑ", "exemption", element_values("vatExemptionCategory")),
                         ("Παρακράτηση", "withheld", element_values("withheldPercentCategory")),
                         ("Λοιποί φόροι", "other", element_values("otherTaxesPercentCategory")),
                         ("Ψηφιακό τέλος", "stamp", element_values("stampDutyPercentCategory")),
                         ("Τέλη", "fees", element_values("feesPercentCategory")),
                         ("Τρόπος πληρωμής", "payment", element_values("type")),
                         ("Μονάδα μέτρησης", "unit", element_values("measurementUnit")),
                         ("Σκοπός διακίνησης", "move_purpose", element_values("movePurpose")),
                         ("Είδος γραμμής", "rec_type", element_values("recType") - {"1", "4", "5"}),  # 1/4/5 ανενεργά (§8.16)
                         ("Ειδική κατηγορία", "special", type_values("SpecialInvoiceCategoryType")),
                         ("Νόμισμα", "currency", type_values("CurrencyType")),
                         ("Τύπος παραστατικού", "invoice_type", type_values("InvoiceType")),
                         ("Κατηγορίες εσόδων", "income_category", type_values("IncomeClassificationCategoryType")),
                         ("Κατηγορίες εξόδων", "expense_category", type_values("ExpensesClassificationCategoryType"))):
    same(f"Γενική φόρμα · {label}", O[key], spec, subset=True)
same("Γενική φόρμα · Τύπος φόρου", dict(aade_form.TAX_TYPES), element_values("taxType"))
same("Τύποι παραστατικού", C.INVOICE_TYPE_NAMES, type_values("InvoiceType"), subset=True)
# Ε3: κάθε κωδικός του σχήματος θέλει ονομασία (αλλιώς φαίνεται σκέτος ο κωδικός)· έξτρα επιτρέπονται
# για εμφάνιση παλιών παραστατικών (π.χ. E3_585_017, που η φόρμα ήδη φιλτράρει με το XSD).
missing = lambda ours, spec: sorted(set(spec) - set(ours))  # noqa: E731
ev = type_values("ExpensesClassificationValueType")
for label, ours, spec in (("Ε3 εσόδων (§8.9)", C.INCOME_TYPES, type_values("IncomeClassificationValueType")),
                          ("Ε3 εξόδων (§8.11)", C.EXPENSE_TYPES, {v for v in ev if v.startswith("E3_")}),
                          ("Χαρακτηρισμοί ΦΠΑ (§8.11)", [k for k in C.VAT_TYPES if k] + ["NOT_VAT_295"],
                           {v for v in ev if not v.startswith("E3_")})):
    if missing(ours, spec):
        problems.append(f"{label}: χωρίς ονομασία {missing(ours, spec)}")

assert not problems, "\n".join(problems)
print("OK")
