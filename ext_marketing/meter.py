"""The usage meter: every billable action is one row (quotas and the Usage & revenue page read these rows)."""
from __future__ import annotations
from . import db

S = db.SCHEMA


async def record(account_id: str, site_id: str | None, metric: str, qty: int = 1, *, tokens_in: int = 0, tokens_out: int = 0,
                 job_id: str | None = None) -> None:
    s = await db.settings(["cost_per_1k_tokens"])
    rate = s.get("cost_per_1k_tokens")
    cost = (float(rate) * (tokens_in + tokens_out) / 1000.0) if rate is not None else None
    await db.pool().execute(
        f"""INSERT INTO {S}.usage_events (account_id, site_id, metric, qty, tokens_in, tokens_out, cost_estimate, job_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""", account_id, site_id, metric, qty, tokens_in, tokens_out, cost, job_id)
    if tokens_in or tokens_out:
        await db.pool().execute(
            f"""INSERT INTO {S}.usage_events (account_id, site_id, metric, qty, tokens_in, tokens_out, job_id)
                VALUES ($1,$2,'aiTokens',0,$3,$4,$5)""", account_id, site_id, tokens_in, tokens_out, job_id)
