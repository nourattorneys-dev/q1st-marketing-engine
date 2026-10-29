"""Site app routes for its website connection (WordPress or Strapi): settings (secret write-only, stored encrypted),
Test connection, categories. A change of type or connection fields is saved only after the test passes."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, security, validate, website as W
from .auth import ApiError, audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")
CONNECT = ("url", "username", "collection")                           # settings that decide where we connect


def _target(d: dict, cur: dict) -> dict:
    """The body merged over the saved record (only the parts sent change)."""
    t = {k: cur.get(k) for k in ("type", "settings", "fieldMap", "body", "arabic", "languages", "learned")}
    for k in ("type", "fieldMap", "body", "arabic", "languages"):
        if k in d:
            t[k] = d[k]
    if "settings" in d:
        if not isinstance(d["settings"], dict):
            raise validate.bad({"settings": "An object of the type's fields"})
        keep = (cur.get("settings") or {}) if d.get("type", cur.get("type")) == cur.get("type") else {}   # a new type starts clean
        t["settings"] = {**keep, **d["settings"]}
    t["settings"] = dict(t.get("settings") or {})
    t["settings"].pop("secret", None)
    return t


def _secret(d: dict) -> str:
    return str(d.get("secret") or (d.get("settings") or {}).get("secret") or "").strip()


async def _status(site_id: str, status: str, error: str | None) -> None:
    from . import webhooks
    await webhooks.set_connection(site_id, "website", status, error)


@r.get("/sites/{site_id}/settings/website")
async def get_website(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    return ok(W.shape(await W.row(site_id)))


@r.put("/sites/{site_id}/settings/website")
async def put_website(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    row = await W.row(site_id)
    if int(d.get("version") or 0) != (row or {}).get("version", 0):
        raise ApiError(409, "version_conflict", "These settings were saved meanwhile. Reload and try again.")
    cur = (row or {}).get("config") or {}
    t = _target(d, cur)
    f = await W.check(t)
    if f:
        raise validate.bad(f)
    new_secret = _secret(d)
    secret = new_secret or security.decrypt((row or {}).get("secret_enc"))
    moved = (row is None or t["type"] != cur.get("type") or bool(new_secret)
             or any((t["settings"].get(k) or None) != ((cur.get("settings") or {}).get(k) or None) for k in CONNECT))
    status, checked = (row or {}).get("status", "missing"), None
    if moved:                                                           # where we connect changed: it must work before it is saved
        if not secret:
            raise validate.bad({"secret": "Required"})
        try:
            res = await W.test(t, secret)
        except W.Refused as e:
            raise ApiError(422, "not_connected", e.message, **({"fields": {f"settings.{e.field}": e.message}} if e.field else {}))
        t["learned"], status, checked = res.get("learned") or {}, "ok", True
    enc = security.encrypt(new_secret) if new_secret else (row or {}).get("secret_enc")
    new = await db.pool().fetchrow(
        f"""INSERT INTO {S}.site_connections (site_id, type, config, secret_enc, status, last_checked_at, version)
            VALUES ($1, 'website', $2, $3, $4, CASE WHEN $5 THEN now() END, 1)
            ON CONFLICT (site_id, type) DO UPDATE SET config = EXCLUDED.config, secret_enc = EXCLUDED.secret_enc, status = EXCLUDED.status,
              last_error = CASE WHEN $5 THEN NULL ELSE {S}.site_connections.last_error END,
              last_checked_at = coalesce(EXCLUDED.last_checked_at, {S}.site_connections.last_checked_at),
              version = {S}.site_connections.version + 1
            RETURNING *""", site_id, t, enc, status, bool(checked))
    await audit(c.name, "website.settings", account_id=c.account_id, site_id=site_id,
                detail={"type": t["type"], "fields": [k for k in d if k != "secret"], "secretChanged": bool(new_secret), "tested": bool(checked)})
    return ok(W.shape(dict(new)))


@r.post("/sites/{site_id}/settings/website/test")
async def test_website(site_id: str, req: Request):
    """Tests the body's unsaved values over the saved ones (omit the secret to use the stored one). Reads only; changes nothing
    on the website, and saves nothing unless it tested exactly the saved connection."""
    c = await caller(req); await site_of(c, site_id)
    try:
        d = await body(req)
    except ApiError:
        d = {}
    row = await W.row(site_id)
    cur = (row or {}).get("config") or {}
    t = _target(d, cur)
    f = await W.check(t)
    if f:
        return ok({"ok": False, "error": next(iter(f.values())), "fields": f})
    secret = _secret(d) or security.decrypt((row or {}).get("secret_enc"))
    if not secret:
        return ok({"ok": False, "error": "Give the password or token to test with.", "fields": {"secret": "Required"}})
    saved = row is not None and not _secret(d) and all(k not in d for k in ("type", "settings"))
    try:
        res = await W.test(t, secret)
    except W.Refused as e:
        if saved:
            await _status(site_id, "error", e.message)
        return ok({"ok": False, "error": e.message, **({"fields": {f"settings.{e.field}": e.message}} if e.field else {})})
    if saved:
        await db.pool().execute(f"UPDATE {S}.site_connections SET config = config || jsonb_build_object('learned', $2::jsonb) "
                                f"WHERE site_id = $1 AND type = 'website'", site_id, res.get("learned") or {})
        await _status(site_id, "ok", None)
    await audit(c.name, "website.test", account_id=c.account_id, site_id=site_id, detail={"type": t["type"], "ok": True})
    return ok({"ok": True, **{k: v for k, v in res.items() if k != "learned"}})


@r.get("/sites/{site_id}/website/categories")
async def website_categories(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    row = await W.row(site_id)
    if not row or not row["secret_enc"] or not (row["config"] or {}).get("type"):
        raise ApiError(422, "not_connected", "The website is not connected yet. Open Settings → Website connection.")
    try:
        return ok(await W.categories(row["config"], security.decrypt(row["secret_enc"])))
    except W.Refused as e:
        raise ApiError(502, "website_error", e.message)
