"""ext_marketing: multi-site marketing API for OUTSIDE client sites (separate from Nour's marketing).
Rule: every query names the ext_marketing schema (pgbouncer drops search_path)."""
from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import asyncio

from . import db, jobs, keywords, posts, routes_admin, routes_client, routes_content, routes_posts, routes_strapi  # noqa: F401 (keywords/posts register their jobs)
from .auth import ApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")   # our own errors reach the journal
log = logging.getLogger("ext_marketing")
REQUIRED_ENV = ("EXT_MKT_ENC_KEY", "EXT_MKT_ADMIN_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log.error("missing env: %s -- admin/secrets will refuse", ", ".join(missing))
    worker = asyncio.create_task(jobs.loop())
    log.info("ext_marketing up")
    yield
    jobs.stop()
    try:
        await asyncio.wait_for(worker, timeout=10)
    except Exception:                                                   # noqa: BLE001
        worker.cancel()
    await db.close_pool()


app = FastAPI(title="Quantum1st Marketing API", version="1", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url="/api/v1/openapi.json")
app.include_router(routes_client.r)
app.include_router(routes_admin.r)
app.include_router(routes_strapi.r)
app.include_router(routes_content.r)
app.include_router(routes_posts.r)


def err(status: int, code: str, message: str, **extra):
    return JSONResponse({"ok": False, "error": {"code": code, "message": message, **extra}}, status_code=status)


@app.exception_handler(ApiError)
async def _api_error(request: Request, e: ApiError):
    return err(e.status, e.code, e.message, **e.extra)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, e: RequestValidationError):
    return err(400, "invalid_input", "Check the request fields.",
               fields={".".join(map(str, x["loc"][1:])): x["msg"] for x in e.errors()})


@app.exception_handler(Exception)
async def _crash(request: Request, e: Exception):
    log.exception("unhandled %s %s", request.method, request.url.path)
    return err(500, "server_error", "Something failed on our side. It has been logged.")


@app.get("/health")
async def health():
    try:
        n = await db.pool().fetchval(f"SELECT count(*) FROM {db.SCHEMA}.settings")
    except Exception as e:                                              # noqa: BLE001
        return err(503, "db_down", f"database not reachable: {type(e).__name__}")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    return {"ok": not missing, "db": "ok", "settings": n, "missingEnv": missing}


@app.get("/api/v1/meta")
async def meta():
    """Lists and limits the portal must not hard-code."""
    s = await db.settings()
    metrics = [dict(r) for r in await db.pool().fetch(
        f"SELECT key, label, unit FROM {db.SCHEMA}.metrics WHERE active ORDER BY key")]
    return {"ok": True, "data": {
        "languages": s.get("languages"), "kinds": s.get("kinds"),
        "platforms": s.get("platforms"), "categories": s.get("site_categories"),
        "metrics": metrics,
        "limits": {"metaTitle": s.get("meta_title_max"), "metaDescription": s.get("meta_description_max"),
                   "photoMaxBytes": s.get("photo_max_bytes"), "photoTypes": s.get("photo_types"),
                   "pageSizeDefault": s.get("page_size_default"), "pageSizeMax": s.get("page_size_max")},
        "pollSeconds": s.get("client_poll_seconds"),
    }}
