"""Site app routes for site health (spec v1.1 §7 …/site): scan, findings, prepare fixes, apply or dismiss."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, health, jobs, quotas, validate, website
from .auth import ApiError, audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")
LEVELS = ("critical", "warn", "info")
STATUSES = ("open", "prepared", "applied", "dismissed")


def _ids(d: dict) -> list[str]:
    ids = d.get("ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
        raise validate.bad({"ids": "A list of finding ids"})
    return list(dict.fromkeys(ids))


async def _running(site_id: str, kind: str) -> str | None:
    return await db.pool().fetchval(f"SELECT id FROM {S}.jobs WHERE site_id = $1 AND kind = $2 AND status IN ('queued','running') "
                                    f"ORDER BY created_at LIMIT 1", site_id, kind)


@r.post("/sites/{site_id}/site/scan")
async def scan(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    if (jid := await _running(site_id, "site.scan")):
        return ok({"jobId": jid, "status": "running"})                  # one scan at a time: the one already going
    await quotas.check(c.account_id, site_id, {"siteScans": 1})
    jid = await jobs.enqueue(c.account_id, site_id, "site.scan", {})
    await audit(c.name, "site.scan", account_id=c.account_id, site_id=site_id, target=jid)
    return ok({"jobId": jid, "status": "queued"})


@r.get("/sites/{site_id}/site/findings")
async def findings(site_id: str, req: Request, status: str = "", level: str = "", page: int = 1, pageSize: int = 0):
    c = await caller(req); await site_of(c, site_id)
    st = [x for x in status.split(",") if x] or list(STATUSES)
    lv = [x for x in level.split(",") if x] or list(LEVELS)
    bad = {**({"status": f"Use {', '.join(STATUSES)}"} if set(st) - set(STATUSES) else {}),
           **({"level": f"Use {', '.join(LEVELS)}"} if set(lv) - set(LEVELS) else {})}
    if bad:
        raise validate.bad(bad)
    s = await db.settings(["page_size_default", "page_size_max"])
    size = max(1, min(int(pageSize or s.get("page_size_default") or 25), int(s.get("page_size_max") or 100)))
    page = max(1, page)
    where = f"site_id = $1 AND status = ANY($2) AND level = ANY($3)"
    total = await db.pool().fetchval(f"SELECT count(*) FROM {S}.site_findings WHERE {where}", site_id, st, lv)
    rows = await db.pool().fetch(
        f"""SELECT * FROM {S}.site_findings WHERE {where}
            ORDER BY array_position($4::text[], level), url, code LIMIT $5 OFFSET $6""", site_id, st, lv, list(LEVELS), size, (page - 1) * size)
    return ok([health.shape(dict(x)) for x in rows], {"page": page, "pageSize": size, "total": total})


@r.post("/sites/{site_id}/site/fixes")
async def fixes(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    ids = _ids(await body(req))
    known = {x["id"] for x in await db.pool().fetch(f"SELECT id FROM {S}.site_findings WHERE site_id = $1 AND id = ANY($2)", site_id, ids)}
    if set(ids) - known:
        raise validate.bad({"ids": "Not found: " + ", ".join(sorted(set(ids) - known))})
    await quotas.check(c.account_id, site_id, {"aiTokens": 0})
    jid = await jobs.enqueue(c.account_id, site_id, "site.fixes", {"ids": ids})
    await audit(c.name, "site.fixes", account_id=c.account_id, site_id=site_id, target=jid, detail={"ids": len(ids)})
    return ok({"jobId": jid, "status": "queued"})


@r.post("/sites/{site_id}/site/findings/bulk")
async def bulk(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    d = await body(req)
    action = d.get("action")
    if action not in ("apply", "dismiss"):
        raise validate.bad({"action": "apply or dismiss"})
    ids = _ids(d)
    rows = {x["id"]: dict(x) for x in await db.pool().fetch(f"SELECT * FROM {S}.site_findings WHERE site_id = $1 AND id = ANY($2)", site_id, ids)}
    done, failed = [], []
    for fid in ids:
        f = rows.get(fid)
        if not f:
            failed.append({"id": fid, "code": "not_found", "message": "Finding not found."}); continue
        if action == "dismiss":
            if f["status"] == "applied":
                failed.append({"id": fid, "code": "invalid_state", "message": "Already applied."}); continue
            await db.pool().execute(f"UPDATE {S}.site_findings SET status = 'dismissed', updated_at = now() WHERE id = $1", fid)
            done.append(fid); continue
        if f["status"] != "prepared" or not f["fix_data"]:
            msg = ("This finding is fixed on the website itself: follow the advice, then scan again." if f["code"] not in health.WRITES
                   else "Prepare a fix first." if f["status"] == "open" else f"It is {f['status']}.")
            failed.append({"id": fid, "code": "not_prepared", "message": msg}); continue
        try:
            res = await health.apply(site, f)
        except website.Refused as e:
            await db.pool().execute(f"UPDATE {S}.site_findings SET error = $2, updated_at = now() WHERE id = $1", fid, e.message)
            failed.append({"id": fid, "code": "website_refused", "message": e.message}); continue
        await db.pool().execute(f"""UPDATE {S}.site_findings SET status = 'applied', applied_at = now(), error = NULL, updated_at = now(),
                                       fix_data = fix_data || jsonb_build_object('before', $2::jsonb, 'externalId', $3::text) WHERE id = $1""",
                                fid, res.get("before") or {}, res.get("externalId"))
        done.append(fid)
    await audit(c.name, f"site.findings.{action}", account_id=c.account_id, site_id=site_id, detail={"done": len(done), "failed": len(failed)})
    return ok({"done": done, "failed": failed})
