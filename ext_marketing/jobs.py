"""The client job queue. One worker loop inside this service; how many jobs run at once, how often it looks, retries and
the crash timeout are settings. Higher plan priority goes first; Nour never waits here (Nour does not use this queue)."""
from __future__ import annotations
import asyncio, logging, traceback

from . import db, security

S = db.SCHEMA
log = logging.getLogger("ext_marketing.jobs")
HANDLERS: dict = {}                                                     # kind -> async fn(job: dict) -> dict (the result)
_running: set = set()
_stop = asyncio.Event()


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


async def enqueue(account_id: str, site_id: str | None, kind: str, payload: dict) -> str:
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind {kind}")
    prio = await db.pool().fetchval(
        f"SELECT coalesce(p.queue_priority, 0) FROM {S}.accounts a LEFT JOIN {S}.plans p ON p.id = a.plan_id WHERE a.id = $1", account_id)
    jid = security.new_id("j")
    await db.pool().execute(f"INSERT INTO {S}.jobs (id, account_id, site_id, kind, payload, priority) VALUES ($1,$2,$3,$4,$5,$6)",
                            jid, account_id, site_id, kind, payload, int(prio or 0))
    return jid


async def progress(job_id: str, value: float) -> None:
    await db.pool().execute(f"UPDATE {S}.jobs SET progress = $2 WHERE id = $1", job_id, max(0.0, min(1.0, float(value))))


async def _claim() -> dict | None:
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.jobs SET status = 'running', started_at = now(), attempts = attempts + 1, error = NULL
            WHERE id = (SELECT j.id FROM {S}.jobs j JOIN {S}.accounts a ON a.id = j.account_id
                        WHERE j.status = 'queued' AND a.status = 'active'
                        ORDER BY j.priority DESC, j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1)
            RETURNING *""")
    return dict(row) if row else None


async def _run(job: dict) -> None:
    s = await db.settings(["job_max_attempts"])
    try:
        result = await HANDLERS[job["kind"]](job)
        await db.pool().execute(f"UPDATE {S}.jobs SET status='done', progress=1, result=$2, finished_at=now() WHERE id=$1", job["id"], result or {})
    except Exception as e:                                              # noqa: BLE001
        if isinstance(e, Final):                                        # an expected outcome (bad input, nothing to do), not a crash
            log.info("job %s (%s) ended: %s", job["id"], job["kind"], e.message)
        else:
            log.error("job %s (%s) failed: %s", job["id"], job["kind"], traceback.format_exc(limit=3))
        again = job["attempts"] < int(s.get("job_max_attempts") or 1) and not isinstance(e, Final)
        await db.pool().execute(
            f"UPDATE {S}.jobs SET status=$2, error=$3, finished_at=CASE WHEN $2='failed' THEN now() END WHERE id=$1",
            job["id"], "queued" if again else "failed", (getattr(e, "message", None) or f"{type(e).__name__}: {e}")[:500])
    finally:
        _running.discard(job["id"])


class Final(Exception):
    """A failure that retrying cannot fix (bad input, not connected): the job fails at once with this message."""
    def __init__(self, message: str):
        super().__init__(message); self.message = message


async def _recover() -> None:
    s = await db.settings(["job_stale_minutes", "job_max_attempts"])
    await db.pool().execute(
        f"""UPDATE {S}.jobs SET status = CASE WHEN attempts < $2 THEN 'queued' ELSE 'failed' END,
                   error = coalesce(error, 'stopped while running (service restart or crash)'),
                   finished_at = CASE WHEN attempts < $2 THEN NULL ELSE now() END
            WHERE status = 'running' AND started_at < now() - make_interval(mins => $1) AND NOT (id = ANY($3))""",
        int(s.get("job_stale_minutes") or 30), int(s.get("job_max_attempts") or 1), list(_running))


async def loop() -> None:
    log.info("job worker started")
    while not _stop.is_set():
        try:
            s = await db.settings(["queue_max_concurrent", "queue_poll_seconds"])
            await _recover()
            from . import idempotency
            await idempotency.purge()
            while len(_running) < int(s.get("queue_max_concurrent") or 1):
                job = await _claim()
                if not job:
                    break
                _running.add(job["id"])
                asyncio.create_task(_run(job))
            wait = float(s.get("queue_poll_seconds") or 2)
        except Exception:                                               # noqa: BLE001 - the loop never dies
            log.exception("job loop")
            wait = 5.0
        try:
            await asyncio.wait_for(_stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def stop() -> None:
    _stop.set()


def shape(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "status": r["status"], "progress": r["progress"], "result": r["result"],
            "error": r["error"], "createdAt": r["created_at"], "finishedAt": r["finished_at"]}
