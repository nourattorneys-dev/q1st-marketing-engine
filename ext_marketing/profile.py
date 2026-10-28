"""A site's profile + SEO settings -> the facts the writer is given. Nothing about any category is written here: the
site says what it is; the model writes for it."""
from __future__ import annotations
from . import db

S = db.SCHEMA
SEO_FIELDS = ("siteName", "siteUrl", "blogPath", "defaultLang", "focusKeywords", "locations", "author", "linkedin", "instagram",
              "ogImage", "cta", "tone", "audience", "avoid", "services", "semrushDatabase")
LIST_FIELDS = ("focusKeywords", "locations", "avoid", "services")


async def seo(site_id: str) -> tuple[dict, int]:
    r = await db.pool().fetchrow(f"SELECT value, version FROM {S}.site_settings WHERE site_id = $1 AND grp = 'seo'", site_id)
    return (dict(r["value"] or {}), r["version"]) if r else ({}, 0)


async def brief(site: dict) -> str:
    """The site's own facts as one block for any prompt."""
    v, _ = await seo(site["id"])
    want = (await db.settings(["site_profile_fields"])).get("site_profile_fields") or []
    prof = {**(site.get("profile") or {}), **v}
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        today = datetime.now(ZoneInfo(site.get("timezone") or "UTC"))
    except Exception:                                                   # noqa: BLE001
        today = datetime.utcnow()
    lines = [f"Today: {today:%d %B %Y}. Never present an earlier year as current. Add a year to a phrase or title only where people really search or "
             f"read with one (events, prices, rules that change yearly); otherwise leave years out.",
             f"Website: {v.get('siteName') or site['name']} ({v.get('siteUrl') or site['url']})"]
    if site.get("category"):
        lines.append(f"Category: {site['category']}")
    for k in want:
        x = prof.get(k)
        if x:
            lines.append(f"{k}: {', '.join(x) if isinstance(x, list) else x}")
    return "\n".join(lines)
