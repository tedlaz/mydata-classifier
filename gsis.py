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

_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"
              xmlns:ns1="http://rgwspublic2/RgWsPublic2Service"
              xmlns:ns2="http://rgwspublic2/RgWsPublic2"
              xmlns:ns3="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
  <env:Header>
    <ns3:Security>
      <ns3:UsernameToken>
        <ns3:Username>{username}</ns3:Username>
        <ns3:Password>{password}</ns3:Password>
      </ns3:UsernameToken>
    </ns3:Security>
  </env:Header>
  <env:Body>
    <ns1:rgWsPublic2AfmMethod>
      <ns2:INPUT_REC>
        <ns2:afm_called_by/>
        <ns2:afm_called_for>{afm}</ns2:afm_called_for>
      </ns2:INPUT_REC>
    </ns1:rgWsPublic2AfmMethod>
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


def lookup(afm: str, creds: tuple[str, str] | None, timeout: int = 15) -> dict | None:
    """{"name", "street", "number", "postal_code", "city"} για το ΑΦΜ, ή None αν δεν βρεθεί /
    δεν υπάρχουν credentials / αποτύχει η κλήση. Δεν σηκώνει exceptions - βοηθητική λειτουργία."""
    afm = re.sub(r"\D", "", afm or "")
    if not creds or len(afm) != 9:
        return None
    body = _ENVELOPE.format(username=escape(creds[0]), password=escape(creds[1]), afm=afm)
    try:
        resp = requests.post(
            ENDPOINT,
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    name = _tag(resp.text, "onomasia")
    if not name:
        return None
    return {
        "name": name,
        "street": _tag(resp.text, "postal_address"),
        "number": _tag(resp.text, "postal_address_no"),
        "postal_code": _tag(resp.text, "postal_zip_code"),
        "city": _tag(resp.text, "postal_area_description"),
    }


def lookup_name(afm: str, creds: tuple[str, str] | None, timeout: int = 15) -> str | None:
    """Επωνυμία (onomasia) για το ΑΦΜ, ή None."""
    r = lookup(afm, creds, timeout)
    return r["name"] if r else None


if __name__ == "__main__":
    assert is_greek_afm("094019245") and is_greek_afm("EL094019245") and not is_greek_afm("DE123456789")
    assert lookup("094019245", None) is None and lookup("123", ("u", "p")) is None
    xml = "<a:onomasia>ΔΟΚΙΜΗ Α.Ε.</a:onomasia><postal_address>ΑΙΟΛΟΥ</postal_address><postal_zip_code>10559</postal_zip_code>"
    assert _tag(xml, "onomasia") == "ΔΟΚΙΜΗ Α.Ε." and _tag(xml, "postal_address_no") == ""
    assert credentials({"GSIS_USERNAME": "u", "GSIS_PASSWORD": "p"}) == ("u", "p")
    print("OK")
