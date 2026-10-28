"""The site's website connection (spec v1.1 PublishTarget): one general record, one adapter per CMS type.
The portal's form is described by the setting website_target_types; only types with an adapter here are offered."""
from __future__ import annotations

from . import db, strapi

S = db.SCHEMA


class Refused(Exception):
    """The CMS refused or could not be reached; message is shown to the person as it is."""
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message); self.message, self.field = message, field


# ---------------------------------------------------------------- the Strapi adapter
def _strapi_cfg(t: dict) -> dict:
    s, learned = t.get("settings") or {}, t.get("learned") or {}
    return {**s, "fieldMap": t.get("fieldMap") or {}, "locales": t.get("languages") or [],
            "fieldKinds": learned.get("fieldKinds") or {}, "strapiVersion": learned.get("strapiVersion")}


async def _strapi_test(t: dict, secret: str) -> dict:
    try:
        r = await strapi.test(_strapi_cfg(t), secret)
    except strapi.StrapiError as e:
        raise Refused(e.message, e.field)
    return {"categories": r["categories"], "languages": r["locales"], "fields": r["fields"], "blockTypes": [],
            "suggestedFieldMap": r["suggestedFieldMap"], "note": r.get("note"),
            "learned": {"fieldKinds": r["fieldKinds"], "strapiVersion": r["strapiVersion"]}}


async def _strapi_categories(t: dict, secret: str) -> list:
    try:
        return await strapi.categories(_strapi_cfg(t), secret)
    except strapi.StrapiError as e:
        raise Refused(e.message)


async def _strapi_check_url(url: str) -> str:
    try:
        return await strapi.check_url(url)
    except strapi.StrapiError as e:
        raise Refused(e.message, "url")


ADAPTERS = {"strapi": {"test": _strapi_test, "categories": _strapi_categories, "check_url": _strapi_check_url}}


# ---------------------------------------------------------------- the stored record
async def target_types() -> list[dict]:
    defs = (await db.settings(["website_target_types"])).get("website_target_types") or []
    return [d for d in defs if d.get("type") in ADAPTERS]


async def row(site_id: str) -> dict | None:
    x = await db.pool().fetchrow(f"SELECT * FROM {S}.site_connections WHERE site_id = $1 AND type = 'website'", site_id)
    return dict(x) if x else None


def shape(r: dict | None) -> dict:
    t = (r or {}).get("config") or {}
    return {"type": t.get("type"), "settings": t.get("settings") or {}, "secretSet": bool(r and r["secret_enc"]),
            "fieldMap": t.get("fieldMap") or {}, "body": t.get("body"), "arabic": t.get("arabic"), "languages": t.get("languages") or [],
            "status": (r or {}).get("status", "missing"), "lastCheckedAt": (r or {}).get("last_checked_at"),
            "lastError": (r or {}).get("last_error"), "version": (r or {}).get("version", 0)}


async def check(t: dict) -> dict:
    """Field errors of a PublishTarget against its type's form (empty = valid)."""
    types = {d["type"]: d for d in await target_types()}
    f = {}
    d = types.get(t.get("type"))
    if not d:
        return {"type": "One of " + ", ".join(types) if types else "No website type is available"}
    s = t.get("settings") or {}
    for fld in d.get("fields") or []:
        if fld["kind"] != "secret" and fld.get("required") and not str(s.get(fld["key"]) or "").strip():
            f[f"settings.{fld['key']}"] = "Required"
    if s.get("url"):
        try:
            s["url"] = await ADAPTERS[t["type"]]["check_url"](s["url"])
        except Refused as e:
            f["settings.url"] = e.message
    fmt = (t.get("body") or {}).get("format")
    if fmt is not None and fmt not in (d.get("bodyFormats") or []):
        f["body.format"] = "One of " + ", ".join(d.get("bodyFormats") or [])
    mode = (t.get("arabic") or {}).get("mode")
    if mode is not None and mode not in (d.get("arabicModes") or []):
        f["arabic.mode"] = "One of " + ", ".join(d.get("arabicModes") or [])
    fm = t.get("fieldMap") or {}
    if not isinstance(fm, dict) or any(not isinstance(v, str) or not v for v in fm.values()):
        f["fieldMap"] = "Each row maps one of our fields to one website field name"
    return f


async def test(t: dict, secret: str) -> dict:
    return await ADAPTERS[t["type"]]["test"](t, secret)


async def categories(t: dict, secret: str) -> list:
    return await ADAPTERS[t["type"]]["categories"](t, secret)
