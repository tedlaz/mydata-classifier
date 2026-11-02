"""MCP server (read-only) πάνω στο τοπικό βιβλίο: ερωτήσεις από Claude Desktop/Code.

Μόνο ανάγνωση: κανένα tool δεν γράφει στη βάση ούτε μιλά με την ΑΑΔΕ, και credentials δεν επιστρέφονται ποτέ.

    uv run mcp_server.py           # stdio
    uv run mcp_server.py --http    # streamable HTTP στο :8342/mcp, Bearer MCP_TOKEN (ensure_mcp_token)

Desktop και τοπικός server τον ξεκινούν μόνοι τους (spawn), το Docker ως service `mcp`.
"""

import hmac
import json
import os
import subprocess
import sys

from mcp.server.mcpserver import MCPServer

import db
import vat_return

mcp = MCPServer("mydata", instructions=(
    "Τοπικό βιβλίο εσόδων-εξόδων myDATA (ΑΑΔΕ). kind: 'income' (έσοδα) ή 'expense' (έξοδα). "
    "status: unclassified → classified → sent → confirmed. Ημερομηνίες yyyy-mm-dd. "
    "company_id: παράλειψη = η ενεργή εταιρεία της εφαρμογής."))

_HIDDEN = {"raw_xml", "cls_xml"}  # μεγάλα XML, όχι χρήσιμα στο μοντέλο


def _cid(company_id: int | None) -> int | None:
    return company_id or db.get_active_company_id()


def _doc(d: dict) -> dict:
    out = {k: v for k, v in d.items() if k not in _HIDDEN}
    for k in ("lines_json", "cls_json"):
        if isinstance(out.get(k), str):
            out[k] = json.loads(out[k])
    return out


@mcp.tool()
def list_companies() -> list[dict]:
    """Οι εταιρείες του βιβλίου (id, επωνυμία, ΑΦΜ, περίοδος ΦΠΑ m/q) και ποια είναι ενεργή."""
    active = db.get_active_company_id()
    with db.get_conn() as conn:  # όχι db.list_companies: αποκρυπτογραφεί credentials
        rows = conn.execute("SELECT id, company_name, aade_vat_number, vat_period, entity_type "
                            "FROM companies ORDER BY id").fetchall()
    return [dict(r, active=r["id"] == active) for r in rows]


@mcp.tool()
def status_counts(kind: str = "expense", company_id: int | None = None) -> dict:
    """Πλήθος παραστατικών ανά κατάσταση (π.χ. πόσα έξοδα μένουν αχαρακτήριστα)."""
    return db.count_by_status(_cid(company_id), kind)


@mcp.tool()
def documents(date_from: str, date_to: str, kind: str = "expense", status: str | None = None,
              company_id: int | None = None) -> list[dict]:
    """Παραστατικά διαστήματος (κατά ημ/νία έκδοσης), με αντισυμβαλλόμενο, ποσά και κατάσταση."""
    keep = ("mark", "issue_date", "invoice_type", "series", "aa", "counterparty_vat", "counterparty_name",
            "status", "local_action", "total_net", "total_vat")
    docs = db.period_documents(_cid(company_id), kind, date_from, date_to, xml=False)
    return [{k: d[k] for k in keep} for d in docs if not status or d["status"] == status]


@mcp.tool()
def document(mark: str, company_id: int | None = None) -> dict | None:
    """Ένα παραστατικό με ΜΑΡΚ: γραμμές (lines_json) και τοπικός χαρακτηρισμός (cls_json)."""
    d = db.get_document(_cid(company_id), mark)
    return _doc(d) if d else None


@mcp.tool()
def counterparties(company_id: int | None = None) -> list[dict]:
    """Συναλλασσόμενοι της εταιρείας: ΑΦΜ, επωνυμία, ρόλος (income/expense/both), πλήθος παραστατικών."""
    cid = _cid(company_id)
    stats = db.counterparty_stats(cid)
    return [s | stats.get(s["vat"], {"role": None, "docs": 0}) for s in db.list_suppliers(cid)]


@mcp.tool()
def vat_return_f2(date_from: str, date_to: str, prev_credit: float = 0.0, prev_debit: float = 0.0,
                  company_id: int | None = None) -> dict:
    """Δήλωση ΦΠΑ Φ2 περιόδου: κωδικοί → ποσά (511 = προς καταβολή, 502 = πιστωτικό).
    prev_credit = 401 (πιστωτικό προηγ. περιόδου), prev_debit = 483. Μόνο απεσταλμένοι χαρακτηρισμοί μετρούν."""
    cid = _cid(company_id)
    inc, exp = (db.period_documents(cid, k, date_from, date_to) for k in ("income", "expense"))
    return vat_return.compute(inc, exp, prev_credit, prev_debit)


# ---- Αναλύσεις της εφαρμογής: τρέχει η ίδια η σελίδα (ίδιοι υπολογισμοί) και κρατά τα δεδομένα του template.

def _web():
    """Το Flask app, φορτωμένο στην πρώτη ανάλυση (το import τρέχει init_db). Τα credentials δεν
    αποκρυπτογραφούνται ποτέ σε αυτή τη διεργασία: οι αναφορές δεν μιλούν με την ΑΑΔΕ."""
    import auth
    auth.dec = lambda _v: ""
    import app
    return app


def _plain(v):
    """JSON-ασφαλές: κλειδιά-tuples → «a / b», tuples → λίστες."""
    if isinstance(v, dict):
        return {(" / ".join(map(str, k)) if isinstance(k, tuple) else str(k)): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    return v if v is None or isinstance(v, (str, int, float, bool)) else str(v)


def _page(endpoint: str, company_id: int | None, keep: tuple, **args) -> dict:
    """Τρέχει τη σελίδα endpoint για την εταιρεία (χωρίς να αλλάξει η ενεργή του χρήστη, όπως ο αυτόματος
    κύκλος) και επιστρέφει τα κλειδιά keep του context του template της."""
    import flask
    web = _web()
    path = next(r.rule for r in web.app.url_map.iter_rules() if r.endpoint == endpoint)
    got = []
    query = {k: v for k, v in args.items() if v not in (None, "")}
    with web.app.test_request_context(path, query_string=query), flask.template_rendered.connected_to(
            lambda _s, template, context, **_: got.append(context), web.app):
        flask.g.auto_cid = _cid(company_id)
        web.app.view_functions[endpoint]()
    if not got:
        raise ValueError("Η σελίδα δεν έδωσε αποτέλεσμα (μη έγκυρες παράμετροι ή χωρίς εταιρεία).")
    return _plain({k: got[0][k] for k in keep if k in got[0]})


@mcp.tool()
def dashboard(company_id: int | None = None) -> dict:
    """Πίνακας ελέγχου: KPI έτους, ΦΠΑ περιόδου, εκκρεμότητες (todos), αποτελέσματα Ε3 (pl),
    κορυφαίοι προμηθευτές (top) / πελάτες (top_in), παραστατικά ανά μήνα."""
    return _page("dashboard", company_id, ("year", "kpi", "vat", "pl", "stock_chk", "todos", "fetch_due_in", "segments",
                                           "exp_total", "done_pct", "inc_st", "top", "top_in", "monthly"))


@mcp.tool()
def yearly_book(year: str, unclassified: bool = False, company_id: int | None = None) -> dict:
    """Συνοπτικό βιβλίο έτους (όπως στο myDATA): ανά μήνα έσοδα/έξοδα (καθαρή, ΦΠΑ, παρακρατήσεις, τέλη,
    πάγια, αποσβέσεις, αποθέματα), υπόλοιπο, ΦΠΑ μήνα/τριμήνου, σύνολα και αποτελέσματα Ε3 (pl).
    unclassified=True: μετρά και τα αχαρακτήριστα."""
    return _page("reports_yearly", company_id, ("year", "rows", "income_total", "expense_total", "pl", "stock_chk",
                                                "unc", "unc_count", "prev_year", "has_prev"),
                 year=year, unc="1" if unclassified else None)


@mcp.tool()
def e3_analysis(year: str, company_id: int | None = None) -> dict:
    """Ανάλυση Ε3 έτους: έσοδα/έξοδα ανά λογαριασμό Ε3 (pl), καθαρό αποτέλεσμα και φόρος (tax)."""
    return _page("reports_yearly_e3", company_id, ("year", "pl", "tax", "stock_chk", "inc_net", "exp_net"), year=year)


@mcp.tool()
def vat_declaration(year: str, period: str = "", prev_credit: float | None = None, prev_debit: float | None = None,
               company_id: int | None = None) -> dict:
    """Δήλωση ΦΠΑ (Φ2) της εφαρμογής: period = m1…m12 (μηνιαία) ή q1…q4 (τριμηνιαία), κενό = η περίοδος που
    δηλώνεται τώρα. Το 401/483 έρχεται αυτόματα από την προηγούμενη περίοδο (auto_credit/auto_debit),
    εκτός αν δοθεί prev_credit/prev_debit. c.codes: κωδικοί → ποσά (511 = προς καταβολή, 502 = πιστωτικό)."""
    return _page("reports_vat", company_id, ("year", "period", "date_from", "date_to", "c", "invoiced_vat",
                                             "auto_credit", "auto_debit", "manual_credit", "manual_debit"),
                 year=year, period=period, prev_credit=prev_credit, prev_debit=prev_debit)


@mcp.tool()
def vat_reconcile(year: str, period: str = "", company_id: int | None = None) -> dict:
    """Συμφωνία Φ2: λογαριασμοί Ε3 × κωδικοί Φ2 (έσοδα/έξοδα) — τι γεμίζει τη δήλωση, τι μένει εκτός,
    ΦΠΑ που δεν εκπίπτει. period όπως στο vat_declaration."""
    return _page("reports_vat_reconcile", company_id, ("year", "period", "date_from", "date_to", "r", "accounts"),
                 year=year, period=period)


@mcp.tool()
def forecast(year: str = "", options: dict[str, str] | None = None, company_id: int | None = None) -> dict:
    """Πρόβλεψη έτους: ανά μήνα πραγματικά + προβλεπόμενα (rows), σύνολο έτους (total), σενάρια p10/p50/p90 (sc),
    φόρος εισοδήματος και προκαταβολή (tax), μέθοδοι (avg/labels). options = οι παράμετροι της σελίδας, π.χ.
    vol_in/vol_out (μεταβλητότητα %, 0–500), assets_small/assets_large/planned_income/planned_expense (€)."""
    return _page("reports_forecast", company_id, ("year", "prev_year", "done", "rows", "total", "ytd", "sc", "expected",
                                                  "tax", "avg", "labels", "history", "dep", "fixed", "errors",
                                                  "plan_result", "forecast_in", "forecast_var", "vol", "shift",
                                                  "stock_chk"),
                 year=year, **(options or {}))


@mcp.tool()
def compare_years(company_id: int | None = None) -> dict:
    """Σύγκριση ετών: το τρέχον έτος απέναντι στα προηγούμενα στους ίδιους ολοκληρωμένους μήνες — KPI,
    μετρικές, σύνολα, πελάτες/προμηθευτές, λογαριασμοί Ε3, παρατηρήσεις (insights)."""
    c = _page("reports_compare", company_id, ("c",))["c"]
    return {k: c[k] for k in ("years", "span", "kpis", "metrics", "totals", "customers", "suppliers", "e3_in", "e3_out",
                              "insights", "unsure", "proj_method") if k in c}


@mcp.tool()
def flows(year: str, stock: bool = True, assets: bool = True, company_id: int | None = None) -> dict:
    """Ροές: από πού έρχονται τα έσοδα (ανά πελάτη / κατηγορία / τύπο / λογαριασμό Ε3) και πού πάνε τα έξοδα
    (ανά κατηγορία / τύπο / λογαριασμό Ε3 / προμηθευτή), με το κέρδος. stock/assets=False: χωρίς αποθέματα / πάγια."""
    p = _page("reports_flows", company_id, ("year", "sankey", "monthly"), year=year,
              stock=None if stock else "0", assets=None if assets else "0")
    s = p.pop("sankey") or {}
    nodes = lambda side, key: [{k: n[k] for k in ("label", "value", "pct")}  # noqa: E731
                               for n in next(x[side] for x in s["pairs"] if x[side + "_key"] == key)]
    return p | {k: s.get(k) for k in ("inc", "exp", "profit")} | {
        "income_by": {title: nodes("src", key) for key, title in s.get("src", {}).items()},
        "expense_by": {title: nodes("dst", key) for key, title in s.get("dst", {}).items()}}


_OLAP_DIMS = {
    "kind": lambda d, f: "έσοδα" if d["k"] == "in" else "έξοδα",
    "year": lambda d, f: d["date"][:4], "month": lambda d, f: d["date"][:7],
    "quarter": lambda d, f: f"{d['date'][:4]}-Q{(int(d['date'][5:7] or 1) - 1) // 3 + 1}",
    "invoice_type": lambda d, f: d["type"], "counterparty": lambda d, f: d["cpn"] or d["cp"] or "—",
    "e3_category": lambda d, f: f[1] or "—", "e3_type": lambda d, f: f[2] or "—",
    "e3_account": lambda d, f: "_".join((f[2] or "").split("_")[:2]) or "—", "vat_category": lambda d, f: f[3] or "—",
}
_OLAP_MEASURES = ("net", "vat", "gross", "withheld", "other_taxes", "stamp_duty", "fees", "deductions",
                  "vat_nondeductible")


@mcp.tool()
def olap(group_by: list[str], date_from: str = "", date_to: str = "", kind: str = "", limit: int = 100,
         company_id: int | None = None) -> dict:
    """OLAP: ο κύβος εσόδων-εξόδων της εφαρμογής (χαρακτηρισμένα, πιστωτικά αρνητικά), ομαδοποιημένος κατά
    group_by ⊆ kind, year, quarter, month, invoice_type, counterparty, e3_category, e3_type, e3_account,
    vat_category. Μέτρα: net, vat, gross, withheld, other_taxes, stamp_duty, fees, deductions, vat_nondeductible,
    docs. Φίλτρα: date_from/date_to (yyyy-mm-dd), kind (income/expense). Ταξινόμηση κατά |net|, έως limit γραμμές."""
    bad = [g for g in group_by if g not in _OLAP_DIMS]
    if bad:
        raise ValueError(f"Άγνωστες διαστάσεις: {bad}")
    cube = _web()._olap_cube(_cid(company_id))
    docs = cube["docs"]
    out: dict = {}
    for f in cube["facts"]:
        d = docs[f[0]]
        if (kind and d["k"] != kind[:2]) or (date_from and d["date"] < date_from) or (date_to and d["date"] > date_to):
            continue
        key = tuple(_OLAP_DIMS[g](d, f) for g in group_by)
        row = out.setdefault(key, [[0.0] * len(_OLAP_MEASURES), set()])
        row[0] = [a + b for a, b in zip(row[0], f[4:])]
        row[1].add(f[0])
    rows = [dict(zip(group_by, k)) | {m: round(v, 2) for m, v in zip(_OLAP_MEASURES, vals)} | {"docs": len(ds)}
            for k, (vals, ds) in out.items()]
    rows.sort(key=lambda r: -abs(r["net"]))
    labels = {"invoice_type": cube["labels"]["type"], "e3_category": cube["labels"]["e3c"],
              "e3_type": cube["labels"]["e3t"], "e3_account": cube["labels"]["e3g"], "vat_category": cube["labels"]["vat"]}
    return {"rows": rows[:limit], "total_rows": len(rows),
            "labels": {g: labels[g] for g in group_by if g in labels}}


def _bearer(app, token: str):
    """ASGI wrapper: κάθε HTTP αίτημα θέλει «Authorization: Bearer <MCP_TOKEN>»."""
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and not hmac.compare_digest(dict(scope["headers"]).get(b"authorization", b""), expected):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await app(scope, receive, send)
    return guarded


def spawn() -> subprocess.Popen:
    """Ο HTTP server ως ξεχωριστή διεργασία δίπλα στην εφαρμογή (desktop, τοπικός server). Ξεχωριστή,
    γιατί το _web μηδενίζει το auth.dec: στη διεργασία της εφαρμογής θα έσπαγαν οι κλήσεις ΑΑΔΕ."""
    frozen = getattr(sys, "frozen", False)
    return subprocess.Popen([sys.executable, "--mcp"] if frozen else [sys.executable, os.path.abspath(__file__), "--http"])


_CLAUDE_MD = """# Ερωτήσεις στο βιβλίο myDATA

Όλα τα δεδομένα έρχονται από τον MCP server `mydata` (read-only). Μη διαβάζεις και μη γράφεις αρχεία.

- Απάντα στα ελληνικά, με ποσά σε € (1.234,56) και πίνακες όπου ταιριάζει.
- Εταιρεία με όνομα → πρώτα `list_companies` και μετά το `company_id`. Χωρίς όνομα → η ενεργή.
- Ανά Ε3 / τρίμηνο / μήνα / αντισυμβαλλόμενο / τύπο → `olap` (group_by, kind, date_from/date_to).
- Πλήρες Ε3 / αποτέλεσμα / φόρος → `e3_analysis`. ΦΠΑ → `vat_return_f2`, `vat_declaration`, `vat_reconcile`.
- Το `olap` μετρά μόνο χαρακτηρισμένα. Αν λείπει περίοδος, ρώτα τα `status_counts` και πες το.
"""


def client_files(url: str, token: str) -> dict[str, str]:
    """Ο φάκελος του Claude Code (`cd claude-client; claude`): .mcp.json με URL+token, έγκριση του server
    και των tools (όλα read-only), οδηγίες. Τον γράφει ο server (write_client) και τον δίνει η εφαρμογή
    ως zip (Παράμετροι → Claude)."""
    mcp_json = {"mcpServers": {"mydata": {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}
    settings = {"enabledMcpjsonServers": ["mydata"], "permissions": {"allow": ["mcp__mydata"]}}
    return {".mcp.json": json.dumps(mcp_json, indent=2), ".claude/settings.local.json": json.dumps(settings, indent=2),
            "CLAUDE.md": _CLAUDE_MD}


def write_client(folder: str, url: str, token: str) -> None:
    """Γράφει τον φάκελο σε κάθε εκκίνηση· το CLAUDE.md μόνο αν λείπει (κρατά τις δικές σου οδηγίες)."""
    for name, text in client_files(url, token).items():
        path = os.path.join(folder, *name.split("/"))
        if name == "CLAUDE.md" and os.path.exists(path):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def main(http: bool = False) -> None:
    db._load_dicts()  # για την αποσυμπίεση των XML (χωρίς init_db: αυτό γράφει migrations)
    if not (http or "--http" in sys.argv):
        mcp.run()
        return
    from dotenv import load_dotenv

    from ensure_env import ensure_mcp_token, env_path
    token = ensure_mcp_token()
    if len(token) < 32:
        sys.exit("MCP_TOKEN (≥32 χαρακτήρες) υποχρεωτικό για --http")
    load_dotenv(env_path())  # όλες οι ρυθμίσεις MCP_* στο .env του φακέλου δεδομένων
    import uvicorn
    host, port = os.getenv("MCP_HOST", "127.0.0.1"), int(os.getenv("MCP_PORT", "8342"))
    url = os.getenv("MCP_PUBLIC_URL") or f"http://127.0.0.1:{port}/mcp"
    try:
        write_client(os.path.join(os.path.dirname(env_path()), "claude-client"), url, token)
    except OSError as e:
        print(f"claude-client: {e}", file=sys.stderr)
    uvicorn.run(_bearer(mcp.streamable_http_app(host=host), token), host=host, port=port,
                log_config=None)  # χωρίς κονσόλα (desktop) το default logging του uvicorn σκάει στο sys.stdout=None


if __name__ == "__main__":
    main()
