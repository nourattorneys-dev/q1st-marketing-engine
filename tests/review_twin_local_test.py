"""Approving with critical findings (reason only where the client requires one; the count is always kept) and an Arabic
translation stored on its English page (it waits for the English one; publishing the English one can take it along).
Scratch database only; publishing to the website is replaced by a stand-in.
Run: PYTHONPATH=<dir with shared/> EXT_MKT_DSN=... EXT_MKT_ENC_KEY=... EXT_MKT_ADMIN_KEY=... python3 tests/review_twin_local_test.py"""
import asyncio, json, os, sys, uuid
from datetime import datetime, timezone
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
import httpx
from ext_marketing import db, publishing as PB
from ext_marketing.server import app

P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

SENT = []
async def fake_run(p, *, auto, job_id=None):
    SENT.append(p["id"])
    pub = {"target": "wordpress", "url": f"https://example.com/{p['id']}/", "externalId": str(len(SENT)), "at": datetime.now(timezone.utc).isoformat(), "status": "publish"}
    return dict(await db.pool().fetchrow(f"UPDATE {db.SCHEMA}.posts SET status='published', published=$2, published_at=now() WHERE id=$1 RETURNING *", p["id"], pub))
PB.run = fake_run
ADM = {"X-Admin-Key": os.environ["EXT_MKT_ADMIN_KEY"]}
CRIT = [{"level": "critical", "code": "x", "message": "A required check fails", "overridable": True}]


async def main():
    await db.open_pool()
    p, S = db.pool(), db.SCHEMA
    async def idem(req): req.headers["Idempotency-Key"] = uuid.uuid4().hex
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", event_hooks={"request": [idem]})
    acc = (await c.post("/admin/v1/accounts", headers=ADM, json={"name": "Twin acc"})).json()["data"]
    site = (await c.post(f"/admin/v1/accounts/{acc['id']}/sites", headers=ADM, json={"name": "Twin site", "url": "https://example.com",
            "timezone": "Asia/Dubai", "languages": ["en", "ar"], "defaultLanguage": "en"})).json()["data"]
    key = (await c.post(f"/admin/v1/accounts/{acc['id']}/api-keys", headers=ADM, json={"siteId": site["id"]})).json()["data"]["key"]
    K = {"X-Api-Key": key}; B = f"/api/v1/sites/{site['id']}"
    await p.execute(f"INSERT INTO {S}.site_connections (site_id, type, config, secret_enc, status) VALUES ($1,'website',$2,$3,'ok')",
                    site["id"], {"type": "wordpress", "settings": {}, "arabic": {"mode": "translatepress"}}, b"x")

    async def post(status, lang="en", findings=None, pair=None, source=None, kind="blog"):
        pid = "p_" + uuid.uuid4().hex[:12]
        await p.execute(f"""INSERT INTO {S}.posts (id, site_id, kind, platform, language, pair_id, source_id, status, topic, title, content, findings,
                            approved_at, requested_by) VALUES ($1,$2,$3,'website',$4,$5,$6,$7,'t','T','<p>x</p>',coalesce($8, '[]'::jsonb),
                            CASE WHEN $7 = 'approved' THEN now() END,'t')""", pid, site["id"], kind, lang, pair, source, status, findings)
        return pid

    a = await post("pending", findings=CRIT)
    r = await c.post(f"{B}/posts/{a}/approve", headers=K, json={})
    ok("T1 with critical findings and no reason: approved (a reason is optional by default)", r.status_code == 200 and r.json()["data"]["status"] == "approved", r.text[:200])
    ok("T2 the number of critical findings it was approved with is kept", r.json()["data"]["approvedWithCritical"] == 1, r.json()["data"].get("approvedWithCritical"))
    cfg = (await c.get(f"{B}/settings/publishing", headers=K)).json()["data"]
    r = await c.put(f"{B}/settings/publishing", headers=K, json={"version": cfg["version"], "overrideReasonRequired": True})
    ok("T3 a client can require a reason", r.status_code == 200 and r.json()["data"]["overrideReasonRequired"] is True, r.text[:200])
    b = await post("pending", findings=CRIT)
    r = await c.post(f"{B}/posts/{b}/approve", headers=K, json={})
    ok("T4 ... then approving with critical findings and no reason is refused", r.status_code == 422, r.status_code)
    r = await c.post(f"{B}/posts/{b}/approve", headers=K, json={"overrideReason": "Checked by the lawyer"})
    ok("T5 ... and accepted with a reason", r.status_code == 200 and r.json()["data"]["overrideReason"] == "Checked by the lawyer", r.text[:200])

    pair = "pr_" + uuid.uuid4().hex[:8]
    en = await post("approved", pair=pair)
    ar = await post("approved", "ar", pair=pair, source=en)
    d = (await c.get(f"{B}/posts/{ar}", headers=K)).json()["data"]
    ok("T6 an Arabic translation whose English page isn't live can't be published now, and says why", "publishNow" not in d["actions"] and d["waitsForSource"] is True, (d["actions"], d["waitsForSource"]))
    d = (await c.get(f"{B}/posts/{en}", headers=K)).json()["data"]
    ok("T7 its English version offers to take the approved translation along", d["canPublishTwin"] is True and "publishNow" in d["actions"], d.get("canPublishTwin"))
    r = await c.post(f"{B}/posts/{en}/publish", headers=K, json={"withTwin": True})
    st = await p.fetchval(f"SELECT status FROM {S}.posts WHERE id = $1", ar)
    ok("T8 publishing the English one with its translation puts both live, English first", r.status_code == 200 and st == "published" and SENT[-2:] == [en, ar], (r.status_code, st, SENT))

    en2 = await post("approved", pair="pr2"); ar2 = await post("approved", "ar", pair="pr2", source=en2)
    await c.post(f"{B}/posts/{en2}/publish", headers=K, json={})
    st = await p.fetchval(f"SELECT status FROM {S}.posts WHERE id = $1", ar2)
    d = (await c.get(f"{B}/posts/{ar2}", headers=K)).json()["data"]
    ok("T9 without 'take it along' only the English one goes; the translation can then be published on its own", st == "approved" and "publishNow" in d["actions"] and not d["waitsForSource"], (st, d["actions"]))
    await p.execute(f"UPDATE {S}.site_connections SET config = config || '{{\"arabic\": {{\"mode\": \"separate\"}}}}'::jsonb WHERE site_id = $1", site["id"])
    en3 = await post("approved", pair="pr3"); ar3 = await post("approved", "ar", pair="pr3", source=en3)
    d = (await c.get(f"{B}/posts/{ar3}", headers=K)).json()["data"]
    ok("T10 where Arabic is a separate post, it never waits for the English one", "publishNow" in d["actions"] and not d["waitsForSource"], d["actions"])

    en4 = await post("approved", pair="pr4"); ar4 = await post("approved", "ar", pair="pr4", source=en4)
    await p.execute(f"UPDATE {S}.site_connections SET config = config || '{{\"arabic\": {{\"mode\": \"translatepress\"}}}}'::jsonb WHERE site_id = $1", site["id"])
    r = await c.post(f"{B}/posts/bulk", headers=K, json={"action": "publishNow", "ids": [ar4, en4]})
    ok("T11 publishing both in one go sends the English version first, whatever order they were picked in",
       r.json()["data"]["failed"] == [] and SENT[-2:] == [en4, ar4], (r.json()["data"], SENT[-2:]))

    await p.execute(f"DELETE FROM {S}.api_keys WHERE account_id = $1", acc["id"])
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)

asyncio.run(main())
