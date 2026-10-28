"""Live probe for the WordPress adapter: publishes a DRAFT with a photo through a site's saved website connection, updates it,
reads it back from WordPress and deletes it. Usage: python wp_publish_probe.py SITE_ID. Prints one JSON line."""
import asyncio, base64, json, os, sys, zlib, struct
sys.path.insert(0, os.environ.get("EXT_SRC", "/opt/nour/src"))
from ext_marketing import db, security, website, wordpress


def png(w=64, h=40) -> bytes:
    raw = b"".join(b"\x00" + bytes([30, 90, 160]) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)   # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


async def main(site_id: str) -> dict:
    await db.open_pool()
    row = await website.row(site_id)
    t, secret = row["config"], security.decrypt(row["secret_enc"])
    cat = (await website.categories(t, secret))[0]["slug"]
    post = {"title": "Q1st test draft — please ignore", "slug": "q1st-test-draft", "subtitle": "A test of the publishing connection.",
            "body_html": "<h2>Test heading</h2><p>Test paragraph.</p>", "meta_title": "Q1st test meta title",
            "meta_description": "Q1st test meta description.", "keywords": ["q1st test keyword"], "category": cat,
            "image_alt": "Blue test picture", "image_credit": "Test"}
    out = {"category": cat}
    a = await website.publish(t, secret, post, (png(), "image/png", "q1st-test.png"), status="draft")
    out["created"] = {k: a.get(k) for k in ("externalId", "status", "target")}
    b = await website.publish(t, secret, {**post, "title": "Q1st test draft (updated)", "external_id": a["externalId"]}, status="draft")
    out["updatedSameId"] = b["externalId"] == a["externalId"]
    s = t["settings"]
    async with await wordpress._client(s, secret) as c:
        base = await wordpress._base(c, s.get("postType") or "post")
        p = (await c.get(f"/wp/v2/{base}/{a['externalId']}", params={"context": "edit"})).json()
        img = (await c.get(f"/wp/v2/media/{a['imageId']}", params={"context": "edit"})).json() if a.get("imageId") else {}
    import httpx
    async with httpx.AsyncClient(timeout=30) as anon:
        out["publicStatus"] = (await anon.get(f"{s['url']}/wp-json/wp/v2/{base}/{a['externalId']}")).status_code
    m = p.get("meta") or {}
    out["readBack"] = {"status": p.get("status"), "title": (p.get("title") or {}).get("raw"), "excerpt": (p.get("excerpt") or {}).get("raw"),
                       "seoTitle": m.get("_yoast_wpseo_title"), "seoDescription": m.get("_yoast_wpseo_metadesc"),
                       "focusKeyword": m.get("_yoast_wpseo_focuskw"), "hasImage": bool(p.get("featured_media")),
                       "imageAlt": img.get("alt_text"), "categoryCount": len(p.get("categories") or []),
                       "contentHasHeading": "<h2>Test heading</h2>" in ((p.get("content") or {}).get("raw") or "")}
    await website.ADAPTERS["wordpress"]["remove"](t, secret, a["externalId"], a.get("imageId"))
    async with await wordpress._client(s, secret) as c:
        out["afterDelete"] = (await c.get(f"/wp/v2/{base}/{a['externalId']}", params={"context": "edit"})).status_code
    await db.close_pool()
    return out


print(json.dumps(asyncio.run(main(sys.argv[1]))))
