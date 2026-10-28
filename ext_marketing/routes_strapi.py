"""Site app routes for its Strapi: settings (token write-only, stored encrypted), Test connection, categories."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, security, strapi, validate
from .auth import ApiError, audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")
CFG_KEYS = ("url", "collection", "articlePath", "siteUrl", "locales", "defaultLocale", "fieldMap", "categoryCollection",
            "categoryNameField", "categorySlugField", "defaultCategory", "fixedFields")


async def _row(site_id: str) -> dict | None:
    x = await db.pool().fetchrow(f"SELECT * FROM {S}.site_connections WHERE site_id = $1 AND type = 'strapi'", site_id)
    return dict(x) if x else None


def _shape(row: dict | None, site: dict) -> dict:
    cfg = (row or {}).get("config") or {}
    return {**{k: cfg.get(k) for k in CFG_KEYS}, "siteUrl": cfg.get("siteUrl") or site["url"],
            "fieldKinds": cfg.get("fieldKinds") or {}, "strapiVersion": cfg.get("strapiVersion"),
            "tokenSet": bool(row and row["secret_enc"]), "status": (row or {}).get("status", "missing"),
            "lastCheckedAt": (row or {}).get("last_checked_at"), "lastError": (row or {}).get("last_error"),
            "version": (row or {}).get("version", 0)}


async def _ours() -> set:
    return set(((await db.settings(["strapi_field_synonyms"])).get("strapi_field_synonyms") or {}).keys())


@r.get("/sites/{site_id}/settings/strapi")
async def get_strapi(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    return ok(_shape(await _row(site_id), site))


@r.put("/sites/{site_id}/settings/strapi")
async def put_strapi(site_id: str, req: Request):
    c = await caller(req); site = await site_of(c, site_id)
    d = await body(req)
    row = await _row(site_id)
    if int(d.get("version") or 0) != (row or {}).get("version", 0):
        raise ApiError(409, "version_conflict", "These settings were saved meanwhile. Reload and try again.")
    cfg = dict((row or {}).get("config") or {})
    f = {}
    for k in CFG_KEYS:
        if k in d:
            cfg[k] = d[k]
    if "url" in d:
        try:
            cfg["url"] = await strapi.check_url(d["url"])
        except strapi.StrapiError as e:
            f["url"] = e.message
    fm = cfg.get("fieldMap") or {}
    if not isinstance(fm, dict) or any(not isinstance(v, str) or not v for v in fm.values()):
        f["fieldMap"] = "Each row maps one of our fields to one Strapi field name."
    elif set(fm) - await _ours():
        f["fieldMap"] = "Unknown field(s): " + ", ".join(sorted(set(fm) - await _ours()))
    if cfg.get("locales") is not None and not (isinstance(cfg["locales"], list) and all(isinstance(x, str) for x in cfg["locales"])):
        f["locales"] = "A list of locale codes, e.g. [\"en\", \"ar\"]"
    if cfg.get("fixedFields") is not None and not isinstance(cfg["fixedFields"], dict):
        f["fixedFields"] = "An object of field -> value"
    if not cfg.get("url"):
        f.setdefault("url", "Required")
    if not cfg.get("collection"):
        f["collection"] = "Required (the collection's API ID, e.g. articles)"
    if f:
        raise validate.bad(f)
    secret = security.encrypt(d["token"].strip()) if (d.get("token") or "").strip() else (row or {}).get("secret_enc")
    changed_conn = row is None or "url" in d or "collection" in d or (d.get("token") or "").strip()
    new = await db.pool().fetchrow(
        f"""INSERT INTO {S}.site_connections (site_id, type, config, secret_enc, status, version)
            VALUES ($1, 'strapi', $2, $3, $4, 1)
            ON CONFLICT (site_id, type) DO UPDATE SET config = EXCLUDED.config, secret_enc = EXCLUDED.secret_enc,
              status = CASE WHEN $5 THEN 'missing' ELSE {S}.site_connections.status END, version = {S}.site_connections.version + 1
            RETURNING *""", site_id, cfg, secret, "missing", bool(changed_conn))
    await audit(c.name, "strapi.settings", account_id=c.account_id, site_id=site_id,
                detail={"fields": [k for k in d if k != "token"], "tokenChanged": bool((d.get("token") or "").strip())})
    return ok(_shape(dict(new), site))


@r.post("/sites/{site_id}/settings/strapi/test")
async def test_strapi(site_id: str, req: Request):
    """Uses the saved settings; a body may override url/token/collection to test before saving. Reads only, writes nothing in Strapi."""
    c = await caller(req); site = await site_of(c, site_id)
    try:
        d = await body(req)
    except ApiError:
        d = {}
    row = await _row(site_id)
    override = any(k in d for k in ("url", "token", "collection"))     # a what-if test: never changes the saved connection
    cfg = {**((row or {}).get("config") or {}), **{k: d[k] for k in CFG_KEYS if k in d}}
    token = (d.get("token") or "").strip() or security.decrypt((row or {}).get("secret_enc"))
    if not cfg.get("url") or not token:
        raise validate.bad({"url" if not cfg.get("url") else "token": "Required before testing"})
    try:
        res = await strapi.test(cfg, token)
    except strapi.StrapiError as e:
        if row and not override:
            await db.pool().execute(f"UPDATE {S}.site_connections SET status='error', last_error=$2, last_checked_at=now() WHERE site_id=$1 AND type='strapi'", site_id, e.message)
        return ok({"ok": False, "error": e.message, **({"field": e.field} if e.field else {})})
    except Exception as e:                                              # noqa: BLE001  (network)
        msg = f"Could not reach Strapi: {type(e).__name__}"
        if row and not override:
            await db.pool().execute(f"UPDATE {S}.site_connections SET status='error', last_error=$2, last_checked_at=now() WHERE site_id=$1 AND type='strapi'", site_id, msg)
        return ok({"ok": False, "error": msg})
    if row and not override:                                            # the SAVED settings work: remember what we learned
        cfg2 = {**(row["config"] or {}), "fieldKinds": res["fieldKinds"], "strapiVersion": res["strapiVersion"]}
        await db.pool().execute(
            f"UPDATE {S}.site_connections SET status='ok', last_error=NULL, last_checked_at=now(), config=$2 WHERE site_id=$1 AND type='strapi'",
            site_id, cfg2)
    await audit(c.name, "strapi.test", account_id=c.account_id, site_id=site_id, detail={"ok": True, "sampled": res["sampled"]})
    return ok(res)


@r.get("/sites/{site_id}/strapi/categories")
async def strapi_categories(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    row = await _row(site_id)
    if not row or not row["secret_enc"]:
        raise ApiError(422, "not_connected", "Strapi is not set up for this site yet.")
    try:
        return ok(await strapi.categories(row["config"] or {}, security.decrypt(row["secret_enc"])))
    except strapi.StrapiError as e:
        raise ApiError(502, "strapi_error", e.message)
