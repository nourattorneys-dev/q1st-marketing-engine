"""Site app route for its webhook (spec v1.1 §7 GET / PUT …/webhook, §8). The signing secret is returned once: on the first
save, or when rotateSecret is true. It is stored encrypted."""
from __future__ import annotations
from fastapi import APIRouter, Request

from . import db, netguard, security, validate, webhooks
from .auth import audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")


@r.get("/sites/{site_id}/webhook")
async def get_webhook(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    return ok(webhooks.shape(await webhooks.row(site_id)))


@r.put("/sites/{site_id}/webhook")
async def put_webhook(site_id: str, req: Request):
    c = await caller(req); await site_of(c, site_id)
    d = await body(req)
    s = await db.settings(["webhook_events", "webhook_require_https"])
    allowed = list(s.get("webhook_events") or [])
    url = (d.get("url") or "").strip() or None
    events = d.get("events") if d.get("events") is not None else allowed
    f = {}
    if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
        f["events"] = "A list of event names"
    elif [e for e in events if e not in allowed]:
        f["events"] = "Unknown: " + ", ".join(e for e in events if e not in allowed)
    if url:
        try:
            await netguard.check(url, require_https=bool(s.get("webhook_require_https", True)))
        except netguard.Blocked as e:
            f["url"] = str(e)
    if f:
        raise validate.bad(f)
    cur = await webhooks.row(site_id)
    if not url:                                                         # no address: the webhook is removed (the app polls instead)
        await db.pool().execute(f"DELETE FROM {S}.webhooks WHERE site_id = $1", site_id)
        await db.pool().execute(f"UPDATE {S}.webhook_deliveries SET status = 'dropped', last_error = 'Webhook removed' "
                                f"WHERE site_id = $1 AND status = 'pending'", site_id)
        await audit(c.name, "webhook.remove", account_id=c.account_id, site_id=site_id)
        return ok({"url": None, "events": []})
    secret = webhooks.new_secret() if (not cur or d.get("rotateSecret") is True) else None
    evs = list(dict.fromkeys(events))
    if cur:
        await db.pool().execute(
            f"""UPDATE {S}.webhooks SET url = $2, events = $3, secret_enc = coalesce($4, secret_enc),
                   status = CASE WHEN url = $2 THEN status ELSE 'new' END, updated_at = now() WHERE site_id = $1""",
            site_id, url, evs, security.encrypt(secret) if secret else None)
    else:
        await db.pool().execute(f"INSERT INTO {S}.webhooks (site_id, url, events, secret_enc) VALUES ($1,$2,$3,$4)",
                                site_id, url, evs, security.encrypt(secret))
    await audit(c.name, "webhook.set", account_id=c.account_id, site_id=site_id,
                detail={"url": url, "events": len(events), "secretRotated": bool(secret)})
    return ok({**webhooks.shape(await webhooks.row(site_id)), **({"secret": secret} if secret else {})})
