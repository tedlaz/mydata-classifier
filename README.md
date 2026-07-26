# myDATA Βιβλίο Εσόδων-Εξόδων & Χαρακτηρισμός

Web εφαρμογή (Python/Flask) που λειτουργεί ως **τοπικό βιβλίο εσόδων-εξόδων** πάνω
από το **myDATA REST API** της ΑΑΔΕ (διεπαφή ERP). Φέρνει τα παραστατικά σου,
τα αποθηκεύει μόνιμα σε τοπική βάση SQLite και σου επιτρέπει να τα χαρακτηρίσεις
ανά γραμμή και να διαβιβάσεις τους χαρακτηρισμούς πίσω στο myDATA.

Βασικές δυνατότητες:

1. **Λήψη παραστατικών** για διάστημα με `RequestDocs` / `RequestTransmittedDocs`
   (έξοδα από τρίτους **και** τα δικά σου έσοδα).
2. **Τοπικό βιβλίο** σε SQLite: τα παραστατικά μένουν αποθηκευμένα με κατάσταση
   (`unclassified → classified → sent → confirmed`) — δεν χάνονται στο restart.
3. **Χαρακτηρισμός ανά γραμμή** (category_x + E3_xxx + προαιρετικά ΦΠΑ) με
   **φιλτραρισμένα** dropdowns βάσει των επιτρεπόμενων συνδυασμών της ΑΑΔΕ.
4. **Αποστολή** χαρακτηρισμών με `SendExpensesClassification` (ανά γραμμή) και
   επιβεβαίωση κατάστασης με `/refresh`.
5. **Πολυ-εταιρική** λειτουργία + κλήση **μέσω λογιστή** (κοινά credentials με
   `entityVatNumber` της εταιρείας).
6. **Νέα εγγραφή** αυτοτιμολογούμενου εξόδου (`SendInvoices`), **απόρριψη/ακύρωση**
   και **αναφορές** αθροισμάτων.

## Εγκατάσταση

### Τοπικά

```bash
pip install -r requirements.txt
cp .env.example .env   # όρισε το FLASK_SECRET
python app.py
```

```bash
uv sync
cp .env.example .env   # όρισε το FLASK_SECRET
uv run app.py
```
Άνοιξε: http://127.0.0.1:5000

### Με Docker

```bash
UID=$(id -u) GID=$(id -g) docker compose up -d --build
```

### Windows installer (για τελικούς χρήστες)

Ο τελικός χρήστης δεν χρειάζεται Python, Docker ή γραμμή εντολών. Εκτελεί το
`myDATA-Classifier-Setup-<version>.exe`, ολοκληρώνει τον οδηγό εγκατάστασης και
ανοίγει την εφαρμογή από το Start menu. Η εφαρμογή ξεκινά τον τοπικό server,
ανοίγει αυτόματα τον browser και εμφανίζει ένα μικρό παράθυρο για νέο άνοιγμα ή
τερματισμό.

Η βάση και οι ρυθμίσεις αποθηκεύονται μόνιμα στο
`%LOCALAPPDATA%\myDATA Classifier`, έξω από τον φάκελο εγκατάστασης. Έτσι δεν
χρειάζονται δικαιώματα administrator και οι αναβαθμίσεις/απεγκαταστάσεις δεν
διαγράφουν τα δεδομένα του χρήστη.

Για δημιουργία νέου installer σε Windows:

1. Εγκατέστησε το [uv](https://docs.astral.sh/uv/) και το
   [Inno Setup 6](https://jrsoftware.org/isinfo.php).
2. Από PowerShell στον φάκελο του project εκτέλεσε:

   ```powershell
   .\build-windows.ps1
   ```

Η έκδοση και το όνομα του installer προκύπτουν αυτόματα από το πεδίο `version`
του `pyproject.toml`. Το έτοιμο αρχείο δημιουργείται στο `installer-output`. Για
portable build χωρίς installer χρησιμοποίησε `-SkipInstaller` και μοίρασε
ολόκληρο τον φάκελο `dist\myDATA Classifier` (όχι μόνο το `.exe`).

Για αυτόματο build στο GitHub, δημιούργησε και κάνε push tag που συμφωνεί με την
έκδοση του `pyproject.toml`:

```powershell
$version = (uv version --short).Trim()
git tag -a "v$version" -m "Release v$version"
git push origin "v$version"
```

Το workflow `.github/workflows/windows-release.yml` εκτελείται σε Windows,
ελέγχει ότι tag και project version συμφωνούν και ανεβάζει το Setup `.exe` στα
Artifacts του workflow run για 30 ημέρες.

Αν το tag είχε ήδη γίνει push πριν προστεθεί το workflow, άνοιξε στο GitHub
`Actions → Build Windows installer → Run workflow`, συμπλήρωσε το υπάρχον tag
(π.χ. `v1.0.0`) και πάτησε `Run workflow`. Το workflow κάνει checkout το ακριβές
tag, χωρίς να χρειάζεται διαγραφή ή επαναδημιουργία του.

## Ρύθμιση (credentials)

Τα credentials **δεν** μπαίνουν πλέον στο `.env` — τα διαχειρίζεσαι μέσα από την
εφαρμογή:

- **Εταιρείες:** για κάθε εταιρεία καταχώρησε `aade-user-id`,
  `Ocp-Apim-Subscription-Key`, ΑΦΜ και περιβάλλον (`prod`/`dev`). Μπορείς να
  εναλλάσσεσαι μεταξύ εταιρειών (η ενεργή χρησιμοποιείται σε όλες τις κλήσεις).
- **Λογιστής:** κοινά credentials λογιστή· όταν μια εταιρεία έχει ενεργό το
  «χρήση credentials λογιστή», η κλήση φέρει το ΑΦΜ της εταιρείας ως
  `entityVatNumber`, ώστε η κίνηση να καταχωρείται στην εταιρεία και όχι στον
  λογιστή.
- **Import/Export:** τα στοιχεία εταιρειών & λογιστή εξάγονται/εισάγονται από
  αρχείο `companies.json` (κατέβασμα/ανέβασμα με επιλογή θέσης στον δίσκο).

Το μόνο υποχρεωτικό env είναι το `FLASK_SECRET`. Προαιρετικά:
`MYDATA_DATA_DIR` (φάκελος για τη βάση `mydata.db`), `GSIS_USERNAME`/`GSIS_PASSWORD`
(αναζήτηση επωνυμίας από ΑΦΜ μέσω ΓΓΠΣ).

Εγγραφή για credentials:
- **Δοκιμαστικό:** https://mydata-dev-register.azurewebsites.net (`env=dev`).
- **Παραγωγή:** myDATA REST API μέσω myAADE με κωδικούς TAXISnet (`env=prod`).

## Λειτουργία & καρτέλες

- **Βιβλίο εξόδων / Βιβλίο εσόδων** — αναζήτηση και λήψη παραστατικών για διάστημα,
  προβολή ανά κατάσταση, χαρακτηρισμός, μαζική αποστολή και ανανέωση κατάστασης
  από το myDATA.
- **Αναφορές** — αθροίσματα (καθαρή αξία / ΦΠΑ / σύνολο) ανά είδος και διάστημα.
- **Νέα εγγραφή** — τοπική δημιουργία αυτοτιμολογούμενου εξόδου, που διαβιβάζεται
  μαζικά μέσω `SendInvoices`.
- **Προμηθευτές** — **κοινοί** για όλες τις εταιρείες (ΑΦΜ + επωνυμία + κανόνας
  χαρακτηρισμού). Χειροκίνητη προσθήκη/διόρθωση και import/export σε `.txt`
  (μία γραμμή = «ΑΦΜ<κενό>Επωνυμία»). Στα παραστατικά αποθηκεύεται μόνο το ΑΦΜ·
  η επωνυμία προκύπτει από τον πίνακα προμηθευτών.
- **Συνδυασμοί** — εισαγωγή του xlsx «Συνδυασμοί χαρακτηρισμών» της ΑΑΔΕ. Ορίζει
  τους **επιτρεπόμενους** συνδυασμούς `invoice_type → category → E3` που φιλτράρουν
  τα dropdowns του χαρακτηρισμού.
- **Εταιρείες / Λογιστής** — διαχείριση credentials (δες παραπάνω).

## Σημαντικές σημειώσεις

- Το `RequestDocs` επιστρέφει παραστατικά που διαβίβασαν **άλλοι** και σε αφορούν
  ως λήπτη (έξοδα). «Αχαρακτήριστο» θεωρείται όταν καμία γραμμή δεν έχει στοιχείο
  `expensesClassification`.
- Χαρακτηρισμοί που έγιναν **χειροκίνητα στην πύλη** myDATA δεν επιστρέφουν από το
  ERP API, οπότε δείχνουν αχαρακτήριστοι. Για το βιβλίο μπορείς να τους
  καταγράψεις **μόνο τοπικά** (χωρίς αποστολή): αποθηκεύονται ως `confirmed` με
  `source='manual'` (σε αντιδιαστολή με το `source='rest'`).
- **Χαρακτηρισμός ανά γραμμή:** η αποστολή γίνεται ανά γραμμή. Το παλιό
  `classificationPostMode=1` (ανά παραστατικό) έχει αποσυρθεί (επέστρεφε σφάλμα 340).
- Το άθροισμα των ποσών χαρακτηρισμού μιας γραμμής πρέπει να ισούται με την
  **καθαρή** αξία της γραμμής. Σε γραμμή ΦΠΑ (`VAT_xxx`): `amount` = καθαρή αξία
  (αλλιώς σφάλμα 306) και **χωρίς** `vatAmount` (αλλιώς σφάλμα 337).
- Στο endpoint υποβολής, το ΑΦΜ οντότητας πάει **μέσα στο XML** (`entityVatNumber`),
  όχι ως query param — το query param εκεί αγνοείται.
- Οι έγκυροι συνδυασμοί ορίζονται από το xls της ΑΑΔΕ:
  https://www.aade.gr/mydata/tehnikes-prodiagrafes-ekdoseis-mydata

## Δομή

```
app.py            # Flask routes / UI (βιβλίο, χαρακτηρισμός, αποστολή, εταιρείες)
db.py             # SQLite: companies, accountant, suppliers, documents, classifications, combos
mydata_client.py  # Κλήσεις API (RequestDocs/RequestTransmittedDocs, RequestIncome,
                  #   SendExpensesClassification, SendInvoices, cancel/reject) + XML build/parse
classifications.py# Λίστες κατηγοριών/κωδικών E3 για τα dropdowns
gsis.py           # Αναζήτηση επωνυμίας από ΑΦΜ (ΓΓΠΣ)
vies.py           # Έλεγχος ενδοκοινοτικού ΑΦΜ (VIES)
wsgi.py           # WSGI entrypoint
templates/        # HTML templates
compose.yml       # Docker Compose
Dockerfile
```

Η βάση ζει στο `<MYDATA_DATA_DIR>/mydata.db` (προεπιλογή: ο φάκελος του project).
