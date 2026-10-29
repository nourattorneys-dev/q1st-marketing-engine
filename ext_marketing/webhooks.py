"""Webhooks (spec v1.1 §8): each site's app registers one address; the engine sends it signed events.
emit() only stores a delivery; deliver_due() sends what is due, up to webhook_max_attempts tries on the webhook_backoff_seconds waits.
X-Signature = "sha256=" + hex HMAC-SHA256 of the raw body with the site's secret; X-Event-Id lets the receiver drop repeats."""
from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, secrets
from datetime import datetime, timedelta, timezone

import httpx

from . import db, netguard, security

S = db.SCHEMA
log = logging.getLogger("ext_marketing.webhooks")
_lock: asyncio.Lock | None = None                                      # one sending pass at a time; a caller waits for it
_tasks: set = set()                                                     # keeps running sends referenced


async def row(site_id: str) -> dict | None:
    r = await db.pool().fetchrow(f"SELECT * FROM {S}.webhooks WHERE site_id = $1", site_id)
    return dict(r) if r else None


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def emit(site_id: str | None, event: str, data: dict, *, always: bool = False) -> None:
    """Queue one event for a site's webhook when it has one and listens to this event. Never raises: an event is never
    allowed to break the work that caused it. always=True sends even an event the site did not pick (a test)."""
    if not site_id:
        return
    try:
        w = await row(site_id)
        if not w or not w["url"] or (not always and event not in (w["events"] or [])):
            return
        tz = await db.pool().fetchval(f"SELECT timezone FROM {S}.sites WHERE id = $1", site_id)
        try:
            from zoneinfo import ZoneInfo
            at = datetime.now(ZoneInfo(tz or "UTC"))
        except Exception:                                               # noqa: BLE001 - a bad zone name falls back to UTC
            at = datetime.now(timezone.utc)
        eid = security.new_id("evt")
        body = json.dumps({"event": event, "siteId": site_id, "at": at.isoformat(timespec="seconds"), "data": data},
                          ensure_ascii=False, default=str, separators=(",", ":"))
        await db.pool().execute(f"INSERT INTO {S}.webhook_deliveries (id, site_id, event, body) VALUES ($1,$2,$3,$4)",
                                eid, site_id, event, body)
        kick()
    except Exception:                                                   # noqa: BLE001
        log.exception("webhook emit %s %s", site_id, event)


async def emit_account(account_id: str, event: str, data: dict) -> None:
    """The same event to every site of an account (quotas and plans are account-wide)."""
    for s in await db.pool().fetch(f"SELECT id FROM {S}.sites WHERE account_id = $1 AND deleted_at IS NULL", account_id):
        await emit(s["id"], event, data)


def kick() -> None:
    """Send what is due now, without waiting for the next loop pass."""
    if not (_lock and _lock.locked()):
        t = asyncio.get_running_loop().create_task(deliver_due())
        _tasks.add(t); t.add_done_callback(_tasks.discard)


async def _send_one(d: dict, w: dict, timeout: float, require_https: bool) -> tuple[int | None, str | None]:
    secret = security.decrypt(w["secret_enc"])
    body = d["body"].encode()
    try:
        await netguard.check(w["url"], require_https=require_https)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.post(w["url"], content=body, headers={"Content-Type": "application/json", "X-Event-Id": d["id"],
                                                              "X-Signature": sign(body, secret), "User-Agent": "Q1st-Webhooks/1"})
        return r.status_code, None if 200 <= r.status_code < 300 else f"The address answered {r.status_code}."
    except netguard.Blocked as e:
        return None, f"Address refused: {e}."
    except httpx.TimeoutException:
        return None, f"No answer within {timeout:g} seconds."
    except httpx.HTTPError as e:
        return None, f"Could not connect ({type(e).__name__})."


async def deliver_due() -> int:
    """Send every delivery whose time has come (oldest first, up to webhook_batch per pass)."""
    global _lock
    _lock = _lock or asyncio.Lock()
    async with _lock:
        return await _pass()


async def _pass() -> int:
    sent = 0
    try:
        s = await db.settings(["webhook_max_attempts", "webhook_backoff_seconds", "webhook_timeout_seconds", "webhook_batch", "webhook_require_https"])
        waits = [float(x) for x in (s.get("webhook_backoff_seconds") or [])] or [60.0]
        tries = int(s.get("webhook_max_attempts") or 1)
        due = await db.pool().fetch(
            f"""SELECT * FROM {S}.webhook_deliveries WHERE status = 'pending' AND next_at <= now() ORDER BY next_at LIMIT $1""",
            int(s.get("webhook_batch") or 20))
        for d in due:
            w = await row(d["site_id"])
            if not w or not w["url"]:
                await db.pool().execute(f"UPDATE {S}.webhook_deliveries SET status = 'dropped', last_error = 'No webhook set' WHERE id = $1", d["id"])
                continue
            code, err = await _send_one(dict(d), w, float(s.get("webhook_timeout_seconds") or 10), bool(s.get("webhook_require_https", True)))
            n = d["attempts"] + 1
            if err is None:
                sent += 1
                await db.pool().execute(f"""UPDATE {S}.webhook_deliveries SET status = 'sent', attempts = $2, last_code = $3, last_error = NULL,
                                               sent_at = now() WHERE id = $1""", d["id"], n, code)
                await db.pool().execute(f"""UPDATE {S}.webhooks SET status = 'ok', last_error = NULL, last_delivery_at = now()
                                               WHERE site_id = $1""", d["site_id"])
            else:
                last = n >= tries
                nxt = datetime.now(timezone.utc) + timedelta(seconds=0 if last else waits[min(n - 1, len(waits) - 1)])
                await db.pool().execute(f"""UPDATE {S}.webhook_deliveries SET status = $2, attempts = $3, last_code = $4, last_error = $5,
                                               next_at = $6 WHERE id = $1""", d["id"], "failed" if last else "pending", n, code, err, nxt)
                await db.pool().execute(f"UPDATE {S}.webhooks SET status = 'failing', last_error = $2 WHERE site_id = $1", d["site_id"], err)
    except Exception:                                                   # noqa: BLE001 - the loop never dies
        log.exception("webhook delivery")
    return sent


async def purge() -> None:
    """Deliveries older than webhook_keep_days are removed."""
    days = int((await db.settings(["webhook_keep_days"])).get("webhook_keep_days") or 30)
    await db.pool().execute(f"DELETE FROM {S}.webhook_deliveries WHERE created_at < now() - make_interval(days => $1) AND status <> 'pending'", days)


def shape(w: dict | None) -> dict:
    """What the site's app reads back (spec v1.1 GET …/webhook): never the secret."""
    return {"url": (w or {}).get("url"), "events": list((w or {}).get("events") or [])}


async def status(site_id: str) -> dict | None:
    """For our staff portal: where it sends, whether it works, and the latest deliveries."""
    w = await row(site_id)
    if not w:
        return None
    recent = await db.pool().fetch(
        f"""SELECT id, event, status, attempts, last_code, last_error, created_at, sent_at FROM {S}.webhook_deliveries
            WHERE site_id = $1 ORDER BY created_at DESC LIMIT 10""", site_id)
    waiting = await db.pool().fetchval(f"SELECT count(*) FROM {S}.webhook_deliveries WHERE site_id = $1 AND status = 'pending'", site_id)
    return {"url": w["url"], "events": list(w["events"] or []), "status": w["status"], "lastError": w["last_error"],
            "lastDeliveryAt": w["last_delivery_at"], "waiting": waiting,
            "recent": [{"id": x["id"], "event": x["event"], "status": x["status"], "attempts": x["attempts"], "code": x["last_code"],
                        "error": x["last_error"], "at": x["created_at"], "sentAt": x["sent_at"]} for x in recent]}


async def set_connection(site_id: str, kind: str, status: str, error: str | None) -> None:
    """Record a connection's state; when it changes, the site's app is told (connection.changed)."""
    old = await db.pool().fetchval(
        f"""WITH o AS (SELECT status FROM {S}.site_connections WHERE site_id = $1 AND type = $2)
            UPDATE {S}.site_connections c SET status = $3, last_error = $4, last_checked_at = now()
            FROM o WHERE c.site_id = $1 AND c.type = $2 RETURNING o.status""", site_id, kind, status, error)
    if old is not None and old != status:
        await emit(site_id, "connection.changed", {"type": kind, "status": status, **({"error": error} if error else {})})
