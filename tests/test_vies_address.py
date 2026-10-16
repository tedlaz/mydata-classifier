"""Διευθύνσεις του VIES σε πεδία, με πραγματικές απαντήσεις ανά χώρα (python -m tests.test_vies_address)."""
from vies import legal_name, split_address


def parts(street, number, postal_code, city):
    return {"street": street, "number": number, "postal_code": postal_code, "city": city}


CASES = {
    # Ελλάδα: μία γραμμή, χωρισμένη με πολλά κενά· «Τ.Κ. - ΠΟΛΗ»
    "ΛΕΩΦΟΡΟΣ ΚΗΦΙΣΙΑΣ 99        15124 - ΜΑΡΟΥΣΙ": parts("ΛΕΩΦΟΡΟΣ ΚΗΦΙΣΙΑΣ", "99", "15124", "ΜΑΡΟΥΣΙ"),
    "ΑΙΟΛΟΥ 86        10559 - ΑΘΗΝΑ": parts("ΑΙΟΛΟΥ", "86", "10559", "ΑΘΗΝΑ"),
    # Ιταλία: πόλη με επαρχία
    "LARGO FRANCESCO RICHINI 6 \n20122 MILANO MI\n": parts("LARGO FRANCESCO RICHINI", "6", "20122", "MILANO MI"),
    # Ολλανδία: κενή πρώτη γραμμή, αριθμός με μηδενικά, Τ.Κ. «4 ψηφία + 2 γράμματα»
    "\nVOLDERSGRACHT 00001\n2611ET DELFT\n": parts("VOLDERSGRACHT", "1", "2611ET", "DELFT"),
    # Αυστρία: πρόθεμα χώρας στον Τ.Κ.
    "Am Brunnen 1\nAT-5330 Fuschl/See": parts("Am Brunnen", "1", "5330", "Fuschl/See"),
    # Γαλλία: ο αριθμός πρώτος
    "12 RUE DE LA PAIX\n75002 PARIS": parts("RUE DE LA PAIX", "12", "75002", "PARIS"),
    # χωρίς αναγνωρίσιμη μορφή: όλα στην οδό, για χειροκίνητη διόρθωση
    "SOMEWHERE": parts("SOMEWHERE", "", "", ""),
    "": parts("", "", "", ""),
}
for text, want in CASES.items():
    got = split_address(text)
    assert got == want, (text, got)

assert legal_name("ΟΡΓΑΝΙΣΜΟΣ ΤΗΛΕΠΙΚΟΙΝΩΝΙΩΝ ΤΗΣ ΕΛΛΑΔΟΣ ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ||ΟΤΕ") == \
    "ΟΡΓΑΝΙΣΜΟΣ ΤΗΛΕΠΙΚΟΙΝΩΝΙΩΝ ΤΗΣ ΕΛΛΑΔΟΣ ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ"
assert legal_name("Red Bull GmbH") == "Red Bull GmbH" and legal_name(None) == ""
print("OK")
