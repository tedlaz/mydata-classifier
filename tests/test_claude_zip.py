"""Παράμετροι → Claude: zip με .mcp.json (URL+token του .env) για κάθε τρόπο λειτουργίας (python -m tests.test_claude_zip)."""
import io
import json
import os
import tempfile
import zipfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("MCP_TOKEN", None)
os.environ.pop("MCP_PUBLIC_URL", None)
import app as A  # noqa: E402

A.app.testing = True
from ensure_env import ensure_mcp_token  # noqa: E402

tok = ensure_mcp_token()
assert len(tok) == 64  # το έφτιαξε η εφαρμογή στην εκκίνηση
c = A.app.test_client()


def url(base, **headers):
    r = c.get("/parameters/claude.zip", base_url=base, headers=headers)
    assert r.status_code == 200 and r.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert set(z.namelist()) == {"claude-client/.mcp.json", "claude-client/.claude/settings.local.json",
                                 "claude-client/CLAUDE.md"}
    cfg = json.loads(z.read("claude-client/.mcp.json"))["mcpServers"]["mydata"]
    assert cfg["headers"]["Authorization"] == f"Bearer {tok}"
    return cfg["url"]


assert url("http://127.0.0.1:53211") == "http://127.0.0.1:8342/mcp"                 # desktop / τοπικός server
assert url("http://nas.local:8341") == "http://nas.local:8342/mcp"                   # Docker χωρίς proxy
assert url("http://my.example.com", **{"X-Forwarded-Proto": "https"}) == "https://my.example.com/mcp"  # proxy
os.environ["MCP_PUBLIC_URL"] = "https://mcp.example.com/mcp"
assert url("http://my.example.com") == "https://mcp.example.com/mcp"
assert tok in c.get("/parameters?tab=claude").get_data(as_text=True)

# Διακόπτης MCP: μόνο όταν ο MCP ανήκει στη διεργασία (desktop/τοπικός), όχι σε Docker.
import db  # noqa: E402
import mcp_server  # noqa: E402


class FakeProc:
    alive = True
    poll = lambda self: None if self.alive else 0  # noqa: E731
    wait = lambda self, timeout: 0  # noqa: E731

    def terminate(self):
        self.alive = False


os.environ.pop("MYDATA_MCP_LOCAL", None)
assert "compose.mcp.yml" in c.get("/parameters?tab=claude").get_data(as_text=True)
assert c.post("/parameters/mcp", data={"mcp_enabled": "1"}).status_code == 404
assert db.get_setting("mcp_enabled") is None
os.environ["MYDATA_MCP_LOCAL"] = "1"
spawned = []
mcp_server.spawn = lambda: spawned.append(FakeProc()) or spawned[-1]
assert c.post("/parameters/mcp", data={"mcp_enabled": "1"}).status_code == 302
assert mcp_server.running() and db.get_setting("mcp_enabled") == "1"
assert "Τρέχει" in c.get("/parameters?tab=claude").get_data(as_text=True)
c.post("/parameters/mcp", data={"mcp_enabled": "1"})
assert len(spawned) == 1  # δεύτερο «άνοιγμα»: όχι δεύτερη διεργασία
assert c.post("/parameters/mcp").status_code == 302
assert not mcp_server.running() and not spawned[0].alive and db.get_setting("mcp_enabled") == "0"
assert "Σταματημένος" in c.get("/parameters?tab=claude").get_data(as_text=True)
print("ok")
