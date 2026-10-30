"""P1c end to end against a scratch database (never the live one): a local server plays the client dashboard (app.q1st.io),
checks each request's signature and time like the app does, and answers. Covers set-up, refresh, refusals, a dashboard
that is down, and the changes the dashboard follows (client rename, site removal).
Run: EXT_MKT_DSN=... EXT_MKT_ENC_KEY=... EXT_MKT_ADMIN_KEY=... python3 tests/dashboard_local_test.py"""
import asyncio, hashlib, hmac, json, os, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
import httpx
from ext_marketing import db, security
from ext_marketing.server import app

P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

SECRET = "provision_secret_for_the_local_test_0123456789"
GOT, ANSWER = [], {"mode": "ok"}
class App(BaseHTTPRequestHandler):
    def do_POST(self):
        b = self.rfile.read(int(self.headers["Content-Length"]))
        ts = self.headers.get("X-Timestamp", "")
        good = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + b, hashlib.sha256).hexdigest()
        signed = hmac.compare_digest(good, self.headers.get("X-Signature", "")) and abs(time.time() - int(ts or 0)) <= 300
        d = json.loads(b)
        GOT.append({"path": self.path, "signed": signed, "d": d})
        if ANSWER["mode"] == "missing" and signed:
            out, code = {"ok": False, "error": {"code": "not_found", "message": "No client in the app for this account."}}, 404
        elif ANSWER["mode"] == "refuse" or not signed:
            out, code = {"ok": False, "error": {"code": "conflict", "message": "This email already has an account with another client."}}, 409
        else:
            out, code = {"ok": True, "data": {"clientId": "cli_x", "siteId": d.get("siteId"), "hookUrl": "https://app.example.com/api/hooks/marketing",
                                              **({"admin": "invited"} if d.get("admin") else {})}}, 200
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(out).encode())
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), App); threading.Thread(target=srv.serve_forever, daemon=True).start()
APP_URL = f"http://127.0.0.1:{srv.server_port}"
ADM = {"X-Admin-Key": os.environ["EXT_MKT_ADMIN_KEY"], "X-Acting-User": "tester"}


async def main():
    await db.open_pool()
    p = db.pool(); S = db.SCHEMA
    async def idem(req): req.headers["Idempotency-Key"] = uuid.uuid4().hex
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", event_hooks={"request": [idem]})
    acc = (await c.post("/admin/v1/accounts", headers=ADM, json={"name": "Dash acc"})).json()["data"]
    A = acc["id"]
    site = (await c.post(f"/admin/v1/accounts/{A}/sites", headers=ADM, json={"name": "Dash site", "url": "https://example.com",
            "timezone": "Asia/Dubai", "languages": ["en"], "defaultLanguage": "en"})).json()["data"]
    SID = site["id"]; D = f"/admin/v1/accounts/{A}/sites/{SID}/dashboard"
    manual = (await c.post(f"/admin/v1/accounts/{A}/api-keys", headers=ADM, json={"siteId": SID, "label": "developer"})).json()["data"]
    live_keys = lambda: p.fetch(f"SELECT id, label FROM {S}.api_keys WHERE site_id = $1 AND revoked_at IS NULL ORDER BY created_at", SID)

    os.environ.pop("Q1ST_APP_URL", None); os.environ.pop("Q1ST_APP_SECRET", None)
    r = await c.post(D, headers=ADM, json={})
    ok("T1 not set up on this engine: 503 dashboard_off, nothing sent", r.status_code == 503 and r.json()["error"]["code"] == "dashboard_off" and not GOT, r.text)
    det = (await c.get(f"/admin/v1/accounts/{A}", headers=ADM)).json()["data"]
    ok("T1 detail says the dashboard is off", det["dashboard"] == {"on": False, "sites": []}, det.get("dashboard"))

    os.environ["Q1ST_APP_URL"] = APP_URL; os.environ["Q1ST_APP_SECRET"] = SECRET
    r = await c.post(D, headers=ADM, json={"adminEmail": " owner@acme.com "})
    g = GOT[-1]
    ok("T2 set up: 200, the app's answer comes back", r.status_code == 200 and r.json()["data"]["admin"] == "invited", r.text)
    ok("T2 the request is signed over time + body, at /api/provision", g["signed"] and g["path"] == "/api/provision", g)
    ok("T2 it names the account, the site, both names, and the admin email trimmed",
       g["d"]["action"] == "site.upsert" and g["d"]["accountId"] == A and g["d"]["siteId"] == SID and g["d"]["accountName"] == "Dash acc"
       and g["d"]["siteName"] == "Dash site" and g["d"]["admin"] == {"email": "owner@acme.com"}, g["d"])
    key = g["d"]["engineKey"]
    k = await c.get("/api/v1/sites", headers={"X-Api-Key": key})
    ok("T2 the key the app got works on the engine, for this site", k.status_code == 200 and k.json()["data"][0]["id"] == SID, k.text)
    keys = await live_keys()
    ok("T2 the developer's own key stays; one dashboard key added", [x["label"] for x in keys] == ["developer", "Q1st dashboard"], keys)
    w = await p.fetchrow(f"SELECT * FROM {S}.webhooks WHERE site_id = $1", SID)
    ok("T2 the webhook goes to the app's address with the secret the app got, every event",
       w and w["url"] == "https://app.example.com/api/hooks/marketing" and security.decrypt(w["secret_enc"]) == g["d"]["webhookSecret"]
       and len(w["events"]) > 0, dict(w) if w else None)
    ok("T2 the key is never in the portal's answer", key not in r.text and g["d"]["webhookSecret"] not in r.text)
    det = (await c.get(f"/admin/v1/accounts/{A}", headers=ADM)).json()["data"]
    ok("T2 detail lists the site as on the dashboard", det["dashboard"] == {"on": True, "sites": [SID]}, det["dashboard"])
    aud = await p.fetchval(f"SELECT count(*) FROM {S}.audit_log WHERE action = 'site.dashboard' AND site_id = $1 AND actor = 'staff:tester'", SID)
    ok("T2 audited with the staff user", aud == 1, aud)

    r = await c.post(D, headers=ADM, json={})
    g2 = GOT[-1]
    keys = await live_keys()
    old = await c.get("/api/v1/sites", headers={"X-Api-Key": key})
    ok("T3 refresh: no admin in the request, a new key, the old dashboard key revoked, the developer's key kept",
       r.status_code == 200 and "admin" not in g2["d"] and g2["d"]["engineKey"] != key and old.status_code == 401
       and [x["label"] for x in keys] == ["developer", "Q1st dashboard"], (r.text, keys, old.status_code))

    before = (await live_keys(), await p.fetchval(f"SELECT secret_enc FROM {S}.webhooks WHERE site_id = $1", SID))
    ANSWER["mode"] = "refuse"
    r = await c.post(D, headers=ADM, json={"adminEmail": "boss@other.com"})
    after = (await live_keys(), await p.fetchval(f"SELECT secret_enc FROM {S}.webhooks WHERE site_id = $1", SID))
    ok("T4 the app refuses: 502 with the app's own message", r.status_code == 502 and "another client" in r.json()["error"]["message"], r.text)
    ok("T4 nothing changed here: same keys, same webhook secret", before == after)
    ANSWER["mode"] = "ok"

    os.environ["Q1ST_APP_URL"] = "http://127.0.0.1:9"                   # nothing listens there
    r = await c.post(D, headers=ADM, json={})
    ok("T5 the app is down: 502 dashboard_down, nothing changed", r.status_code == 502 and r.json()["error"]["code"] == "dashboard_down"
       and (await live_keys()) == after[0], r.text)
    os.environ["Q1ST_APP_URL"] = APP_URL

    n = len(GOT)
    ver = (await c.get(f"/admin/v1/accounts/{A}", headers=ADM)).json()["data"]["account"]["version"]
    r = await c.put(f"/admin/v1/accounts/{A}", headers=ADM, json={"version": ver, "name": "Dash group"})
    ok("T6 a client rename is followed by the dashboard", r.status_code == 200 and GOT[-1]["d"] == {"action": "client.rename", "accountId": A, "accountName": "Dash group"}
       and "dashboardError" not in r.json()["data"], (r.text, GOT[-1]))
    ver = r.json()["data"]["version"]
    r = await c.put(f"/admin/v1/accounts/{A}", headers=ADM, json={"version": ver, "notes": "only notes"})
    ok("T6 a save without a name sends nothing", len(GOT) == n + 1, len(GOT) - n)
    ANSWER["mode"] = "refuse"
    ver = r.json()["data"]["version"]
    r = await c.put(f"/admin/v1/accounts/{A}", headers=ADM, json={"version": ver, "name": "Dash group 2"})
    ok("T6 the dashboard fails: the rename stands here, the answer carries why", r.status_code == 200 and r.json()["data"]["name"] == "Dash group 2"
       and "another client" in r.json()["data"].get("dashboardError", ""), r.text)
    ANSWER["mode"] = "ok"

    other = (await c.post(f"/admin/v1/accounts/{A}/sites", headers=ADM, json={"name": "Not on dash", "url": "https://other.example.com",
             "timezone": "Asia/Dubai", "languages": ["en"], "defaultLanguage": "en"})).json()["data"]
    n = len(GOT)
    r = await c.delete(f"/admin/v1/accounts/{A}/sites/{other['id']}", headers=ADM)
    ok("T7 removing a site that isn't on the dashboard sends nothing", r.status_code == 200 and len(GOT) == n, r.text)
    r = await c.delete(f"/admin/v1/accounts/{A}/sites/{SID}", headers=ADM)
    ok("T7 removing a dashboard site tells the dashboard (signed), then revokes its keys",
       r.status_code == 200 and GOT[-1]["d"] == {"action": "site.remove", "accountId": A, "siteId": SID} and GOT[-1]["signed"]
       and not await live_keys(), (r.text, GOT[-1]))
    r = await c.post(D, headers=ADM, json={})
    ok("T8 a removed site can't be set up: 404", r.status_code == 404, r.text)

    # ---- client status and delete
    acct = lambda: c.get(f"/admin/v1/accounts/{A}", headers=ADM)
    ver = (await acct()).json()["data"]["account"]["version"]
    r = await c.post(f"/admin/v1/accounts/{A}/delete", headers=ADM)
    ok("T9 an active client can't be deleted: 409 not_closed", r.status_code == 409 and r.json()["error"]["code"] == "not_closed", r.text)
    n = len(GOT)
    r = await c.put(f"/admin/v1/accounts/{A}", headers=ADM, json={"version": ver, "status": "suspended"})
    ok("T10 suspending is followed by the dashboard", r.status_code == 200 and {"action": "client.status", "accountId": A, "status": "suspended"} in [g["d"] for g in GOT[n:]], [g["d"] for g in GOT[n:]])
    ver = r.json()["data"]["version"]; n = len(GOT)
    r = await c.put(f"/admin/v1/accounts/{A}", headers=ADM, json={"version": ver, "status": "suspended", "notes": "x"})
    ok("T10 saving the same status again sends no status change", not [g for g in GOT[n:] if g["d"]["action"] == "client.status"], [g["d"] for g in GOT[n:]])
    ver = r.json()["data"]["version"]
    ANSWER["mode"] = "missing"
    r = await c.put(f"/admin/v1/accounts/{A}", headers=ADM, json={"version": ver, "status": "closed"})
    ok("T10 a client the dashboard doesn't have is not a warning", r.status_code == 200 and "dashboardError" not in r.json()["data"], r.text)
    ANSWER["mode"] = "refuse"
    keep = await c.post(f"/admin/v1/accounts/{A}/sites", headers=ADM, json={"name": "Keep", "url": "https://keep.example.com",
                        "timezone": "Asia/Dubai", "languages": ["en"], "defaultLanguage": "en"})
    r = await c.post(f"/admin/v1/accounts/{A}/delete", headers=ADM)
    ok("T11 the dashboard refuses the delete: 502, and the client and its site stay here", r.status_code == 502
       and (await acct()).status_code == 200 and keep.status_code == 200, r.text)
    ANSWER["mode"] = "ok"; n = len(GOT)
    r = await c.post(f"/admin/v1/accounts/{A}/delete", headers=ADM)
    ok("T12 a closed client is deleted: the dashboard is told (signed)", r.status_code == 200 and GOT[n]["d"] == {"action": "client.remove", "accountId": A} and GOT[n]["signed"], r.text)
    ok("T12 gone from the list and its page", A not in [x["id"] for x in (await c.get("/admin/v1/accounts", headers=ADM)).json()["data"]]
       and (await acct()).status_code == 404)
    left = await p.fetchval(f"SELECT count(*) FROM {S}.sites WHERE account_id = $1 AND deleted_at IS NULL", A)
    ok("T12 its sites are removed and its row kept for history", left == 0 and await p.fetchval(f"SELECT 1 FROM {S}.accounts WHERE id = $1", A), left)
    r = await c.post(f"/admin/v1/accounts/{A}/delete", headers=ADM)
    ok("T12 deleting again: 404", r.status_code == 404, r.text)

    await p.execute(f"DELETE FROM {S}.api_keys WHERE account_id = $1", A)
    await p.execute(f"DELETE FROM {S}.webhooks WHERE site_id IN (SELECT id FROM {S}.sites WHERE account_id = $1)", A)
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)

asyncio.run(main())
