# ---------- Stage 1: build wheels ----------
# Χτίζουμε τα dependencies ξεχωριστά ώστε το τελικό image να μην κουβαλά
# compilers/headers. Χρήση Alpine για ελάχιστο μέγεθος.
FROM python:3.14-alpine AS builder

WORKDIR /build
RUN apk add --no-cache gcc musl-dev libffi-dev

COPY requirements.txt .
# Προ-χτίσιμο wheels για γρήγορη & καθαρή εγκατάσταση στο επόμενο stage
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- Stage 2: runtime ----------
FROM python:3.14-alpine

# Μη-root χρήστης για ασφάλεια
RUN adduser -D -H -u 10001 appuser

WORKDIR /app

# Αντιγραφή μόνο των εγκατεστημένων πακέτων από το builder (χωρίς build tools)
COPY --from=builder /install /usr/local

# Ο κώδικας της εφαρμογής (μόνο ό,τι χρειάζεται - δες .dockerignore)
COPY app.py wsgi.py db.py ensure_env.py mydata_client.py classifications.py vies.py gsis.py ./
COPY templates ./templates

# Φάκελος δεδομένων (mounted volume) - ιδιοκτησία στον μη-root χρήστη
ENV MYDATA_DATA_DIR=/data
RUN mkdir -p /data && chown appuser:appuser /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_SECRET=change-me-in-compose

USER appuser
EXPOSE 8000
VOLUME ["/data"]

# gunicorn: 2 workers x 4 threads αρκούν για single-user εργαλείο με I/O-bound κλήσεις
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "4", \
     "--timeout", "120", "--access-logfile", "-", "wsgi:app"]
