"""Παράγει το aade_form.py (κανόνες και επιλογές της Φόρμας καταχώρησης) από την Ειδική Φόρμα Καταχώρησης της ΑΑΔΕ.

Χρήση (Βοήθεια → Αλλαγές ΑΑΔΕ):  uv run python tools/aade_form_rules.py <φάκελος με aade_labels.json και aade_app.js>

Από το aade_labels.json διαβάζει μόνο τους πίνακες της φόρμας (όχι το loggedInUser — τα στοιχεία χρήστη)·
από το aade_app.js μόνο τις λίστες τύπων που είναι γραμμένες στον κώδικα της φόρμας. Γράφει το aade_form.py
και τυπώνει τις διαφορές από το προηγούμενο."""
import json
import os
import pprint
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "aade_form.py")

# Πεδίο της ΑΑΔΕ → ομάδα της Φόρμας καταχώρησης (ό,τι δεν είναι εδώ, η φόρμα μας δεν το έχει).
FIELDS = {
    "dispatchDate": "dispatch", "dispatchTime": "dispatch", "movePurpose": "dispatch", "vehicleNumber": "dispatch",
    "measurementUnit": "unit", "quantity": "unit",
    "withheldAmount": "withheld", "withheldPercentCategory": "withheld",
    "otherTaxesAmount": "other", "otherTaxesPercentCategory": "other",
    "stampDutyAmount": "stamp", "stampDutyPercentCategory": "stamp",
    "feesAmount": "fees", "feesPercentCategory": "fees", "deductionsAmount": "deductions",
    "correlatedInvoices": "correlated", "selfPricing": "self_pricing", "paymentMethods": "payments",
    "counterpart": "counterpart", "issuer": "issuer",
}
TAX_MAPS = {"WITHHELD_TAX_MAP": "1", "FEES_TAX_MAP": "2", "OTHER_TAX_MAP": "3", "STAMP_TAX_MAP": "4"}


def invoice_type(item: str) -> str:  # «Item14_5» → «14.5»
    return item[4:].replace("_", ".", 1)


def order(t: str) -> list[int]:
    return [int(x) for x in t.split(".")]


def js_array(src: str, name: str):
    """Η τιμή ενός «name=[...]» του aade_app.js ως Python (JSON ή απλές λίστες strings)."""
    m = re.search(re.escape(name) + r"\s*=\s*\[", src)
    i, depth = m.end() - 1, 0
    for j in range(i, len(src)):
        depth += {"[": 1, "]": -1}.get(src[j], 0)
        if depth == 0:
            return json.loads(src[i:j + 1])
    raise ValueError(name)


def build(folder: str) -> dict:
    page = json.load(open(os.path.join(folder, "aade_labels.json"), encoding="utf-8"))["pageData"]
    js = open(os.path.join(folder, "aade_app.js"), encoding="utf-8").read()
    L = page["labelsMap"]
    R = {}
    for k in ("returnedReferenceDataMaps", "returnedReferenceDataMapsB", "returnedReferenceDataMapsC",
              "returnedReferenceDataMapsD", "returnedReferenceDataMapsS", "returnedReferenceDataMapsPent"):
        R.update(page[k])

    def plain(name):  # {key, value: label}: με τη σειρά της ΑΑΔΕ
        return [[str(x["key"]), L[x["value"]]] for x in R[name]]

    def rated(name, reverse=False):  # {key, valueA: label, valueB: ενεργό, valueC: συντελεστής %, valueD: σειρά}
        rows = sorted((x for x in R[name] if x.get("valueB") == 1), key=lambda x: x["valueD"], reverse=reverse)
        return [[str(x["key"]), L[x["valueA"]], float(x["valueC"]) if x.get("valueC") else None] for x in rows]

    mappings = js_array(js, "var mappingsIndex")
    out = {
        "HIDDEN": {invoice_type(item): sorted({FIELDS[f["value"]] for f in fields
                                               if f["key"] in ("invoice", "invoiceHeader", "invoiceDetails")
                                               and f["value"] in FIELDS})
                   for item, fields in sorted(page["emptyFieldsMap"].items(), key=lambda kv: order(invoice_type(kv[0])))},
        "CORRELATED": sorted(set(page["correlatedInvoicesTypesList"]), key=order),
        # «1.1.1» = παραλλαγή «παράλειψη εκδότη» του 1.1 (οι «1.7.x», «2.5.x» είναι εσωτερικές της ΑΑΔΕ)
        "OMISSION": sorted({x["key"].rsplit(".", 1)[0] for x in page["invTypesList"]
                            if x["value"].get("isOmissionDeviation") and x["key"].count(".") == 2}, key=order),
        "EXCLUDED_TAX_OPTIONS": {invoice_type(item): {TAX_MAPS[m]: sorted(map(str, v), key=int)
                                                      for m, v in maps.items() if m in TAX_MAPS}
                                 for item, maps in page["excludedOptionsMap"].items()},
        # ΦΠΑ ανά τύπο: «χωρίς ΦΠΑ» (κατηγορία 8), «μόνο 0%», «υποχρεωτικός» (όχι 0% και 8)· αλλού όλα εκτός της 8.
        "VAT_EMPTY": js_array(js, "emptyVatInvoiceTypes"),
        "VAT_ZERO": js_array(js, "zeroVatInvoiceTypes"),
        "VAT_MANDATORY": js_array(js, "mandatoryVatInvoiceTypes"),
        # Χώρα εκδότη / αντισυμβαλλόμενου: σε αυτούς τους τύπους ΔΕΝ είναι Ελλάδα· σε όλους τους άλλους κλειδώνει σε Ελλάδα.
        "NOT_GREEK_ISSUER": js_array(js, "notGreekIssuer"),
        "NOT_GREEK_COUNTERPART": js_array(js, "notGreekCounterpart"),
        "TAX_TYPES": [[str(m["id"]), L[m["label"]]] for m in mappings if m.get("id")],
        "OPTIONS": {
            "family": plain("INVOICE_TYPE_MAP"),
            "invoice_type": [[x["key"], L[x["valueB"]]] for x in R["INVOICE_SUBTYPE_MAP"]],
            "vat": rated("VAT_VALUES_MAP", reverse=True),
            "withheld": rated("WITHHELD_TAX_VALUES_MAP"),
            "other": rated("OTHER_TAX_VALUES_MAP"),
            "stamp": rated("STAMP_DUTY_VALUES_MAP"),
            "fees": rated("FEES_TYPES_VALUES_MAP"),
            "exemption": [[str(x["key"]), L[x["valueA"]]] for x in sorted(R["VAT_EXEMPTIONS_MAP"], key=lambda x: x["valueB"])],
            "payment": plain("PAYMENT_METHOD_MAP"),
            "unit": plain("QUANTITY_UNITS_MAP"),
            "move_purpose": plain("SHIPPING_PURPOSE_MAP"),
            "special": [[x["key"], L[x["valueA"]]] for x in R["SPECIAL_INV_CATEGORY_MAP"] if x.get("valueB") == 1],
            "rec_type": plain("REC_TYPES_MAP"),
            "currency": [[c["id"], c["descr"]] for c in sorted(page["currenciesList"], key=lambda c: (c["displayOrder"], c["descr"]))],
            "income_category": plain("INC_CLASS_CAT_MAP"),
            "expense_category": plain("EXP_CLASS_CAT_MAP"),
            "income_e3": sorted(plain("INC_CLASS_TYPE_MAP")),
            "expense_e3": sorted(plain("EXP_CLASS_TYPE_MAP")),
        },
    }
    return out


def main(folder: str) -> None:
    new = build(folder)
    old = {}
    if os.path.exists(OUT):
        exec(open(OUT, encoding="utf-8").read(), old)  # noqa: S102 — δικό μας, παραγόμενο αρχείο
    lines = ['"""ΠΑΡΑΓΕΤΑΙ ΑΥΤΟΜΑΤΑ από το tools/aade_form_rules.py — μην το αλλάζεις με το χέρι.',
             "",
             "Κανόνες και επιλογές της Ειδικής Φόρμας Καταχώρησης της ΑΑΔΕ (εφαρμογή myDATA), για τη Φόρμα καταχώρησης",
             'καταχώρησης. Οδηγίες ενημέρωσης: Βοήθεια → Αλλαγές ΑΑΔΕ."""', ""]
    for name, value in new.items():
        lines.append(f"{name} = " + pprint.pformat(value, width=118, sort_dicts=False))
        lines.append("")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print(f"Γράφτηκε το {os.path.relpath(OUT, ROOT)}.")
    diffs = []
    for name, value in new.items():
        if name not in old:
            diffs.append(f"{name}: νέο")
        elif name == "OPTIONS":
            diffs += [f"OPTIONS[{k!r}]: άλλαξε" for k in value if old[name].get(k) != value[k]]
        elif old[name] != value:
            diffs.append(f"{name}: άλλαξε")
    print("Διαφορές από το προηγούμενο:", ", ".join(diffs) or "καμία")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
