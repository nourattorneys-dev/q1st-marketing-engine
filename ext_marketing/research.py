"""Facts for an article from the open web (SearXNG, already on this server) - read through the address guard.
Never Nour's knowledge base or law shelf. Sources travel with the draft so the reviewer can check every claim."""
from __future__ import annotations
import os
from urllib.parse import urlparse

import httpx

from . import db, sitepages


async def find(query: str, language: str) -> list[dict]:
    base = os.environ.get("WEB_SEARXNG_URL", "").rstrip("/")
    if not base:
        return []
    s = await db.settings(["research_results", "research_pages", "research_page_chars", "site_fetch_max_bytes"])
    try:
        async with httpx.AsyncClient(timeout=20) as c:                  # our own search server (internal by design)
            r = await c.get(f"{base}/search", params={"q": query, "format": "json", "language": language, "safesearch": 1})
            hits = (r.json().get("results") or [])[: int(s.get("research_results") or 6)] if r.status_code < 400 else []
    except (httpx.HTTPError, ValueError):
        return []
    from shared.kb_sources import html_to_text
    out = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"User-Agent": "Quantum1st-MarketingBot/1.0 (+https://q1st.io)"}) as c:
        for h in hits:
            if len(out) >= int(s.get("research_pages") or 3):
                break
            url = h.get("url") or ""
            if not urlparse(url).scheme.startswith("http"):
                continue
            p = await sitepages._get(c, url, int(s.get("site_fetch_max_bytes") or 2_000_000))   # through the guard
            if p is None or "html" not in p.ctype:
                continue
            title, text, _ = html_to_text(p.text, p.url)
            if len(text.strip()) > 200:
                out.append({"title": title or h.get("title") or url, "url": p.url, "text": text[: int(s.get("research_page_chars") or 3000)]})
    return out
