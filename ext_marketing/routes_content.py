"""Site app routes: SEO settings (the site's profile for the writer), keywords, AI proposals, jobs."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import quotas, db, jobs, keywords, profile, validate
from .auth import ApiError, audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- SEO settings = the site's profile
@r.get("/sites/{site_id}/settings/seo")
async def get_seo(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    v, ver = await profile.seo(site_id)
    return ok({**{k: v.get(k) for k in profile.SEO_FIELDS}, "siteName": v.get("siteName") or site["name"],
               "siteUrl": v.get("siteUrl") or site["url"], "defaultLang": v.get("defaultLang") or site["default_language"], "version": ver})


@r.put("/sites/{site_id}/settings/seo")
async def put_seo(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    d = await body(req)
    v, ver = await profile.seo(site_id)
    if int(d.get("version") or 0) != ver:
        raise ApiError(409, "version_conflict", "These settings were saved meanwhile. Reload and try again.")
    f = {}
    for k in profile.SEO_FIELDS:
        if k not in d:
            continue
        x = d[k]
        if k in profile.LIST_FIELDS:
            if isinstance(x, str):
                x = [t.strip() for t in x.replace("\n", ",").split(",") if t.strip()]
            if not (isinstance(x, list) and all(isinstance(t, str) for t in x)):
                f[k] = "A list of short texts"; continue
        elif x is not None and not isinstance(x, str):
            f[k] = "Text"; continue
        v[k] = x
    if d.get("defaultLang") and d["defaultLang"] not in site["languages"]:
        f["defaultLang"] = "Must be one of the site's languages"
    if f:
        raise validate.bad(f)
    await db.pool().execute(
        f"""INSERT INTO {S}.site_settings (site_id, grp, value, version, updated_by) VALUES ($1,'seo',$2,1,$3)
            ON CONFLICT (site_id, grp) DO UPDATE SET value = EXCLUDED.value, version = {S}.site_settings.version + 1,
              updated_by = EXCLUDED.updated_by, updated_at = now()""", site_id, v, c.name)
    await audit(c.name, "seo.settings", account_id=c.account_id, site_id=site_id, detail={"fields": [k for k in d if k != "version"]})
    return await get_seo(site_id, req)


# ---------------------------------------------------------------- keywords
@r.get("/sites/{site_id}/keywords")
async def list_keywords(site_id: str, req: Request, status: str = "", language: str = "", q: str = "", page: int = 1, pageSize: int = 0):
    c = await caller(req); await site_of(c, site_id)
    s = await db.settings(["page_size_default", "page_size_max"])
    size = max(1, min(int(pageSize or s.get("page_size_default") or 25), int(s.get("page_size_max") or 100)))
    st = [x for x in status.split(",") if x]
    where, args = ["site_id = $1"], [site_id]
    if st:
        args.append(st); where.append(f"status = ANY(${len(args)})")
    if language:
        args.append(language); where.append(f"language = ${len(args)}")
    if q:
        args.append(f"%{q}%"); where.append(f"phrase ILIKE ${len(args)}")
    w = " AND ".join(where)
    total = await db.pool().fetchval(f"SELECT count(*) FROM {S}.keywords WHERE {w}", *args)
    rows = await db.pool().fetch(f"SELECT * FROM {S}.keywords WHERE {w} ORDER BY search_volume DESC NULLS LAST, created_at DESC "
                                 f"LIMIT {size} OFFSET {(max(1, page) - 1) * size}", *args)
    return ok([keywords.shape(dict(x)) for x in rows], {"page": max(1, page), "pageSize": size, "total": total})


@r.post("/sites/{site_id}/keywords")
async def add_keywords(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    d = await body(req)
    ph = d.get("phrases")
    if isinstance(ph, str):
        ph = [t for t in ph.replace("\n", ",").split(",")]
    if not isinstance(ph, list) or not [p for p in ph if str(p).strip()]:
        raise validate.bad({"phrases": "One or more phrases"})
    lg = d.get("language") or site["default_language"]
    if lg not in site["languages"]:
        raise validate.bad({"language": "Must be one of the site's languages"})
    added, skipped = await keywords.add(site_id, ph, lg, "person", c.name)
    await audit(c.name, "keywords.add", account_id=c.account_id, site_id=site_id, detail={"added": len(added)})
    return ok(added)                                                    # spec v1.1: Keyword[] (meta is only for paging; duplicates are not added)


@r.post("/sites/{site_id}/keywords/bulk")
async def bulk_keywords(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    to = {"approve": "approved", "dismiss": "dismissed", "restore": "new"}.get(d.get("action"))
    ids = [x for x in (d.get("ids") or []) if isinstance(x, str)]
    if not to or not ids:
        raise validate.bad({"action": "approve, dismiss or restore", "ids": "One or more keyword ids"} if not to else {"ids": "One or more keyword ids"})
    rows = await db.pool().fetch(
        f"UPDATE {S}.keywords SET status = $3, updated_at = now() WHERE site_id = $1 AND id = ANY($2) AND status <> 'covered' RETURNING id", site_id, ids, to)
    done = {x["id"] for x in rows}
    await audit(c.name, f"keywords.{d['action']}", account_id=c.account_id, site_id=site_id, detail={"n": len(done)})
    return ok({"done": sorted(done), "failed": [{"id": i, "code": "not_found_or_covered", "message": "Not found, or already covered by a published article."}
                                             for i in ids if i not in done]})


@r.post("/sites/{site_id}/keywords/propose")
async def propose_keywords(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    try:
        d = await body(req)
    except ApiError:
        d = {}
    lg = d.get("language")
    if lg and lg not in site["languages"]:
        raise validate.bad({"language": "Must be one of the site's languages"})
    await quotas.check(c.account_id, site_id, {"keywordProposals": 1, "aiTokens": 0})
    jid = await jobs.enqueue(c.account_id, site_id, "keywords.propose", {"language": lg, "by": c.name})
    await audit(c.name, "keywords.propose", account_id=c.account_id, site_id=site_id, target=jid)
    return ok({"jobId": jid, "status": "queued"})


# ---------------------------------------------------------------- jobs
@r.get("/jobs/{job_id}")
async def get_job(job_id: str, req: Request):
    c = await caller(req)
    row = await db.pool().fetchrow(f"SELECT * FROM {S}.jobs WHERE id = $1 AND site_id = $2", job_id, c.site_id)
    if not row:
        raise ApiError(404, "not_found", "Job not found.")
    return ok(jobs.shape(dict(row)))
