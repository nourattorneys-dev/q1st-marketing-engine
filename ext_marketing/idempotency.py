"""Idempotent changes (spec v1.1 §3): a POST/PUT/DELETE repeated with the same Idempotency-Key within the window returns the
first answer, so a retried click never publishes twice. The key is scoped to the caller's API key."""
from __future__ import annotations
import hashlib

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import db

S = db.SCHEMA
METHODS = ("POST", "PUT", "DELETE")


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


async def middleware(request: Request, call_next):
    if request.method not in METHODS or not request.url.path.startswith("/api/v1/") or not request.headers.get("x-api-key"):
        return await call_next(request)                                 # admin routes and unauthenticated calls are not keyed
    key = (request.headers.get("idempotency-key") or "").strip()
    s = await db.settings(["idempotency_hours", "idempotency_required"])
    if not key:
        if s.get("idempotency_required"):
            return _err(400, "idempotency_key_required", "Send an Idempotency-Key header (a new one per user action).")
        return await call_next(request)
    if len(key) > 200:
        return _err(400, "invalid_input", "The Idempotency-Key is too long.")
    scope = hashlib.sha256(request.headers["x-api-key"].encode()).hexdigest()
    p = db.pool()
    await p.execute(f"DELETE FROM {S}.idempotency WHERE scope = $1 AND key = $2 AND created_at < now() - make_interval(hours => $3)",
                    scope, key, int(s.get("idempotency_hours") or 24))
    got = await p.fetchrow(
        f"""INSERT INTO {S}.idempotency (scope, key, method, path) VALUES ($1,$2,$3,$4)
            ON CONFLICT (scope, key) DO NOTHING RETURNING key""", scope, key, request.method, request.url.path)
    if not got:
        old = await p.fetchrow(f"SELECT * FROM {S}.idempotency WHERE scope = $1 AND key = $2", scope, key)
        if old and (old["method"], old["path"]) != (request.method, request.url.path):
            return _err(422, "idempotency_mismatch", "This Idempotency-Key was already used for another action. Send a new key.")
        if not old or old["status"] is None:
            return _err(409, "in_progress", "The same action is still being processed. Wait a moment and reload.")
        return Response(content=bytes(old["body"] or b""), status_code=old["status"], media_type=old["content_type"],
                        headers={"Idempotent-Replay": "true"})
    try:
        resp = await call_next(request)
    except Exception:
        await p.execute(f"DELETE FROM {S}.idempotency WHERE scope = $1 AND key = $2", scope, key)
        raise
    if resp.status_code >= 500:                                         # our own failure: the same key may try again
        await p.execute(f"DELETE FROM {S}.idempotency WHERE scope = $1 AND key = $2", scope, key)
        return resp
    chunks = [c async for c in resp.body_iterator]
    data = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
    await p.execute(f"UPDATE {S}.idempotency SET status = $3, body = $4, content_type = $5 WHERE scope = $1 AND key = $2",
                    scope, key, resp.status_code, data, resp.headers.get("content-type"))
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    return Response(content=data, status_code=resp.status_code, headers=headers)


async def purge() -> None:
    h = int((await db.settings(["idempotency_hours"])).get("idempotency_hours") or 24)
    await db.pool().execute(f"DELETE FROM {S}.idempotency WHERE created_at < now() - make_interval(hours => $1)", h)
