"""Site app routes: review (approve / changes / reject / hold), publish now, bulk, the publishing settings and the queue."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, jobs, locks, posts, publishing as PB, validate
from .auth import ApiError, audit, caller, site_of
from .routes_client import body, ok
from .routes_posts import _post, delete_post

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")


async def _opt(req: Request) -> dict:
    try:
        return await body(req)
    except ApiError:
        return {}


async def _guarded(c, site_id: str, pid: str) -> dict:
    p = await _post(site_id, pid)
    locks.guard(c, p)                                                   # someone else is editing it
    return p


# ---------------------------------------------------------------- publishing settings
@r.get("/sites/{site_id}/settings/publishing")
async def get_publishing(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    cfg, ver = await PB.cadence(site_id)
    return ok({**cfg, "version": ver})


@r.put("/sites/{site_id}/settings/publishing")
async def put_publishing(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    cur, ver = await PB.cadence(site_id)
    if int(d.pop("version", 0) or 0) != ver:
        raise ApiError(409, "version_conflict", "These settings were saved meanwhile. Reload and try again.")
    base = (await db.settings(["publishing_defaults"])).get("publishing_defaults") or {}
    f = {k: "Unknown setting" for k in sorted(set(d) - set(base))} or PB.check_cadence({**cur, **d})
    if f:
        raise validate.bad(f)
    await db.pool().execute(
        f"""INSERT INTO {S}.site_settings (site_id, grp, value, version, updated_by) VALUES ($1,'publishing',$2,1,$3)
            ON CONFLICT (site_id, grp) DO UPDATE SET value = {S}.site_settings.value || EXCLUDED.value, version = {S}.site_settings.version + 1,
              updated_by = EXCLUDED.updated_by, updated_at = now()""", site_id, d, c.name)
    await audit(c.name, "publishing.settings", account_id=c.account_id, site_id=site_id, detail={"keys": list(d)})
    return await get_publishing(site_id, req)


# ---------------------------------------------------------------- the queue
@r.get("/sites/{site_id}/scheduled")
async def scheduled(site_id: str, req: Request, page: int = 1, pageSize: int = 0):
    c = await caller(req); site = await site_of(c, site_id)
    s = await db.settings(["page_size_default", "page_size_max"])
    size = max(1, min(int(pageSize or s.get("page_size_default") or 25), int(s.get("page_size_max") or 100)))
    st = await PB.state(site)
    rows, why, nxt = PB.plan(site, st)
    part = rows[(max(1, page) - 1) * size: max(1, page) * size]
    items = [{**await posts.shape(x["post"], site_id, full=False), "position": x["position"], "expectedAt": x["expectedAt"], "waiting": x["waiting"]}
             for x in part]
    return ok({"autoPublish": bool(st["cfg"].get("autoPublish")), "nextSlotAt": nxt, "reason": why, "publishedToday": st["today"],
               "perDay": st["cfg"].get("perDay"), "timezone": site["timezone"], "items": items},
              {"page": max(1, page), "pageSize": size, "total": len(rows)})


# ---------------------------------------------------------------- review
async def _approve(c, site_id: str, pid: str, reason) -> dict:
    p = await _guarded(c, site_id, pid)
    if p["status"] != "pending":
        raise ApiError(409, "wrong_status", f"Only a post waiting for review can be approved (this one is {p['status']}).")
    crit = [f for f in p["findings"] or [] if f.get("level") == "critical"]
    hard = [f for f in crit if f.get("overridable") is False]
    reason = " ".join(str(reason or "").split())
    if hard or (crit and not reason):
        raise ApiError(422, "framework_blocked",
                       "This post fails a required check that cannot be overridden. Fix it first." if hard else
                       "This post fails a required check. Fix it, or approve with a reason.",
                       findings=[f["message"] for f in (hard or crit)], details={"overridable": not hard})
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'approved', approved_at = now(), reviewed_by = $3, override_reason = $4, hold = false,
               error = NULL, version = version + 1, updated_at = now() WHERE id = $1 AND site_id = $2 AND status = 'pending' RETURNING *""",
        pid, site_id, c.user, reason if crit else None)
    if not row:
        raise ApiError(409, "wrong_status", "The post changed meanwhile. Reload and try again.")
    await audit(c.name, "post.approve", account_id=c.account_id, site_id=site_id, target=pid,
                detail={"override": reason, "critical": [f["message"] for f in crit]} if crit else None)
    return dict(row)


@r.post("/sites/{site_id}/posts/{pid}/approve")
async def approve(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    return ok(await posts.shape(await _approve(c, site_id, pid, (await _opt(req)).get("overrideReason")), site_id))


@r.post("/sites/{site_id}/posts/{pid}/changes")
async def changes(site_id: str, pid: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    await _guarded(c, site_id, pid)
    note = " ".join(str((await _opt(req)).get("note") or "").split())
    if not note:
        raise validate.bad({"note": "Say what should change"})
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'drafting', review_note = $3, approved_at = NULL, hold = false, reviewed_by = $4, error = NULL,
               version = version + 1, updated_at = now()
            WHERE id = $1 AND site_id = $2 AND status IN ('pending','approved','failed') RETURNING *""", pid, site_id, note[:2000], c.user)
    if not row:
        p = await _post(site_id, pid)
        raise ApiError(409, "wrong_status", f"Changes can be asked for a post waiting for review, approved, or failed (this one is {p['status']}).")
    jid = await jobs.enqueue(site["account_id"], site_id, "post.twin" if row["source_id"] else "post.draft", {"postId": pid})
    await audit(c.name, "post.changes", account_id=c.account_id, site_id=site_id, target=pid, detail={"note": note[:500]})
    return ok({"jobId": jid, "postId": pid, "status": "drafting"})


async def _reject(c, site_id: str, pid: str, note) -> dict:
    await _guarded(c, site_id, pid)
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'rejected', review_note = $3, reviewed_by = $4, approved_at = NULL, hold = false,
               version = version + 1, updated_at = now()
            WHERE id = $1 AND site_id = $2 AND status IN ('pending','approved','failed') RETURNING *""",
        pid, site_id, (" ".join(str(note or "").split()) or None), c.user)
    if not row:
        p = await _post(site_id, pid)
        raise ApiError(409, "wrong_status", f"A post that is {p['status']} cannot be rejected.")
    await audit(c.name, "post.reject", account_id=c.account_id, site_id=site_id, target=pid)
    return dict(row)


@r.post("/sites/{site_id}/posts/{pid}/reject")
async def reject(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    return ok(await posts.shape(await _reject(c, site_id, pid, (await _opt(req)).get("note")), site_id))


async def _hold(c, site_id: str, pid: str, hold: bool) -> dict:
    await _guarded(c, site_id, pid)
    row = await db.pool().fetchrow(
        f"UPDATE {S}.posts SET hold = $3, version = version + 1, updated_at = now() WHERE id = $1 AND site_id = $2 AND status = 'approved' RETURNING *",
        pid, site_id, hold)
    if not row:
        p = await _post(site_id, pid)
        raise ApiError(409, "wrong_status", f"Only an approved post in the queue can be held or resumed (this one is {p['status']}).")
    await audit(c.name, "post.hold" if hold else "post.resume", account_id=c.account_id, site_id=site_id, target=pid)
    return dict(row)


@r.post("/sites/{site_id}/posts/{pid}/hold")
async def hold(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    h = (await _opt(req)).get("hold", True)
    if not isinstance(h, bool):
        raise validate.bad({"hold": "true or false"})
    return ok(await posts.shape(await _hold(c, site_id, pid, h), site_id))


async def _publish(c, site_id: str, pid: str, section) -> dict:
    p = await _guarded(c, site_id, pid)
    kinds = (await db.settings(["publish_kinds"])).get("publish_kinds") or []
    if p["kind"] not in kinds:
        raise ApiError(409, "no_channel", f"Posting to {p['platform']} is not connected yet; this post waits in the queue.")
    if section is not None:
        if not isinstance(section, str):
            raise validate.bad({"section": "A website category slug"})
        await db.pool().execute(f"UPDATE {S}.posts SET section = $2 WHERE id = $1 AND status IN ('approved','failed')", pid, section or None)
    q = await PB.claim(site_id, pid)
    if not q:
        raise ApiError(409, "wrong_status", f"Only an approved post, or one whose publishing failed, can be published (this one is {p['status']}).")
    try:
        row = await PB.run(q, auto=False)
    except PB.Failed as e:
        raise ApiError(502, "publish_failed", e.message)
    await audit(c.name, "post.publish", account_id=c.account_id, site_id=site_id, target=pid, detail={"url": (row["published"] or {}).get("url")})
    return row


@r.post("/sites/{site_id}/posts/{pid}/publish")
async def publish_now(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    return ok(await posts.shape(await _publish(c, site_id, pid, (await _opt(req)).get("section")), site_id))


@r.post("/sites/{site_id}/posts/bulk")
async def bulk(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    run = {"approve": lambda i: _approve(c, site_id, i, d.get("overrideReason")), "reject": lambda i: _reject(c, site_id, i, d.get("note")),
           "publishNow": lambda i: _publish(c, site_id, i, None), "hold": lambda i: _hold(c, site_id, i, True),
           "resume": lambda i: _hold(c, site_id, i, False), "delete": lambda i: delete_post(site_id, i, req)}.get(d.get("action"))
    ids = [x for x in (d.get("ids") or []) if isinstance(x, str)]
    if not run or not ids:
        raise validate.bad({**({"action": "One of approve, reject, delete, publishNow, hold, resume"} if not run else {}),
                            **({"ids": "One or more post ids"} if not ids else {})})
    done, failed = [], []
    for pid in ids:
        try:
            await run(pid)
            done.append(pid)
        except ApiError as e:
            failed.append({"id": pid, "code": e.code, "message": e.message, **({"findings": e.extra["findings"]} if e.extra.get("findings") else {})})
    return ok({"done": done, "failed": failed})
