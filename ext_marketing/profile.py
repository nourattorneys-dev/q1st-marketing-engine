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


def today_line(site: dict) -> str:
    """Today's date in the site's own time zone, so no prompt treats an earlier year as current."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        today = datetime.now(ZoneInfo(site.get("timezone") or "UTC"))
    except Exception:                                                   # noqa: BLE001
        today = datetime.now(timezone.utc)
    return (f"Today: {today:%d %B %Y}. Never present an earlier year as current. Add a year to a phrase or title only where people really search or "
            f"read with one (events, prices, rules that change yearly); otherwise leave years out.")


async def brief(site: dict) -> str:
    """The site's own facts as one block for any prompt."""
    v, _ = await seo(site["id"])
    want = (await db.settings(["site_profile_fields"])).get("site_profile_fields") or []
    prof = {**(site.get("profile") or {}), **v}
    lines = [today_line(site),
             f"Website: {v.get('siteName') or site['name']} ({v.get('siteUrl') or site['url']})"]
    if site.get("category"):
        lines.append(f"Category: {site['category']}")
    for k in want:
        x = prof.get(k)
        if x:
            lines.append(f"{k}: {', '.join(x) if isinstance(x, list) else x}")
    return "\n".join(lines)


# ---------------------------------------------------------------- "Fill from my website": the whole profile, proposed
TEXT_FIELDS = ("siteName", "author", "cta", "tone", "audience", "linkedin", "instagram")


def _proposal(raw: dict, langs: list, paths: set | None = None) -> dict:
    """Only the shape is checked: texts stay texts, lists stay lists of short texts. The model decides the content."""
    out: dict = {}
    for k in TEXT_FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:500]
    for k in ("focusKeywords", "locations", "avoid", "services"):
        v = raw.get(k)
        if isinstance(v, list):
            items = [x.strip()[:120] for x in v if isinstance(x, str) and x.strip()]
            if items:
                out[k] = items[:30]
    if raw.get("defaultLang") in langs:
        out["defaultLang"] = raw["defaultLang"]
    bp = raw.get("blogPath")                                            # only a page the site really links to
    if isinstance(bp, str) and paths is not None and _path(bp) in paths:
        out["blogPath"] = _path(bp)
    for k in ("linkedin", "instagram"):                                 # an address the pages actually link to, or nothing
        if k in out and not out[k].startswith("https://"):
            out.pop(k)
    return out


def _path(u: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(u.strip()).path or "/"
    return p if p.endswith("/") else p + "/"


async def propose_from_site(job: dict) -> dict:
    from . import jobs, llm, meter, sitepages
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1 AND deleted_at IS NULL", job["site_id"]) or {})
    if not site:
        raise jobs.Final("The site no longer exists.")
    s = await db.settings(["seo_propose_pages", "seo_propose_max_tokens", "seo_propose_links", "site_reader_name"])
    v, _ = await seo(site["id"])
    pages, problems, _ = await sitepages.read(v.get("siteUrl") or site["url"], int(s["seo_propose_pages"]), raw=True)
    paths = sorted({_path(link) for p in pages for link in p.get("links", [])} - {"/"})
    await jobs.progress(job["id"], 0.4)
    if not pages:
        why = problems[0]["reason"] if problems else "could not be read"
        reader = s.get("site_reader_name") or "Quantum1st-MarketingBot"
        raise jobs.Final(f"The website {why}. Allow the reader '{reader}' in the website's firewall, then try again.")
    site_txt = "\n\n".join(f"[{p['title'] or p['url']}] {p['url']}\n{p['text']}" for p in pages)
    langs = list(site["languages"] or [])
    current = {k: v.get(k) for k in SEO_FIELDS if v.get(k)}
    raw, tin, tout = await llm.chat([
        {"role": "system", "content": "You read a company's own website and write the profile an SEO writer will work from. Use only what the pages say "
                                      "or clearly show; leave a field out when the pages don't support it. Write in the site's main language. "
                                      "Answer ONLY JSON with any of these keys: {\"siteName\": \"...\", \"services\": [\"...\"], \"audience\": \"...\", "
                                      "\"locations\": [\"...\"], \"focusKeywords\": [\"...\"], \"cta\": \"one short sentence\", \"tone\": \"a few words\", "
                                      "\"avoid\": [\"...\"], \"author\": \"...\", \"linkedin\": \"https://...\", \"instagram\": \"https://...\", "
                                      "\"defaultLang\": \"" + "|".join(langs) + "\", \"blogPath\": \"/...\"}. "
                                      "focusKeywords: the phrases its customers search for. avoid: claims the company should not make (for example guarantees). "
                                      "linkedin and instagram only when the pages link to them. blogPath: the one page in SITE LINKS that lists the "
                                      "site's articles (blog, news, insights); leave it out when none does."},
        {"role": "user", "content": f"{today_line(site)}\nWebsite: {site['name']} ({v.get('siteUrl') or site['url']})\nLanguages: {', '.join(langs)}\n"
                                    f"What is already filled in (keep it in mind, improve it where the pages say more): {current or '(nothing)'}\n\n"
                                    f"SITE LINKS:\n{chr(10).join(paths[:int(s['seo_propose_links'])]) or '(none)'}\n\n"
                                    f"WEBSITE PAGES:\n{site_txt[:16000]}"}],
        max_tokens=int(s["seo_propose_max_tokens"]))
    await jobs.progress(job["id"], 0.9)
    await meter.record(job["account_id"], site["id"], "profileProposals", 1, tokens_in=tin, tokens_out=tout, job_id=job["id"])
    proposal = _proposal(llm.json_from(raw), langs, set(paths))
    return {**proposal, "pagesRead": len(pages), "readProblems": problems[:10]}
