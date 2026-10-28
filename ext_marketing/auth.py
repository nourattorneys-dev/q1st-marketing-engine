"""Request identity: portal user (Bearer token), account API key (X-Api-Key), or our admin (X-Admin-Key)."""
from __future__ import annotations
from dataclasses import dataclass

from fastapi import Request

from . import db, security

S = db.SCHEMA


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra


@dataclass
class Caller:
    account_id: str
    site_id: str             # the one site this key may use
    name: str                # for the audit log: key label, or the acting person the site app passes
    user: str = ""           # the acting person alone (X-Acting-User); who holds an editing lock


async def caller(request: Request) -> Caller:
    """Each site's app calls from its server with that site's key (X-Api-Key). Optional X-Acting-User names the person."""
    h = request.headers
    if not h.get("x-api-key"):
        raise ApiError(401, "unauthorized", "API key missing.")
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.api_keys k SET last_used_at = now() FROM {S}.accounts a, {S}.sites s
            WHERE k.key_hash = $1 AND k.revoked_at IS NULL AND a.id = k.account_id AND a.status = 'active'
              AND s.id = k.site_id AND s.deleted_at IS NULL
            RETURNING k.account_id, k.site_id, coalesce(k.label, k.key_prefix) AS label""",
        security.hash_api_key(h["x-api-key"]))
    if not row:
        raise ApiError(401, "unauthorized", "API key not valid, revoked, or its account is suspended.")
    who = (h.get("x-acting-user") or "").strip()[:120]
    return Caller(row["account_id"], row["site_id"], f"{who} via {row['label']}" if who else f"key:{row['label']}", who or f"key:{row['label']}")


async def site_of(c: Caller, site_id: str) -> dict:
    """The isolation gate: a key reaches only its own site."""
    if site_id != c.site_id:
        raise ApiError(404, "not_found", "Site not found.")
    row = await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1 AND deleted_at IS NULL", site_id)
    if not row:
        raise ApiError(404, "not_found", "Site not found.")
    return dict(row)


def admin(request: Request) -> None:
    if not security.admin_key_ok(request.headers.get("x-admin-key")):
        raise ApiError(401, "unauthorized", "Admin key not valid.")


async def audit(actor: str, action: str, *, account_id=None, site_id=None, target=None, detail=None) -> None:
    await db.pool().execute(
        f"INSERT INTO {S}.audit_log (account_id, site_id, actor, action, target, detail) VALUES ($1,$2,$3,$4,$5,$6)",
        account_id, site_id, actor, action, target, detail)
