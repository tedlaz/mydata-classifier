"""502/503/504 της ΑΑΔΕ: νέα προσπάθεια μόνο στα GET, φιλικό μήνυμα χωρίς HTML (python -m tests.test_http_errors)."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import mydata_client as M

hits = {"GET": 0, "POST": 0}


class H(BaseHTTPRequestHandler):
    def _reply(self, method):
        hits[method] += 1
        ok = method == "GET" and hits["GET"] > 2  # τα δύο πρώτα GET: 502
        self.send_response(200 if ok else 502)
        self.end_headers()
        self.wfile.write(b"<ok/>" if ok else b"<html>502 Bad Gateway</html>")

    def do_GET(self):
        self._reply("GET")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply("POST")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/"

c = M.MyDataClient("u", "k", "dev")
c.session.mount("http://", c.session.get_adapter("https://"))  # ίδιες ρυθμίσεις για τον τοπικό server
r = c.session.get(url)
assert r.status_code == 200 and hits["GET"] == 3, hits  # 2 × 502 → νέα προσπάθεια → 200

r = c.session.post(url, data=b"x")
assert r.status_code == 502 and hits["POST"] == 1, hits  # αποστολή: ποτέ ξανά αυτόματα
e = str(M.http_error(r, sent=True))
assert "HTTP 502" in e and "<html>" not in e and "καταχωρήθηκε" in e, e
assert "καταχωρήθηκε" not in str(M.http_error(r, "RequestDocs"))
srv.shutdown()
print("OK")
