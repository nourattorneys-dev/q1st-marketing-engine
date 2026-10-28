"""Input checks against settings (allowed languages...) -- lists come from the DB, not code."""
from __future__ import annotations
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from . import db
from .auth import ApiError


def bad(fields: dict) -> ApiError:
    return ApiError(400, "invalid_input", "Check the highlighted fields.", fields=fields)


async def site_fields(d: dict, partial: bool = False) -> dict:
    s = await db.settings(["languages", "site_categories"])
    allowed = s.get("languages") or []
    cats = s.get("site_categories") or []
    f: dict = {}
    out: dict = {}
    if not partial or "name" in d:
        n = (d.get("name") or "").strip()
        f.update({} if n else {"name": "Required"}); out["name"] = n
    if not partial or "url" in d:
        u = (d.get("url") or "").strip().rstrip("/")
        if u and "://" not in u:
            u = "https://" + u                                          # "www.site.com" is accepted as https://www.site.com
        pu = urlparse(u)
        if pu.scheme not in ("http", "https") or "." not in (pu.hostname or ""):
            f["url"] = "Use a website address, e.g. www.example.com"
        out["url"] = u
    if "category" in d:
        c = (d.get("category") or "").strip() or None
        if c and cats and c not in cats:
            f["category"] = "Choose a category from the list (admins add more in Engine settings)"
        out["category"] = c
    if not partial or "timezone" in d:
        tz = (d.get("timezone") or "").strip()
        try:
            ZoneInfo(tz)
        except Exception:                                               # noqa: BLE001
            f["timezone"] = "Unknown time zone, e.g. Asia/Dubai"
        out["timezone"] = tz
    if not partial or "languages" in d:
        langs = [x for x in (d.get("languages") or []) if isinstance(x, str)]
        if not langs or any(x not in allowed for x in langs):
            f["languages"] = f"Choose from {', '.join(allowed)}"
        out["languages"] = langs
    if not partial or "defaultLanguage" in d:
        dl = d.get("defaultLanguage") or (out.get("languages") or [None])[0]
        langs = out.get("languages")
        if dl not in allowed or (langs and dl not in langs):
            f["defaultLanguage"] = "Must be one of the site's languages"
        out["default_language"] = dl
    if "profile" in d:
        if not isinstance(d["profile"], dict):
            f["profile"] = "Must be an object"
        out["profile"] = d["profile"]
    if f:
        raise bad(f)
    return out
