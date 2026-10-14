# Γραμμή σπασμένη σε 2 Ε3 με τον ίδιο ΦΠΑ → ένας VAT_364 με το άθροισμα (σφάλμα 305).
from mydata_client import build_expenses_classification_xml

x = build_expenses_classification_xml("1", [
    {"line_number": 2, "classification_type": "E3_102_001", "classification_category": "category2_1", "amount": 60},
    {"line_number": 2, "classification_type": "VAT_364", "classification_category": "", "amount": 60},
    {"line_number": 2, "classification_type": "E3_585_016", "classification_category": "category2_4", "amount": 40},
    {"line_number": 2, "classification_type": "VAT_364", "classification_category": "", "amount": 40},
])
assert x.count("VAT_364<") == 1 and "<amount>100.00</amount>" in x, x
print("ok")
