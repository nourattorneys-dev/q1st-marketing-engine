"""Offline: Test connection stores what it learned about the website when it tested the saved connection - also when the
form sends its fields along (as the dashboard does) - and never when it tested a different address or a new token."""
import asyncio, os, sys
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
from ext_marketing import routes_website as RW
P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

SAVED = {"site_id": "s1", "config": {"type": "strapi", "settings": {"url": "https://cms.law.example", "collection": "articles", "siteUrl": "https://law.example"},
         "fieldMap": {}, "learned": {}}, "secret_enc": b"x", "status": "ok"}
stored = []
class Pool:
    async def execute(self, q, *a): stored.append(a)
class C: name, account_id = "t", "a"
async def caller(req): return C()
async def site_of(c, sid): return None
async def audit(*a, **k): return None
async def status(*a): return None
async def row(sid): return SAVED
async def check(t): return {}
async def test(t, secret): return {"categories": [], "learned": {"locales": ["en", "ar"], "schema": {}}}
RW.caller, RW.site_of, RW.audit, RW._status = caller, site_of, audit, status
RW.W.row, RW.W.check, RW.W.test = row, check, test
RW.security.decrypt = lambda b: "token"
RW.db.pool = lambda: Pool()
def call(payload):
    async def body(req): return payload
    RW.body = body
    stored.clear()
    asyncio.run(RW.test_website("s1", None))
    return list(stored)

ok("dashboard form (type + same fields sent): learned is stored", call({"type": "strapi", "settings": {"url": "https://cms.law.example", "collection": "articles", "siteUrl": "https://law.example"}}))
ok("nothing sent: learned is stored", call({}))
ok("other non-connection field changed (website address): still stored", call({"settings": {"siteUrl": "https://new.example"}}))
ok("different CMS address: not stored", not call({"settings": {"url": "https://other.example"}}))
ok("different collection: not stored", not call({"settings": {"collection": "posts"}}))
ok("a new token: not stored", not call({"secret": "new"}))
ok("another website type: not stored", not call({"type": "wordpress"}))
print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)
