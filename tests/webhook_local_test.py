"""Webhooks end to end against a scratch database (never the live one): set, sign, deliver, retry, give up, events from
connections, quotas and plans, the admin test, removal. A local receiver plays the site's app.
Run: EXT_MKT_DSN=postgresql://... EXT_MKT_ENC_KEY=... EXT_MKT_ADMIN_KEY=... python3 tests/webhook_local_test.py"""
import asyncio, hashlib, hmac, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
import httpx
from ext_marketing import db, meter, netguard, webhooks
from ext_marketing.server import app

P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

GOT, ANSWER = [], {"code": 200}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        b = self.rfile.read(int(self.headers["Content-Length"]))
        GOT.append({"h": dict(self.headers), "b": b}); self.send_response(ANSWER["code"]); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{srv.server_port}/api/hooks/marketing"

_real = netguard.check
async def _local(url, require_https=True):                             # the receiver is on this machine: only the address check is skipped
    u = urlparse(url)
    if require_https and u.scheme != "https":
        raise netguard.Blocked("the address must start with https://")
    if u.hostname != "127.0.0.1":
        return await _real(url, require_https)
    return url
netguard.check = _local

ADM = {"X-Admin-Key": os.environ["EXT_MKT_ADMIN_KEY"]}


async def main():
    await db.open_pool()
    p = db.pool(); S = db.SCHEMA
    await p.execute(f"UPDATE {S}.settings SET value = 'false' WHERE key = 'webhook_require_https'")
    await p.execute(f"UPDATE {S}.settings SET value = '3' WHERE key = 'webhook_max_attempts'")
    import uuid
    async def idem(req): req.headers["Idempotency-Key"] = uuid.uuid4().hex             # a new key per action, as the app sends
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", event_hooks={"request": [idem]})
    plan = (await c.post("/admin/v1/plans", headers=ADM, json={"name": "WH test", "price": 100,
            "quotas": [{"metric": "articles", "limit": 3, "mode": "hard", "warnAt": 0.5}]})).json()["data"]
    plan2 = (await c.post("/admin/v1/plans", headers=ADM, json={"name": "WH test 2", "quotas": []})).json()["data"]
    acc = (await c.post("/admin/v1/accounts", headers=ADM, json={"name": "WH acc", "planId": plan["id"]})).json()["data"]
    site = (await c.post(f"/admin/v1/accounts/{acc['id']}/sites", headers=ADM, json={"name": "WH site", "url": "https://example.com",
            "timezone": "Asia/Dubai", "languages": ["en"], "defaultLanguage": "en"})).json()["data"]
    key = (await c.post(f"/admin/v1/accounts/{acc['id']}/api-keys", headers=ADM, json={"siteId": site["id"]})).json()["data"]["key"]
    K = {"X-Api-Key": key}; W = f"/api/v1/sites/{site['id']}/webhook"
    await p.execute(f"INSERT INTO {S}.site_connections (site_id, type, status, config) VALUES ($1,'website','ok','{{}}')", site["id"])

    r = (await c.get(W, headers=K)).json()
    ok("T1 nothing set: url null, no events", r["ok"] and r["data"] == {"url": None, "events": []}, r)
    all_events = json.loads(await p.fetchval(f"SELECT value::text FROM {S}.settings WHERE key = 'webhook_events'"))
    r = (await c.put(W, headers=K, json={"url": HOOK, "events": ["post.published", "connection.changed", "quota.warning", "quota.exceeded", "plan.changed"]})).json()
    sec = r["data"].get("secret", "")
    ok("T2 first save returns the secret once", r["ok"] and sec.startswith("whsec_") and len(sec) > 30, r)
    g = (await c.get(W, headers=K)).json()["data"]
    ok("T3 GET has url + events, never the secret", g["url"] == HOOK and "secret" not in g and len(g["events"]) == 5, g)
    r = (await c.put(W, headers=K, json={"url": HOOK, "events": g["events"]})).json()["data"]
    ok("T4 saving again keeps the secret (none returned)", "secret" not in r, r)
    r = (await c.put(W, headers=K, json={"url": HOOK, "events": g["events"], "rotateSecret": True})).json()["data"]
    ok("T5 rotateSecret returns a new secret", r.get("secret", "").startswith("whsec_") and r["secret"] != sec, r)
    sec = r["secret"]
    r = (await c.put(W, headers=K, json={"url": HOOK, "events": ["post.nope"]})).json()
    ok("T6 an unknown event is refused on the field", r.get("error", {}).get("fields", {}).get("events", "").startswith("Unknown"), r)
    await p.execute(f"UPDATE {S}.settings SET value = 'true' WHERE key = 'webhook_require_https'")
    r = (await c.put(W, headers=K, json={"url": HOOK})).json()
    ok("T7 http address refused while https is required", "https" in r.get("error", {}).get("fields", {}).get("url", ""), r)
    await p.execute(f"UPDATE {S}.settings SET value = 'false' WHERE key = 'webhook_require_https'")
    r = (await c.put("/api/v1/sites/site_other/webhook", headers=K, json={"url": HOOK})).json()
    ok("T8 a key cannot set another site's webhook", r.get("error", {}).get("code") == "not_found", r)

    GOT.clear()
    await webhooks.emit(site["id"], "post.published", {"postId": "p_1", "url": "https://example.com/a", "platform": "website"})
    await webhooks.deliver_due()
    d = GOT[-1] if GOT else {"h": {}, "b": b"{}"}
    body = json.loads(d["b"])
    good = "sha256=" + hmac.new(sec.encode(), d["b"], hashlib.sha256).hexdigest()
    ok("T9 delivered, signed with the current secret", d["h"].get("X-Signature") == good, d["h"])
    ok("T10 X-Event-Id evt_…, body {event, siteId, at, data}", d["h"].get("X-Event-Id", "").startswith("evt_") and
       body.get("event") == "post.published" and body.get("siteId") == site["id"] and body.get("data", {}).get("postId") == "p_1", body)
    ok("T11 'at' carries the site's offset (+04:00)", body.get("at", "").endswith("+04:00"), body.get("at"))
    n = len(GOT)
    await webhooks.emit(site["id"], "post.drafted", {"postId": "p_1", "status": "pending"}); await webhooks.deliver_due()
    ok("T12 an event the site did not pick is not sent", len(GOT) == n, len(GOT))

    ANSWER["code"] = 500
    await webhooks.emit(site["id"], "post.published", {"postId": "p_2"}); await webhooks.deliver_due()
    x = await p.fetchrow(f"SELECT * FROM {S}.webhook_deliveries WHERE site_id = $1 ORDER BY created_at DESC LIMIT 1", site["id"])
    wait = json.loads(await p.fetchval(f"SELECT value::text FROM {S}.settings WHERE key = 'webhook_backoff_seconds'"))[0]
    secs = (x["next_at"] - await p.fetchval("SELECT now()")).total_seconds()
    ok("T13 a 500 keeps it pending, tries again after the first wait", x["status"] == "pending" and x["attempts"] == 1 and
       x["last_code"] == 500 and wait - 5 < secs <= wait, dict(x))
    st = await webhooks.status(site["id"])
    ok("T14 the webhook shows failing with the reason", st["status"] == "failing" and "500" in (st["lastError"] or ""), st)
    ANSWER["code"] = 200
    await p.execute(f"UPDATE {S}.webhook_deliveries SET next_at = now() WHERE id = $1", x["id"]); await webhooks.deliver_due()
    y = await p.fetchrow(f"SELECT * FROM {S}.webhook_deliveries WHERE id = $1", x["id"])
    ok("T15 the retry succeeds with the SAME event id; webhook back to ok", y["status"] == "sent" and y["attempts"] == 2 and
       GOT[-1]["h"].get("X-Event-Id") == x["id"] and (await webhooks.status(site["id"]))["status"] == "ok", dict(y))
    ANSWER["code"] = 503
    await webhooks.emit(site["id"], "post.published", {"postId": "p_3"})
    for _ in range(3):
        await p.execute(f"UPDATE {S}.webhook_deliveries SET next_at = now() WHERE status = 'pending'"); await webhooks.deliver_due()
    z = await p.fetchrow(f"SELECT * FROM {S}.webhook_deliveries WHERE site_id = $1 ORDER BY created_at DESC LIMIT 1", site["id"])
    ok("T16 after webhook_max_attempts tries it is marked failed (no more tries)", z["status"] == "failed" and z["attempts"] == 3, dict(z))
    ANSWER["code"] = 200

    GOT.clear()
    await webhooks.set_connection(site["id"], "website", "error", "Login refused"); await webhooks.deliver_due()
    await webhooks.set_connection(site["id"], "website", "error", "Login refused"); await webhooks.deliver_due()
    ev = [json.loads(g["b"]) for g in GOT]
    ok("T17 connection ok→error sends connection.changed once", len(ev) == 1 and ev[0]["data"] == {"type": "website", "status": "error", "error": "Login refused"}, ev)

    GOT.clear()
    for _ in range(4):
        await meter.record(acc["id"], site["id"], "articles", 1)
    await webhooks.deliver_due()
    ev = [(json.loads(g["b"])["event"], json.loads(g["b"])["data"]) for g in GOT]
    ok("T18 limit 3, warn 0.5: warning at 2, exceeded at 3 (hard), nothing at 4", ev == [
        ("quota.warning", {"metric": "articles", "used": 2, "limit": 3}),
        ("quota.exceeded", {"metric": "articles", "used": 3, "limit": 3, "mode": "hard"})], ev)

    GOT.clear()
    a = (await c.get(f"/admin/v1/accounts/{acc['id']}", headers=ADM)).json()["data"]["account"]
    pr = (await c.put(f"/admin/v1/accounts/{acc['id']}", headers=ADM, json={"version": a["version"], "planId": plan2["id"]})).json(); print(pr) if not pr.get("ok") else None; await webhooks.deliver_due()
    ev = [json.loads(g["b"]) for g in GOT]
    ok("T19 a new plan sends plan.changed {planId}", [e["event"] for e in ev] == ["plan.changed"] and ev[0]["data"] == {"planId": plan2["id"]}, ev)

    GOT.clear()
    r = (await c.post(f"/admin/v1/sites/{site['id']}/webhook/test", headers=ADM)).json()["data"]
    ok("T20 admin test: sent now (even though not a picked event), shown first in the log",
       len(GOT) == 1 and r["recent"][0]["event"] == "webhook.test" and r["recent"][0]["status"] == "sent", r["recent"][:1])
    det = (await c.get(f"/admin/v1/accounts/{acc['id']}", headers=ADM)).json()["data"]
    ok("T21 the client page data has each site's webhook status", det["webhooks"][site["id"]]["status"] == "ok", det.get("webhooks"))

    ANSWER["code"] = 500
    await webhooks.emit(site["id"], "post.published", {"postId": "p_9"}); await webhooks.deliver_due()
    r = (await c.put(W, headers=K, json={"url": ""})).json()["data"]
    left = await p.fetchval(f"SELECT count(*) FROM {S}.webhook_deliveries WHERE site_id = $1 AND status = 'pending'", site["id"])
    ok("T22 an empty url removes the webhook and drops what was waiting", r == {"url": None, "events": []} and left == 0, (r, left))
    n = len(GOT)
    await webhooks.emit(site["id"], "post.published", {"postId": "p_10"}); await webhooks.deliver_due()
    ok("T23 nothing is sent once removed", len(GOT) == n)
    print(f"\n{P} passed, {F} failed")
    with open("/tmp/wh_secret", "w") as f:
        f.write(sec)


asyncio.run(main())
sys.exit(1 if F else 0)
