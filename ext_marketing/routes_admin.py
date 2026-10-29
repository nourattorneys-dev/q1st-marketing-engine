"""Owner routes (/admin/v1) -- called only by our staff portal's Clients section with X-Admin-Key.
Plans, quotas, accounts, sites, per-site API keys, quota overrides."""
from __future__ import annotations
from fastapi import APIRouter, Request

import json

from . import db, quotas, security, shapes, validate
from .auth import ApiError, admin, audit
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/admin/v1")


def ACTOR_OF(req: Request) -> str:
    """Our staff user, passed by the staff portal (X-Acting-User); audit shows who did it."""
    return "staff:" + ((req.headers.get("x-acting-user") or "").strip()[:120] or "unknown")


async def _metric_keys() -> set[str]:
    return {x["key"] for x in await db.pool().fetch(f"SELECT key FROM {S}.metrics WHERE active")}


async def _quota_rows(items) -> list[dict]:
    keys = await _metric_keys()
    out, f = [], {}
    for i, q in enumerate(items or []):
        m = q.get("metric")
        if m not in keys:
            f[f"quotas[{i}].metric"] = f"Unknown metric {m}"
            continue
        if q.get("mode", "hard") not in ("hard", "soft"):
            f[f"quotas[{i}].mode"] = "hard or soft"
        lim = q.get("limit")
        if lim is not None and (not isinstance(lim, int) or lim < 0):
            f[f"quotas[{i}].limit"] = "Whole number ≥ 0, or null for unlimited"
        w = q.get("warnAt")
        if w is not None and not (isinstance(w, (int, float)) and 0 < w <= 1):
            f[f"quotas[{i}].warnAt"] = "Between 0 and 1, or null"
        out.append({"metric": m, "limit": lim, "mode": q.get("mode", "hard"), "warnAt": w})
    if f:
        raise validate.bad(f)
    return out


async def _plan(pid: str) -> dict:
    p = await db.pool().fetchrow(f"SELECT * FROM {S}.plans WHERE id = $1", pid)
    if not p:
        raise ApiError(404, "not_found", "Plan not found.")
    q = await db.pool().fetch(f"SELECT * FROM {S}.plan_quotas WHERE plan_id = $1 ORDER BY metric", pid)
    return shapes.plan(dict(p), [dict(x) for x in q])


# ---------------------------------------------------------------- plans
@r.get("/plans")
async def plans(req: Request):
    admin(req)
    ids = [x["id"] for x in await db.pool().fetch(f"SELECT id FROM {S}.plans ORDER BY price NULLS LAST, name")]
    return ok([await _plan(i) for i in ids])


@r.post("/plans")
async def create_plan(req: Request):
    admin(req)
    d = await body(req)
    if not (d.get("name") or "").strip():
        raise validate.bad({"name": "Required"})
    quotas = await _quota_rows(d.get("quotas"))
    pid = security.new_id("plan")
    async with db.pool().acquire() as con, con.transaction():
        await con.execute(
            f"""INSERT INTO {S}.plans (id, name, price, currency, billing_period, queue_priority)
                VALUES ($1,$2,$3,$4,$5,$6)""",
            pid, d["name"].strip(), d.get("price"), d.get("currency"), d.get("billingPeriod") or "month", int(d.get("queuePriority") or 0))
        for q in quotas:
            await con.execute(f"INSERT INTO {S}.plan_quotas (plan_id, metric, limit_value, mode, warn_at) VALUES ($1,$2,$3,$4,$5)",
                              pid, q["metric"], q["limit"], q["mode"], q["warnAt"])
    await audit(ACTOR_OF(req), "plan.create", target=pid)
    return ok(await _plan(pid))


@r.put("/plans/{pid}")
async def update_plan(pid: str, req: Request):
    admin(req)
    d = await body(req)
    await _plan(pid)
    quotas = await _quota_rows(d.get("quotas")) if "quotas" in d else None
    async with db.pool().acquire() as con, con.transaction():
        n = await con.execute(
            f"""UPDATE {S}.plans SET name = coalesce($3, name), price = CASE WHEN $4::boolean THEN $5 ELSE price END,
                currency = coalesce($6, currency), queue_priority = coalesce($7, queue_priority), active = coalesce($8, active),
                version = version + 1, updated_at = now() WHERE id = $1 AND version = $2""",
            pid, int(d.get("version") or 0), d.get("name"), "price" in d, d.get("price"), d.get("currency"),
            d.get("queuePriority"), d.get("active"))
        if n.endswith(" 0"):
            raise ApiError(409, "version_conflict", "Plan changed meanwhile. Reload.")
        if quotas is not None:
            await con.execute(f"DELETE FROM {S}.plan_quotas WHERE plan_id = $1", pid)
            for q in quotas:
                await con.execute(f"INSERT INTO {S}.plan_quotas (plan_id, metric, limit_value, mode, warn_at) VALUES ($1,$2,$3,$4,$5)",
                                  pid, q["metric"], q["limit"], q["mode"], q["warnAt"])
    await audit(ACTOR_OF(req), "plan.update", target=pid, detail={"fields": list(d)})
    return ok(await _plan(pid))


# ---------------------------------------------------------------- accounts
@r.get("/metrics")
async def metrics(req: Request):
    """What can be metered, with labels and units (the site API's /meta lists only the names)."""
    admin(req)
    return ok([dict(x) for x in await db.pool().fetch(f"SELECT key, label, unit FROM {S}.metrics WHERE active ORDER BY key")])


# ---------------------------------------------------------------- upgrade requests (sent from the clients' apps)
@r.get("/upgrade-requests")
async def upgrade_requests(req: Request, status: str = "open"):
    admin(req)
    rows = await db.pool().fetch(
        f"""SELECT u.*, a.name AS account_name, s.name AS site_name FROM {S}.upgrade_requests u
            JOIN {S}.accounts a ON a.id = u.account_id LEFT JOIN {S}.sites s ON s.id = u.site_id
            WHERE ($1 = '' OR u.status = ANY(string_to_array($1, ','))) ORDER BY u.created_at DESC""", status)
    return ok([{"id": x["id"], "accountId": x["account_id"], "accountName": x["account_name"], "siteId": x["site_id"], "siteName": x["site_name"],
                "metric": x["metric"], "note": x["note"], "by": x["requested_by"], "status": x["status"], "handledBy": x["handled_by"],
                "handledAt": x["handled_at"], "at": x["created_at"]} for x in rows])


@r.post("/upgrade-requests/{uid}")
async def handle_upgrade_request(uid: str, req: Request):
    """Mark a request done or declined (the change itself is made on the client's plan or limits)."""
    admin(req)
    d = await body(req)
    if d.get("status") not in ("done", "declined", "open"):
        raise validate.bad({"status": "done, declined or open"})
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.upgrade_requests SET status = $2, handled_by = CASE WHEN $2 = 'open' THEN NULL ELSE $3 END,
               handled_at = CASE WHEN $2 = 'open' THEN NULL ELSE now() END WHERE id = $1 RETURNING *""", uid, d["status"], ACTOR_OF(req))
    if not row:
        raise ApiError(404, "not_found", "Request not found.")
    await audit(ACTOR_OF(req), "upgrade_request." + d["status"], account_id=row["account_id"], site_id=row["site_id"], target=uid)
    return ok({"id": uid, "status": row["status"]})


# ---------------------------------------------------------------- usage & revenue (one month, every client)
@r.get("/revenue")
async def revenue(req: Request, month: str = ""):
    """Per client for one billing month: the price it pays, usage against each limit, overage, and the estimated AI cost."""
    admin(req)
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    s = await db.settings(["billing_timezone", "currency", "cost_per_1k_tokens"])
    tz = s.get("billing_timezone") or "UTC"
    month = month or f"{datetime.now(ZoneInfo(tz)):%Y-%m}"
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise validate.bad({"month": "A month like 2026-10"})
    p = db.pool()
    accs = await p.fetch(
        f"""SELECT a.id, a.name, a.status, p.name AS plan_name, coalesce(a.price_override, p.price) AS price, coalesce(p.currency, $1) AS currency,
                   p.billing_period FROM {S}.accounts a LEFT JOIN {S}.plans p ON p.id = a.plan_id WHERE a.status <> 'closed' ORDER BY a.name""", s.get("currency"))
    use = await p.fetch(
        f"""SELECT account_id, metric, sum(CASE WHEN metric = 'aiTokens' THEN tokens_in + tokens_out ELSE qty END) AS used,
                   sum(CASE WHEN over_quota THEN qty ELSE 0 END) AS over, sum(CASE WHEN metric = 'aiTokens' THEN 0 ELSE coalesce(cost_estimate, (tokens_in + tokens_out) * $3::numeric / 1000, 0) END) AS cost
            FROM {S}.usage_events WHERE to_char(at AT TIME ZONE $1, 'YYYY-MM') = $2 GROUP BY 1, 2""", tz, month, s.get("cost_per_1k_tokens"))
    by: dict = {}
    for u in use:
        by.setdefault(u["account_id"], {})[u["metric"]] = u
    cur = month == f"{datetime.now(ZoneInfo(tz)):%Y-%m}"
    out, totals = [], {"price": 0.0, "cost": 0.0}
    for a in accs:
        m = by.get(a["id"], {})
        lim = {q["metric"]: q for q in (await quotas.effective(a["id"]))["quotas"]} if cur else {}
        rows = [{"metric": k, "used": int(v["used"] or 0), "over": int(v["over"] or 0),
                 "limit": (lim.get(k) or {}).get("limit"), "mode": (lim.get(k) or {}).get("mode")} for k, v in sorted(m.items())]
        cost = float(sum(float(v["cost"] or 0) for v in m.values()))
        price = (float(a["price"]) / (12 if a["billing_period"] == "year" else 1)) if a["price"] is not None else None   # this month's share
        totals["price"] += price or 0
        totals["cost"] += cost
        out.append({"accountId": a["id"], "name": a["name"], "status": a["status"], "plan": a["plan_name"], "price": price,
                    "currency": a["currency"], "usage": rows, "overage": sum(r["over"] for r in rows), "aiCost": round(cost, 2),
                    "margin": round(price - cost, 2) if price is not None else None})
    return ok({"month": month, "currency": s.get("currency"), "costRateSet": s.get("cost_per_1k_tokens") is not None,
               "clients": out, "totals": {"price": round(totals["price"], 2), "aiCost": round(totals["cost"], 2),
                                          "margin": round(totals["price"] - totals["cost"], 2)}})


@r.get("/accounts")
async def accounts(req: Request):
    admin(req)
    rows = await db.pool().fetch(
        f"""SELECT a.*, p.name AS plan_name, coalesce(a.price_override, p.price) AS price, p.currency,
                   (SELECT count(*) FROM {S}.sites s WHERE s.account_id = a.id AND s.deleted_at IS NULL) AS site_count,
                   (SELECT count(*) FROM {S}.upgrade_requests u WHERE u.account_id = a.id AND u.status = 'open') AS open_requests
            FROM {S}.accounts a LEFT JOIN {S}.plans p ON p.id = a.plan_id ORDER BY a.created_at DESC""")
    return ok([{**shapes.account(dict(x)), "planName": x["plan_name"], "price": x["price"], "currency": x["currency"],
                "siteCount": x["site_count"], "openRequests": x["open_requests"]} for x in rows])


@r.get("/accounts/{aid}")
async def account_detail(aid: str, req: Request):
    """Everything the Client page shows: account, plan, effective quotas with usage, overrides, sites, keys."""
    admin(req)
    p = db.pool()
    a = await p.fetchrow(f"SELECT * FROM {S}.accounts WHERE id = $1", aid)
    if not a:
        raise ApiError(404, "not_found", "Account not found.")
    plan = await _plan(a["plan_id"]) if a["plan_id"] else None
    sites = [await shapes.site(dict(x)) for x in await p.fetch(
        f"SELECT * FROM {S}.sites WHERE account_id = $1 AND deleted_at IS NULL ORDER BY created_at", aid)]
    ovr = await p.fetch(f"SELECT * FROM {S}.quota_overrides WHERE account_id = $1 ORDER BY metric, site_id NULLS FIRST", aid)
    keys = await p.fetch(f"SELECT * FROM {S}.api_keys WHERE account_id = $1 ORDER BY created_at DESC", aid)
    ups = await p.fetch(f"SELECT * FROM {S}.upgrade_requests WHERE account_id = $1 ORDER BY created_at DESC", aid)
    return ok({"account": shapes.account(dict(a)), "plan": plan,
               "price": a["price_override"] if a["price_override"] is not None else (plan or {}).get("price"),
               "usage": await quotas.effective(aid), "sites": sites,
               "overrides": [{"id": x["id"], "metric": x["metric"], "siteId": x["site_id"], "limit": x["limit_value"], "mode": x["mode"],
                              "warnAt": x["warn_at"], "reason": x["reason"], "setBy": x["set_by"], "at": x["created_at"]} for x in ovr],
               "upgradeRequests": [{"id": x["id"], "siteId": x["site_id"], "metric": x["metric"], "note": x["note"], "by": x["requested_by"],
                                    "status": x["status"], "at": x["created_at"]} for x in ups],
               "keys": [{"id": x["id"], "siteId": x["site_id"], "prefix": x["key_prefix"], "label": x["label"], "createdAt": x["created_at"],
                         "lastUsedAt": x["last_used_at"], "revokedAt": x["revoked_at"]} for x in keys]})


@r.post("/accounts")
async def create_account(req: Request):
    admin(req)
    d = await body(req)
    f = {}
    if not (d.get("name") or "").strip():
        f["name"] = "Required"
    if d.get("planId") and not await db.pool().fetchval(f"SELECT 1 FROM {S}.plans WHERE id = $1", d["planId"]):
        f["planId"] = "Unknown plan"
    if f:
        raise validate.bad(f)
    aid = security.new_id("acc")
    row = await db.pool().fetchrow(
        f"INSERT INTO {S}.accounts (id, name, plan_id, price_override, notes) VALUES ($1,$2,$3,$4,$5) RETURNING *",
        aid, d["name"].strip(), d.get("planId"), d.get("priceOverride"), d.get("notes"))
    await audit(ACTOR_OF(req), "account.create", account_id=aid, detail={"plan": d.get("planId")})
    return ok(shapes.account(dict(row)))


@r.put("/accounts/{aid}")
async def update_account(aid: str, req: Request):
    admin(req)
    d = await body(req)
    if d.get("status") and d["status"] not in ("active", "suspended", "closed"):
        raise validate.bad({"status": "active, suspended or closed"})
    if d.get("planId") and not await db.pool().fetchval(f"SELECT 1 FROM {S}.plans WHERE id = $1", d["planId"]):
        raise validate.bad({"planId": "Unknown plan"})
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.accounts SET name = coalesce($3, name), plan_id = coalesce($4, plan_id), status = coalesce($5, status),
            price_override = CASE WHEN $6::boolean THEN $7 ELSE price_override END, notes = coalesce($8, notes),
            version = version + 1, updated_at = now() WHERE id = $1 AND version = $2 RETURNING *""",
        aid, int(d.get("version") or 0), d.get("name"), d.get("planId"), d.get("status"),
        "priceOverride" in d, d.get("priceOverride"), d.get("notes"))
    if not row:
        raise ApiError(409, "version_conflict", "Account not found or changed meanwhile. Reload.")
    await audit(ACTOR_OF(req), "account.update", account_id=aid, detail={"fields": list(d)})
    return ok(shapes.account(dict(row)))


@r.get("/accounts/{aid}/sites")
async def account_sites(aid: str, req: Request):
    admin(req)
    rows = await db.pool().fetch(f"SELECT * FROM {S}.sites WHERE account_id = $1 AND deleted_at IS NULL ORDER BY created_at", aid)
    return ok([await shapes.site(dict(x)) for x in rows])


@r.post("/accounts/{aid}/sites")
async def create_site(aid: str, req: Request):
    admin(req)
    if not await db.pool().fetchval(f"SELECT 1 FROM {S}.accounts WHERE id = $1", aid):
        raise ApiError(404, "not_found", "Account not found.")
    v = await validate.site_fields(await body(req))
    sid = security.new_id("site")
    row = await db.pool().fetchrow(
        f"""INSERT INTO {S}.sites (id, account_id, name, url, category, timezone, languages, default_language, profile)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
        sid, aid, v["name"], v["url"], v.get("category"), v["timezone"], v["languages"], v["default_language"], v.get("profile") or {})
    await audit(ACTOR_OF(req), "site.create", account_id=aid, site_id=sid, detail={"url": v["url"]})
    return ok(await shapes.site(dict(row)))


@r.delete("/accounts/{aid}/sites/{sid}")
async def delete_site(aid: str, sid: str, req: Request):
    admin(req)
    n = await db.pool().execute(f"UPDATE {S}.sites SET deleted_at = now() WHERE id = $1 AND account_id = $2 AND deleted_at IS NULL", sid, aid)
    if n.endswith(" 0"):
        raise ApiError(404, "not_found", "Site not found.")
    await db.pool().execute(f"UPDATE {S}.api_keys SET revoked_at = now() WHERE site_id = $1 AND revoked_at IS NULL", sid)
    await audit(ACTOR_OF(req), "site.delete", account_id=aid, site_id=sid)
    return ok({"deleted": True})


# ---------------------------------------------------------------- API keys
@r.post("/accounts/{aid}/api-keys")
async def new_key(aid: str, req: Request):
    admin(req)
    d = await body(req)
    sid = d.get("siteId")
    if not sid or not await db.pool().fetchval(f"SELECT 1 FROM {S}.sites WHERE id = $1 AND account_id = $2 AND deleted_at IS NULL", sid, aid):
        raise validate.bad({"siteId": "Choose a site of this account"})
    raw, h, prefix = security.new_api_key()
    kid = security.new_id("key")
    secs = (await db.settings(["key_reveal_seconds"])).get("key_reveal_seconds")
    await db.pool().execute(f"UPDATE {S}.api_keys SET reveal_enc = NULL, reveal_until = NULL WHERE reveal_until < now()")   # never-shown copies expire
    await db.pool().execute(
        f"""INSERT INTO {S}.api_keys (id, account_id, site_id, key_hash, key_prefix, label, reveal_enc, reveal_until)
            VALUES ($1,$2,$3,$4,$5,$6,$7, now() + make_interval(secs => $8))""",
        kid, aid, sid, h, prefix, d.get("label"), security.encrypt(raw), float(secs or 0))
    await audit(ACTOR_OF(req), "apikey.create", account_id=aid, target=kid)
    return ok({"id": kid, "siteId": sid, "prefix": prefix, "label": d.get("label"), "key": raw, "note": "Shown once. Store it now."})


@r.delete("/accounts/{aid}/api-keys/{kid}")
async def revoke_key(aid: str, kid: str, req: Request):
    admin(req)
    n = await db.pool().execute(f"UPDATE {S}.api_keys SET revoked_at = now(), reveal_enc = NULL, reveal_until = NULL WHERE id = $1 AND account_id = $2 AND revoked_at IS NULL", kid, aid)
    if n.endswith(" 0"):
        raise ApiError(404, "not_found", "Key not found or already revoked.")
    await audit(ACTOR_OF(req), "apikey.revoke", account_id=aid, target=kid)
    return ok({"revoked": True})


# ---------------------------------------------------------------- quota overrides (raise / lower per account or site)
@r.put("/accounts/{aid}/quota-overrides")
async def set_override(aid: str, req: Request):
    admin(req)
    d = await body(req)
    q = (await _quota_rows([{"metric": d.get("metric"), "limit": d.get("limit"), "mode": d.get("mode") or "hard", "warnAt": d.get("warnAt")}]))[0]
    sid = d.get("siteId")
    if sid and not await db.pool().fetchval(f"SELECT 1 FROM {S}.sites WHERE id = $1 AND account_id = $2", sid, aid):
        raise validate.bad({"siteId": "Not a site of this account"})
    await db.pool().execute(
        f"""INSERT INTO {S}.quota_overrides (account_id, site_id, metric, limit_value, mode, warn_at, reason, set_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (account_id, (coalesce(site_id, '')), metric) DO UPDATE SET limit_value = EXCLUDED.limit_value, mode = EXCLUDED.mode,
              warn_at = EXCLUDED.warn_at, reason = EXCLUDED.reason, set_by = EXCLUDED.set_by, created_at = now()""",
        aid, sid, q["metric"], q["limit"], q["mode"], q["warnAt"], d.get("reason"), ACTOR_OF(req))
    await audit(ACTOR_OF(req), "quota.override", account_id=aid, site_id=sid, detail=q)
    rows = await db.pool().fetch(f"SELECT * FROM {S}.quota_overrides WHERE account_id = $1 ORDER BY metric", aid)
    return ok([{"metric": x["metric"], "siteId": x["site_id"], "limit": x["limit_value"], "mode": x["mode"],
                "warnAt": x["warn_at"], "reason": x["reason"]} for x in rows])


@r.get("/accounts/{aid}/api-keys")
async def list_keys(aid: str, req: Request):
    admin(req)
    rows = await db.pool().fetch(f"SELECT * FROM {S}.api_keys WHERE account_id = $1 ORDER BY created_at DESC", aid)
    return ok([{"id": x["id"], "siteId": x["site_id"], "prefix": x["key_prefix"], "label": x["label"], "createdAt": x["created_at"],
                "lastUsedAt": x["last_used_at"], "revokedAt": x["revoked_at"]} for x in rows])


@r.put("/accounts/{aid}/sites/{sid}")
async def admin_update_site(aid: str, sid: str, req: Request):
    admin(req)
    d = await body(req)
    v = await validate.site_fields(d, partial=True)
    cols = {k: v[k] for k in ("name", "url", "category", "timezone", "languages", "default_language", "profile") if k in v}
    if not cols:
        raise validate.bad({"name": "Nothing to change"})
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(cols))
    row = await db.pool().fetchrow(
        f"UPDATE {S}.sites SET {sets}, version = version + 1 WHERE id = $1 AND account_id = $2 AND deleted_at IS NULL RETURNING *",
        sid, aid, *cols.values())
    if not row:
        raise ApiError(404, "not_found", "Site not found.")
    await audit(ACTOR_OF(req), "site.update", account_id=aid, site_id=sid, detail={"fields": list(cols)})
    return ok(await shapes.site(dict(row)))


@r.delete("/accounts/{aid}/quota-overrides/{oid}")
async def remove_override(aid: str, oid: int, req: Request):
    admin(req)
    row = await db.pool().fetchrow(f"DELETE FROM {S}.quota_overrides WHERE id = $1 AND account_id = $2 RETURNING metric, site_id", oid, aid)
    if not row:
        raise ApiError(404, "not_found", "Override not found.")
    await audit(ACTOR_OF(req), "quota.override_removed", account_id=aid, site_id=row["site_id"], detail={"metric": row["metric"]})
    return ok({"removed": True})


# ---------------------------------------------------------------- system settings
@r.get("/settings")
async def get_settings(req: Request):
    admin(req)
    rows = await db.pool().fetch(f"SELECT key, value, description, updated_by, updated_at FROM {S}.settings ORDER BY key")
    return ok([{"key": x["key"], "value": x["value"], "description": x["description"], "updatedBy": x["updated_by"],
                "updatedAt": x["updated_at"]} for x in rows])


@r.put("/settings")
async def put_settings(req: Request):
    """Body {key: value_as_json_text, ...}. Only existing keys; each value must be valid JSON of the same kind as before."""
    admin(req)
    d = await body(req)
    cur = {x["key"]: x["value"] for x in await db.pool().fetch(f"SELECT key, value FROM {S}.settings")}
    f, new = {}, {}
    for k, raw in d.items():
        if k not in cur:
            f[k] = "Unknown setting"
            continue
        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            f[k] = "Not valid JSON (text needs quotes)"
            continue
        old = cur[k]
        num = (int, float)
        same = (old is None or v is None or (isinstance(old, num) and isinstance(v, num) and not isinstance(v, bool))
                or type(old) is type(v))
        if not same:
            f[k] = f"Must be a {type(old).__name__}"
            continue
        if v != old:
            new[k] = v
    if f:
        raise validate.bad(f)
    for k, v in new.items():
        await db.pool().execute(f"UPDATE {S}.settings SET value = $2, updated_by = $3, updated_at = now() WHERE key = $1", k, v, ACTOR_OF(req))
    if new:
        await audit(ACTOR_OF(req), "settings.update", detail={"keys": list(new)})
    return ok({"changed": list(new)})


@r.post("/accounts/{aid}/api-keys/{kid}/reveal")
async def reveal_key(aid: str, kid: str, req: Request):
    """Returns a newly issued key ONCE and deletes the stored copy in the same step; expired or already shown -> 404."""
    admin(req)
    async with db.pool().acquire() as con, con.transaction():
        row = await con.fetchrow(
            f"""SELECT reveal_enc, reveal_until > now() AS live, site_id, label FROM {S}.api_keys
                WHERE id = $1 AND account_id = $2 AND revoked_at IS NULL FOR UPDATE""", kid, aid)
        if row and row["reveal_enc"] is not None:
            await con.execute(f"UPDATE {S}.api_keys SET reveal_enc = NULL, reveal_until = NULL WHERE id = $1", kid)
    if not row or row["reveal_enc"] is None or not row["live"]:
        raise ApiError(404, "not_found", "This key was already shown or the time to show it has passed.")
    raw = security.decrypt(row["reveal_enc"])
    if not raw:
        raise ApiError(404, "not_found", "This key can no longer be shown.")
    await audit(ACTOR_OF(req), "apikey.revealed", account_id=aid, target=kid)
    return ok({"id": kid, "siteId": row["site_id"], "label": row["label"], "key": raw})


@r.post("/accounts/{aid}/api-keys/{kid}/delete")
async def delete_key(aid: str, kid: str, req: Request):
    """Removes a key's row. Only a REVOKED key can be deleted (an active one is revoked first); the audit log keeps its history."""
    admin(req)
    row = await db.pool().fetchrow(f"SELECT revoked_at, site_id, key_prefix FROM {S}.api_keys WHERE id = $1 AND account_id = $2", kid, aid)
    if not row:
        raise ApiError(404, "not_found", "Key not found.")
    if row["revoked_at"] is None:
        raise ApiError(409, "key_active", "Revoke the key first; an active key cannot be deleted.")
    await db.pool().execute(f"DELETE FROM {S}.api_keys WHERE id = $1 AND account_id = $2 AND revoked_at IS NOT NULL", kid, aid)
    await audit(ACTOR_OF(req), "apikey.delete", account_id=aid, site_id=row["site_id"], target=kid, detail={"prefix": row["key_prefix"]})
    return ok({"deleted": True})
