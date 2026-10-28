"""ext_marketing DB access. Own schema only -- never reads Nour's public.* marketing tables."""
from __future__ import annotations
import json, os
import asyncpg

SCHEMA = "ext_marketing"
_pool: asyncpg.Pool | None = None


async def _init(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    # no SET search_path: pgbouncer (transaction pooling) drops session state -> every query names the schema


async def open_pool() -> asyncpg.Pool:
    global _pool
    dsn = os.environ.get("EXT_MKT_DSN") or os.environ["POSTGRES_DSN"]
    # pool sizes come from env so ops can tune without code; asyncpg defaults otherwise
    kw = {k: int(os.environ[e]) for k, e in (("min_size", "EXT_MKT_POOL_MIN"), ("max_size", "EXT_MKT_POOL_MAX")) if os.environ.get(e)}
    # statement_cache_size=0: DSN goes through pgbouncer (:6432), transaction pooling breaks prepared statements
    _pool = await asyncpg.create_pool(dsn, init=_init, statement_cache_size=0, **kw)
    return _pool


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not open")
    return _pool


async def settings(keys: list[str] | None = None) -> dict:
    """System knobs from ext_marketing.settings -- the only source of limits/numbers."""
    q = f"SELECT key, value FROM {SCHEMA}.settings" + (" WHERE key = ANY($1)" if keys else "")
    rows = await pool().fetch(q, *( [keys] if keys else [] ))
    return {r["key"]: r["value"] for r in rows}
