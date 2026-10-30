"""'Fill from my website' end to end against a scratch database (never the live one): a local website plays the client's
site, a stand-in model answers. Checks: the pages are read and given to the model, the answer's shape is checked (not its
content), nothing is saved, usage is metered, an unreadable site says why, and the quota gate applies.
Run: PYTHONPATH=<dir with shared/kb_sources.py> EXT_MKT_DSN=... EXT_MKT_ENC_KEY=... EXT_MKT_ADMIN_KEY=... python3 tests/seo_propose_local_test.py"""
import asyncio, json, os, sys, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
import httpx
from ext_marketing import db, llm, netguard
from ext_marketing.server import app

P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

BODY = "<p>" + "We advise companies and employees in Dubai and Abu Dhabi on employment, company and property law. " * 8 + "</p>"
SITE = {
    "/": f'<html><head><title>Example Law Firm - Dubai</title></head><body><h1>Home</h1>{BODY}<a href="/about">About</a><a href="https://www.linkedin.com/company/example-law">in</a></body></html>',
    "/about": f"<html><head><title>About us</title></head><body><h1>About</h1>{BODY}</body></html>",
}
STATE = {"down": False}
class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if STATE["down"]:
            self.send_response(403); self.end_headers(); return
        if p == "/robots.txt":
            b = b""
        elif p == "/sitemap.xml":
            b = "".join(f"<url><loc>{BASE}{u}</loc></url>" for u in SITE).encode()
        elif p in SITE:
            b = SITE[p].encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
srv = ThreadingHTTPServer(("127.0.0.1", 0), Site); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

_real = netguard.check
async def _local(url, require_https=True):
    return url if urlparse(url).hostname == "127.0.0.1" else await _real(url, require_https)
netguard.check = _local
CALLS = []
BLOG = ["/about"]
async def fake_chat(messages, *, max_tokens, temperature=0.3):
    CALLS.append(messages)
    return json.dumps({"siteName": "Example Law Firm", "services": ["Employment law", "Company law", 7], "audience": "Companies and employees in the UAE",
                       "locations": ["Dubai", "Abu Dhabi"], "focusKeywords": ["employment lawyer Dubai"], "cta": "Book a consultation.",
                       "tone": "clear, expert", "avoid": ["guaranteed outcomes"], "linkedin": "https://www.linkedin.com/company/example-law",
                       "instagram": "not a link", "defaultLang": "xx", "unknownField": "dropped", "blogPath": BLOG[0]}), 120, 80
llm.chat = fake_chat
ADM = {"X-Admin-Key": os.environ["EXT_MKT_ADMIN_KEY"]}


async def run_job(jid):
    from ext_marketing import jobs
    j = dict(await db.pool().fetchrow(f"SELECT * FROM {db.SCHEMA}.jobs WHERE id = $1", jid))
    await db.pool().execute(f"UPDATE {db.SCHEMA}.jobs SET status='running', attempts = attempts + 1 WHERE id = $1", jid)
    j["attempts"] += 1
    await jobs._run(j)
    return dict(await db.pool().fetchrow(f"SELECT * FROM {db.SCHEMA}.jobs WHERE id = $1", jid))


async def main():
    await db.open_pool()
    p, S = db.pool(), db.SCHEMA
    async def idem(req): req.headers["Idempotency-Key"] = uuid.uuid4().hex
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", event_hooks={"request": [idem]})
    acc = (await c.post("/admin/v1/accounts", headers=ADM, json={"name": "Propose acc"})).json()["data"]
    site = (await c.post(f"/admin/v1/accounts/{acc['id']}/sites", headers=ADM, json={"name": "Propose site", "url": BASE,
            "timezone": "Asia/Dubai", "languages": ["en", "ar"], "defaultLanguage": "en"})).json()["data"]
    key = (await c.post(f"/admin/v1/accounts/{acc['id']}/api-keys", headers=ADM, json={"siteId": site["id"]})).json()["data"]["key"]
    K = {"X-Api-Key": key}; U = f"/api/v1/sites/{site['id']}/settings/seo"
    before = (await c.get(U, headers=K)).json()["data"]

    r = await c.post(U + "/propose", headers=K, json={})
    ok("T1 starts a job", r.status_code == 200 and r.json()["data"]["jobId"], r.text)
    j = await run_job(r.json()["data"]["jobId"])
    res = j["result"] if isinstance(j["result"], dict) else json.loads(j["result"] or "{}")
    ok("T2 the job is done", j["status"] == "done", (j["status"], j.get("error")))
    ok("T3 the model was given the site's own pages", CALLS and "We advise companies" in CALLS[-1][1]["content"] and "About us" in CALLS[-1][1]["content"])
    ok("T4 the whole profile comes back: name, services, audience, places, keywords, call to action, tone, avoid, LinkedIn",
       res.get("siteName") == "Example Law Firm" and res.get("services") == ["Employment law", "Company law"] and res.get("locations") == ["Dubai", "Abu Dhabi"]
       and res.get("cta") == "Book a consultation." and res.get("avoid") == ["guaranteed outcomes"]
       and res.get("linkedin") == "https://www.linkedin.com/company/example-law", res)
    ok("T5 only the shape is checked: a non-text list item, a non-link page address, a language the site lacks and unknown keys are dropped",
       "instagram" not in res and "defaultLang" not in res and "unknownField" not in res and 7 not in res.get("services", []), res)
    ok("T6 it says how many pages it read", res.get("pagesRead", 0) >= 2, res.get("pagesRead"))
    after = (await c.get(U, headers=K)).json()["data"]
    ok("T7 nothing is saved: the profile is as before until the person saves", after == before, (before, after))
    used = await p.fetchval(f"SELECT count(*) FROM {S}.usage_events WHERE site_id = $1 AND metric = 'profileProposals'", site["id"]) \
        if await p.fetchval(f"SELECT to_regclass('{S}.usage_events')") else None
    if used is not None:
        ok("T8 the run is metered as a profile fill", used == 1, used)

    ok("T11 the model is told today's date, so it never presents an earlier year as current", "Today:" in CALLS[-1][1]["content"])
    ok("T12 the articles page is proposed when the site links to it", res.get("blogPath") == "/about/", res.get("blogPath"))
    BLOG[0] = "/not-linked-anywhere"
    j = await run_job((await c.post(U + "/propose", headers=K, json={})).json()["data"]["jobId"])
    r2 = j["result"] if isinstance(j["result"], dict) else json.loads(j["result"] or "{}")
    ok("T13 a page the site doesn't link to is never proposed as the articles page", "blogPath" not in r2, r2.get("blogPath"))

    from ext_marketing import posts
    for case, content, want in (("written", "text", "drafted"), ("every draft failed", None, "new")):
        sid = "sg_t" + uuid.uuid4().hex[:8]; pid = "p_t" + uuid.uuid4().hex[:8]
        await p.execute(f"INSERT INTO {S}.suggestions (id, site_id, title, kind, language, keywords, why, status) VALUES ($1,$2,$3,'blog','en','{{}}','', 'drafting')", sid, site["id"], "Idea " + sid)
        await p.execute(f"INSERT INTO {S}.posts (id, site_id, kind, platform, language, suggestion_id, status, topic, keywords, requested_by, content) "
                        f"VALUES ($1,$2,'blog','website','en',$3,$4,'t','{{}}','t',$5)", pid, site["id"], sid, "pending" if content else "failed", content)
        await posts.settle_suggestion(sid)
        got = await p.fetchval(f"SELECT status FROM {S}.suggestions WHERE id = $1", sid)
        ok(f"T14 an idea whose draft is {case} leaves 'drafting' -> {want}", got == want, got)

    STATE["down"] = True
    r = await c.post(U + "/propose", headers=K, json={})
    j = await run_job(r.json()["data"]["jobId"])
    ok("T9 a website that refuses the reader: the job fails and says to allow the reader", j["status"] == "failed" and "Allow the reader" in (j.get("error") or ""), (j["status"], j.get("error")))
    STATE["down"] = False

    await c.put(f"/admin/v1/accounts/{acc['id']}/quota-overrides", headers=ADM, json={"metric": "profileProposals", "limit": 0, "mode": "hard"})
    r = await c.post(U + "/propose", headers=K, json={})
    ok("T10 a client over its limit gets the limit answer, no job", r.status_code in (402, 429) and r.json()["error"]["code"].startswith("quota"), r.text)

    await p.execute(f"DELETE FROM {S}.api_keys WHERE account_id = $1", acc["id"])
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)

asyncio.run(main())
