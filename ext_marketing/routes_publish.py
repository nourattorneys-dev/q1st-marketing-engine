"""Site app routes: review (approve / changes / reject / hold), publish now, bulk, the publishing settings and the queue."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, jobs, locks, posts, publishing as PB, quotas, validate
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
    site = await db.pool().fetchrow(f"SELECT timezone FROM {S}.sites WHERE id = $1", site_id)
    return ok({**cfg, "version": ver, "maxPerDay": await PB.daily_cap(c.account_id),
               "publishedToday": await PB.published_today(c.account_id, PB.ZoneInfo(site["timezone"] or "UTC"))})


@r.put("/sites/{site_id}/settings/publishing")
async def put_publishing(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    cur, ver = await PB.cadence(site_id)
    if int(d.pop("version", 0) or 0) != ver:
        raise ApiError(409, "version_conflict", "These settings were saved meanwhile. Reload and try again.")
    base = (await db.settings(["publishing_defaults"])).get("publishing_defaults") or {}
    d.pop("maxPerDay", None); d.pop("publishedToday", None)            # set by Quantum1st / counted here; shown only
    f = {k: "Unknown setting" for k in sorted(set(d) - set(base))} or PB.check_cadence({**cur, **d})
    cap = await PB.daily_cap(c.account_id)
    if not f and cap is not None and "perDay" in d and isinstance(d["perDay"], int) and d["perDay"] > cap:
        f = {"perDay": f"At most {cap} a day on this account."}
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
    items = []
    for x in part:
        p = await posts.shape(x["post"], site_id, full=False)
        p["schedule"] = {**(p.get("schedule") or {}), "hold": x["post"]["hold"], "position": x["position"], "scheduledAt": x["expectedAt"]}
        items.append({**p, "waiting": x["waiting"]})                     # waiting: why this one has no slot (on_hold, no_channel, ...)
    return ok({"autoPublish": bool(st["cfg"].get("autoPublish")), "nextSlotAt": nxt, "waitingReason": why, "publishedToday": st["today"],
               "perDay": st["cfg"].get("perDay"), "timezone": site["timezone"], "posts": items},
              {"page": max(1, page), "pageSize": size, "total": len(rows)})


# ---------------------------------------------------------------- review
async def _approve(c, site_id: str, pid: str, reason) -> dict:
    p = await _guarded(c, site_id, pid)
    if p["status"] != "pending":
        raise ApiError(409, "wrong_status", f"Only a post waiting for review can be approved (this one is {p['status']}).")
    crit = [f for f in p["findings"] or [] if f.get("level") == "critical"]
    hard = [f for f in crit if f.get("overridable") is False]
    reason = " ".join(str(reason or "").split())
    need = bool((await PB.cadence(site_id))[0].get("overrideReasonRequired"))
    if hard or (crit and need and not reason):
        raise ApiError(422, "framework_blocked",
                       "This post fails a required check that cannot be overridden. Fix it first." if hard else
                       "This post fails a required check. Fix it, or approve with a reason.",
                       findings=[f["message"] for f in (hard or crit)], details={"overridable": not hard})
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'approved', approved_at = now(), reviewed_by = $3, override_reason = $4, hold = false,
               approved_critical = $5, error = NULL, version = version + 1, updated_at = now()
            WHERE id = $1 AND site_id = $2 AND status = 'pending' RETURNING *""",
        pid, site_id, c.user, (reason or None) if crit else None, len(crit))
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
    await quotas.check(c.account_id, site_id, {"aiTokens": 0})          # a new draft is AI work
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
    """Publishes the post with its translation (or source) as one article: the source first, then the translation. Both
    must be approved; it counts once against the account's articles per day and the plan's publishes."""
    p = await _guarded(c, site_id, pid)
    kinds = (await db.settings(["publish_kinds"])).get("publish_kinds") or []
    if p["kind"] not in kinds:
        raise ApiError(409, "no_channel", f"Posting to {p['platform']} is not connected yet; this post waits in the queue.")
    if section is not None:
        if not isinstance(section, str):
            raise validate.bad({"section": "A website category slug"})
        await db.pool().execute(f"UPDATE {S}.posts SET section = $2 WHERE id = $1 AND status IN ('approved','failed')", pid, section or None)
    members = await PB.pair(dict(p))
    todo = [m for m in members if m["status"] != "published"]
    for m in members:
        if m["id"] != pid and m["status"] not in PB.READY:
            raise ApiError(409, "pair_not_ready", f"Approve the {m['language'].upper()} version too: an article and its translation are published together.",
                           details={"postId": m["id"], "language": m["language"]})
    new_article = any(not m.get("source_id") for m in todo)
    site = await db.pool().fetchrow(f"SELECT timezone FROM {S}.sites WHERE id = $1", site_id)
    cap = await PB.daily_cap(c.account_id)
    if new_article and cap is not None and await PB.published_today(c.account_id, PB.ZoneInfo(site["timezone"] or "UTC")) >= cap:
        raise ApiError(429, "daily_cap", f"Today's {cap} articles are already published. It goes out tomorrow, or ask Quantum1st to raise the daily limit.")
    if new_article:
        await quotas.check(c.account_id, site_id, {"publishes": 1})
    out = None
    for m in todo:
        q = await PB.claim(site_id, m["id"])
        if not q:
            raise ApiError(409, "wrong_status", f"Only an approved post, or one whose publishing failed, can be published (this one is {m['status']}).")
        try:
            row = await PB.run(q, auto=False)
        except PB.Failed as e:
            raise ApiError(502, "publish_failed", e.message)
        await audit(c.name, "post.publish", account_id=c.account_id, site_id=site_id, target=m["id"], detail={"url": (row["published"] or {}).get("url")})
        if m["id"] == pid:
            out = row
    return out or dict(await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1", pid))


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
    if d.get("action") == "publishNow":                                 # an English version before its translation
        src = {r["id"]: r["source_id"] for r in await db.pool().fetch(f"SELECT id, source_id FROM {S}.posts WHERE id = ANY($1)", ids)}
        ids = sorted(ids, key=lambda i: src.get(i) in ids)
    done, failed = [], []
    if d.get("action") == "publishNow":                                 # a pair goes out once, whichever of the two was picked
        seen, kept = set(), []
        for pid in ids:
            pr = await db.pool().fetchval(f"SELECT pair_id FROM {S}.posts WHERE id = $1", pid)
            if pr and pr in seen:
                done.append(pid); continue
            seen.add(pr); kept.append(pid)
        ids = kept
    for pid in ids:
        try:
            await run(pid)
            done.append(pid)
        except ApiError as e:
            failed.append({"id": pid, "code": e.code, "message": e.message, **({"findings": e.extra["findings"]} if e.extra.get("findings") else {})})
    return ok({"done": done, "failed": failed})
