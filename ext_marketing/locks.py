"""Editing locks (spec v1.1 §3): opening a post in the editor takes the lock, renewing it while editing; while another person
holds it, changes to the post get 409 locked. An expired lock counts as free. The length is the setting lock_minutes."""
from __future__ import annotations
from datetime import datetime, timezone

from . import db
from .auth import ApiError, Caller

S = db.SCHEMA


def holder(p: dict) -> str | None:
    """Who holds a live lock on the post, or None."""
    exp = p.get("lock_expires_at")
    return p.get("locked_by") if p.get("locked_by") and exp and exp > datetime.now(timezone.utc) else None


def shape(p: dict) -> dict | None:
    h = holder(p)
    return {"by": h, "at": p["locked_at"], "expiresAt": p["lock_expires_at"]} if h else None


def guard(c: Caller, p: dict) -> None:
    """Refuse a change while someone else edits the post."""
    h = holder(p)
    if h and h != c.user:
        raise ApiError(409, "locked", f"Being edited by {h}. Take over to edit it.", details={"lockedBy": h, "lockedAt": p["locked_at"]})


async def take(c: Caller, site_id: str, pid: str, *, force: bool) -> dict | None:
    """Take or renew the lock (force = take over from someone else). None when someone else holds it and force is off."""
    m = int((await db.settings(["lock_minutes"])).get("lock_minutes") or 15)
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET locked_by = $3,
               locked_at = CASE WHEN locked_by = $3 AND lock_expires_at > now() THEN locked_at ELSE now() END,
               lock_expires_at = now() + make_interval(mins => $4)
            WHERE id = $1 AND site_id = $2 AND ($5 OR locked_by IS NULL OR locked_by = $3 OR lock_expires_at IS NULL OR lock_expires_at <= now())
            RETURNING *""", pid, site_id, c.user, m, force)
    return dict(row) if row else None


async def release(c: Caller, site_id: str, pid: str) -> None:
    await db.pool().execute(f"UPDATE {S}.posts SET locked_by = NULL, locked_at = NULL, lock_expires_at = NULL "
                            f"WHERE id = $1 AND site_id = $2 AND locked_by = $3", pid, site_id, c.user)
