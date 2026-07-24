"""WSGI entry point για production serving (gunicorn wsgi:app)."""

from app import app

if __name__ == "__main__":
    app.run()
