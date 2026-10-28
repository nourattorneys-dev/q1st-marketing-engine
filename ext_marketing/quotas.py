"""Effective quotas = plan quota, overridden by an account override, overridden by a site override.
'used' counts usage_events in the current billing month. Used by the admin views now and by enforcement later."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db

S = db.SCHEMA


async def period() -> tuple[datetime, datetime]:
    tz = ZoneInfo((await db.settings(["billing_timezone"])).get("billing_timezone") or "UTC")
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


async def effective(account_id: str, site_id: str | None = None) -> dict:
    p = db.pool()
    start, end = await period()
    warn_default = (await db.settings(["quota_warn_at"])).get("quota_warn_at")
    metrics = await p.fetch(f"SELECT key, label, unit FROM {S}.metrics WHERE active ORDER BY key")
    plan_q = {r["metric"]: r for r in await p.fetch(
        f"""SELECT q.* FROM {S}.plan_quotas q JOIN {S}.accounts a ON a.plan_id = q.plan_id WHERE a.id = $1""", account_id)}
    ovr = await p.fetch(f"SELECT * FROM {S}.quota_overrides WHERE account_id = $1", account_id)
    acc_o = {r["metric"]: r for r in ovr if r["site_id"] is None}
    site_o = {r["metric"]: r for r in ovr if site_id and r["site_id"] == site_id}
    used = {r["metric"]: r["q"] for r in await p.fetch(
        f"""SELECT metric, sum(CASE WHEN metric = 'aiTokens' THEN tokens_in + tokens_out ELSE qty END) q
            FROM {S}.usage_events WHERE account_id = $1 AND at >= $2 AND at < $3 GROUP BY 1""", account_id, start, end)}
    used["sites"] = await p.fetchval(f"SELECT count(*) FROM {S}.sites WHERE account_id = $1 AND deleted_at IS NULL", account_id)
    rows = []
    for m in metrics:
        k = m["key"]
        src, q = ("site", site_o[k]) if k in site_o else ("account", acc_o[k]) if k in acc_o else ("plan", plan_q[k]) if k in plan_q else (None, None)
        rows.append({"metric": k, "label": m["label"], "unit": m["unit"],
                     "limit": q["limit_value"] if q else None, "mode": (q["mode"] if q and q["mode"] else "hard") if q else None,
                     "warnAt": (q["warn_at"] if q and q["warn_at"] is not None else warn_default), "used": int(used.get(k) or 0),
                     "source": src})
    return {"period": {"start": start, "end": end}, "quotas": rows}
