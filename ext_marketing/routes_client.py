"""Site app routes (/api/v1). Caller = one site's key; every route is limited to that site."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, shapes, validate
from .auth import ApiError, audit, caller, site_of

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")


def ok(data, meta=None):
    return {"ok": True, "data": data, **({"meta": meta} if meta is not None else {})}


async def body(req: Request) -> dict:
    try:
        d = await req.json()
    except Exception:                                                   # noqa: BLE001
        raise ApiError(400, "invalid_input", "Body must be JSON.")
    if not isinstance(d, dict):
        raise ApiError(400, "invalid_input", "Body must be a JSON object.")
    return d


@r.get("/sites")
async def my_sites(req: Request):
    """The key's own site, as a one-item list (keeps the spec's /sites shape)."""
    c = await caller(req)
    return ok([await shapes.site(await site_of(c, c.site_id))])


@r.get("/sites/{site_id}")
async def get_site(site_id: str, req: Request):
    c = await caller(req)
    return ok(await shapes.site(await site_of(c, site_id)))


@r.put("/sites/{site_id}")
async def update_site(site_id: str, req: Request):
    c = await caller(req)
    await site_of(c, site_id)
    d = await body(req)
    v = await validate.site_fields(d, partial=True)
    if "version" not in d:
        raise validate.bad({"version": "Required"})
    cols = {k: v[k] for k in ("name", "url", "category", "timezone", "languages", "default_language", "profile") if k in v}
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(cols)) or "name = name"
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.sites SET {sets}, version = version + 1
            WHERE id = $1 AND version = $2 AND deleted_at IS NULL RETURNING *""", site_id, int(d["version"]), *cols.values())
    if not row:
        raise ApiError(409, "version_conflict", "Someone saved this site first. Reload and try again.")
    await audit(c.name, "site.update", account_id=c.account_id, site_id=site_id, detail={"fields": list(cols)})
    return ok(await shapes.site(dict(row)))
