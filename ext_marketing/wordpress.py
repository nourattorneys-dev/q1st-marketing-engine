"""WordPress adapter: the site's REST API with an Editor user's application password, plus our Q1st Connector plugin on the
site (sign-in on hosts that drop the login header, Yoast SEO fields, site status). Nothing about one site is in code: the
address, user, post type, field map and body format are the site's settings."""
from __future__ import annotations
import base64, re
from urllib.parse import urlparse

import httpx

from . import db, netguard

# our field -> the WordPress targets it may be mapped to (the connection test offers these)
TARGETS = {"title": ["title"], "slug": ["slug"], "subtitle": ["excerpt"], "metaTitle": ["yoast.title"],
           "metaDescription": ["yoast.description"], "keywords": ["yoast.focuskw", "tags"], "image": ["featuredImage"],
           "category": ["categories"], "author": ["author"]}
YOAST = {"yoast.title": "_yoast_wpseo_title", "yoast.description": "_yoast_wpseo_metadesc", "yoast.focuskw": "_yoast_wpseo_focuskw"}


class WPError(Exception):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message); self.message, self.field = message, field


async def check_url(url: str) -> str:
    u = urlparse((url or "").strip().rstrip("/"))
    if u.scheme not in ("https", "http") or not u.hostname:
        raise WPError("Use the site address, e.g. https://example.com", "url")
    try:
        await netguard.check(f"{u.scheme}://{u.netloc}", require_https=True)
    except netguard.Blocked as e:
        m = str(e)
        raise WPError("This address points to a private network; use the public site address." if "private" in m
                      else ("The site address must start with https://" if "https" in m else m[0].upper() + m[1:] + "."), "url")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


async def _client(s: dict, secret: str) -> httpx.AsyncClient:
    t = (await db.settings(["website_timeout_seconds"])).get("website_timeout_seconds") or 30
    basic = "Basic " + base64.b64encode(f"{s.get('username') or ''}:{secret}".encode()).decode()
    return httpx.AsyncClient(base_url=await check_url(s["url"]) + "/wp-json", timeout=float(t), follow_redirects=False,
                             headers={"Authorization": basic, "X-Q1st-Auth": basic, "Accept": "application/json"})


def _code(r: httpx.Response) -> tuple[str, str]:
    try:
        j = r.json()
        return (j.get("code") or "", j.get("message") or "") if isinstance(j, dict) else ("", "")
    except ValueError:
        return "", ""


def _human(r: httpx.Response, what: str) -> str:
    code, msg = _code(r)
    if code == "application_passwords_disabled" or ("application password" in msg.lower() and "disabled" in msg.lower()):
        return "WordPress refused: application passwords are disabled on this site (often by a security plugin such as Wordfence)."
    if code in ("incorrect_password", "invalid_username", "invalid_email"):
        return "WordPress refused the user name or application password. Check both, or create a new application password."
    if r.status_code == 401:
        return ("WordPress did not sign the user in. Install and activate the Q1st Connector plugin on the site "
                "(some hosts remove the login header), then check the user name and application password.")
    if r.status_code == 403:
        return f"The WordPress user is not allowed to {what} (403). Give the user the Editor role."
    if r.status_code == 404:
        return f"Not found (404) while trying to {what}. Check the site address and the post type."
    if r.status_code in (301, 302, 307, 308):
        return f"The site redirects to {r.headers.get('location', 'another address')}. Use that address."
    return f"WordPress error while trying to {what}: {msg or code or f'HTTP {r.status_code}'}"


async def _base(c: httpx.AsyncClient, post_type: str) -> str:
    """The REST path of the configured post type (post -> posts, a custom type -> its rest_base)."""
    r = await c.get("/wp/v2/types")
    if r.status_code >= 400:
        raise WPError(_human(r, "read the post types"))
    t = (r.json() or {}).get(post_type or "post")
    if not t:
        raise WPError(f"The site has no post type '{post_type}'. Use one of: {', '.join(sorted(r.json() or {}))}.", "postType")
    return t.get("rest_base") or post_type


async def _all(c: httpx.AsyncClient, path: str, params: dict | None = None) -> list:
    out, page = [], 1
    while True:
        r = await c.get(path, params={**(params or {}), "per_page": 100, "page": page})
        if r.status_code >= 400:
            raise WPError(_human(r, f"read {path}"))
        out += r.json() or []
        if page >= int(r.headers.get("x-wp-totalpages") or 1):
            return out
        page += 1


async def test(s: dict, secret: str) -> dict:
    """Signs in, checks the user may write and upload, reads the post type, categories and our plugin's status. Writes nothing."""
    if not s.get("username"):
        raise WPError("Give the WordPress user name.", "username")
    async with await _client(s, secret) as c:
        r = await c.get("/")
        try:
            root = r.json() if r.status_code < 400 else None
        except ValueError:
            root = None
        if not isinstance(root, dict) or "namespaces" not in root:
            raise WPError("This address does not answer as a WordPress site (no /wp-json). Check the site address.", "url")
        me = await c.get("/wp/v2/users/me", params={"context": "edit"})
        if me.status_code >= 400:
            raise WPError(_human(me, "sign in"), "secret")
        caps = (me.json() or {}).get("capabilities") or {}
        missing = [k for k in ("edit_posts", "publish_posts", "upload_files") if not caps.get(k)]
        if missing:
            raise WPError(f"The user '{s['username']}' may not {', '.join(missing)}. Give the user the Editor role.", "username")
        await _base(c, s.get("postType") or "post")
        st = await c.get("/q1st/v1/status")
        plugin = st.json() if st.status_code < 400 else None
        cats = [{"slug": x["slug"], "name": _text(x.get("name"))} for x in await _all(c, "/wp/v2/categories")]
    tp = (plugin or {}).get("translatepress") or {}
    fields = ["title", "slug", "excerpt", "content", "featuredImage", "categories", "tags", "author"]
    if (plugin or {}).get("yoast"):
        fields += list(YOAST)
    suggested = {ours: next(t for t in opts if t in fields) for ours, opts in TARGETS.items() if any(t in fields for t in opts)}
    note = None if plugin else ("The Q1st Connector plugin is not active on the site: SEO title and description cannot be written. "
                                "Install it from the plugin file we send.")
    return {"categories": cats, "languages": [l.split("_")[0] for l in tp.get("languages") or []] or None, "fields": fields,
            "blockTypes": [], "suggestedFieldMap": suggested, "note": note,
            "learned": {"plugin": (plugin or {}).get("plugin"), "yoast": (plugin or {}).get("yoast"),
                        "translatepress": {k: tp.get(k) for k in ("version", "defaultLanguage", "languages", "urlSlugs")} if tp else None}}


def _text(v) -> str:
    v = v.get("rendered", "") if isinstance(v, dict) else (v or "")
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", v)).strip()


async def categories(s: dict, secret: str) -> list[dict]:
    async with await _client(s, secret) as c:
        return [{"slug": x["slug"], "name": _text(x.get("name"))} for x in await _all(c, "/wp/v2/categories")]


async def _term_ids(c: httpx.AsyncClient, tax: str, names: list[str], create: bool) -> list[int]:
    ids = []
    for n in [x for x in names if x]:
        hit = None
        for q in ({"slug": n}, {"search": n}):                          # a category is given by slug, a tag by its name
            r = await c.get(f"/wp/v2/{tax}", params={**q, "per_page": 100})
            hit = next((x["id"] for x in (r.json() if r.status_code < 400 else []) if x.get("slug") == n or _text(x.get("name")).lower() == n.lower()), None)
            if hit is not None:
                break
        if hit is None and create:
            r = await c.post(f"/wp/v2/{tax}", json={"name": n})
            if r.status_code >= 400:
                raise WPError(_human(r, f"create the {tax[:-1]} '{n}'"))
            hit = r.json()["id"]
        if hit is not None:
            ids.append(hit)
    return ids


async def publish(t: dict, secret: str, post: dict, image: tuple[bytes, str, str] | None = None, status: str = "publish") -> dict:
    """post: {title, slug, subtitle, body_html, meta_title, meta_description, keywords[], category, image_alt, image_credit,
    external_id?}. status: publish | draft. Creates the post, or updates external_id when it still exists.
    Returns {externalId, url, slug, imageUrl, imageId}."""
    s, fm = t.get("settings") or {}, t.get("fieldMap") or {}
    fmt = (t.get("body") or {}).get("format") or "html"
    if fmt != "html":
        raise WPError(f"The body format '{fmt}' is not available yet. Use html.", "body.format")
    async with await _client(s, secret) as c:
        base = await _base(c, s.get("postType") or "post")
        data: dict = {"title": post.get("title") or "", "content": post.get("body_html") or "", "status": status}
        meta: dict = {}
        img_url = img_id = None
        if image and fm.get("image") == "featuredImage":
            r = await c.post("/wp/v2/media", content=image[0], headers={"Content-Type": image[1],
                             "Content-Disposition": f'attachment; filename="{image[2]}"'})
            if r.status_code >= 400:
                raise WPError(_human(r, "upload the photo"))
            m = r.json()
            img_id, img_url = m["id"], m.get("source_url")
            await c.post(f"/wp/v2/media/{img_id}", json={"alt_text": post.get("image_alt") or "", "caption": post.get("image_credit") or ""})
            data["featured_media"] = img_id
        values = {"slug": post.get("slug"), "subtitle": post.get("subtitle"), "metaTitle": post.get("meta_title"),
                  "metaDescription": post.get("meta_description")}
        for ours, v in values.items():
            target = fm.get(ours)
            if not target or v in (None, ""):
                continue
            if target in YOAST:
                meta[YOAST[target]] = v
            elif target == "excerpt":
                data["excerpt"] = v
            elif target == "slug":
                data["slug"] = re.sub(r"[^\w-]+", "-", v.lower(), flags=re.U).strip("-")[:190]
        kws = post.get("keywords") or []
        if kws and fm.get("keywords") == "yoast.focuskw":
            meta[YOAST["yoast.focuskw"]] = kws[0]
        elif kws and fm.get("keywords") == "tags":
            data["tags"] = await _term_ids(c, "tags", kws, create=True)
        cat = post.get("category") or s.get("defaultCategory")
        if cat and fm.get("category") == "categories":
            ids = await _term_ids(c, "categories", [cat], create=False)
            if not ids:
                raise WPError(f"The site has no category '{cat}'. Pick one from the list.", "category")
            data["categories"] = ids
        if meta:
            data["meta"] = meta
        doc = post.get("external_id")
        r = await c.post(f"/wp/v2/{base}/{doc}" if doc else f"/wp/v2/{base}", json=data)
        if r.status_code == 404 and doc:                                # removed on the site meanwhile -> create again
            r = await c.post(f"/wp/v2/{base}", json=data)
        if r.status_code >= 400:
            code, msg = _code(r)
            if code == "rest_invalid_param" and "meta" in msg.lower():
                raise WPError("The site did not accept the SEO fields. Activate the Q1st Connector plugin, or remove the SEO rows from the field map.")
            raise WPError(_human(r, "save the article"))
        p = r.json()
    return {"externalId": str(p["id"]), "url": p.get("link"), "slug": p.get("slug"), "imageUrl": img_url, "imageId": img_id,
            "status": p.get("status")}


async def remove(t: dict, secret: str, external_id: str, image_id: int | None = None) -> None:
    """Deletes a post (and its uploaded photo) for good. Used by the live test to leave the site as it was."""
    s = t.get("settings") or {}
    async with await _client(s, secret) as c:
        base = await _base(c, s.get("postType") or "post")
        await c.delete(f"/wp/v2/{base}/{external_id}", params={"force": "true"})
        if image_id:
            await c.delete(f"/wp/v2/media/{image_id}", params={"force": "true"})


async def translate(t: dict, secret: str, external_id: str, language: str, pieces: list[dict], extra: list[dict]) -> dict:
    """Stores a published post's translation in the site's translation plugin (TranslatePress) through our Connector plugin.
    pieces: [{source, translated}] in reading order; extra: single strings (title, SEO title, description).
    Returns the plugin's answer {sent, matched, stored, missed, url}."""
    s = t.get("settings") or {}
    async with await _client(s, secret) as c:
        r = await c.post(f"/q1st/v1/translate/{external_id}", json={"language": language, "pieces": pieces, "extra": extra})
        if r.status_code == 404 and _code(r)[0] == "rest_no_route":
            raise WPError("The site's Q1st Connector plugin is too old to store translations. Install the latest version.")
        if r.status_code >= 400:
            code, msg = _code(r)
            raise WPError(f"The translation could not be stored: {msg or code or f'HTTP {r.status_code}'}")
        return r.json()


def _same_page(a: str, b: str) -> bool:
    n = lambda u: (lambda p: (p.hostname or "").lower().removeprefix("www.") + (p.path.rstrip("/") or "/"))(urlparse(u or ""))
    return n(a) == n(b)


async def set_seo(t: dict, secret: str, page_url: str, values: dict) -> dict:
    """Writes the SEO title and/or description (values: {metaTitle?, metaDescription?}) of the post or page at page_url,
    then reads them back. Returns {externalId, type, before: the values it replaced}. Raises WPError with a plain reason when the page cannot be found or the field cannot be written."""
    s = t.get("settings") or {}
    if not ((t.get("learned") or {}).get("yoast")):
        raise WPError("The site has no SEO plugin the engine can write to (Yoast SEO). Change this on the website.")
    path = urlparse(page_url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    if not slug:
        raise WPError("The home page's SEO text is set in the site's SEO plugin settings. Change it there.")
    async with await _client(s, secret) as c:
        r = await c.get("/wp/v2/search", params={"search": slug.replace("-", " "), "type": "post", "subtype": "any", "per_page": 50,
                                                 "_fields": "id,url,subtype"})
        if r.status_code >= 400:
            raise WPError(_human(r, "find the page"))
        hit = next((x for x in r.json() or [] if _same_page(x.get("url"), page_url)), None)
        if not hit:
            raise WPError("This address is not a post or page the engine can edit (it may be an archive, or a translation "
                          "kept by a translation plugin). Change it on the website.")
        base = await _base(c, hit["subtype"])
        old = await c.get(f"/wp/v2/{base}/{hit['id']}", params={"context": "edit", "_fields": "meta"})
        before = ((old.json() or {}).get("meta") or {}) if old.status_code < 400 else {}
        meta = {YOAST["yoast.title"]: values["metaTitle"]} if values.get("metaTitle") else {}
        if values.get("metaDescription"):
            meta[YOAST["yoast.description"]] = values["metaDescription"]
        w = await c.post(f"/wp/v2/{base}/{hit['id']}", json={"meta": meta})
        if w.status_code >= 400:
            code, msg = _code(w)
            if code == "rest_invalid_param" and "meta" in msg.lower():
                raise WPError("The site did not accept the SEO fields. Activate the Q1st Connector plugin.")
            raise WPError(_human(w, "save the SEO text"))
        got = (w.json() or {}).get("meta") or {}
        if any(got.get(k) != v for k, v in meta.items()):
            raise WPError("The site answered, but the SEO text did not change. Check the Q1st Connector plugin is active.")
        back = {"metaTitle": YOAST["yoast.title"], "metaDescription": YOAST["yoast.description"]}
        return {"externalId": str(hit["id"]), "type": hit["subtype"],
                "before": {k: before.get(m) or "" for k, m in back.items() if values.get(k)}}
