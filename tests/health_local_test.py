"""Site health end to end against a scratch database (never the live one). A local website plays the client's site, a local
fake WordPress plays its REST API, and a stand-in model writes the proposals (its first answer is too long on purpose).
Run: PYTHONPATH=<dir with shared/kb_sources.py> EXT_MKT_DSN=... EXT_MKT_ENC_KEY=... EXT_MKT_ADMIN_KEY=... python3 tests/health_local_test.py"""
import asyncio, json, os, re, sys, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
import httpx
from ext_marketing import db, health, llm, netguard, security, webhooks
from ext_marketing.server import app

P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

D = "A clear description of what this page offers its visitors, long enough to sit inside the band."
def pg(title, desc=D, body="<h1>Main</h1>"):
    desc = f"{desc} ({title})" if desc == D else desc                    # every page its own description unless a test shares one
    d = f'<meta name="description" content="{desc}">' if desc else ""
    return f'<html lang="en"><head><title>{title}</title>{d}</head><body>{body}</body></html>'
SITE = {
    "/": pg("Fake Law Firm Dubai - employment and company law", body='<h1>Home</h1><a href="/services">S</a><a href="/about">A</a><a href="/old-page">old</a>'),
    "/services": pg("Our services: employment and company law in Dubai", desc=None, body='<h1>Services</h1><img src="/a.jpg"><a href="/team">T</a>'),
    "/about": pg("About us", body='<h1>About</h1><h1>Again</h1>'),
    "/team": pg("Our services: employment and company law in Dubai", body='<h2>no h1</h2><a href="/gone">g</a>'),
}
STATE = {"fixed_services": False}
class Site(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/robots.txt": return self._send(200, f"Sitemap: {BASE}/sitemap.xml\n".encode(), "text/plain")
        if p == "/sitemap.xml": return self._send(200, "".join(f"<url><loc>{BASE}{u}</loc></url>" for u in SITE).encode(), "application/xml")
        if p == "/services" and STATE["fixed_services"]:
            return self._send(200, pg("Our services: employment and company law in Dubai", body='<h1>Services</h1><img src="/a.jpg" alt="Our team"><a href="/team">T</a>').encode())
        return self._send(200, SITE[p].encode()) if p in SITE else self._send(404, b"not found")
    do_HEAD = do_GET
    def log_message(self, *a): pass
srv = ThreadingHTTPServer(("127.0.0.1", 0), Site); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

WP_META = {101: {"_yoast_wpseo_title": "", "_yoast_wpseo_metadesc": "old text"}}
WP_LOG = []
class WP(BaseHTTPRequestHandler):
    def _json(self, code, o):
        b = json.dumps(o).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        WP_LOG.append(("GET", u.path, self.headers.get("X-Q1st-Auth")))
        if u.path == "/wp-json/wp/v2/search":
            hits = [{"id": 101, "url": f"{BASE}/services/", "subtype": "page"}, {"id": 7, "url": f"{BASE}/services-old/", "subtype": "post"}]
            return self._json(200, [h for h in hits if q["search"][0].split()[0] in h["url"]])
        if u.path == "/wp-json/wp/v2/types": return self._json(200, {"post": {"rest_base": "posts"}, "page": {"rest_base": "pages"}})
        m = re.fullmatch(r"/wp-json/wp/v2/pages/(\d+)", u.path)
        if m: return self._json(200, {"meta": WP_META.get(int(m.group(1)), {})})
        return self._json(404, {"code": "rest_no_route"})
    def do_POST(self):
        u = urlparse(self.path); body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        WP_LOG.append(("POST", u.path, body))
        m = re.fullmatch(r"/wp-json/wp/v2/pages/(\d+)", u.path)
        if m:
            WP_META.setdefault(int(m.group(1)), {}).update(body.get("meta") or {})
            return self._json(200, {"id": int(m.group(1)), "meta": WP_META[int(m.group(1))]})
        return self._json(404, {"code": "rest_no_route"})
    def log_message(self, *a): pass
wps = ThreadingHTTPServer(("127.0.0.1", 0), WP); threading.Thread(target=wps.serve_forever, daemon=True).start()
WPURL = f"http://127.0.0.1:{wps.server_port}"

_real = netguard.check
async def _local(url, require_https=True):                             # the test servers are on this machine
    return url if urlparse(url).hostname == "127.0.0.1" else await _real(url, require_https)
netguard.check = _local
CALLS = []
async def fake_chat(messages, *, max_tokens, temperature=0.3):
    CALLS.append(messages)
    if len(CALLS) == 1:                                                # first answer too long: the measure must catch it
        return json.dumps({"value": "x" * 400}), 100, 50
    return json.dumps({"value": "Employment, company and contract law services for businesses in Dubai, from advice to court."}), 100, 30
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
    plan = (await c.post("/admin/v1/plans", headers=ADM, json={"name": "H plan", "quotas": [{"metric": "siteScans", "limit": 2, "mode": "hard"}]})).json()["data"]
    acc = (await c.post("/admin/v1/accounts", headers=ADM, json={"name": "H acc", "planId": plan["id"]})).json()["data"]
    site = (await c.post(f"/admin/v1/accounts/{acc['id']}/sites", headers=ADM, json={"name": "H", "url": "https://example.com",
            "timezone": "Asia/Dubai", "languages": ["en", "ar"], "defaultLanguage": "en"})).json()["data"]
    await p.execute(f"UPDATE {S}.sites SET url = $2 WHERE id = $1", site["id"], BASE)
    key = (await c.post(f"/admin/v1/accounts/{acc['id']}/api-keys", headers=ADM, json={"siteId": site["id"]})).json()["data"]["key"]
    K = {"X-Api-Key": key}; A = f"/api/v1/sites/{site['id']}"
    await p.execute(f"""INSERT INTO {S}.site_connections (site_id, type, status, config, secret_enc) VALUES ($1,'website','ok',$2,$3)""",
                    site["id"], {"type": "wordpress", "settings": {"url": WPURL, "username": "articles-ai", "postType": "post"}, "learned": {"yoast": "22.0"}},
                    security.encrypt("app pass"))

    r = (await c.post(f"{A}/site/scan", headers=K)).json()["data"]
    r2 = (await c.post(f"{A}/site/scan", headers=K)).json()["data"]
    ok("T1 scan starts a job; asking again while it waits gives the same job", r["jobId"] == r2["jobId"] and r2["status"] == "running", (r, r2))
    j = await run_job(r["jobId"])
    ok("T2 the scan job finishes", j["status"] == "done", j)
    res = j["result"]
    fs = (await c.get(f"{A}/site/findings", headers=K, params={"pageSize": 100})).json()
    got = sorted((urlparse(f["url"]).path, f["code"], f["level"]) for f in fs["data"])
    want = sorted([("/services", "missing_description", "critical"), ("/services", "img_alt", "warn"), ("/about", "title_length", "warn"),
                   ("/about", "many_h1", "info"), ("/team", "missing_h1", "warn"), ("/services", "duplicate_title", "warn"),
                   ("/team", "duplicate_title", "warn"), ("/old-page", "broken_link", "critical"), ("/gone", "broken_link", "critical")])
    ok("T3 every problem of the test site is found, with the level from settings", got == want, got)
    bl = next(f for f in fs["data"] if f["url"].endswith("/old-page"))
    det = await p.fetchval(f"SELECT detail FROM {S}.site_findings WHERE id = $1", bl["id"])
    ok("T4 a broken link names the page that links to it (once)", [urlparse(u).path or "/" for u in det["linkedFrom"]] == ["/"], det)
    ok("T5 findings come critical first; the list has paging", fs["data"][0]["level"] == "critical" and fs["meta"]["total"] == 9, (fs["data"][0], fs["meta"]))
    ok("T6 shape: id, url, level, issue, fix (advice), status open", set(fs["data"][0]) >= {"id", "url", "level", "issue", "fix", "status"} and
       all(f["status"] == "open" and f["fix"] for f in fs["data"]), fs["data"][0])
    crit = (await c.get(f"{A}/site/findings", headers=K, params={"level": "critical"})).json()["data"]
    ok("T7 filter by level", {f["level"] for f in crit} == {"critical"} and len(crit) == 3, len(crit))
    ok("T8 a wrong filter is refused on the field", list((await c.get(f"{A}/site/findings", headers=K, params={"status": "nope"})).json()["error"]["fields"]) == ["status"])
    used = await p.fetchval(f"SELECT sum(qty) FROM {S}.usage_events WHERE site_id = $1 AND metric = 'siteScans'", site["id"])
    ok("T9 one scan is metered", used == 1, used)

    md = next(f for f in fs["data"] if f["code"] == "missing_description")
    h1 = next(f for f in fs["data"] if f["code"] == "missing_h1")
    r = (await c.post(f"{A}/site/fixes", headers=K, json={"ids": [md["id"], h1["id"]]})).json()["data"]
    j = await run_job(r["jobId"])
    ok("T10 prepare: the description is written; the heading is left to the website", j["result"]["prepared"] == [md["id"]] and j["result"]["manual"] == [h1["id"]], j["result"])
    ok("T11 the too-long first answer was measured and asked again", len(CALLS) == 2 and "must be 70–155" in CALLS[1][-1]["content"], len(CALLS))
    row = await p.fetchrow(f"SELECT * FROM {S}.site_findings WHERE id = $1", md["id"])
    ok("T12 the finding is prepared, fix shows the proposal", row["status"] == "prepared" and row["fix"].startswith("Proposed: Employment") and
       70 <= len(row["fix_data"]["value"]) <= 155, dict(row))
    ok("T13 the model saw the page's own text", "PAGE TEXT" in CALLS[0][1]["content"] and "Services" in CALLS[0][1]["content"])
    tok = await p.fetchval(f"SELECT sum(tokens_in + tokens_out) FROM {S}.usage_events WHERE site_id = $1 AND metric = 'siteScans'", site["id"])
    ok("T14 the AI tokens of the proposals are metered (no extra scan counted)", tok == 280 and
       await p.fetchval(f"SELECT sum(qty) FROM {S}.usage_events WHERE site_id = $1 AND metric = 'siteScans'", site["id"]) == 1, tok)

    r = (await c.post(f"{A}/site/findings/bulk", headers=K, json={"action": "apply", "ids": [md["id"], h1["id"], "sf_nope"]})).json()["data"]
    ok("T15 apply: the prepared one is written; the others fail with a reason each", r["done"] == [md["id"]] and
       [(x["id"], x["code"]) for x in r["failed"]] == [(h1["id"], "not_prepared"), ("sf_nope", "not_found")], r)
    ok("T16 WordPress got the description on the right page (Yoast field), signed in", WP_META[101]["_yoast_wpseo_metadesc"] == row["fix_data"]["value"] and
       any(x[0] == "GET" and x[2] and x[2].startswith("Basic ") for x in WP_LOG), WP_META)
    fd = await p.fetchval(f"SELECT fix_data FROM {S}.site_findings WHERE id = $1", md["id"])
    ok("T17 what it replaced is kept (to undo by hand)", fd.get("before") == {"metaDescription": "old text"} and fd.get("externalId") == "101", fd)
    ab = next(f for f in fs["data"] if f["code"] == "title_length")
    await p.execute(f"UPDATE {S}.site_findings SET status='prepared', fix_data='{{\"field\":\"metaTitle\",\"value\":\"About our firm in Dubai and Abu Dhabi\"}}' WHERE id=$1", ab["id"])
    r = (await c.post(f"{A}/site/findings/bulk", headers=K, json={"action": "apply", "ids": [ab["id"]]})).json()["data"]
    ok("T18 a page WordPress cannot find is refused with a plain reason, nothing written", r["failed"] and r["failed"][0]["code"] == "website_refused" and
       "not a post or page" in r["failed"][0]["message"], r)
    r = (await c.post(f"{A}/site/findings/bulk", headers=K, json={"action": "dismiss", "ids": [h1["id"], md["id"]]})).json()["data"]
    ok("T19 dismiss: an open one is dismissed; an applied one is not", r["done"] == [h1["id"]] and r["failed"][0]["code"] == "invalid_state", r)

    STATE["fixed_services"] = True                                       # the site is fixed by hand: /services has its alt text now
    SITE["/team"] = pg("Our team of employment and company lawyers", body='<h2>no h1</h2><a href="/gone">g</a>')
    r = (await c.post(f"{A}/site/scan", headers=K)).json()["data"]; await run_job(r["jobId"])
    after = {(urlparse(f["url"]).path, f["code"]): f["status"] for f in (await c.get(f"{A}/site/findings", headers=K, params={"pageSize": 100})).json()["data"]}
    ok("T20 rescan: fixed problems are gone (img alt, duplicate titles)", ("/services", "img_alt") not in after and ("/team", "duplicate_title") not in after, after)
    ok("T21 rescan: a dismissed one stays dismissed", after.get(("/team", "missing_h1")) == "dismissed", after)
    ok("T22 rescan: the applied description stays applied (the live page now has one)", after.get(("/services", "missing_description")) == "applied", after)
    ok("T23 rescan: what is still wrong stays open", after.get(("/old-page", "broken_link")) == "open" and after.get(("/about", "many_h1")) == "open", after)
    r = (await c.post(f"{A}/site/scan", headers=K)).json()
    ok("T24 the third scan passes the hard limit of 2: refused 429 quota_exceeded", r.get("error", {}).get("code") == "quota_exceeded", r)
    print(f"\nPASSED {P}   FAILED {F}")


asyncio.run(main())
sys.exit(1 if F else 0)
