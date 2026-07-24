# Εκτέλεση σε Docker

## Γρήγορη εκκίνηση

```bash
cp .env.docker.example .env      # και βάλε ένα FLASK_SECRET
docker compose up -d --build
```

Άνοιξε: http://localhost:8000

## Δομή / μέγεθος

- Multi-stage build πάνω σε **python:3.12-alpine** — το τελικό image περιέχει
  μόνο το runtime της Python, τα εγκατεστημένα πακέτα (Flask, requests,
  gunicorn) και τον κώδικα. Τα build tools (gcc κ.λπ.) μένουν στο πρώτο stage
  και ΔΕΝ μπαίνουν στο τελικό image.
- Αναμενόμενο μέγεθος τελικού image: **~70–90 MB** (Alpine base ~50 MB +
  πακέτα ~25 MB + κώδικας < 1 MB).
- Τρέχει ως **μη-root** χρήστης, με gunicorn (2 workers × 4 threads).

## Δεδομένα

Οι εταιρείες, οι κανόνες και οι επωνυμίες αποθηκεύονται στο **named volume
`mydata-data`** (mounted στο `/data`), οπότε επιβιώνουν σε restart/rebuild.

Backup:
```bash
docker run --rm -v mydata-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/mydata-backup.tgz -C /data .
```

Restore:
```bash
docker run --rm -v mydata-data:/data -v "$PWD":/backup alpine \
  sh -c "tar xzf /backup/mydata-backup.tgz -C /data"
```

## Ενημέρωση

```bash
docker compose up -d --build   # ξαναχτίζει με τον νέο κώδικα, κρατά τα δεδομένα
```

## Σημειώσεις παραγωγής

- Τα κλειδιά myDATA καταχωρούνται από την εφαρμογή (σελίδα «Εταιρείες») και
  αποθηκεύονται στο volume — όχι στο image.
- Για έκθεση στο διαδίκτυο, βάλε reverse proxy (nginx/Caddy/Traefik) με HTTPS
  μπροστά· το gunicorn δεν πρέπει να εκτίθεται απευθείας.
