"""
Προαιρετική αναζήτηση επωνυμίας στο Μητρώο της ΑΑΔΕ (υπηρεσία RgWsPublic2 / GSIS).

Απαιτεί «ειδικούς κωδικούς πρόσβασης» που εκδίδονται από το myAADE
(Μητρώο & Επικοινωνία → Ειδικοί Κωδικοί για χρήση Web Services) - ΔΕΝ είναι
οι κωδικοί TAXISnet ούτε τα κλειδιά του myDATA. Ρυθμίζονται μόνο ανά εταιρεία
(Εταιρείες → καρτέλα «Μητρώο ΑΑΔΕ»). Αν λείπουν, η αναζήτηση απλώς παραλείπεται.
Χρησιμοποιείται ως fallback του VIES για ελληνικά ΑΦΜ.
"""

import re
from html import unescape
from xml.sax.saxutils import escape

import requests

ENDPOINT = "https://www1.gsis.gr/wsaade/RgWsPublic2/RgWsPublic2"

# SOAP 1.2 (application/soap+xml): με SOAP 1.1 / text/xml η υπηρεσία απαντά 415.
# Το INPUT_REC ανήκει στο namespace του Service, τα πεδία του στο RgWsPublic2.
_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:ns1="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
              xmlns:ns2="http://rgwspublic2/RgWsPublic2Service"
              xmlns:ns3="http://rgwspublic2/RgWsPublic2">
  <env:Header>
    <ns1:Security>
      <ns1:UsernameToken>
        <ns1:Username>{username}</ns1:Username>
        <ns1:Password>{password}</ns1:Password>
      </ns1:UsernameToken>
    </ns1:Security>
  </env:Header>
  <env:Body>
    <ns2:rgWsPublic2AfmMethod>
      <ns2:INPUT_REC>
        <ns3:afm_called_by/>
        <ns3:afm_called_for>{afm}</ns3:afm_called_for>
      </ns2:INPUT_REC>
    </ns2:rgWsPublic2AfmMethod>
  </env:Body>
</env:Envelope>"""


def credentials(company: dict | None = None) -> tuple[str, str] | None:
    """(username, password) της εταιρείας, αλλιώς None (υπηρεσία ανενεργή)."""
    c = company or {}
    user, pw = (c.get("GSIS_USERNAME") or "").strip(), c.get("GSIS_PASSWORD") or ""
    return (user, pw) if user and pw else None


def is_greek_afm(afm: str) -> bool:
    return bool(re.fullmatch(r"(?:EL|GR)?\d{9}", re.sub(r"\s", "", afm or ""), re.I))


def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}>(.*?)</(?:\w+:)?{name}>", xml, re.DOTALL)
    return unescape(m.group(1)).strip() if m else ""


# Ετικέτες των πεδίων του basic_rec· άγνωστο πεδίο εμφανίζεται με το όνομα του tag.
# None = δεν εμφανίζεται χωριστά (κωδικός που συνοδεύει την περιγραφή του).
LABELS = {
    "afm": "ΑΦΜ", "onomasia": "Επωνυμία", "commer_title": "Διακριτικός τίτλος",
    "legal_status_descr": "Νομική μορφή", "i_ni_flag_descr": "Φυσικό / μη φυσικό πρόσωπο",
    "firm_flag_descr": "Ιδιότητα", "deactivation_flag": None, "deactivation_flag_descr": "Κατάσταση",
    "doy": "Κωδικός ΔΟΥ", "doy_descr": "ΔΟΥ", "postal_address": "Οδός", "postal_address_no": "Αριθμός",
    "postal_zip_code": "Τ.Κ.", "postal_area_description": "Περιοχή", "regist_date": "Ημ/νία έναρξης",
    "stop_date": "Ημ/νία διακοπής", "normal_vat_system_flag": "Κανονικό καθεστώς ΦΠΑ",
}
_LEAF = re.compile(r"<(?:\w+:)?(\w+)(?:\s[^>]*)?>([^<]*)</(?:\w+:)?\1>")


def _blocks(xml: str, name: str) -> list[str]:
    """Το εσωτερικό κάθε <name>…</name> (με ή χωρίς prefix)."""
    return re.findall(rf"<(?:\w+:)?{name}>(.*?)</(?:\w+:)?{name}>", xml, re.DOTALL)


def _fields(xml: str, rec: str) -> list[tuple[str, str]]:
    """(tag, τιμή) για κάθε μη κενό πεδίο του rec, με τη σειρά της απάντησης."""
    return [(k, unescape(v).strip()) for k, v in _LEAF.findall("".join(_blocks(xml, rec)[:1])) if v.strip()]


def _display(tag: str, value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "/".join(value.split("-")[::-1])
    return {"Y": "Ναι", "N": "Όχι"}.get(value, value) if tag.endswith("_flag") else value


def lookup(afm: str, creds: tuple[str, str] | None, timeout: int = 15) -> dict | None:
    """{"name", "title", "street", "number", "postal_code", "city", "details", "activities"} για το ΑΦΜ·
    details = [(ετικέτα, τιμή)] όλων των πεδίων του basic_rec, activities = δραστηριότητες (ΚΑΔ)·
    {"error": μήνυμα} αν η υπηρεσία απάντησε με σφάλμα (λάθος κωδικοί, άκυρο ΑΦΜ κ.λπ.)·
    None χωρίς credentials / ΑΦΜ. Δεν σηκώνει exceptions - βοηθητική λειτουργία."""
    afm = re.sub(r"\D", "", afm or "")
    if not creds or len(afm) != 9:
        return None
    body = _ENVELOPE.format(username=escape(creds[0]), password=escape(creds[1]), afm=afm)
    try:
        resp = requests.post(
            ENDPOINT,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return {"error": f"Η υπηρεσία της ΑΑΔΕ δεν απάντησε ({type(e).__name__})."}
    if resp.status_code != 200:
        return {"error": f"Η υπηρεσία της ΑΑΔΕ απάντησε HTTP {resp.status_code}."}
    xml = resp.content.decode("utf-8", "replace")
    if code := _tag(xml, "error_code"):
        return {"error": f"{_tag(xml, 'error_descr') or 'Σφάλμα ΑΑΔΕ'} ({code})"}
    name = _tag(xml, "onomasia")
    if not name:
        return {"error": "Η ΑΑΔΕ δεν επέστρεψε επωνυμία για το ΑΦΜ."}
    return {
        "name": name,
        "title": _tag(xml, "commer_title"),
        "street": _tag(xml, "postal_address"),
        "number": _tag(xml, "postal_address_no"),
        "postal_code": _tag(xml, "postal_zip_code"),
        "city": _tag(xml, "postal_area_description"),
        "details": [(LABELS.get(k, k), _display(k, v)) for k, v in _fields(xml, "basic_rec") if LABELS.get(k, k)],
        "activities": [
            {k: _tag(item, k) for k in ("firm_act_code", "firm_act_descr", "firm_act_kind", "firm_act_kind_descr")}
            for item in _blocks("".join(_blocks(xml, "firm_act_tab")), "item")
        ],
    }


def lookup_name(afm: str, creds: tuple[str, str] | None, timeout: int = 15) -> str | None:
    """Επωνυμία (onomasia) για το ΑΦΜ, ή None."""
    r = lookup(afm, creds, timeout)
    return (r or {}).get("name")


if __name__ == "__main__":
    assert is_greek_afm("094019245") and is_greek_afm("EL094019245") and not is_greek_afm("DE123456789")
    assert lookup("094019245", None) is None and lookup("123", ("u", "p")) is None
    xml = "<a:onomasia>ΔΟΚΙΜΗ Α.Ε.</a:onomasia><postal_address>ΑΙΟΛΟΥ</postal_address><postal_zip_code>10559</postal_zip_code>"
    assert _tag(xml, "onomasia") == "ΔΟΚΙΜΗ Α.Ε." and _tag(xml, "postal_address_no") == ""
    assert _tag('<onomasia xsi:nil="true"/>', "onomasia") == ""  # κενά πεδία της απάντησης σφάλματος
    assert credentials({"GSIS_USERNAME": "u", "GSIS_PASSWORD": "p"}) == ("u", "p")
    xml = ("<basic_rec><afm>094019245</afm><doy xsi:nil=\"true\"/><onomasia>Α &amp; Β ΑΕ</onomasia>"
           "<deactivation_flag>1</deactivation_flag><regist_date>2001-03-05</regist_date>"
           "<normal_vat_system_flag>Y</normal_vat_system_flag><new_field>Χ</new_field></basic_rec>"
           "<firm_act_tab><item><firm_act_code>62010000</firm_act_code><firm_act_kind>1</firm_act_kind></item>"
           "<item><firm_act_code>63110000</firm_act_code></item></firm_act_tab>")
    det = [(LABELS.get(k, k), _display(k, v)) for k, v in _fields(xml, "basic_rec") if LABELS.get(k, k)]
    assert det == [("ΑΦΜ", "094019245"), ("Επωνυμία", "Α & Β ΑΕ"), ("Ημ/νία έναρξης", "05/03/2001"),
                   ("Κανονικό καθεστώς ΦΠΑ", "Ναι"), ("new_field", "Χ")], det
    assert [_tag(i, "firm_act_code") for i in _blocks("".join(_blocks(xml, "firm_act_tab")), "item")] == ["62010000", "63110000"]
    print("OK")
