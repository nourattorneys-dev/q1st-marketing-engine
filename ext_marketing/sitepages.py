"""Reads a client's own website as text, through the address guard.
Same site = same host ignoring a leading 'www.' (www.x.com redirects to x.com and back are one site).
Pages come from the home page's links AND sitemap.xml. A page built by JavaScript has little body text: it is then read
from what it still carries (title, meta description, Open Graph, headings, structured data)."""
from __future__ import annotations
import json, re
from urllib.parse import urlparse, urljoin

import httpx

from . import db, netguard

SKIP = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".css", ".js")
REASON = {401: "needs a login", 403: "refused our reader (HTTP 403)", 404: "not found", 429: "asked us to slow down (HTTP 429)", 503: "unavailable (HTTP 503)"}


def same_site(a: str | None, b: str | None) -> bool:
    n = lambda h: (h or "").lower().removeprefix("www.")
    return bool(a) and n(a) == n(b)


def key(url: str) -> str:
    """One page, however it is written: no fragment, host without 'www.', no trailing slash ("x.com" = "x.com/")."""
    u = urlparse((url or "").split("#")[0])
    return f"{(u.hostname or '').lower().removeprefix('www.')}{u.path.rstrip('/') or '/'}{'?' + u.query if u.query else ''}"


class Page:
    def __init__(self, url, status, ctype, text, truncated):
        self.url, self.status, self.ctype, self.text, self.truncated = url, status, ctype, text, truncated


async def _get(c: httpx.AsyncClient, url: str, max_bytes: int, problems: list | None = None):
    """GET with redirects followed by hand (every hop through the guard). A heavy page is read up to max_bytes and
    used, never thrown away. A refusal is recorded in `problems` with a plain reason."""
    try:
        for _ in range(5):
            await netguard.check(url, require_https=False)
            async with c.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = str(r.url.join(r.headers["location"]))
                    continue
                if r.status_code >= 400:
                    if problems is not None:
                        problems.append({"url": url, "status": r.status_code, "reason": REASON.get(r.status_code, f"HTTP {r.status_code}")})
                    return None
                buf, trunc = bytearray(), False
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) >= max_bytes:
                        trunc = True
                        break
                enc = r.encoding or "utf-8"
                return Page(str(r.url), r.status_code, r.headers.get("content-type") or "", bytes(buf[:max_bytes]).decode(enc, "replace"), trunc)
    except netguard.Blocked as e:
        if problems is not None:
            problems.append({"url": url, "reason": str(e)})
    except httpx.TimeoutException:
        if problems is not None:
            problems.append({"url": url, "reason": "too slow to answer"})
    except httpx.HTTPError as e:
        if problems is not None:
            problems.append({"url": url, "reason": f"could not connect ({type(e).__name__})"})
    return None


def _meta_text(html: str) -> str:
    """What a JavaScript-built page still tells a reader without running scripts."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for sel in ("description", "og:title", "og:description", "twitter:description", "keywords"):
        m = soup.find("meta", attrs={"name": sel}) or soup.find("meta", attrs={"property": sel})
        if m and m.get("content"):
            out.append(m["content"].strip())
    out += [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"]) if h.get_text(strip=True)]
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(s.string or "")
        except ValueError:
            continue
        for x in (d if isinstance(d, list) else [d]):
            if isinstance(x, dict):
                out += [str(x[k]) for k in ("name", "description", "slogan", "about") if isinstance(x.get(k), str)]
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return "\n".join(uniq)


async def read(site_url: str, max_pages: int, raw: bool = False) -> tuple:
    """(pages, problems). problems = [{url, reason, status?}] for pages the site refused or that failed - reported, never silent.
    raw=True also keeps each page's HTML and links, and adds a third item {refs: {address: [pages linking to it]}, unvisited: [...]}."""
    s = await db.settings(["site_fetch_timeout_seconds", "site_fetch_max_bytes", "site_page_chars", "site_min_body_chars",
                           "site_sitemap_urls", "site_sitemap_paths", "site_reader_name"])
    from shared.kb_sources import html_to_text                          # the portal's safe HTML reader
    mx, chars, minb = int(s.get("site_fetch_max_bytes") or 2_000_000), int(s.get("site_page_chars") or 6000), int(s.get("site_min_body_chars") or 400)
    nmax = int(s.get("site_sitemap_urls") or 200)
    ua = {"User-Agent": f"{s.get('site_reader_name') or 'Quantum1st-MarketingBot'}/1.0 (+https://q1st.io)"}
    host = urlparse(site_url).hostname
    pages, problems, seen, todo = [], [], set(), [site_url]
    refs: dict = {}
    async with httpx.AsyncClient(timeout=float(s.get("site_fetch_timeout_seconds") or 20), follow_redirects=False, headers=ua) as c:
        maps = []
        rb = await _get(c, urljoin(site_url, "/robots.txt"), mx)        # the sitemap the site itself declares
        if rb is not None:
            maps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb.text)
        maps += [urljoin(site_url, p) for p in (s.get("site_sitemap_paths") or [])]
        locs: list = []
        for m in maps:
            if len(locs) >= nmax:
                break
            x = await _get(c, m, mx)
            if x is None or "<loc>" not in x.text:
                continue
            found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x.text)
            subs = [u for u in found if u.lower().endswith(".xml")]
            locs += [u for u in found if u not in subs]
            for sub in subs[:5]:                                        # a sitemap index: one level down
                y = await _get(c, sub, mx)
                if y is not None:
                    locs += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", y.text) if not u.lower().endswith(".xml")]
            if locs:
                break
        todo += [u for u in locs[:nmax] if same_site(urlparse(u).hostname, host) and not u.lower().endswith(SKIP)]
        while todo and len(pages) < max_pages:
            url = todo.pop(0)
            if key(url) in seen:
                continue
            seen.add(key(url))
            r = await _get(c, url, mx, problems)
            if r is None or "html" not in r.ctype:
                continue
            if key(r.url) in seen and key(r.url) != key(url):              # redirected to a page already read
                continue
            seen.add(key(r.url))
            title, text, links = html_to_text(r.text, r.url)
            how = "page"
            if len(text.strip()) < minb:
                text, how = (text.strip() + "\n" + _meta_text(r.text)).strip(), "meta"
            own = [l for l in links if same_site(urlparse(l).hostname, host) and not l.lower().endswith(SKIP)]
            if raw:
                for l in dict.fromkeys(key(x) for x in own):
                    if r.url not in refs.setdefault(l, []):
                        refs[l].append(r.url)
                pages.append({"url": r.url, "asked": url, "title": title, "text": text[:chars], "read": how, "truncated": r.truncated,
                              "html": r.text, "links": own})
            elif text.strip():
                pages.append({"url": r.url, "title": title, "text": text[:chars], "read": how, "truncated": r.truncated})
            todo += [l for l in own if key(l) not in seen]
    if raw:
        for p in problems:
            p["linkedFrom"] = refs.get(key(p["url"]), [])
        left = {key(u): u for u in todo if key(u) not in seen}
        return pages, problems, {"refs": refs, "unvisited": list(left.values())}
    return pages, problems
