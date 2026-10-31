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
RUN = uuid.uuid4().hex[:6]
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

    # ---- an article and its translation are one: published together, counted once
    await p.execute(f"UPDATE {S}.accounts SET max_publishes_per_day = 2 WHERE id = $1", acc["id"])
    en = await post("approved", pair=RUN + "pr1"); ar = await post("pending", "ar", pair=RUN + "pr1", source=en)
    d = (await c.get(f"{B}/posts/{en}", headers=K)).json()["data"]
    ok("T6 while the translation isn't approved, the article can't go out, and says which one to approve",
       "publishNow" not in d["actions"] and d["waitsFor"] == {"id": ar, "language": "ar"}, (d["actions"], d.get("waitsFor")))
    r = await c.post(f"{B}/posts/{en}/publish", headers=K, json={})
    ok("T7 ... publishing it anyway is refused (pair_not_ready)", r.status_code == 409 and r.json()["error"]["code"] == "pair_not_ready", r.text[:200])
    await p.execute(f"UPDATE {S}.posts SET status = 'approved', approved_at = now() WHERE id = $1", ar)
    r = await c.post(f"{B}/posts/{ar}/publish", headers=K, json={})
    st = await p.fetchval(f"SELECT status FROM {S}.posts WHERE id = $1", en)
    ok("T8 publishing either one sends both, the English (source) first", r.status_code == 200 and st == "published" and SENT[-2:] == [en, ar], (r.status_code, st, SENT[-2:]))
    en4 = await post("approved", pair=RUN + "pr4"); ar4 = await post("approved", "ar", pair=RUN + "pr4", source=en4)
    r = await c.post(f"{B}/posts/bulk", headers=K, json={"action": "publishNow", "ids": [ar4, en4]})
    ok("T10 both picked in bulk: one article, English first, nothing fails",
       r.json()["data"]["failed"] == [] and SENT[-2:] == [en4, ar4], (r.json()["data"], SENT[-2:]))

    en5 = await post("approved", pair=RUN + "pr5"); ar5 = await post("approved", "ar", pair=RUN + "pr5", source=en5)
    r = await c.post(f"{B}/posts/{en5}/publish", headers=K, json={})
    st = await p.fetchval(f"SELECT status FROM {S}.posts WHERE id = $1", ar5)
    ok("T11 the account's daily limit (2 articles; a pair is one): the third is refused, nothing goes out",
       r.status_code == 429 and r.json()["error"]["code"] == "daily_cap" and st == "approved", (r.status_code, r.text[:200], st))
    cfg = (await c.get(f"{B}/settings/publishing", headers=K)).json()["data"]
    ok("T12 the schedule shows the limit", cfg["maxPerDay"] == 2, cfg.get("maxPerDay"))
    r = await c.put(f"{B}/settings/publishing", headers=K, json={"version": cfg["version"], "perDay": 5})
    ok("T13 more a day than the account's limit can't be set", r.status_code == 400 and "perDay" in r.json()["error"]["fields"], r.text[:200])
    from ext_marketing import publishing as PB2
    rows, why, _ = PB2.plan(dict(await p.fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", site["id"])),
                            await PB2.state(dict(await p.fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", site["id"]))))
    w = {x["post"]["id"]: x["waiting"] for x in rows}
    ok("T14 the scheduler: the translation goes with its article, never on its own slot", w.get(ar5) == "source_not_published", w)
    await p.execute(f"UPDATE {S}.accounts SET max_publishes_per_day = NULL WHERE id = $1", acc["id"])
    await p.execute(f"UPDATE {S}.settings SET value = 'null'::jsonb WHERE key = 'publish_daily_cap_default'")
    en6 = await post("approved", pair=RUN + "pr6"); ar6 = await post("approved", "ar", pair=RUN + "pr6", source=en6)
    await PB2.claim(site["id"], en6)
    await PB2.publish_job({"id": "job_t", "payload": {"postId": en6, "auto": True}})
    st = await p.fetchval(f"SELECT status FROM {S}.posts WHERE id = $1", ar6)
    ok("T15 an automatic publish sends the article and then its translation", st == "published" and SENT[-2:] == [en6, ar6], (st, SENT[-2:]))
    await p.execute(f"UPDATE {S}.settings SET value = '3'::jsonb WHERE key = 'publish_daily_cap_default'")

    await p.execute(f"DELETE FROM {S}.api_keys WHERE account_id = $1", acc["id"])
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)

asyncio.run(main())
