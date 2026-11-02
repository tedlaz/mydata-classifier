"""MCP server: tools διαβάζουν το βιβλίο, χωρίς credentials (uv run python -m tests.test_mcp_server)."""
import asyncio
import json
import os
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

db.init_db()
cid = db.add_company({"company_name": "Test", "AADE_VAT_NUMBER": "046949583",
                      "AADE_USER_ID": "SECRET_USER", "AADE_SUBSCRIPTION_KEY": "SECRET_KEY"})
db.set_active_company_id(cid)
db.upsert_document(cid, "expense", {
    "mark": "4001", "issue_date": "2026-03-11", "issuer_vat": "2", "invoice_type": "1.1", "total_net": 80.0,
    "total_vat": 12.0, "total_gross": 92.0, "raw_xml": "<x>SECRET_XML</x>",
    "lines": [{"line_number": 1, "net_value": 80.0, "vat_amount": 12.0, "vat_category": "1"}],
    "cls_info": [{"type": "E3_102_001", "category": "category2_1", "amount": 80.0, "line": 1},
                 {"type": "VAT_361", "amount": 80.0, "line": 1}]}, "confirmed")

import mcp_server as M  # noqa: E402

M.db._load_dicts()
out = {
    "companies": M.list_companies(),
    "counts": M.status_counts(),
    "docs": M.documents("2026-03-01", "2026-03-31"),
    "doc": M.document("4001"),
    "cps": M.counterparties(),
    "f2": M.vat_return_f2("2026-03-01", "2026-03-31"),
}
assert out["companies"] == [{"id": cid, "company_name": "Test", "aade_vat_number": "046949583",
                             "vat_period": "m", "entity_type": "legal", "active": True}], out["companies"]
assert out["counts"] == {"confirmed": 1}
assert [d["mark"] for d in out["docs"]] == ["4001"] and M.documents("2026-03-01", "2026-03-31", status="sent") == []
assert out["doc"]["lines_json"][0]["net_value"] == 80.0
assert out["f2"]["codes"]["361"] == 80.0
dump = json.dumps(out, ensure_ascii=False)
assert "SECRET" not in dump, dump  # ούτε credentials ούτε XML

# Τα tools είναι δηλωμένα στο MCP.
names = {t.name for t in asyncio.run(M.mcp.list_tools())}
assert names == {"list_companies", "status_counts", "documents", "document", "counterparties", "vat_return_f2",
                 "dashboard", "yearly_book", "e3_analysis", "vat_declaration", "vat_reconcile", "forecast", "compare_years",
                 "flows", "olap"}, names

# Αναλύσεις: τρέχουν οι σελίδες της εφαρμογής, χωρίς credentials στο αποτέλεσμα.
rep = {
    "dash": M.dashboard(), "yearly": M.yearly_book("2026"), "e3": M.e3_analysis("2026"),
    "vat": M.vat_declaration("2026", "m3"), "rec": M.vat_reconcile("2026", "m3"), "fc": M.forecast("2026"),
    "cmp": M.compare_years(), "flows": M.flows("2026"),
    "olap": M.olap(["month", "e3_type"], kind="expense"),
}
assert rep["yearly"]["rows"][2]["expense"]["net"] == 80.0, rep["yearly"]["rows"][2]
assert rep["vat"]["c"]["361"] == 80.0, rep["vat"]
assert rep["flows"]["exp"] == 80.0, rep["flows"]
assert rep["olap"]["rows"] == [{"month": "2026-03", "e3_type": "E3_102_001", "net": 80.0, "vat": 12.0, "gross": 92.0,
                                "withheld": 0.0, "other_taxes": 0.0, "stamp_duty": 0.0, "fees": 0.0, "deductions": 0.0,
                                "vat_nondeductible": 0.0, "docs": 1}], rep["olap"]
dump = json.dumps(rep, ensure_ascii=False)
assert "SECRET" not in dump, dump

# HTTP: χωρίς σωστό Bearer → 401.
sent = []


async def _send(m):
    sent.append(m)


async def _app(scope, receive, send):
    sent.append("passed")

guard = M._bearer(_app, lambda: "t" * 32)
asyncio.run(guard({"type": "http", "headers": [(b"authorization", b"Bearer wrong")]}, None, _send))
assert sent[0]["status"] == 401
sent.clear()
asyncio.run(guard({"type": "http", "headers": [(b"authorization", b"Bearer " + b"t" * 32)]}, None, _send))
assert sent == ["passed"]

# MCP_TOKEN: αυτόματο στο .env του φακέλου δεδομένων, σταθερό στις επόμενες εκκινήσεις, το env υπερισχύει.
from ensure_env import ensure_mcp_token, mcp_token_external, rotate_mcp_token  # noqa: E402

env = os.path.join(os.environ["MYDATA_DATA_DIR"], "token.env")
os.environ.pop("MCP_TOKEN", None)
tok = ensure_mcp_token(env)
assert len(tok) == 64 and ensure_mcp_token(env) == tok and f"MCP_TOKEN={tok}" in open(env, encoding="utf-8").read()
os.environ["MCP_TOKEN"] = "x" * 32
assert ensure_mcp_token(env) == "x" * 32
assert mcp_token_external(env)  # env ≠ .env → δεν αλλάζει από το UI

# Αλλαγή token: νέο στο .env, ο server το βλέπει στο επόμενο αίτημα, το παλιό → 401. Κενό token → 401.
from dotenv import dotenv_values  # noqa: E402

new = rotate_mcp_token(env)
assert len(new) == 64 and new != tok and dotenv_values(env)["MCP_TOKEN"] == new and not mcp_token_external(env)
guard = M._bearer(_app, lambda: dotenv_values(env).get("MCP_TOKEN") or "")
for t, ok in ((tok, False), (new, True)):
    sent.clear()
    asyncio.run(guard({"type": "http", "headers": [(b"authorization", f"Bearer {t}".encode())]}, None, _send))
    assert (sent == ["passed"]) == ok, (t, sent)
sent.clear()
asyncio.run(M._bearer(_app, lambda: "")({"type": "http", "headers": [(b"authorization", b"Bearer ")]}, None, _send))
assert sent[0]["status"] == 401

# Φάκελος Claude: .mcp.json με URL+token, έγκριση server, CLAUDE.md που δεν ξαναγράφεται.
cc = os.path.join(os.environ["MYDATA_DATA_DIR"], "claude-client")
M.write_client(cc, "https://h/mcp", tok)
cfg = json.load(open(os.path.join(cc, ".mcp.json"), encoding="utf-8"))["mcpServers"]["mydata"]
assert cfg == {"type": "http", "url": "https://h/mcp", "headers": {"Authorization": f"Bearer {tok}"}}, cfg
assert json.load(open(os.path.join(cc, ".claude", "settings.local.json"), encoding="utf-8"))["enabledMcpjsonServers"] == ["mydata"]
open(os.path.join(cc, "CLAUDE.md"), "w", encoding="utf-8").write("δικό μου")
M.write_client(cc, "https://h/mcp", tok)
assert open(os.path.join(cc, "CLAUDE.md"), encoding="utf-8").read() == "δικό μου"
print("ok")
