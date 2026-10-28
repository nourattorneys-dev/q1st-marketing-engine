"""Site app routes for the account: plan and quotas (/account/plan), usage by month for this key's site (/account/usage),
and asking our team for an upgrade (/account/upgrade-request)."""
from __future__ import annotations
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Request

from . import db, quotas, security, validate
from .auth import audit, caller, site_of
from .routes_client import body, ok

S = db.SCHEMA
r = APIRouter(prefix="/api/v1")


@r.get("/account/plan")
async def plan(req: Request):
    c = await caller(req)
    a = await db.pool().fetchrow(f"SELECT a.plan_id, p.name FROM {S}.accounts a LEFT JOIN {S}.plans p ON p.id = a.plan_id WHERE a.id = $1", c.account_id)
    e = await quotas.effective(c.account_id, c.site_id)
    start, end = e["period"]["start"], e["period"]["end"]
    return ok({"plan": {"id": a["plan_id"] or "none", "name": a["name"] or "No plan"},
               "period": {"start": start, "end": end - timedelta(seconds=1)},
               "quotas": [{"metric": q["metric"], "limit": q["limit"], "used": q["used"], "scope": "site" if q["source"] == "site" else "account",
                           "mode": q["mode"], "warnAt": float(q["warnAt"]) if q["warnAt"] is not None else None}
                          for q in e["quotas"]],                            # no limit set = unlimited (limit null)
               "metrics": [q["metric"] for q in e["quotas"]]})


@r.get("/account/usage")
async def usage(req: Request):
    """Month by month (the billing timezone's months), for this key's site only: every metric's total, and its results."""
    c = await caller(req)
    q = req.query_params
    frm, til = q.get("from") or "", q.get("to") or ""
    s = await db.settings(["usage_max_months", "billing_timezone"])
    month = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
    from zoneinfo import ZoneInfo
    til = til or f"{datetime.now(ZoneInfo(s.get('billing_timezone') or 'UTC')):%Y-%m}"
    f = {k: "A month like 2026-10" for k, v in (("from", frm or til), ("to", til)) if not month.match(v)}
    if f:
        raise validate.bad(f)
    frm = frm or til
    if frm > til:
        raise validate.bad({"from": "Must not be after 'to'"})
    ms = []
    y, m = map(int, frm.split("-"))
    while f"{y:04d}-{m:02d}" <= til:
        ms.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if len(ms) > int(s.get("usage_max_months") or 24):
        raise validate.bad({"from": f"At most {s.get('usage_max_months') or 24} months at once"})
    tz = s.get("billing_timezone") or "UTC"
    rows = await db.pool().fetch(
        f"""SELECT to_char(at AT TIME ZONE $3, 'YYYY-MM') AS month, metric,
                   sum(CASE WHEN metric = 'aiTokens' THEN tokens_in + tokens_out ELSE qty END) AS n
            FROM {S}.usage_events WHERE site_id = $1 AND to_char(at AT TIME ZONE $3, 'YYYY-MM') = ANY($2) GROUP BY 1, 2""",
        c.site_id, ms, tz)
    by = {x: {} for x in ms}
    for x in rows:
        by[x["month"]][x["metric"]] = int(x["n"] or 0)
    res = await db.pool().fetchrow(
        f"""SELECT (SELECT count(*) FROM {S}.posts WHERE site_id = $1 AND status = 'published'
                     AND to_char(published_at AT TIME ZONE $3, 'YYYY-MM') = ANY($2)) AS published,
                   (SELECT count(*) FROM {S}.keywords WHERE site_id = $1 AND status = 'covered'
                     AND to_char(updated_at AT TIME ZONE $3, 'YYYY-MM') = ANY($2)) AS covered""", c.site_id, ms, tz)
    return ok({"months": [{"month": x, "sites": [{"siteId": c.site_id, "metrics": by[x]}]} for x in ms],
               "results": [{"siteId": c.site_id, "published": int(res["published"]), "keywordsCovered": int(res["covered"])}]})


@r.post("/account/upgrade-request")
async def upgrade_request(req: Request):
    """Sent to our team (the staff portal lists open requests on the client's page)."""
    c = await caller(req)
    await site_of(c, c.site_id)
    try:
        d = await body(req)
    except Exception:                                                   # noqa: BLE001 - an empty body is a plain request
        d = {}
    metric = d.get("metric")
    if metric is not None and not await db.pool().fetchval(f"SELECT 1 FROM {S}.metrics WHERE key = $1", metric):
        raise validate.bad({"metric": "Unknown metric"})
    note = " ".join(str(d.get("note") or "").split())[:2000] or None
    await db.pool().execute(
        f"INSERT INTO {S}.upgrade_requests (id, account_id, site_id, metric, note, requested_by) VALUES ($1,$2,$3,$4,$5,$6)",
        security.new_id("up"), c.account_id, c.site_id, metric, note, c.user)
    await audit(c.name, "account.upgrade_request", account_id=c.account_id, site_id=c.site_id, detail={"metric": metric})
    return ok({"requested": True})
