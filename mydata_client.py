"""
myDATA API client (AADE) - ERP interface v2.0.1
Υλοποιεί: RequestDocs (λήψη παραστατικών εξόδων) και SendExpensesClassification.

Τεκμηρίωση: https://www.aade.gr/mydata/tehnikes-prodiagrafes-ekdoseis-mydata
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

# Base URLs
PROD_URL = "https://mydatapi.aade.gr/myDATA/"
DEV_URL = "https://mydataapidev.aade.gr/"

# Namespaces που χρησιμοποιεί το myDATA στα responses
NS = {
    "req": "http://www.aade.gr/myDATA/RequestedDoc/v1.0",
    "inv": "http://www.aade.gr/myDATA/invoice/v1.0",
    "ecls": "https://www.aade.gr/myDATA/expensesClassificaton/v1.0",
    "icls": "https://www.aade.gr/myDATA/incomeClassificaton/v1.0",
}

ECLS_NS = "https://www.aade.gr/myDATA/expensesClassificaton/v1.0"

# Τύποι παραστατικών διακίνησης - δεν χαρακτηρίζονται, εξαιρούνται από τη λίστα
MOVEMENT_INVOICE_TYPES = {"9.3"}  # Δελτίο Αποστολής

# Τύποι παραστατικών εξόδων που διαβιβάζει Ο ΙΔΙΟΣ ο λήπτης (αυτοτιμολόγηση):
# λιανικής ημεδαπής/αλλοδαπής, αλλοδαπής, ενοίκιο, μισθοδοσία, αποσβέσεις, τακτοποίηση.
# Επιστρέφονται από την RequestTransmittedDocs, όχι από την RequestDocs.
SELF_EXPENSE_DOC_TYPES = {
    "13.1",
    "13.2",
    "13.3",
    "13.4",
    "13.30",
    "13.31",
    "14.1",
    "14.2",
    "14.3",
    "14.4",
    "14.5",
    "14.30",
    "14.31",
    "15.1",
    "16.1",
    "17.1",
    "17.2",
    "17.5",
    "17.6",
}


def _local(tag: str) -> str:
    """Επιστρέφει το local name ενός tag (χωρίς namespace)."""
    return tag.split("}")[-1]


class MyDataError(Exception):
    pass


@dataclass
class InvoiceLine:
    line_number: int
    net_value: float
    vat_amount: float
    vat_category: str | None
    has_expenses_classification: bool
    classifications: list[dict] = field(
        default_factory=list
    )  # [{"type","category","amount"}]


@dataclass
class ExpenseInvoice:
    mark: str
    uid: str | None
    issuer_vat: str | None
    issuer_name: str | None
    invoice_type: str | None
    series: str | None
    aa: str | None
    issue_date: str | None
    total_net: float | None
    total_vat: float | None
    total_gross: float | None
    lines: list[InvoiceLine] = field(default_factory=list)
    has_summary_classification: bool = (
        False  # συγκεντρωτικός χαρακτηρισμός στο invoiceSummary
    )
    cls_info: list[dict] = field(
        default_factory=list
    )  # συγκεντρωμένοι χαρακτηρισμοί για εμφάνιση
    # Αντισυμβαλλόμενος (πελάτης) — γεμίζει μόνο για παραστατικά ΕΣΟΔΩΝ που
    # εκδίδουμε εμείς (στα έξοδα ο αντισυμβαλλόμενος είναι ο εκδότης/issuer).
    counterpart_vat: str | None = None
    counterpart_name: str | None = None
    # Ενσωματωμένος χαρακτηρισμός ΕΣΟΔΩΝ (incomeClassification σε γραμμή/σύνοψη).
    has_income_line_classification: bool = False
    # MARK ακυρωτικής εγγραφής (cancelledByMark) — αν υπάρχει, το παραστατικό ακυρώθηκε.
    cancelled_by_mark: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return bool(self.cancelled_by_mark)
    # Πεδία εμφάνισης: γεμίζουν από το τοπικό ledger (db) ή, για παραστατικά ήδη
    # χαρακτηρισμένα στο myDATA, από το classificationMark της υποβολής.
    classification_mark: str = ""
    local_action: str = "classify"
    source: str = "rest"

    @property
    def has_embedded_classification(self) -> bool:
        """Χαρακτηρισμός ενσωματωμένος στο ίδιο το παραστατικό (γραμμή ή σύνοψη)."""
        return self.has_summary_classification or any(
            l.has_expenses_classification for l in self.lines
        )

    @property
    def is_movement_doc(self) -> bool:
        """Παραστατικό διακίνησης (π.χ. 9.3 Δελτίο Αποστολής) - δεν χαρακτηρίζεται."""
        return (self.invoice_type or "") in MOVEMENT_INVOICE_TYPES

    @property
    def is_self_issued(self) -> bool:
        """Αυτοτιμολογούμενο παραστατικό εξόδου (13.x/14.x/15.1/16.1/17.x).
        Μόνο αυτά μπορεί να ακυρώσει ο χρήστης - τα υπόλοιπα τα ακυρώνει ο εκδότης τους."""
        return (self.invoice_type or "") in SELF_EXPENSE_DOC_TYPES


class MyDataClient:
    def __init__(
        self,
        user_id: str,
        subscription_key: str,
        environment: str = "dev",
        entity_vat: str = "",
    ):
        self.base_url = PROD_URL if environment == "prod" else DEV_URL
        # Όταν καλεί λογιστής/εκπρόσωπος για λογαριασμό πελάτη, το ΑΦΜ του
        # εντολέα στέλνεται ως entityVatNumber σε κάθε κλήση.
        self.entity_vat = (entity_vat or "").strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "aade-user-id": user_id,
                "Ocp-Apim-Subscription-Key": subscription_key,
            }
        )

    def _params(self, extra: dict | None = None) -> dict:
        """Base query params + entityVatNumber (αν υπάρχει) + ό,τι δώσει η κλήση."""
        p = {}
        if self.entity_vat:
            p["entityVatNumber"] = self.entity_vat
        if extra:
            p.update(extra)
        return p

    # ------------------------------------------------------------------ #
    # RequestDocs: επιστρέφει παραστατικά που διαβίβασαν ΑΛΛΟΙ και μας
    # αφορούν ως λήπτες (δηλ. τα έξοδά μας).
    # ------------------------------------------------------------------ #
    def request_docs(
        self, date_from: str, date_to: str
    ) -> tuple[list[ExpenseInvoice], dict[str, dict], set[str]]:
        """
        date_from / date_to σε μορφή dd/MM/yyyy. Χειρίζεται pagination με continuationToken.

        Επιστρέφει (invoices, cls_map, cancelled_marks):
        - cls_map: MARK -> {"entries": χαρακτηρισμοί από το expensesClassificationsDoc
          (ανά γραμμή, ή απόρριψη/απόκλιση μέσω transactionMode),
          "cls_mark": το MARK χαρακτηρισμού της υποβολής}.
        - cancelled_marks: MARK παραστατικών που έχουν ακυρωθεί (cancelledInvoicesDoc).
        """
        invoices: list[ExpenseInvoice] = []
        cls_map: dict[str, dict] = {}
        cancelled_marks: set[str] = set()
        params = self._params({"mark": "0", "dateFrom": date_from, "dateTo": date_to})

        while True:
            resp = self.session.get(
                self.base_url + "RequestDocs", params=params, timeout=60
            )
            if resp.status_code == 401:
                raise MyDataError(
                    "Μη έγκυρα credentials (401). Έλεγξε aade-user-id / subscription key."
                )
            if resp.status_code != 200:
                raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")

            root = ET.fromstring(resp.content)
            invoices.extend(self._parse_requested_doc(root))
            cls_map.update(self._parse_expenses_classifications(root))
            cancelled_marks |= self._parse_cancelled_invoices(root)

            # pagination
            npk = root.findtext(
                ".//req:continuationToken/req:nextPartitionKey", namespaces=NS
            )
            nrk = root.findtext(
                ".//req:continuationToken/req:nextRowKey", namespaces=NS
            )
            if npk and nrk:
                params["nextPartitionKey"] = npk
                params["nextRowKey"] = nrk
            else:
                break

        return invoices, cls_map, cancelled_marks

    def request_transmitted(self, date_from: str, date_to: str):
        """
        Στοιχεία από τις διαβιβάσεις ΤΟΥ ΙΔΙΟΥ του χρήστη (RequestTransmittedDocs):

        Επιστρέφει (self_invoices, cls_map, cancelled_marks):
        - self_invoices: αυτοτιμολογούμενα παραστατικά ΕΞΟΔΩΝ που διαβίβασε ο ίδιος
          (τύποι 13.x, 14.x, 15.1, 16.1, 17.x - π.χ. μισθοδοσία). Αυτά ΔΕΝ
          επιστρέφονται από την RequestDocs, γιατί εκδότης είναι ο ίδιος ο χρήστης.
        - cls_map: MARK -> {"entries", "cls_mark"} για τους χαρακτηρισμούς που
          υπέβαλε ο χρήστης μέσω SendExpensesClassification (ανά γραμμή,
          transactionMode) μαζί με το MARK χαρακτηρισμού.
        - cancelled_marks: ακυρωμένες δικές του διαβιβάσεις.

        Το date_to εδώ πρέπει να φτάνει μέχρι σήμερα, γιατί ο χαρακτηρισμός μπορεί
        να υποβλήθηκε αργότερα από την ημερομηνία έκδοσης του παραστατικού.
        """
        self_invoices: list[ExpenseInvoice] = []
        cls_map: dict[str, dict] = {}
        cancelled_marks: set[str] = set()
        params = self._params({"mark": "0", "dateFrom": date_from, "dateTo": date_to})

        while True:
            resp = self.session.get(
                self.base_url + "RequestTransmittedDocs", params=params, timeout=60
            )
            if resp.status_code == 401:
                raise MyDataError(
                    "Μη έγκυρα credentials (401). Έλεγξε aade-user-id / subscription key."
                )
            if resp.status_code != 200:
                raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")

            root = ET.fromstring(resp.content)
            for inv in self._parse_requested_doc(root):
                if (inv.invoice_type or "") in SELF_EXPENSE_DOC_TYPES:
                    self_invoices.append(inv)
            cls_map.update(self._parse_expenses_classifications(root))
            cancelled_marks |= self._parse_cancelled_invoices(root)

            npk = root.findtext(
                ".//req:continuationToken/req:nextPartitionKey", namespaces=NS
            )
            nrk = root.findtext(
                ".//req:continuationToken/req:nextRowKey", namespaces=NS
            )
            if npk and nrk:
                params["nextPartitionKey"] = npk
                params["nextRowKey"] = nrk
            else:
                break

        return self_invoices, cls_map, cancelled_marks

    def request_unclassified_expenses(
        self, date_from: str, date_to: str
    ) -> list[ExpenseInvoice]:
        """
        Παραστατικά εξόδων ΧΩΡΙΣ κανέναν τρόπο χαρακτηρισμού:
        - ούτε ενσωματωμένο σε γραμμή ή στη σύνοψη του παραστατικού
        - ούτε ξεχωριστή υποβολή μέσω SendExpensesClassification
          (ανά γραμμή, ανά παραστατικό, ή απόρριψη/απόκλιση - transactionMode),
          είτε αυτή εμφανίζεται στην RequestDocs είτε στις δικές μας διαβιβάσεις
          (RequestTransmittedDocs, με διάστημα μέχρι σήμερα)
        Εξαιρούνται ακυρωμένα παραστατικά και παραστατικά διακίνησης (9.3).
        """
        invoices, cls_map, cancelled_marks = self._fetch_all(date_from, date_to)
        return [
            i
            for i in invoices
            if not i.is_movement_doc
            and i.mark not in cancelled_marks
            and i.mark not in cls_map
            and not i.has_embedded_classification
        ]

    def request_classified_expenses(
        self, date_from: str, date_to: str
    ) -> list[ExpenseInvoice]:
        """
        Παραστατικά εξόδων που ΕΧΟΥΝ χαρακτηριστεί με οποιονδήποτε τρόπο.
        Στο cls_info κάθε παραστατικού συγκεντρώνονται οι χαρακτηρισμοί του
        (από γραμμές, από ξεχωριστές υποβολές, ή απόρριψη/απόκλιση) για εμφάνιση.
        Εξαιρούνται ακυρωμένα και παραστατικά διακίνησης (9.3).
        """
        invoices, cls_map, cancelled_marks = self._fetch_all(date_from, date_to)
        result = []
        for i in invoices:
            if i.is_movement_doc or i.mark in cancelled_marks:
                continue
            if i.mark not in cls_map and not i.has_embedded_classification:
                continue
            # Τελευταίος ενεργός χαρακτηρισμός: αν υπάρχει ξεχωριστή υποβολή
            # (SendExpensesClassification), υπερισχύει του αρχικού ενσωματωμένου.
            if i.mark in cls_map:
                i.cls_info = cls_map[i.mark]["entries"]
                # MARK χαρακτηρισμού: υπάρχει μόνο για ξεχωριστές υποβολές
                # (SendExpensesClassification) — όχι για ενσωματωμένο χαρακτηρισμό.
                i.classification_mark = cls_map[i.mark]["cls_mark"]
            else:
                info: list[dict] = []
                for line in i.lines:
                    info.extend(line.classifications)
                i.cls_info = info
            result.append(i)
        return result

    def request_income(
        self, date_from: str, date_to: str
    ) -> tuple[list[ExpenseInvoice], list[ExpenseInvoice], set[str]]:
        """
        Παραστατικά ΕΣΟΔΩΝ, δηλαδή αυτά που έχει διαβιβάσει Ο ΙΔΙΟΣ ο χρήστης ως
        εκδότης (RequestTransmittedDocs), εξαιρώντας τα αυτοτιμολογούμενα ΕΞΟΔΑ
        (13.x/14.x/15.1/16.1/17.x) και τα παραστατικά διακίνησης (9.3).

        Επιστρέφει (unclassified, classified, cancelled_marks): «χαρακτηρισμένο»
        θεωρείται όποιο έχει ενσωματωμένο χαρακτηρισμό εσόδων (incomeClassification
        σε γραμμή/σύνοψη) ή ξεχωριστή υποβολή SendIncomeClassification. Στα
        classified το cls_info γεμίζει με τους χαρακτηρισμούς για εμφάνιση. Τα
        cancelled_marks (ακυρωμένα) εξαιρούνται από τις λίστες και επιστρέφονται
        ώστε ο caller να τα αφαιρέσει και από το τοπικό βιβλίο. date_to έως σήμερα.
        """
        invoices: list[ExpenseInvoice] = []
        cls_map: dict[str, list[dict]] = {}
        cancelled_marks: set[str] = set()
        params = self._params({"mark": "0", "dateFrom": date_from, "dateTo": date_to})

        while True:
            resp = self.session.get(
                self.base_url + "RequestTransmittedDocs", params=params, timeout=60
            )
            if resp.status_code == 401:
                raise MyDataError(
                    "Μη έγκυρα credentials (401). Έλεγξε aade-user-id / subscription key."
                )
            if resp.status_code != 200:
                raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")

            root = ET.fromstring(resp.content)
            for inv in self._parse_requested_doc(root):
                itype = inv.invoice_type or ""
                if itype in SELF_EXPENSE_DOC_TYPES or inv.is_movement_doc:
                    continue  # αυτοτιμολογούμενα έξοδα / διακίνηση — όχι έσοδα
                # Ακυρωμένο (cancelledByMark στο ίδιο το παραστατικό): κράτησέ το
                # στα cancelled ώστε να εξαιρεθεί/αφαιρεθεί, όχι στη λίστα εσόδων.
                if inv.is_cancelled:
                    cancelled_marks.add(inv.mark)
                    continue
                invoices.append(inv)
            cls_map.update(self._parse_income_classifications(root))
            cancelled_marks |= self._parse_cancelled_invoices(root)

            npk = root.findtext(
                ".//req:continuationToken/req:nextPartitionKey", namespaces=NS
            )
            nrk = root.findtext(
                ".//req:continuationToken/req:nextRowKey", namespaces=NS
            )
            if npk and nrk:
                params["nextPartitionKey"] = npk
                params["nextRowKey"] = nrk
            else:
                break

        unclassified: list[ExpenseInvoice] = []
        classified: list[ExpenseInvoice] = []
        for i in invoices:
            if i.mark in cancelled_marks:
                continue
            if i.mark in cls_map:
                i.cls_info = cls_map[i.mark]
                classified.append(i)
            elif i.has_income_line_classification:
                info: list[dict] = []
                for line in i.lines:
                    info.extend(line.classifications)
                i.cls_info = info
                classified.append(i)
            else:
                unclassified.append(i)
        return unclassified, classified, cancelled_marks

    @staticmethod
    def _parse_income_classifications(root: ET.Element) -> dict[str, list[dict]]:
        """
        Χάρτης MARK -> λίστα χαρακτηρισμών εσόδων από το incomeClassificationsDoc
        (ξεχωριστές υποβολές SendIncomeClassification). Δομή ανάλογη των εξόδων:
        incomeInvoiceClassification → invoicesIncomeClassificationDetails →
        incomeClassificationDetailData. Νεότερη υποβολή αντικαθιστά την παλιά.
        """
        result: dict[str, list[dict]] = {}
        for el in root.iter():
            if _local(el.tag) != "incomeClassificationsDoc":
                continue
            for cls in el:
                if _local(cls.tag) != "incomeInvoiceClassification":
                    continue
                mark = None
                entries: list[dict] = []
                for child in cls:
                    tag = _local(child.tag)
                    if tag == "invoiceMark" and child.text:
                        mark = child.text.strip()
                    elif tag == "invoicesIncomeClassificationDetails":
                        line_no = None
                        for node in child:
                            t = _local(node.tag)
                            if t == "lineNumber" and node.text:
                                try:
                                    line_no = int(node.text)
                                except ValueError:
                                    line_no = None
                            elif t == "incomeClassificationDetailData":
                                entry: dict = {"line": line_no}
                                for ch in node:
                                    tt = _local(ch.tag)
                                    if tt == "classificationType":
                                        entry["type"] = ch.text
                                    elif tt == "classificationCategory":
                                        entry["category"] = ch.text
                                    elif tt == "amount":
                                        entry["amount"] = _f(ch.text)
                                if entry.get("type") or entry.get("category"):
                                    entries.append(entry)
                if mark:
                    result[mark] = entries
        return result

    def _fetch_all(self, date_from: str, date_to: str):
        """
        Κοινός κορμός: RequestDocs (παραστατικά τρίτων) + RequestTransmittedDocs
        έως σήμερα (δικοί μας χαρακτηρισμοί + αυτοτιμολογούμενα έξοδα π.χ. 17.1).
        """

        from datetime import date as _date
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        def _gr_date(s: str) -> _date:
            """Καθαρή ημερολογιακή ημερομηνία από 'dd/mm/yyyy' (χωρίς ζώνη/ώρα)."""
            d, m, y = (int(x) for x in s.split("/"))
            return _date(y, m, d)

        invoices, cls_map, cancelled_marks = self.request_docs(date_from, date_to)

        # Διαβιβάσεις του ίδιου του χρήστη: αναζήτηση από την αρχή του διαστήματος
        # μέχρι ΣΗΜΕΡΑ (ο χαρακτηρισμός γίνεται μεταγενέστερα της έκδοσης).
        # Ώρα Ελλάδας ώστε το «σήμερα» να μην εξαρτάται από τη ζώνη του server.
        today = _dt.now(ZoneInfo("Europe/Athens")).strftime("%d/%m/%Y")
        self_invoices, t_cls_map, t_cancelled = self.request_transmitted(
            date_from, today
        )

        cls_map.update(t_cls_map)  # οι δικές μας υποβολές υπερισχύουν
        cancelled_marks |= t_cancelled

        # Τα αυτοτιμολογούμενα φιλτράρονται στο ζητούμενο διάστημα με βάση την
        # ημερομηνία έκδοσης (η κλήση έγινε έως σήμερα) και συγχωνεύονται χωρίς διπλά.
        d_to = _gr_date(date_to)
        d_from = _gr_date(date_from)
        seen = {i.mark for i in invoices}
        for inv in self_invoices:
            if inv.mark in seen:
                continue
            try:
                d = _date.fromisoformat((inv.issue_date or "")[:10])
                if not (d_from <= d <= d_to):
                    continue
            except ValueError:
                pass  # χωρίς αναγνώσιμη ημερομηνία: το κρατάμε, ας το κρίνει ο χρήστης
            invoices.append(inv)
            seen.add(inv.mark)

        return invoices, cls_map, cancelled_marks

    @staticmethod
    def _parse_expenses_classifications(root: ET.Element) -> dict[str, dict]:
        """
        Χάρτης MARK -> {"entries": [...], "cls_mark": <MARK χαρακτηρισμού>} από το
        expensesClassificationsDoc.
        Κάθε στοιχείο του entries: {"type", "category", "amount", "line": <αρ.γραμμής>|None}
        ή {"transaction_mode": "1"/"2"} (1=Απόρριψη, 2=Απόκλιση).

        - Νεότερη υποβολή για το ίδιο MARK αντικαθιστά την παλαιότερη.
        - Το transactionMode κρατιέται ΜΟΝΟ όταν η υποβολή δεν έχει αναλυτικά
          στοιχεία (σκέτη απόρριψη/απόκλιση) - αλλιώς είναι θόρυβος.
        Parsing με local names για ανοχή σε namespaces.
        """
        result: dict[str, dict] = {}
        for el in root.iter():
            if _local(el.tag) != "expensesClassificationsDoc":
                continue
            for cls in el:
                if _local(cls.tag) != "expensesInvoiceClassification":
                    continue
                mark = None
                cls_mark = ""
                entries: list[dict] = []
                tmode = None
                for child in cls:
                    tag = _local(child.tag)
                    if tag == "invoiceMark" and child.text:
                        mark = child.text.strip()
                    elif tag == "classificationMark" and child.text:
                        cls_mark = child.text.strip()
                    elif tag == "transactionMode" and child.text:
                        tmode = child.text.strip()
                    elif tag == "invoicesExpensesClassificationDetails":
                        line_no = None
                        for node in child:
                            t = _local(node.tag)
                            if t == "lineNumber" and node.text:
                                try:
                                    line_no = int(node.text)
                                except ValueError:
                                    line_no = None
                            elif t == "expensesClassificationDetailData":
                                entry: dict = {"line": line_no}
                                for ch in node:
                                    tt = _local(ch.tag)
                                    if tt == "classificationType":
                                        entry["type"] = ch.text
                                    elif tt == "classificationCategory":
                                        entry["category"] = ch.text
                                    elif tt == "amount":
                                        entry["amount"] = _f(ch.text)
                                if entry.get("type") or entry.get("category"):
                                    entries.append(entry)
                # transactionMode μόνο αν ΔΕΝ υπάρχουν αναλυτικά στοιχεία
                if tmode and not entries:
                    entries.append({"transaction_mode": tmode})
                if mark:
                    # νεότερη υποβολή για το ίδιο MARK αντικαθιστά την παλαιότερη
                    result[mark] = {"entries": entries, "cls_mark": cls_mark}
        return result

    @staticmethod
    def _parse_cancelled_invoices(root: ET.Element) -> set[str]:
        """MARK ακυρωμένων παραστατικών από το cancelledInvoicesDoc."""
        marks: set[str] = set()
        for el in root.iter():
            if _local(el.tag) == "cancelledInvoicesDoc":
                for cd in el.iter():
                    if _local(cd.tag) == "invoiceMark" and cd.text:
                        marks.add(cd.text.strip())
        return marks

    def _parse_requested_doc(self, root: ET.Element) -> list[ExpenseInvoice]:
        result = []
        for inv in root.iter(f"{{{NS['inv']}}}invoice"):
            mark = inv.findtext("inv:mark", namespaces=NS) or ""
            uid = inv.findtext("inv:uid", namespaces=NS)
            # Ακυρωμένο παραστατικό: φέρει το MARK της ακυρωτικής εγγραφής
            # (cancelledByMark). Ανοχή σε namespace με local-name αναζήτηση.
            cancelled_by = None
            for ch in inv:
                if _local(ch.tag) == "cancelledByMark" and ch.text and ch.text.strip():
                    cancelled_by = ch.text.strip()
                    break

            issuer = inv.find("inv:issuer", namespaces=NS)
            issuer_vat = (
                issuer.findtext("inv:vatNumber", namespaces=NS)
                if issuer is not None
                else None
            )
            issuer_name = (
                issuer.findtext("inv:name", namespaces=NS)
                if issuer is not None
                else None
            )

            # Αντισυμβαλλόμενος (πελάτης) — υπάρχει στα δικά μας παραστατικά εσόδων.
            counterpart = inv.find("inv:counterpart", namespaces=NS)
            counterpart_vat = (
                counterpart.findtext("inv:vatNumber", namespaces=NS)
                if counterpart is not None
                else None
            )
            counterpart_name = (
                counterpart.findtext("inv:name", namespaces=NS)
                if counterpart is not None
                else None
            )

            header = inv.find("inv:invoiceHeader", namespaces=NS)
            inv_type = series = aa = issue_date = None
            if header is not None:
                inv_type = header.findtext("inv:invoiceType", namespaces=NS)
                series = header.findtext("inv:series", namespaces=NS)
                aa = header.findtext("inv:aa", namespaces=NS)
                issue_date = header.findtext("inv:issueDate", namespaces=NS)

            summary = inv.find("inv:invoiceSummary", namespaces=NS)
            total_net = total_vat = total_gross = None
            has_summary_cls = False
            has_income_cls = False
            if summary is not None:
                total_net = _f(summary.findtext("inv:totalNetValue", namespaces=NS))
                total_vat = _f(summary.findtext("inv:totalVatAmount", namespaces=NS))
                total_gross = _f(summary.findtext("inv:totalGrossValue", namespaces=NS))
                has_summary_cls = any(
                    _local(ch.tag) == "expensesClassification" for ch in summary
                )
                has_income_cls = any(
                    _local(ch.tag) == "incomeClassification" for ch in summary
                )

            lines = []
            for det in inv.findall("inv:invoiceDetails", namespaces=NS):
                # ανοχή σε namespace: το expensesClassification μπορεί να έρθει
                # σε inv ή ecls namespace ανάλογα με την έκδοση
                line_cls = []
                for ch in det:
                    if _local(ch.tag) != "expensesClassification":
                        continue
                    entry = {}
                    for node in ch:
                        t = _local(node.tag)
                        if t == "classificationType":
                            entry["type"] = node.text
                        elif t == "classificationCategory":
                            entry["category"] = node.text
                        elif t == "amount":
                            entry["amount"] = _f(node.text)
                    if entry:
                        line_cls.append(entry)
                has_ecls = bool(line_cls)
                # Ενσωματωμένος χαρακτηρισμός εσόδων στη γραμμή (incomeClassification).
                if any(_local(ch.tag) == "incomeClassification" for ch in det):
                    has_income_cls = True
                lines.append(
                    InvoiceLine(
                        line_number=int(
                            det.findtext("inv:lineNumber", default="1", namespaces=NS)
                        ),
                        net_value=_f(det.findtext("inv:netValue", namespaces=NS))
                        or 0.0,
                        vat_amount=_f(det.findtext("inv:vatAmount", namespaces=NS))
                        or 0.0,
                        vat_category=det.findtext("inv:vatCategory", namespaces=NS),
                        has_expenses_classification=has_ecls,
                        classifications=line_cls,
                    )
                )

            result.append(
                ExpenseInvoice(
                    mark=mark,
                    uid=uid,
                    issuer_vat=issuer_vat,
                    issuer_name=issuer_name,
                    invoice_type=inv_type,
                    series=series,
                    aa=aa,
                    issue_date=issue_date,
                    total_net=total_net,
                    total_vat=total_vat,
                    total_gross=total_gross,
                    lines=lines,
                    has_summary_classification=has_summary_cls,
                    counterpart_vat=counterpart_vat,
                    counterpart_name=counterpart_name,
                    has_income_line_classification=has_income_cls,
                    cancelled_by_mark=cancelled_by,
                )
            )
        return result

    # ------------------------------------------------------------------ #
    # SendExpensesClassification
    # classifications: λίστα από dicts:
    #   {"line_number": 1, "classification_type": "E3_102_001",
    #    "classification_category": "category2_1", "amount": 100.0}
    # Αν classification_category == "" στέλνεται μόνο type (π.χ. VAT_361).
    # ------------------------------------------------------------------ #
    def send_expenses_classification(
        self,
        invoice_mark: str,
        classifications: list[dict],
        post_per_invoice: bool = False,
    ) -> dict:
        xml_payload = build_expenses_classification_xml(
            invoice_mark, classifications, post_per_invoice, entity_vat=self.entity_vat
        )
        resp = self.session.post(
            self.base_url + "SendExpensesClassification",
            data=xml_payload.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            params=self._params(),
            timeout=60,
        )
        if resp.status_code != 200:
            raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return parse_response_doc(resp.content)

    def send_self_expense_invoice(
        self,
        invoice_type: str,
        series: str,
        aa: str,
        issue_date: str,
        lines: list[dict],
        issuer_vat: str = "",
        issuer_country: str = "GR",
        own_vat: str = "",
        payment_method: str = "5",
    ) -> dict:
        """
        Δημιουργία + διαβίβαση + χαρακτηρισμός σε ένα βήμα: αυτοτιμολογούμενη
        εγγραφή εξόδου (17.1 Μισθοδοσία, 17.2 Αποσβέσεις, 17.5/17.6 τακτοποίηση)
        μέσω SendInvoices, με ενσωματωμένους χαρακτηρισμούς.
        """
        rules = type_rules(invoice_type)
        xml_payload = build_self_expense_invoice_xml(
            invoice_type,
            series,
            aa,
            issue_date,
            lines,
            issuer_vat=issuer_vat,
            issuer_country=issuer_country,
            counterpart_vat=own_vat if rules["counterpart"] else "",
            include_payment=rules["payment"],
            payment_method=payment_method,
        )
        resp = self.session.post(
            self.base_url + "SendInvoices",
            data=xml_payload.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            params=self._params(),
            timeout=60,
        )
        if resp.status_code != 200:
            raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return parse_response_doc(resp.content)

    def cancel_invoice(self, mark: str) -> dict:
        """
        Ακύρωση παραστατικού που έχει διαβιβάσει Ο ΙΔΙΟΣ ο χρήστης (CancelInvoice).
        Ισχύει μόνο για δικές του διαβιβάσεις (π.χ. 17.1 μισθοδοσία) - παραστατικά
        τρίτων ακυρώνονται μόνο από τον εκδότη τους.
        """
        resp = self.session.post(
            self.base_url + "CancelInvoice",
            params=self._params({"mark": str(mark)}),
            timeout=60,
        )
        if resp.status_code != 200:
            raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return parse_response_doc(resp.content)

    def reject_invoice(self, mark: str) -> dict:
        """
        Απόρριψη παραστατικού τρίτου: χαρακτηρισμός με transactionMode=1.
        Χρησιμοποιείται όταν το παραστατικό δεν αφορά τον λήπτη ή είναι λανθασμένο.
        """
        xml_payload = build_expenses_classification_xml(
            mark, [], transaction_mode="1", entity_vat=self.entity_vat
        )
        resp = self.session.post(
            self.base_url + "SendExpensesClassification",
            data=xml_payload.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            params=self._params(),
            timeout=60,
        )
        if resp.status_code != 200:
            raise MyDataError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return parse_response_doc(resp.content)


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


INV_NS = "http://www.aade.gr/myDATA/invoice/v1.0"

# Τύποι αυτοτιμολογούμενων εγγραφών εξόδων (διαβιβάζονται από τον ίδιο τον λήπτη).
# Στους 13.x/14.x/15.1/16.1 ο εκδότης είναι ΤΡΙΤΟΣ μη υπόχρεος (π.χ. ΕΦΚΑ,
# αλλοδαπός προμηθευτής, ιδιώτης εκμισθωτής) - στους 17.x ο ίδιος ο υπόχρεος.
SELF_EXPENSE_TYPES = {
    "13.1": "Έξοδα - Αγορές Λιανικών Συναλλαγών ημεδαπής/αλλοδαπής",
    "13.2": "Παροχή Λιανικών Συναλλαγών ημεδαπής/αλλοδαπής",
    "13.3": "Κοινόχρηστα",
    "13.4": "Συνδρομές",
    "13.31": "Πιστωτικό Στοιχείο Λιανικής ημεδαπής/αλλοδαπής",
    "14.1": "Τιμολόγιο / Ενδοκοινοτικές Αποκτήσεις",
    "14.2": "Τιμολόγιο / Αποκτήσεις Τρίτων Χωρών",
    "14.3": "Τιμολόγιο / Ενδοκοινοτική Λήψη Υπηρεσιών",
    "14.4": "Τιμολόγιο / Λήψη Υπηρεσιών Τρίτων Χωρών",
    "14.5": "ΕΦΚΑ και λοιποί Ασφαλιστικοί Οργανισμοί",
    "15.1": "Συμβόλαιο - Έξοδο",
    "16.1": "Ενοίκιο - Έξοδο",
    "17.1": "Μισθοδοσία",
    "17.2": "Αποσβέσεις",
    "17.5": "Λοιπές Εγγραφές Τακτοποίησης Εξόδων - Λογιστική Βάση",
    "17.6": "Λοιπές Εγγραφές Τακτοποίησης Εξόδων - Φορολογική Βάση",
}

# Κατηγορίες ΦΠΑ γραμμής (πεδίο vatCategory του σχήματος)
VAT_CATEGORIES = {
    "1": "24%",
    "2": "13%",
    "3": "6%",
    "7": "0% (Άνευ ΦΠΑ)",
    "8": "Χωρίς ΦΠΑ (εγγραφές)",
}

# Κανόνες δομής ανά οικογένεια τύπων (βάσει validation myDATA):
# counterpart: αν απαιτείται ο λήπτης (= ο ίδιος ο χρήστης) ως αντισυμβαλλόμενος
# payment: αν επιτρέπεται/απαιτείται τρόπος πληρωμής
# payment: επιβεβαιωμένα ΑΠΑΓΟΡΕΥΕΤΑΙ (σφάλμα 205) στα 14.x (αλλοδαποί/ΕΦΚΑ) και
# 16.1 (ενοίκιο). Στα 17.x (μισθοδοσία/αποσβέσεις/τακτοποιήσεις) είναι ΥΠΟΧΡΕΩΤΙΚΟΣ.
# 13.x (λιανικές) & 15.1 (συμβόλαιο): επιτρέπεται.
SELF_TYPE_RULES = {
    "13": {"counterpart": False, "payment": True},  # λιανικές - χωρίς αντισυμβαλλόμενο
    "14": {"counterpart": True, "payment": False},  # αλλοδαποί/ΕΦΚΑ - error 205
    "15": {"counterpart": True, "payment": True},  # συμβόλαιο
    "16": {"counterpart": True, "payment": False},  # ενοίκιο - error 205
    "17": {"counterpart": False, "payment": True},  # δικές μας εγγραφές - υποχρεωτικός
}


def type_rules(invoice_type: str) -> dict:
    return SELF_TYPE_RULES.get(
        (invoice_type or "").split(".")[0], {"counterpart": False, "payment": True}
    )


# Τρόποι πληρωμής (πεδίο paymentMethodDetails/type)
PAYMENT_METHODS = {
    "1": "Επαγγελματικός λογαριασμός ημεδαπής",
    "2": "Επαγγελματικός λογαριασμός αλλοδαπής",
    "3": "Μετρητά",
    "4": "Επιταγή",
    "5": "Επί πιστώσει",
    "6": "Web τραπεζικής",
    "7": "POS / e-POS",
}


def build_self_expense_invoice_xml(
    invoice_type: str,
    series: str,
    aa: str,
    issue_date: str,
    lines: list[dict],
    issuer_vat: str = "",
    issuer_country: str = "GR",
    counterpart_vat: str = "",
    include_payment: bool = True,
    payment_method: str = "5",
) -> str:
    """
    Δημιουργεί InvoicesDoc για αυτοτιμολογούμενη εγγραφή εξόδου (π.χ. 17.1 Μισθοδοσία)
    με ενσωματωμένους χαρακτηρισμούς εξόδων ανά γραμμή.

    issue_date: yyyy-MM-dd
    lines: [{"amount": 1000.0, "classification_type": "E3_581_001",
             "classification_category": "category2_6"}, ...]
    issuer_vat: το ΑΦΜ του υπόχρεου (απαιτείται από το validation - error 204)
    payment_method: κωδικός τρόπου πληρωμής (default 5 = Επί πιστώσει)

    Οι εγγραφές 17.x είναι χωρίς ΦΠΑ (vatCategory 8, vatAmount 0), χωρίς αντισυμβαλλόμενο.
    Σειρά στοιχείων κατά το σχήμα: issuer, invoiceHeader, paymentMethods,
    invoiceDetails, invoiceSummary.
    """
    ET.register_namespace("", INV_NS)
    ET.register_namespace("ecls", ECLS_NS)
    doc = ET.Element(f"{{{INV_NS}}}InvoicesDoc")
    inv = ET.SubElement(doc, f"{{{INV_NS}}}invoice")

    if issuer_vat:
        issuer = ET.SubElement(inv, f"{{{INV_NS}}}issuer")
        ET.SubElement(issuer, f"{{{INV_NS}}}vatNumber").text = issuer_vat
        ET.SubElement(issuer, f"{{{INV_NS}}}country").text = issuer_country or "GR"
        ET.SubElement(issuer, f"{{{INV_NS}}}branch").text = "0"

    # Αντισυμβαλλόμενος = ο ίδιος ο χρήστης (λήπτης), όπου απαιτείται (π.χ. 14.x)
    if counterpart_vat:
        cp = ET.SubElement(inv, f"{{{INV_NS}}}counterpart")
        ET.SubElement(cp, f"{{{INV_NS}}}vatNumber").text = counterpart_vat
        ET.SubElement(cp, f"{{{INV_NS}}}country").text = "GR"
        ET.SubElement(cp, f"{{{INV_NS}}}branch").text = "0"

    header = ET.SubElement(inv, f"{{{INV_NS}}}invoiceHeader")
    ET.SubElement(header, f"{{{INV_NS}}}series").text = series or "0"
    ET.SubElement(header, f"{{{INV_NS}}}aa").text = aa or "1"
    ET.SubElement(header, f"{{{INV_NS}}}issueDate").text = issue_date
    ET.SubElement(header, f"{{{INV_NS}}}invoiceType").text = invoice_type
    ET.SubElement(header, f"{{{INV_NS}}}currency").text = "EUR"

    total_net = 0.0
    total_vat = 0.0
    # συγκεντρωτικά ανά (type, category) για το invoiceSummary
    aggregates: dict[tuple[str, str], float] = {}
    for line in lines:
        amount = round(float(line["amount"]), 2)
        vat_amount = round(float(line.get("vat_amount", 0) or 0), 2)
        key = (line["classification_type"], line["classification_category"])
        total_net += amount
        total_vat += vat_amount
        aggregates[key] = aggregates.get(key, 0.0) + amount
    total_gross = round(total_net + total_vat, 2)

    # paymentMethods: μετά το header, πριν τα invoiceDetails (σειρά σχήματος).
    # Σε ορισμένους τύπους (π.χ. 14.x) απαγορεύεται - error 205.
    if include_payment:
        pm = ET.SubElement(inv, f"{{{INV_NS}}}paymentMethods")
        pmd = ET.SubElement(pm, f"{{{INV_NS}}}paymentMethodDetails")
        ET.SubElement(pmd, f"{{{INV_NS}}}type").text = payment_method
        ET.SubElement(pmd, f"{{{INV_NS}}}amount").text = f"{total_gross:.2f}"

    for idx, line in enumerate(lines, start=1):
        amount = round(float(line["amount"]), 2)
        ctype = line["classification_type"]
        ccat = line["classification_category"]

        vat_amount = round(float(line.get("vat_amount", 0) or 0), 2)
        vat_category = str(line.get("vat_category") or "8")
        det = ET.SubElement(inv, f"{{{INV_NS}}}invoiceDetails")
        ET.SubElement(det, f"{{{INV_NS}}}lineNumber").text = str(idx)
        ET.SubElement(det, f"{{{INV_NS}}}netValue").text = f"{amount:.2f}"
        ET.SubElement(det, f"{{{INV_NS}}}vatCategory").text = vat_category
        ET.SubElement(det, f"{{{INV_NS}}}vatAmount").text = f"{vat_amount:.2f}"
        ecls = ET.SubElement(det, f"{{{INV_NS}}}expensesClassification")
        ET.SubElement(ecls, f"{{{ECLS_NS}}}classificationType").text = ctype
        ET.SubElement(ecls, f"{{{ECLS_NS}}}classificationCategory").text = ccat
        ET.SubElement(ecls, f"{{{ECLS_NS}}}amount").text = f"{amount:.2f}"
        ET.SubElement(ecls, f"{{{ECLS_NS}}}id").text = "1"

    summary = ET.SubElement(inv, f"{{{INV_NS}}}invoiceSummary")
    ET.SubElement(summary, f"{{{INV_NS}}}totalNetValue").text = f"{total_net:.2f}"
    ET.SubElement(summary, f"{{{INV_NS}}}totalVatAmount").text = f"{total_vat:.2f}"
    ET.SubElement(summary, f"{{{INV_NS}}}totalWithheldAmount").text = "0.00"
    ET.SubElement(summary, f"{{{INV_NS}}}totalFeesAmount").text = "0.00"
    ET.SubElement(summary, f"{{{INV_NS}}}totalStampDutyAmount").text = "0.00"
    ET.SubElement(summary, f"{{{INV_NS}}}totalOtherTaxesAmount").text = "0.00"
    ET.SubElement(summary, f"{{{INV_NS}}}totalDeductionsAmount").text = "0.00"
    ET.SubElement(summary, f"{{{INV_NS}}}totalGrossValue").text = f"{total_gross:.2f}"
    for (ctype, ccat), amt in aggregates.items():
        agg = ET.SubElement(summary, f"{{{INV_NS}}}expensesClassification")
        ET.SubElement(agg, f"{{{ECLS_NS}}}classificationType").text = ctype
        ET.SubElement(agg, f"{{{ECLS_NS}}}classificationCategory").text = ccat
        ET.SubElement(agg, f"{{{ECLS_NS}}}amount").text = f"{amt:.2f}"

    return '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
        doc, encoding="unicode"
    )


def build_expenses_classification_xml(
    invoice_mark: str,
    classifications: list[dict],
    post_per_invoice: bool = False,
    transaction_mode: str | None = None,
    entity_vat: str = "",
) -> str:
    ET.register_namespace("", ECLS_NS)
    doc = ET.Element(f"{{{ECLS_NS}}}ExpensesClassificationsDoc")
    eic = ET.SubElement(doc, f"{{{ECLS_NS}}}expensesInvoiceClassification")
    ET.SubElement(eic, f"{{{ECLS_NS}}}invoiceMark").text = str(invoice_mark)

    # Υποβολή από εξουσιοδοτημένο λογιστή για λογαριασμό πελάτη: το ΑΦΜ της
    # επιχείρησης ΠΡΕΠΕΙ να μπει ΜΕΣΑ στο XML (αμέσως μετά το invoiceMark).
    # Στα endpoints ΥΠΟΒΟΛΗΣ το query param entityVatNumber ΑΓΝΟΕΙΤΑΙ - το AADE
    # αποδίδει την κίνηση στον κάτοχο του key αν λείπει από το XML.
    if entity_vat:
        ET.SubElement(eic, f"{{{ECLS_NS}}}entityVatNumber").text = str(entity_vat)

    # Απόρριψη/απόκλιση: στέλνεται ΜΟΝΟ invoiceMark + transactionMode, χωρίς details
    if transaction_mode:
        ET.SubElement(eic, f"{{{ECLS_NS}}}transactionMode").text = str(transaction_mode)
        return '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
            doc, encoding="unicode"
        )

    # ομαδοποίηση ανά γραμμή
    by_line: dict[int, list[dict]] = {}
    for c in classifications:
        by_line.setdefault(int(c["line_number"]), []).append(c)

    for line_no in sorted(by_line):
        details = ET.SubElement(
            eic, f"{{{ECLS_NS}}}invoicesExpensesClassificationDetails"
        )
        ET.SubElement(details, f"{{{ECLS_NS}}}lineNumber").text = str(line_no)
        for c in by_line[line_no]:
            data = ET.SubElement(
                details, f"{{{ECLS_NS}}}expensesClassificationDetailData"
            )
            if c.get("classification_type"):
                ET.SubElement(data, f"{{{ECLS_NS}}}classificationType").text = c[
                    "classification_type"
                ]
            if c.get("classification_category"):
                ET.SubElement(data, f"{{{ECLS_NS}}}classificationCategory").text = c[
                    "classification_category"
                ]
            ET.SubElement(
                data, f"{{{ECLS_NS}}}amount"
            ).text = f"{float(c['amount']):.2f}"
            # ΠΡΟΣΟΧΗ: σε χαρακτηρισμό ΑΝΑ ΓΡΑΜΜΗ, τα vatAmount/vatCategory/
            # vatExemptionCategory ΠΡΕΠΕΙ να είναι null (σφάλμα 337). Το ποσό ΦΠΑ
            # μεταφέρεται ως το ίδιο το amount της γραμμής χαρακτηρισμού VAT_xxx.

    if post_per_invoice:
        ET.SubElement(eic, f"{{{ECLS_NS}}}classificationPostMode").text = "1"

    return '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
        doc, encoding="unicode"
    )


def parse_response_doc(content: bytes) -> dict:
    """Parse του ResponseDoc: statusCode, classificationMark ή σφάλματα."""
    root = ET.fromstring(content)
    out = {
        "status": None,
        "classification_mark": None,
        "invoice_mark": None,
        "invoice_uid": None,
        "cancellation_mark": None,
        "errors": [],
    }
    for resp in root.iter():
        tag = resp.tag.split("}")[-1]
        if tag == "statusCode":
            out["status"] = resp.text
        elif tag == "classificationMark":
            out["classification_mark"] = resp.text
        elif tag == "invoiceMark":
            out["invoice_mark"] = resp.text
        elif tag == "invoiceUid":
            out["invoice_uid"] = resp.text
        elif tag == "cancellationMark":
            out["cancellation_mark"] = resp.text
        elif tag == "error":
            code = resp.findtext("{*}code") or ""
            msg = resp.findtext("{*}message") or ""
            out["errors"].append(f"[{code}] {msg}")
    return out
