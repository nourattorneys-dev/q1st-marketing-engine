"""Site app routes: rules, topic suggestions, drafts, posts (read/edit/delete/check/export), photos, media."""
from __future__ import annotations
import html as _h
import re
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import db, jobs, locks, media, meter, posts, quotas, rules as R, validate
from .auth import ApiError, audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")
EDIT = ("title", "subtitle", "content", "slug", "meta_title", "meta_description", "keywords", "hashtags", "section")
CAMEL = {"metaTitle": "meta_title", "metaDescription": "meta_description"}


async def _post(site_id: str, pid: str) -> dict:
    p = await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1 AND site_id = $2", pid, site_id)
    if not p:
        raise ApiError(404, "not_found", "Post not found.")
    return dict(p)


async def _kinds() -> tuple[list, list, list]:
    s = await db.settings(["kinds", "platforms", "languages"])
    return s.get("kinds") or [], s.get("platforms") or [], s.get("languages") or []


async def _draft_input(site: dict, d: dict) -> dict:
    kinds, plats, _ = await _kinds()
    f = {}
    topic = " ".join(str(d.get("topic") or "").split())
    if not topic:
        f["topic"] = "Required"
    kind = d.get("kind") or "blog"
    if kind not in kinds:
        f["kind"] = f"One of {', '.join(kinds)}"
    plat = d.get("platform") or ("website" if kind == "blog" else None)
    if plat not in plats:
        f["platform"] = f"One of {', '.join(plats)}"
    lg = d.get("language") or site["default_language"]
    if lg != "both" and lg not in site["languages"]:
        f["language"] = "One of the site's languages, or both"
    if kind == "blog" and len(site["languages"] or []) > 1:
        lg = "both"                                                     # an article is always every site language, published together
    kws = d.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    if f:
        raise validate.bad(f)
    return {"topic": topic, "kind": kind, "platform": plat, "language": lg, "tone": d.get("tone"), "keywords": kws[:10]}


# ---------------------------------------------------------------- the site's rules
@r.get("/sites/{site_id}/settings/rules")
async def get_rules(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    rl, ver = await R.for_site(site_id)
    return ok({**rl, "version": ver})


@r.put("/sites/{site_id}/settings/rules")
async def put_rules(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    _, ver = await R.for_site(site_id)
    if int(d.pop("version", 0) or 0) != ver:
        raise ApiError(409, "version_conflict", "The rules were saved meanwhile. Reload and try again.")
    base = (await db.settings(["default_rules"])).get("default_rules") or {}
    bad = sorted(set(d) - set(base))
    if bad:
        raise validate.bad({k: "Unknown rule" for k in bad})
    for k, v in d.items():
        if type(v) is not type(base[k]) and not (isinstance(base[k], (int, float)) and isinstance(v, (int, float))):
            raise validate.bad({k: f"Must be a {type(base[k]).__name__} like the default"})
    await db.pool().execute(
        f"""INSERT INTO {S}.site_settings (site_id, grp, value, version, updated_by) VALUES ($1,'rules',$2,1,$3)
            ON CONFLICT (site_id, grp) DO UPDATE SET value = EXCLUDED.value, version = {S}.site_settings.version + 1,
              updated_by = EXCLUDED.updated_by, updated_at = now()""", site_id, d, c.name)
    await audit(c.name, "rules.update", account_id=c.account_id, site_id=site_id, detail={"keys": list(d)})
    return await get_rules(site_id, req)


# ---------------------------------------------------------------- topics and suggestions
@r.post("/sites/{site_id}/keywords/topics")
async def topics(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    try:
        d = await body(req)
    except ApiError:
        d = {}
    await quotas.check(c.account_id, site_id, {"topicRuns": 1, "aiTokens": 0})
    jid = await jobs.enqueue(c.account_id, site_id, "topics.suggest", {"keywordIds": [x for x in (d.get("ids") or []) if isinstance(x, str)]})
    return ok({"jobId": jid, "status": "queued"})


@r.get("/sites/{site_id}/suggestions")
async def suggestions(site_id: str, req: Request, status: str = "new", kind: str = "", language: str = ""):
    c = await caller(req); await site_of(c, site_id)
    where, args = ["site_id = $1"], [site_id]
    for col, val in (("status", status), ("kind", kind), ("language", language)):
        if val:
            args.append(val.split(",")); where.append(f"{col} = ANY(${len(args)})")
    rows = await db.pool().fetch(f"SELECT * FROM {S}.suggestions WHERE {' AND '.join(where)} ORDER BY created_at DESC", *args)
    return ok([{"id": x["id"], "title": x["title"], "kind": x["kind"], "language": x["language"], "keywords": x["keywords"], "why": x["why"],
                "status": x["status"], "createdAt": x["created_at"]} for x in rows])


async def _draft_suggestion(c, site: dict, sid: str, language: str | None, platform: str | None) -> dict:
    sg = await db.pool().fetchrow(f"SELECT * FROM {S}.suggestions WHERE id = $1 AND site_id = $2 AND status = 'new'", sid, site["id"])
    if not sg:
        raise ApiError(404, "not_found", "Suggestion not found, or already drafted or dismissed.")
    inp = await _draft_input(site, {"topic": sg["title"], "kind": sg["kind"], "platform": platform, "language": language or sg["language"], "keywords": sg["keywords"]})
    ids, jids = await posts.create(site, by=c.name, suggestion_id=sid, **inp)
    await db.pool().execute(f"UPDATE {S}.suggestions SET status = 'drafting' WHERE id = $1", sid)
    return {"postIds": ids, "jobIds": jids}


@r.post("/sites/{site_id}/suggestions/{sid}/draft")
async def draft_suggestion(site_id: str, sid: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    try:
        d = await body(req)
    except ApiError:
        d = {}
    res = await _draft_suggestion(c, site, sid, d.get("language"), d.get("platform"))
    await audit(c.name, "suggestion.draft", account_id=c.account_id, site_id=site_id, target=sid)
    return ok({**res, "jobId": res["jobIds"][0]})


@r.post("/sites/{site_id}/suggestions/{sid}/dismiss")
async def dismiss_suggestion(site_id: str, sid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    n = await db.pool().execute(f"UPDATE {S}.suggestions SET status = 'dismissed' WHERE id = $1 AND site_id = $2 AND status = 'new'", sid, site_id)
    if n.endswith(" 0"):
        raise ApiError(404, "not_found", "Suggestion not found, or already drafted or dismissed.")
    return ok({"dismissed": True})


@r.post("/sites/{site_id}/suggestions/bulk")
async def bulk_suggestions(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    d = await body(req)
    if d.get("action") not in ("draft", "dismiss"):
        raise validate.bad({"action": "draft or dismiss"})
    done, failed = [], []
    for sid in [x for x in (d.get("ids") or []) if isinstance(x, str)]:
        try:
            if d["action"] == "draft":
                await _draft_suggestion(c, site, sid, d.get("language"), d.get("platform"))
            else:
                await dismiss_suggestion(site_id, sid, req)
            done.append(sid)
        except ApiError as e:
            failed.append({"id": sid, "code": e.code, "message": e.message})
    return ok({"done": done, "failed": failed})


# ---------------------------------------------------------------- drafts
@r.post("/sites/{site_id}/drafts")
async def new_draft(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    inp = await _draft_input(site, await body(req))
    ids, jids = await posts.create(site, by=c.name, **inp)
    await audit(c.name, "post.draft", account_id=c.account_id, site_id=site_id, detail={"posts": ids})
    return ok({"jobId": jids[0], "jobIds": jids, "postIds": ids, "status": "queued"})


@r.post("/sites/{site_id}/keywords/draft-articles")
async def keyword_articles(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    d = await body(req)
    ids = [x for x in (d.get("ids") or []) if isinstance(x, str)]
    rows = await db.pool().fetch(f"SELECT * FROM {S}.keywords WHERE site_id = $1 AND id = ANY($2) AND status IN ('approved','new')", site_id, ids)
    if not rows:
        raise validate.bad({"ids": "Choose approved or new keywords of this site"})
    all_ids, all_j = [], []
    for k in rows:
        inp = await _draft_input(site, {"topic": k["phrase"], "kind": "blog", "language": d.get("language") or k["language"], "keywords": [k["phrase"]]})
        p, j = await posts.create(site, by=c.name, **inp)
        all_ids += p; all_j += j
    return ok({"postIds": all_ids, "jobIds": all_j, "jobId": all_j[0]})


# ---------------------------------------------------------------- posts
@r.get("/sites/{site_id}/posts")
async def list_posts(site_id: str, req: Request, status: str = "", kind: str = "", platform: str = "", language: str = "", q: str = "", page: int = 1, pageSize: int = 0):
    c = await caller(req); await site_of(c, site_id)
    s = await db.settings(["page_size_default", "page_size_max"])
    size = max(1, min(int(pageSize or s.get("page_size_default") or 25), int(s.get("page_size_max") or 100)))
    where, args = ["site_id = $1"], [site_id]
    for col, val in (("status", status), ("kind", kind), ("platform", platform), ("language", language)):
        if val:
            args.append(val.split(",")); where.append(f"{col} = ANY(${len(args)})")
    if q:
        args.append(f"%{q}%"); where.append(f"(coalesce(title,'') ILIKE ${len(args)} OR topic ILIKE ${len(args)})")
    w = " AND ".join(where)
    total = await db.pool().fetchval(f"SELECT count(*) FROM {S}.posts WHERE {w}", *args)
    rows = await db.pool().fetch(f"SELECT * FROM {S}.posts WHERE {w} ORDER BY updated_at DESC LIMIT {size} OFFSET {(max(1, page) - 1) * size}", *args)
    return ok([await posts.shape(dict(x), site_id, full=False) for x in rows], {"page": max(1, page), "pageSize": size, "total": total})


@r.get("/sites/{site_id}/posts/{pid}")
async def get_post(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    return ok(await posts.shape(await _post(site_id, pid), site_id))


@r.put("/sites/{site_id}/posts/{pid}")
async def edit_post(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    p = await _post(site_id, pid)
    locks.guard(c, p)
    ed = (await db.settings(["editable_statuses"])).get("editable_statuses") or ["pending"]
    if p["status"] not in ed:
        raise ApiError(409, "not_editable", f"A post that is {p['status']} cannot be edited.")
    d = await body(req)
    if int(d.get("version") or 0) != p["version"]:
        raise ApiError(409, "version_conflict", "Someone saved this post first. Reload and try again.")
    ch = {}
    for k, v in d.items():
        col = CAMEL.get(k, k)
        if col in EDIT:
            if col in ("keywords", "hashtags"):
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                    raise validate.bad({k: "A list of texts"})
            elif v is not None and not isinstance(v, str):
                raise validate.bad({k: "Text"})
            ch[col] = v
    if "content" in ch and p["kind"] == "blog":
        ch["content"] = R.clean_html(ch["content"])                       # no scripts in HTML that goes to the client's website
    merged = {**p, **ch}
    f, seo = await posts.recheck(merged)
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(ch))
    row = await db.pool().fetchrow(
        f"UPDATE {S}.posts SET {sets + ', ' if sets else ''}findings = ${len(ch) + 3}, seo = ${len(ch) + 4}, version = version + 1, updated_at = now() "
        f"WHERE id = $1 AND version = $2 RETURNING *", pid, p["version"], *ch.values(), f, seo)
    if not row:
        raise ApiError(409, "version_conflict", "Someone saved this post first. Reload and try again.")
    await posts.refresh_twins(pid)                                      # its twins are checked (or translated) again
    await audit(c.name, "post.edit", account_id=c.account_id, site_id=site_id, target=pid, detail={"fields": list(ch)})
    return ok(await posts.shape(dict(row), site_id))


@r.delete("/sites/{site_id}/posts/{pid}")
async def delete_post(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    p = await _post(site_id, pid)
    locks.guard(c, p)
    if p["status"] == "published":
        raise ApiError(409, "published", "A published post cannot be deleted here; remove it in the site's CMS.")
    if p["status"] == "publishing":
        raise ApiError(409, "wrong_status", "The post is being published right now. Try again in a moment.")
    await media.remove((p["image"] or {}).get("key"))
    await db.pool().execute(f"DELETE FROM {S}.posts WHERE id = $1", pid)
    if p["suggestion_id"]:
        await db.pool().execute(f"UPDATE {S}.suggestions SET status = 'new' WHERE id = $1 AND NOT EXISTS (SELECT 1 FROM {S}.posts WHERE suggestion_id = $1)", p["suggestion_id"])
    await audit(c.name, "post.delete", account_id=c.account_id, site_id=site_id, target=pid)
    return ok({"deleted": True})


@r.get("/sites/{site_id}/posts/{pid}/seo-check")
async def seo_check(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    f, seo = await posts.recheck(await _post(site_id, pid))
    return ok({**seo, "findings": f})


@r.get("/sites/{site_id}/posts/{pid}/export.{fmt}")
async def export(site_id: str, pid: str, fmt: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    p = await _post(site_id, pid)
    name = (p["slug"] or pid)[:80]
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or pid
    disp = lambda ext: f"attachment; filename=\"{ascii_name}.{ext}\"; filename*=UTF-8''{quote(name + '.' + ext)}"   # noqa: E731  any script
    if fmt == "md":
        md = f"# {p['title'] or p['topic']}\n\n" + (f"*{p['subtitle']}*\n\n" if p["subtitle"] else "") + R.text_of(p["content"] or "") if p["kind"] == "blog" else (p["content"] or "")
        return Response(md, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": disp("md")})
    if fmt == "html":
        e = _h.escape
        doc = (f"<!doctype html><html lang=\"{p['language']}\" dir=\"{'rtl' if p['language'] == 'ar' else 'ltr'}\"><head><meta charset=\"utf-8\">"
               f"<title>{e(p['meta_title'] or p['title'] or '')}</title><meta name=\"description\" content=\"{e(p['meta_description'] or '')}\">"
               f"<meta property=\"og:title\" content=\"{e(p['title'] or '')}\"><meta property=\"og:description\" content=\"{e(p['meta_description'] or '')}\"></head>"
               f"<body><article><h1>{e(p['title'] or '')}</h1>{('<p><em>' + e(p['subtitle']) + '</em></p>') if p['subtitle'] else ''}{p['content'] or ''}</article></body></html>")
        return Response(doc, media_type="text/html; charset=utf-8", headers={"Content-Disposition": disp("html")})
    raise ApiError(404, "not_found", "Use export.html or export.md")


# ---------------------------------------------------------------- photos
@r.get("/sites/{site_id}/photos/search")
async def photo_search(site_id: str, req: Request, q: str = "", orientation: str = "landscape", n: int = 0):
    c = await caller(req); await site_of(c, site_id)
    if not q.strip():
        raise validate.bad({"q": "What the photo should show"})
    per = int(n or (await db.settings(["page_size_default"])).get("page_size_default") or 25)
    found = await media.search(q, orientation, max(1, min(per, 80)))
    for ph in found:                                                    # remembered so a pick needs only the photo's id (spec v1.1)
        await db.pool().execute(
            f"""INSERT INTO {S}.photo_seen (site_id, pexels_id, full_url, photographer, alt) VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (site_id, pexels_id) DO UPDATE SET full_url = EXCLUDED.full_url, photographer = EXCLUDED.photographer,
                  alt = EXCLUDED.alt, seen_at = now()""", site_id, int(ph["pexelsId"]), ph["full"], ph.get("photographer"), ph.get("alt"))
    return ok(found)


async def _set_image(c, site_id: str, p: dict, img: dict | None, action: str) -> dict:
    old = (p["image"] or {}).get("key")
    merged = {**p, "image": img}
    f, seo = await posts.recheck(merged)
    row = await db.pool().fetchrow(f"UPDATE {S}.posts SET image = $2, findings = $3, seo = $4, version = version + 1, updated_at = now() WHERE id = $1 RETURNING *",
                                   p["id"], img, f, seo)
    if old and old != (img or {}).get("key"):
        await media.remove(old)
    if img:
        await meter.record(c.account_id, site_id, "photos", 1)
    await audit(c.name, action, account_id=c.account_id, site_id=site_id, target=p["id"])
    return await posts.shape(dict(row), site_id)


async def _photo_ok(c, site_id: str, pid: str) -> dict:
    p = await _post(site_id, pid)
    locks.guard(c, p)
    ed = (await db.settings(["editable_statuses"])).get("editable_statuses") or ["pending"]
    if p["status"] not in ed:
        raise ApiError(409, "not_editable", f"A post that is {p['status']} cannot change its photo.")
    return p


@r.post("/sites/{site_id}/posts/{pid}/image/pick")
async def pick(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    await quotas.check(c.account_id, site_id, {"photos": 1})
    p = await _photo_ok(c, site_id, pid)
    d = await body(req)
    pxid = d.get("pexelsId")
    if not isinstance(pxid, int) or isinstance(pxid, bool):
        raise validate.bad({"pexelsId": "The photo's pexelsId from the search result"})
    seen = await db.pool().fetchrow(f"SELECT * FROM {S}.photo_seen WHERE site_id = $1 AND pexels_id = $2", site_id, pxid)
    if not seen:
        raise validate.bad({"pexelsId": "Search for the photo first, then pick it from the results"})
    try:
        data, ct = await media.download(seen["full_url"])
    except ValueError as e:
        raise validate.bad({"pexelsId": str(e)})
    key = await media.store(site_id, pid, data, ct)
    img = {"key": key, "ctype": ct, "bytes": len(data), "alt": (d.get("alt") or seen["alt"] or "")[:300],
           "credit": (f"Photo by {seen['photographer']} on Pexels" if seen["photographer"] else "Photo from Pexels")[:255],
           "source": "pexels", "pexelsId": pxid}
    return ok((await _set_image(c, site_id, p, img, "post.photo_pick"))["image"])


@r.post("/sites/{site_id}/posts/{pid}/image")
async def upload(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    await quotas.check(c.account_id, site_id, {"photos": 1})
    p = await _photo_ok(c, site_id, pid)
    form = await req.form()
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise validate.bad({"file": "Attach the photo as 'file' (multipart/form-data)"})
    data = await f.read()
    ct = (f.content_type or "").lower()
    try:
        await media.validate_upload(data, ct)
    except ValueError as e:
        raise validate.bad({"file": str(e)})
    key = await media.store(site_id, pid, data, ct)
    img = {"key": key, "ctype": ct, "bytes": len(data), "alt": (form.get("alt") or "")[:300], "credit": (form.get("credit") or "")[:255], "source": "upload"}
    return ok((await _set_image(c, site_id, p, img, "post.photo_upload"))["image"])     # spec v1.1: Post.image


@r.delete("/sites/{site_id}/posts/{pid}/image")
async def remove_image(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    p = await _photo_ok(c, site_id, pid)
    await _set_image(c, site_id, p, None, "post.photo_remove")
    return ok({"removed": True})


@r.get("/sites/{site_id}/media/{pid}")
async def media_get(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    p = await _post(site_id, pid)
    img = p["image"] or {}
    if not img.get("key"):
        raise ApiError(404, "not_found", "This post has no photo.")
    return Response(await media.load(img["key"]), media_type=img.get("ctype") or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- editing locks
@r.post("/sites/{site_id}/posts/{pid}/lock")
async def lock(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    p = await _post(site_id, pid)
    row = await locks.take(c, site_id, pid, force=False)
    if not row:
        locks.guard(c, await _post(site_id, pid))
    return ok(locks.shape(row or p))


@r.delete("/sites/{site_id}/posts/{pid}/lock")
async def unlock(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    await _post(site_id, pid)
    await locks.release(c, site_id, pid)
    return ok({"released": True})


@r.post("/sites/{site_id}/posts/{pid}/takeover")
async def takeover(site_id: str, pid: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    before = locks.holder(await _post(site_id, pid))
    row = await locks.take(c, site_id, pid, force=True)
    await audit(c.name, "post.takeover", account_id=c.account_id, site_id=site_id, target=pid, detail={"from": before})
    return ok(await posts.shape(row, site_id))
