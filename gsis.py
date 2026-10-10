"""
Προαιρετική αναζήτηση επωνυμίας στο Μητρώο της ΑΑΔΕ (υπηρεσία RgWsPublic2 / GSIS).

Απαιτεί «ειδικούς κωδικούς πρόσβασης» που εκδίδονται από το myAADE
(Μητρώο & Επικοινωνία → Ειδικοί Κωδικοί για χρήση Web Services) - ΔΕΝ είναι
οι κωδικοί TAXISnet ούτε τα κλειδιά του myDATA. Ρυθμίζονται στο .env ως
GSIS_USERNAME / GSIS_PASSWORD. Αν λείπουν, η αναζήτηση απλώς παραλείπεται.
"""

import os
import re

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


def gsis_available() -> bool:
    return bool(os.getenv("GSIS_USERNAME") and os.getenv("GSIS_PASSWORD"))


def lookup_name(afm: str, timeout: int = 15) -> str | None:
    """
    Επιστρέφει την επωνυμία (onomasia) για το ΑΦΜ, ή None αν δεν βρεθεί /
    δεν υπάρχουν credentials / αποτύχει η κλήση. Δεν σηκώνει exceptions -
    η εμφάνιση επωνυμίας είναι βοηθητική, όχι κρίσιμη λειτουργία.
    """
    if not gsis_available() or not afm:
        return None
    body = _ENVELOPE.format(
        username=os.getenv("GSIS_USERNAME"),
        password=os.getenv("GSIS_PASSWORD"),
        afm=re.sub(r"\D", "", afm),
    )
    try:
        resp = requests.post(
            ENDPOINT,
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        m = re.search(
            r"<(?:\w+:)?onomasia>(.*?)</(?:\w+:)?onomasia>", resp.text, re.DOTALL
        )
        if m:
            name = m.group(1).strip()
            return name or None
    except requests.RequestException:
        pass
    return None
