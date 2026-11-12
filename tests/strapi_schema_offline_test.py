"""Offline: Strapi field definitions -> suggested map, required-field check, length fitting, enum categories, the
publish date; and that a site whose token cannot read definitions (old mapping) gets exactly the same request as before.
Set STRAPI_OLD to a copy of strapi.py from before this change to compare requests."""
import asyncio, importlib.util, json, os, sys
import httpx
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
from ext_marketing import db, strapi as ST
P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

SETTINGS = {"strapi_field_synonyms": {"title": ["title", "name"], "slug": ["slug"], "body": ["content", "body"],
            "subtitle": ["subtitle", "excerpt", "summary"], "metaTitle": ["seo.metaTitle"], "metaDescription": ["seo.metaDescription"],
            "image": ["cover", "image"], "category": ["category"]},
            "strapi_timeout_seconds": 5, "strapi_sample_size": 3, "strapi_require_https": True}
async def fake_settings(keys=None): return {k: v for k, v in SETTINGS.items() if not keys or k in keys}

def patch(mod, handler):
    mod.db.settings = fake_settings
    async def client(base, token):
        return httpx.AsyncClient(base_url="https://cms.example.com", transport=httpx.MockTransport(handler), headers={"Authorization": f"Bearer {token}"})
    mod._client = client

# ---- a site with definitions (types like a law firm CMS) and one without (a different shape: a magazine)
LAW = {"title": {"type": "string", "required": True, "maxLength": 90}, "slug": {"type": "uid", "required": True},
       "summary": {"type": "text", "required": True, "maxLength": 200}, "body": {"type": "blocks"},
       "publishedDate": {"type": "date", "required": True}, "service": {"type": "enumeration", "required": True, "enum": ["tax", "family-law"]},
       "seo": {"type": "component", "component": "shared.seo"}}
SEO = {"metaTitle": {"type": "string", "maxLength": 60}, "metaDescription": {"type": "text", "maxLength": 160}, "ogImage": {"type": "media"}}
MAG = {"headline": {"type": "string", "required": True}, "content": {"type": "richtext"}, "issueAt": {"type": "datetime", "required": True},
       "section": {"type": "enumeration", "required": True, "enum": ["sport", "travel"]}, "editorNote": {"type": "string", "required": True}}
sent = []
def site(attrs, plural, defs=True):
    def h(req: httpx.Request):
        u = req.url.path
        if u == "/api/content-type-builder/content-types":
            return httpx.Response(200, json={"data": [{"uid": "api::x.x", "schema": {"pluralName": plural, "attributes": attrs}}]}) if defs else httpx.Response(403, json={})
        if u.startswith("/api/content-type-builder/components/"):
            return httpx.Response(200, json={"data": {"schema": {"attributes": SEO}}})
        if u == "/api/i18n/locales":
            return httpx.Response(200, json=[{"code": "en"}, {"code": "ar"}])
        if req.method == "POST" and u == f"/api/{plural}":
            sent.append(json.loads(req.content)); return httpx.Response(200, json={"data": {"documentId": "d1", "slug": "s"}})
        if u == f"/api/{plural}" and any(k.startswith("filters") for k in req.url.params):
            return httpx.Response(200, json={"data": []})              # the slug is free
        if u == f"/api/{plural}":
            return httpx.Response(200, json={"data": [{"documentId": "a", "title": "T", "slug": "t", "content": "<p>x</p>", "cover": {"url": "/u.jpg", "hash": "h", "mime": "image/jpeg"}}]})
        return httpx.Response(404, json={})
    return h

async def main():
    patch(ST, site(LAW, "articles"))
    r = await ST.test({"url": "https://cms.example.com", "collection": "articles"}, "t")
    m = r["suggestedFieldMap"]
    ok("law: definitions read, component opened", r["schema"]["seo.metaTitle"]["maxLength"] == 60, r["schema"])
    ok("law: required date gets the publish date", m.get("date") == "publishedDate", m)
    ok("law: required allowed-values field becomes the category", m.get("category") == "service", m)
    ok("law: summary takes the subtitle by name", m.get("subtitle") == "summary", m)
    ok("law: categories are the field's allowed values", [c["slug"] for c in r["categories"]] == ["tax", "family-law"], r["categories"])
    ok("law: nothing required left unfilled", not r["note"], r["note"])

    patch(ST, site(MAG, "stories"))
    r = await ST.test({"url": "https://cms.example.com", "collection": "stories"}, "t")
    m = r["suggestedFieldMap"]
    ok("magazine: datetime field gets the publish date", m.get("date") == "issueAt", m)
    ok("magazine: its own allowed values", m.get("category") == "section" and [c["slug"] for c in r["categories"]] == ["sport", "travel"], r)
    ok("magazine: a required field nothing can fill is named", "editorNote" in (r["note"] or "") and "headline" in (r["note"] or ""), r["note"])

    # publish on the law site
    cfg = {"url": "https://cms.example.com", "collection": "articles", "strapiVersion": 5, "locales": ["en", "ar"],
           "fieldMap": {"title": "title", "slug": "slug", "subtitle": "summary", "body": "body", "date": "publishedDate", "category": "service",
                        "metaTitle": "seo.metaTitle"},
           "fieldKinds": {"title": "text", "slug": "text", "summary": "text", "body": "blocks", "publishedDate": "date", "service": "enum", "seo.metaTitle": "text"}}
    patch(ST, site(LAW, "articles")); cfg["schema"] = (await ST.test({"url": "x", "collection": "articles"}, "t"))["schema"]
    sent.clear()
    long_sub = "First sentence is here. " + "word " * 60
    await ST.publish(cfg, "t", {"title": "A", "slug": "a", "subtitle": long_sub, "body_html": "<p>x</p>", "category": "tax", "language": "en",
                                "meta_title": "A meta title that is clearly much longer than sixty characters in total length"})
    d = sent[-1]["data"]
    ok("publish: date in the field's format (YYYY-MM-DD)", len(d.get("publishedDate", "")) == 10, d.get("publishedDate"))
    ok("publish: chosen category sent as its value", d.get("service") == "tax", d.get("service"))
    ok("publish: summary within 200, cut at a word end", len(d["summary"]) <= 200 and d["summary"].endswith("word"), d["summary"])
    ok("fit: a sentence end in the second half is kept", ST._fit("One two three. Four five six seven", 25) == "One two three.", ST._fit("One two three. Four five six seven", 25))
    ok("fit: Arabic text cut at a word end", ST._fit("هذا نص عربي طويل جدا للتجربة", 14) == "هذا نص عربي", ST._fit("هذا نص عربي طويل جدا للتجربة", 14))
    ok("publish: meta title within 60, whole words", len(d["seo"]["metaTitle"]) <= 60 and not d["seo"]["metaTitle"].endswith(" "), d["seo"])
    try:
        await ST.publish(cfg, "t", {"title": "A", "slug": "a", "body_html": "<p>x</p>", "language": "en"}); ok("no category: refused with a clear message", False)
    except ST.StrapiError as e:
        ok("no category: refused with a clear message", "category" in e.message and "tax" in e.message, e.message)
    sent.clear()
    await ST.publish({**cfg, "defaultCategory": "family-law"}, "t", {"title": "A", "slug": "a", "body_html": "<p>x</p>", "language": "en"})
    ok("no category chosen: the default is used", sent[-1]["data"].get("service") == "family-law", sent[-1]["data"])

    # an existing site whose token cannot read definitions: request identical to before
    old_path = os.environ.get("STRAPI_OLD")
    if old_path:
        spec = importlib.util.spec_from_file_location("ext_marketing.strapi_old", old_path); OLD = importlib.util.module_from_spec(spec)
        OLD.__package__ = "ext_marketing"; spec.loader.exec_module(OLD)
        legacy = {"url": "https://cms.example.com", "collection": "blogs", "strapiVersion": 5, "locales": ["en", "ar"],
                  "fieldMap": {"title": "title", "slug": "slug", "body": "content", "subtitle": "excerpt", "metaTitle": "seo.metaTitle",
                               "metaDescription": "seo.metaDescription", "keywords": "seo.keywords"},
                  "fieldKinds": {"title": "text", "slug": "text", "content": "text", "excerpt": "text", "seo.metaTitle": "text"}}
        post = {"title": "Old way", "slug": "old-way", "subtitle": "x " * 200, "body_html": "<h2>H</h2><p>body</p>", "meta_title": "m" * 90,
                "meta_description": "d", "keywords": ["a", "b"], "language": "ar"}
        out = []
        for mod in (OLD, ST):
            patch(mod, site({}, "blogs", defs=False)); sent.clear()
            await mod.publish(legacy, "t", post); out.append(sent[-1])
        ok("site without definitions: the request is identical to before", out[0] == out[1], out)
        patch(ST, site({}, "blogs", defs=False))
        r = await ST.test({"url": "x", "collection": "blogs"}, "t")
        patch(OLD, site({}, "blogs", defs=False))
        r0 = await OLD.test({"url": "x", "collection": "blogs"}, "t")
        ok("site without definitions: same suggested map and fields", r["suggestedFieldMap"] == r0["suggestedFieldMap"] and r["fieldKinds"] == r0["fieldKinds"], (r, r0))
    # languages the CMS offers are learned on Test connection: an Arabic article goes to the Arabic locale with a Latin slug
    from ext_marketing import website as W2
    patch(ST, site(LAW, "articles"))
    t = {"type": "strapi", "settings": {"url": "https://cms.example.com", "collection": "articles", "siteUrl": "https://law.example", "articlePath": "articles"}, "learned": {}}
    r = await W2.test(t, "t"); t["learned"], t["fieldMap"] = r["learned"], r["suggestedFieldMap"]
    seen = []
    def h2(req):
        if req.method == "POST" and req.url.path == "/api/articles":
            seen.append((req.url.params.get("locale"), json.loads(req.content)["data"].get("slug")))
            return httpx.Response(200, json={"data": {"documentId": "d", "slug": json.loads(req.content)["data"].get("slug")}})
        return site(LAW, "articles")(req)
    patch(ST, h2)
    ar = await W2.publish(t, "t", {"title": "عقود", "slug": "صياغة-العقود", "source_slug": "commercial-contracts", "body_html": "<p>س</p>", "language": "ar", "category": "tax"})
    en = await W2.publish(t, "t", {"title": "Contracts", "slug": "commercial-contracts", "body_html": "<p>x</p>", "language": "en", "category": "tax"})
    ok("languages learned: Arabic sent to the ar locale", seen[0][0] == "ar", seen)
    ok("Arabic slug: the source article's Latin slug", seen[0][1] == "commercial-contracts", seen)
    ok("live links carry the language", ar["url"] == "https://law.example/ar/articles/commercial-contracts" and en["url"].endswith("/en/articles/commercial-contracts"), (ar, en))
    t2 = {**t, "languages": ["en"]}
    seen.clear(); await W2.publish(t2, "t", {"title": "عقود", "slug": "x", "body_html": "<p>س</p>", "language": "ar", "category": "tax"})
    ok("languages set on the connection still win", seen[0][0] is None, seen)

    # a CMS that does not answer: a plain message, for test, categories and publish alike
    from ext_marketing import website as W
    def down(req): raise httpx.ConnectError("refused")
    def slow(req): raise httpx.ReadTimeout("slow")
    for name, h in (("not reachable", down), ("timeout", slow)):
        patch(ST, h)
        t = {"type": "strapi", "settings": {"url": "https://cms.example.com", "collection": "articles"}, "fieldMap": cfg["fieldMap"], "learned": {}}
        for call in ("test", "categories", "publish"):
            try:
                if call == "publish": await W.publish(t, "t", {"title": "A", "body_html": "<p>x</p>", "language": "en", "category": "tax"})
                elif call == "categories": await W.categories({**t, "settings": {**t["settings"], "categoryCollection": "cats"}}, "t")
                else: await W.test(t, "t")
                ok(f"{name}: {call} gives a plain message", False, "no error")
            except W.Refused as e:
                ok(f"{name}: {call} gives a plain message", "Strapi address" in e.message, e.message)
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)
asyncio.run(main())
