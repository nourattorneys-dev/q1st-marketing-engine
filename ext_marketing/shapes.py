"""DB rows -> the exact JSON shapes in the front-end build spec (camelCase)."""
from __future__ import annotations
from . import db

S = db.SCHEMA


async def connection_types() -> list[str]:
    """the website + every social platform the engine can publish to (from settings, not code)."""
    s = await db.settings(["platforms"])
    return ["website"] + [p for p in (s.get("platforms") or []) if p != "website"]


async def site(row: dict) -> dict:
    p = db.pool()
    conns: dict = {t: "missing" for t in await connection_types()}
    conns["website"] = {"type": None, "status": "missing"}
    for r in await p.fetch(f"SELECT type, status, config FROM {S}.site_connections WHERE site_id = $1", row["id"]):
        conns[r["type"]] = {"type": (r["config"] or {}).get("type"), "status": r["status"]} if r["type"] == "website" else r["status"]
    counts = {k: 0 for k in ("suggestions", "pending", "approved", "scheduled", "published", "failed", "keywordsNew")}
    for r in await p.fetch(f"SELECT status, count(*) n FROM {S}.posts WHERE site_id = $1 GROUP BY 1", row["id"]):
        if r["status"] in counts:
            counts[r["status"]] = r["n"]
    counts["scheduled"] = counts["approved"]
    counts["suggestions"] = await p.fetchval(f"SELECT count(*) FROM {S}.suggestions WHERE site_id=$1 AND status='new'", row["id"])
    counts["keywordsNew"] = await p.fetchval(f"SELECT count(*) FROM {S}.keywords WHERE site_id=$1 AND status='new'", row["id"])
    return {"id": row["id"], "accountId": row["account_id"], "name": row["name"], "url": row["url"],
            "category": row["category"], "timezone": row["timezone"], "languages": row["languages"],
            "defaultLanguage": row["default_language"], "profile": row["profile"] or {},
            "connections": conns, "counts": counts, "createdAt": row["created_at"], "version": row["version"]}


def account(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "planId": r["plan_id"], "status": r["status"],
            "priceOverride": r["price_override"], "notes": r["notes"], "createdAt": r["created_at"], "version": r["version"]}


def plan(r: dict, quotas: list[dict]) -> dict:
    return {"id": r["id"], "name": r["name"], "price": r["price"], "currency": r["currency"],
            "billingPeriod": r["billing_period"], "queuePriority": r["queue_priority"], "active": r["active"],
            "version": r["version"],
            "quotas": [{"metric": q["metric"], "limit": q["limit_value"], "mode": q["mode"], "warnAt": q["warn_at"]} for q in quotas]}
