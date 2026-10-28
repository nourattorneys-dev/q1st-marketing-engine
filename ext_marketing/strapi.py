"""Per-site Strapi connector (v4 and v5). One implementation for every client site; nothing about any one site is in code:
the site's collection, locales and field map are its settings, the field-name hints are a system setting.
Safety: a client gives us an address and we call it, so every call checks the address (https, public IP only)."""
from __future__ import annotations
import asyncio, html as _html, ipaddress, re, socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from . import db

S = db.SCHEMA


class StrapiError(Exception):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message); self.message, self.field = message, field


# ---------------------------------------------------------------- the address guard (SSRF)
async def check_url(url: str) -> str:
    from . import netguard
    s = await db.settings(["strapi_require_https"])
    u = urlparse((url or "").strip().rstrip("/"))
    if u.scheme not in ("https", "http") or not u.hostname:
        raise StrapiError("Use the Strapi address, e.g. https://cms.example.com", "url")
    try:
        await netguard.check(f"{u.scheme}://{u.netloc}", require_https=bool(s.get("strapi_require_https", True)))
    except netguard.Blocked as e:
        msg = str(e)
        raise StrapiError("This address points to a private network; use the public Strapi address." if "private" in msg
                          else ("The Strapi address must start with https://" if "https" in msg else msg[0].upper() + msg[1:] + "."), "url")
    return f"{u.scheme}://{u.netloc}"


async def _client(base: str, token: str) -> httpx.AsyncClient:
    t = (await db.settings(["strapi_timeout_seconds"])).get("strapi_timeout_seconds") or 30
    return httpx.AsyncClient(base_url=await check_url(base), headers={"Authorization": f"Bearer {token}"},
                             timeout=float(t), follow_redirects=False)


def _err(r: httpx.Response) -> str:
    try:
        e = (r.json() or {}).get("error") or {}
        det = "; ".join(f"{'.'.join(map(str, x.get('path', [])))}: {x.get('message')}" for x in ((e.get("details") or {}).get("errors") or [])[:3])
        return (f"{e.get('name', '')} {e.get('message', '')} {det}").strip() or f"HTTP {r.status_code}"
    except Exception:                                                   # noqa: BLE001
        return f"HTTP {r.status_code}"


def _human(r: httpx.Response, what: str) -> str:
    if r.status_code == 401:
        return "Strapi refused the token (401). Check the API token."
    if r.status_code == 403:
        return f"The token is not allowed to {what} (403). In Strapi: Settings → API Tokens → give it that permission."
    if r.status_code == 404:
        return f"Not found (404) while trying to {what}. Check the collection name."
    return f"Strapi error while trying to {what}: {_err(r)}"


# ---------------------------------------------------------------- reading the site's shape
def _rows(j: dict) -> tuple[list[dict], int]:
    """(entries, strapi major version). v5 returns flat entries with documentId; v4 nests them in attributes."""
    data = j.get("data") or []
    data = data if isinstance(data, list) else [data]
    if data and isinstance(data[0], dict) and "attributes" in data[0]:
        return [{"id": x.get("id"), **(x.get("attributes") or {})} for x in data], 4
    return data, 5


def _flatten(entry: dict, prefix: str = "") -> dict:
    """{'seo': {'metaTitle': 'x'}} -> {'seo.metaTitle': 'x'}; media/relations kept as one field."""
    out = {}
    for k, v in (entry or {}).items():
        if k in ("id", "documentId", "createdAt", "updatedAt", "publishedAt", "locale", "localizations"):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict) and not _is_media(v) and not _is_relation(v):
            inner = v.get("data", v) if "data" in v and len(v) == 1 else v
            if isinstance(inner, dict) and not _is_media(inner) and not _is_relation(inner):
                out.update(_flatten(inner, key + "."))
                continue
        out[key] = v
    return out


def _is_media(v) -> bool:
    v = v.get("data", v) if isinstance(v, dict) and "data" in v else v
    v = v[0] if isinstance(v, list) and v else v
    v = v.get("attributes", v) if isinstance(v, dict) else v
    return isinstance(v, dict) and ("mime" in v or "formats" in v or ("url" in v and "hash" in v))


def _is_relation(v) -> bool:
    v = v.get("data", v) if isinstance(v, dict) and "data" in v else v
    v = v[0] if isinstance(v, list) and v else v
    return isinstance(v, dict) and ("documentId" in v or ("id" in v and ("slug" in v or "name" in v or "attributes" in v)))


def _kind(v) -> str:
    if _is_media(v): return "media"
    if _is_relation(v): return "relation"
    if isinstance(v, list) and v and isinstance(v[0], dict) and "type" in v[0] and "children" in v[0]: return "blocks"
    if isinstance(v, str): return "text"
    if isinstance(v, list): return "list"
    return type(v).__name__


def _tokens(name: str) -> list[str]:
    """'seo.metaTitle' -> ['seo','meta','title']; 'url_slug' -> ['url','slug']; 'mainImage' -> ['main','image']."""
    return [t.lower() for t in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)]


async def suggest_map(fields: dict) -> dict:
    """Our field -> the site's field, scored over ALL pairs at once: an exact name scores highest; otherwise a field whose
    name parts contain every part of a hint scores by how much of its name the hint covers (seoTitle fits 'seo.title'
    fully, so it becomes the meta title and not the article title). Best pairs are taken first; kinds must fit."""
    syn = (await db.settings(["strapi_field_synonyms"])).get("strapi_field_synonyms") or {}
    want_kind = {"image": ("media",), "category": ("relation",), "author": ("relation",),
                 "body": ("text", "blocks"), "title": ("text",), "subtitle": ("text",), "slug": ("text",)}
    cand = []
    for ours, names in syn.items():
        for order, n in enumerate(names):
            nt = _tokens(n)
            for f, kind in fields.items():
                if ours in want_kind and kind not in want_kind[ours]:
                    continue
                if f.lower() == n.lower():
                    score = 2.0
                else:
                    ft = _tokens(f)
                    if not nt or not all(t in ft for t in nt):
                        continue
                    score = len(nt) / len(ft)
                cand.append((-score, order, len(f), ours, f))
    out, used = {}, set()
    for _s, _o, _l, ours, f in sorted(cand):
        if ours not in out and f not in used:
            out[ours] = f; used.add(f)
    return out


async def test(cfg: dict, token: str) -> dict:
    """Reads a few published entries, the locales and the categories; returns fields + a suggested map. Writes nothing."""
    coll = (cfg.get("collection") or "").strip("/")
    if not coll:
        raise StrapiError("Give the collection name (the API ID, e.g. articles).", "collection")
    n = (await db.settings(["strapi_sample_size"])).get("strapi_sample_size") or 3
    async with await _client(cfg["url"], token) as c:
        r = await c.get(f"/api/{coll}", params={"pagination[pageSize]": int(n), "populate": "*"})
        if r.status_code >= 400:
            raise StrapiError(_human(r, f"read {coll}"))
        rows, ver = _rows(r.json())
        fields: dict = {}
        for e in rows:
            for k, v in _flatten(e).items():
                if v not in (None, "", [], {}) or k not in fields:
                    fields[k] = _kind(v) if v not in (None, "", [], {}) else fields.get(k, "empty")
        locales = []
        rl = await c.get("/api/i18n/locales")
        if rl.status_code < 400 and isinstance(rl.json(), list):
            locales = [x.get("code") for x in rl.json() if x.get("code")]
        cats = await _categories(c, cfg, ver) if cfg.get("categoryCollection") else []
    return {"ok": True, "strapiVersion": ver, "sampled": len(rows), "fields": sorted(fields), "fieldKinds": fields,
            "suggestedFieldMap": await suggest_map(fields), "locales": locales, "categories": cats,
            "note": None if rows else f"No entries in {coll} yet: add one article in Strapi so the fields can be read."}


async def _categories(c: httpx.AsyncClient, cfg: dict, ver: int) -> list[dict]:
    coll = cfg["categoryCollection"].strip("/")
    name, slug = cfg.get("categoryNameField") or "name", cfg.get("categorySlugField") or "slug"
    r = await c.get(f"/api/{coll}", params={"pagination[pageSize]": 100, f"fields[0]": name, f"fields[1]": slug})
    if r.status_code >= 400:
        raise StrapiError(_human(r, f"read {coll}"))
    rows, _ = _rows(r.json())
    return [{"slug": x.get(slug), "name": x.get(name), "ref": x.get("documentId") if ver == 5 else x.get("id")} for x in rows if x.get(slug)]


async def categories(cfg: dict, token: str) -> list[dict]:
    if not cfg.get("categoryCollection"):
        return []
    async with await _client(cfg["url"], token) as c:
        return [{"slug": x["slug"], "name": x["name"]} for x in await _categories(c, cfg, int(cfg.get("strapiVersion") or 5))]


# ---------------------------------------------------------------- HTML -> Strapi v5 blocks (for sites whose body field is "Rich text (Blocks)")
class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(); self.out, self.cur, self.marks, self.list = [], None, [], None

    def _open(self, b):
        self.cur = b
        (self.list["children"] if self.list is not None and b["type"] == "list-item" else self.out).append(b)

    def handle_starttag(self, t, a):
        a = dict(a)
        if t in ("p", "blockquote"): self._open({"type": "paragraph" if t == "p" else "quote", "children": []})
        elif re.fullmatch(r"h[1-6]", t): self._open({"type": "heading", "level": int(t[1]), "children": []})
        elif t in ("ul", "ol"): self.list = {"type": "list", "format": "unordered" if t == "ul" else "ordered", "children": []}; self.out.append(self.list)
        elif t == "li": self._open({"type": "list-item", "children": []})
        elif t in ("strong", "b"): self.marks.append("bold")
        elif t in ("em", "i"): self.marks.append("italic")
        elif t == "a" and self.cur is not None:
            link = {"type": "link", "url": a.get("href", ""), "children": []}; self.cur["children"].append(link); self.marks.append(("link", link))
        elif t == "br" and self.cur is not None: self.cur["children"].append({"type": "text", "text": "\n"})

    def handle_endtag(self, t):
        if t in ("strong", "b", "em", "i", "a") and self.marks: self.marks.pop()
        elif t in ("ul", "ol"): self.list = None
        elif t in ("p", "blockquote", "li") or re.fullmatch(r"h[1-6]", t): self.cur = None

    def handle_data(self, d):
        if not d.strip() and self.cur is None: return
        if self.cur is None: self._open({"type": "paragraph", "children": []})
        node = {"type": "text", "text": d}
        for m in self.marks:
            if m == "bold": node["bold"] = True
            elif m == "italic": node["italic"] = True
        target = next((m[1] for m in reversed(self.marks) if isinstance(m, tuple)), self.cur)
        target["children"].append(node)


def html_to_blocks(h: str) -> list:
    p = _Blocks(); p.feed(h or ""); p.close()
    for b in p.out:                                                     # Strapi requires at least one child per block
        for x in [b] + b.get("children", []):
            if isinstance(x, dict) and "children" in x and not x["children"]: x["children"] = [{"type": "text", "text": ""}]
    return p.out


# ---------------------------------------------------------------- publishing one article
def _set(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


async def _slug_free(c, coll, slug_field, slug, locale, ver, own=None) -> str:
    n, cand = 1, slug
    while True:
        params = {f"filters[{slug_field}][$eq]": cand, "pagination[pageSize]": 1}
        if locale: params["locale"] = locale
        if ver == 5: params["status"] = "draft"                       # v5: drafts too (a draft shares the slug)
        r = await c.get(f"/api/{coll}", params=params)
        rows, _ = _rows(r.json()) if r.status_code < 400 else ([], ver)
        rows = [x for x in rows if (x.get("documentId") or x.get("id")) != own]
        if not rows:
            return cand
        n += 1; cand = f"{slug}-{n}"


async def publish(cfg: dict, token: str, post: dict, image: tuple[bytes, str, str] | None = None, publish_now: bool = True) -> dict:
    """post: {title, slug, subtitle, body_html, meta_title, meta_description, keywords[], category, language, image_alt, image_credit,
    document_id?}. image: (bytes, content_type, filename). Returns {url, documentId, slug, imageUrl}."""
    fm, coll, ver = cfg.get("fieldMap") or {}, cfg["collection"].strip("/"), int(cfg.get("strapiVersion") or 5)
    kinds = cfg.get("fieldKinds") or {}
    for need in ("title", "body"):
        if not fm.get(need):
            raise StrapiError(f"The field map has no '{need}' field. Run Test connection and save the map.", "fieldMap")
    locale = post.get("language") if post.get("language") in (cfg.get("locales") or []) else None
    async with await _client(cfg["url"], token) as c:
        data: dict = {}
        img_url = None
        if image and fm.get("image"):
            files = {"files": (image[2], image[0], image[1])}
            r = await c.post("/api/upload", files=files, data={"fileInfo": '{"alternativeText": %s, "caption": %s}' % (
                _json(post.get("image_alt") or ""), _json(post.get("image_credit") or ""))})
            if r.status_code >= 400:
                raise StrapiError(_human(r, "upload the photo"))
            up = r.json()[0]
            img_url = up.get("url") if (up.get("url") or "").startswith("http") else cfg["url"].rstrip("/") + (up.get("url") or "")
            _set(data, fm["image"], up.get("id"))
        body = post.get("body_html") or ""
        values = {"title": post.get("title"), "subtitle": post.get("subtitle"), "metaTitle": post.get("meta_title"),
                  "metaDescription": post.get("meta_description"),
                  "body": html_to_blocks(body) if kinds.get(fm["body"]) == "blocks" else body,
                  "keywords": ", ".join(post.get("keywords") or []) if kinds.get(fm.get("keywords", ""), "text") == "text" else (post.get("keywords") or [])}
        for ours, v in values.items():
            if fm.get(ours) and v not in (None, ""):
                _set(data, fm[ours], v)
        if fm.get("slug"):
            base = re.sub(r"[^a-z0-9]+", "-", (post.get("slug") or post.get("title") or "post").lower()).strip("-")[:80] or "post"
            _set(data, fm["slug"], await _slug_free(c, coll, fm["slug"], base, locale, ver, post.get("document_id")))
        if fm.get("category") and (post.get("category") or cfg.get("defaultCategory")) and cfg.get("categoryCollection"):
            want = post.get("category") or cfg.get("defaultCategory")
            ref = next((x["ref"] for x in await _categories(c, cfg, ver) if x["slug"] == want), None)
            if ref is not None:
                _set(data, fm["category"], {"set": [ref]} if ver == 5 else ref)
        for path, v in (cfg.get("fixedFields") or {}).items():         # site-specific constant fields (e.g. author), from settings
            _set(data, path, v)
        params = {"locale": locale} if locale else {}
        if ver == 5:
            params["status"] = "published" if publish_now else "draft"
        elif publish_now:
            data["publishedAt"] = _now_iso()
        doc = post.get("document_id")
        r = await (c.put(f"/api/{coll}/{doc}", params=params, json={"data": data}) if doc else c.post(f"/api/{coll}", params=params, json={"data": data}))
        if r.status_code == 404 and doc:                                # removed in the CMS meanwhile -> create again
            r = await c.post(f"/api/{coll}", params=params, json={"data": data})
        if r.status_code >= 400:
            raise StrapiError(_human(r, "save the article"))
        ent = (r.json() or {}).get("data") or {}
        ent = {"id": ent.get("id"), **(ent.get("attributes") or {}), **{k: v for k, v in ent.items() if k != "attributes"}}
        slug = ent.get(fm.get("slug", "slug")) or ""
        site = (cfg.get("siteUrl") or "").rstrip("/")
        path = (cfg.get("articlePath") or "").strip("/")
        prefix = "" if not locale or locale == cfg.get("defaultLocale") else f"/{locale}"
        return {"documentId": ent.get("documentId") or ent.get("id"), "slug": slug, "imageUrl": img_url,
                "url": f"{site}{prefix}/{path}/{slug}".replace("//", "/").replace(":/", "://") if site and slug else None}


def _json(s: str) -> str:
    import json
    return json.dumps(s)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
