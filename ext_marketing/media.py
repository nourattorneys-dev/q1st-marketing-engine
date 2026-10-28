"""Client photos: Pexels search (the server's Pexels service), stock download (allowed hosts only), uploads, storage in our
own bucket. The image record on a post: {key, ctype, bytes, alt, credit, source, pexelsId}."""
from __future__ import annotations
import asyncio, io, os
from urllib.parse import urlparse

import httpx

from . import db, security

TYPES = None


async def _limits() -> tuple[int, list]:
    s = await db.settings(["photo_max_bytes", "photo_types"])
    return int(s.get("photo_max_bytes") or 8_388_608), s.get("photo_types") or []


def _minio():
    from minio import Minio
    ep = urlparse(os.environ.get("MINIO_ENDPOINT", "http://localhost:9001"))
    return Minio(ep.netloc, access_key=os.environ.get("MINIO_KEY"), secret_key=os.environ.get("MINIO_SEC"), secure=ep.scheme == "https")


async def _bucket() -> str:
    b = (await db.settings(["media_bucket"])).get("media_bucket") or "ext-marketing"
    def ensure():
        m = _minio()
        if not m.bucket_exists(b):
            m.make_bucket(b)
    await asyncio.to_thread(ensure)
    return b


async def search(query: str, orientation: str, per_page: int) -> list[dict]:
    url = os.environ.get("PEXELS_CONNECTOR_URL", "http://127.0.0.1:9035").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(f"{url}/search", json={"query": query[:120], "orientation": orientation, "per_page": per_page})
        photos = (r.json().get("photos") or []) if r.status_code < 400 else []
    except (httpx.HTTPError, ValueError):
        return []
    return [{"pexelsId": p["id"], "thumb": p.get("url_medium"), "full": p.get("url_large") or p.get("url_original"),
             "width": p.get("width"), "height": p.get("height"), "photographer": p.get("photographer"), "alt": p.get("alt") or ""} for p in photos]


async def download(url: str) -> tuple[bytes, str]:
    hosts = (await db.settings(["photo_hosts"])).get("photo_hosts") or []
    if urlparse(url).hostname not in hosts or urlparse(url).scheme != "https":
        raise ValueError("photo address not allowed")
    mx, types = await _limits()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if r.status_code >= 400 or ct not in types or not r.content or len(r.content) > mx:
        raise ValueError("photo could not be used")
    return r.content, ct


async def validate_upload(data: bytes, ctype: str) -> None:
    mx, types = await _limits()
    if ctype not in types:
        raise ValueError(f"Use one of: {', '.join(types)}")
    if not data or len(data) > mx:
        raise ValueError(f"The photo is empty or larger than {mx // 1048576} MB")


async def store(site_id: str, post_id: str, data: bytes, ctype: str) -> str:
    b = await _bucket()
    key = f"{site_id}/{post_id}/{security.new_id('img')}"
    await asyncio.to_thread(lambda: _minio().put_object(b, key, io.BytesIO(data), len(data), content_type=ctype))
    return key


async def load(key: str) -> bytes:
    b = await _bucket()
    def get():
        r = _minio().get_object(b, key)
        try:
            return r.read()
        finally:
            r.close(); r.release_conn()
    return await asyncio.to_thread(get)


async def remove(key: str | None) -> None:
    if not key:
        return
    b = await _bucket()
    try:
        await asyncio.to_thread(lambda: _minio().remove_object(b, key))
    except Exception:                                                   # noqa: BLE001 - a stale object never blocks
        pass
