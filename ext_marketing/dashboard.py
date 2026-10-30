"""P1c: our hosted client dashboard (app.q1st.io). The staff portal sets a client's site up there in one step: the engine
issues the site's key and webhook secret, hands both to the app over a signed server-to-server call, and only then keeps
them; the client's first Admin is invited by the app. Names and removals follow.
Env: Q1ST_APP_URL (the app's address) and Q1ST_APP_SECRET (the app's PROVISION_SECRET). Neither set: the feature is off.
Signature: X-Signature = "sha256=" + hex HMAC-SHA256 of "<X-Timestamp>.<raw body>"; the app refuses an old timestamp."""
from __future__ import annotations
import json, os, time

import httpx

from . import db, security, shapes, webhooks
from .auth import ApiError

S = db.SCHEMA
KEY_LABEL = "Q1st dashboard"                                            # the key the dashboard uses; replaced on every set-up


def configured() -> bool:
    return bool(os.environ.get("Q1ST_APP_URL") and os.environ.get("Q1ST_APP_SECRET"))


async def call(payload: dict) -> dict:
    """Sends one request to the app; returns its data, or raises ApiError with the app's own message."""
    if not configured():
        raise ApiError(503, "dashboard_off", "The client dashboard is not set up on this engine (Q1ST_APP_URL, Q1ST_APP_SECRET).")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    timeout = float((await db.settings(["dashboard_timeout_seconds"])).get("dashboard_timeout_seconds") or 20)
    sig = webhooks.sign(stamp.encode() + b"." + body, os.environ["Q1ST_APP_SECRET"])
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.post(os.environ["Q1ST_APP_URL"].rstrip("/") + "/api/provision", content=body,
                             headers={"Content-Type": "application/json", "X-Timestamp": stamp, "X-Signature": sig, "User-Agent": "Q1st-Engine/1"})
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ApiError(502, "dashboard_down", f"The client dashboard did not answer ({type(e).__name__}).")
    if not d.get("ok"):
        e = d.get("error") or {}
        raise ApiError(502, "dashboard_" + str(e.get("code") or "error"), e.get("message") or f"The client dashboard answered {r.status_code}.")
    return d.get("data") or {}


async def _site(aid: str, sid: str):
    row = await db.pool().fetchrow(
        f"""SELECT s.id, s.name, a.name AS account_name, a.brand FROM {S}.sites s JOIN {S}.accounts a ON a.id = s.account_id
            WHERE s.id = $1 AND s.account_id = $2 AND s.deleted_at IS NULL""", sid, aid)
    if not row:
        raise ApiError(404, "not_found", "Site not found.")
    return row


async def set_up(aid: str, sid: str, admin_email: str | None) -> dict:
    """Puts the site on the dashboard (or refreshes it): a new key and webhook secret, kept only once the app has them.
    The key the dashboard used before is revoked; the client's other keys are left alone."""
    s = await _site(aid, sid)
    raw, h, prefix = security.new_api_key()
    secret = webhooks.new_secret()
    payload = {"action": "site.upsert", "accountId": aid, "accountName": s["account_name"], "siteId": sid, "siteName": s["name"],
               "engineKey": raw, "webhookSecret": secret, "brand": shapes.brand(dict(s))}
    if admin_email and admin_email.strip():
        payload["admin"] = {"email": admin_email.strip()}
    data = await call(payload)
    if not isinstance(data.get("hookUrl"), str) or not data["hookUrl"].startswith(("https://", "http://")):
        raise ApiError(502, "dashboard_error", "The client dashboard did not say where to send its events.")
    events = list((await db.settings(["webhook_events"])).get("webhook_events") or [])
    kid = security.new_id("key")
    async with db.pool().acquire() as con, con.transaction():
        await con.execute(f"UPDATE {S}.api_keys SET revoked_at = now() WHERE site_id = $1 AND label = $2 AND revoked_at IS NULL", sid, KEY_LABEL)
        await con.execute(f"INSERT INTO {S}.api_keys (id, account_id, site_id, key_hash, key_prefix, label) VALUES ($1,$2,$3,$4,$5,$6)",
                          kid, aid, sid, h, prefix, KEY_LABEL)
        await con.execute(
            f"""INSERT INTO {S}.webhooks (site_id, url, events, secret_enc) VALUES ($1,$2,$3,$4)
                ON CONFLICT (site_id) DO UPDATE SET url = EXCLUDED.url, events = EXCLUDED.events, secret_enc = EXCLUDED.secret_enc,
                status = 'new', last_error = NULL, updated_at = now()""", sid, data.get("hookUrl"), events, security.encrypt(secret))
    return {"siteId": sid, "keyId": kid, "admin": data.get("admin")}


async def on_dashboard(sid: str) -> bool:
    return bool(await db.pool().fetchval(
        f"SELECT 1 FROM {S}.api_keys WHERE site_id = $1 AND label = $2 AND revoked_at IS NULL", sid, KEY_LABEL))


async def follow(payload: dict, sid: str | None = None) -> str | None:
    """A change the dashboard should follow (a client's name or status, a removed site). Returns None when done or not
    needed (a client the dashboard doesn't have is not an error), else why not: the engine's own change already stands, so
    the portal shows this as a warning."""
    if not configured() or (sid and not await on_dashboard(sid)):
        return None
    try:
        await call(payload)
        return None
    except ApiError as e:
        return None if e.code == "dashboard_not_found" else e.message
