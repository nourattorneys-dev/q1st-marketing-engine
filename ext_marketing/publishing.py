"""Publishing: each site's cadence (settings), the queue of approved posts, the slot maths, and publishing one post through
the site's website connection. The slot maths is pure (no database) so it is tested offline; the scheduler only reads,
decides and enqueues."""
from __future__ import annotations
import asyncio, logging, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, jobs, media, meter, mirror, quotas, security, website

S = db.SCHEMA
log = logging.getLogger("ext_marketing.publishing")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_stop = asyncio.Event()


class Failed(Exception):
    def __init__(self, message: str):
        super().__init__(message); self.message = message


# ---------------------------------------------------------------- the site's cadence
async def cadence(site_id: str) -> tuple[dict, int]:
    base = (await db.settings(["publishing_defaults"])).get("publishing_defaults") or {}
    r = await db.pool().fetchrow(f"SELECT value, version FROM {S}.site_settings WHERE site_id = $1 AND grp = 'publishing'", site_id)
    return ({**base, **(r["value"] if r else {})}, r["version"] if r else 0)


def check_cadence(d: dict) -> dict:
    """Field errors of a PublishingSettings body (empty = valid)."""
    f = {}
    hm = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    isint = lambda v, lo: isinstance(v, int) and not isinstance(v, bool) and v >= lo   # noqa: E731
    if "autoPublish" in d and not isinstance(d["autoPublish"], bool):
        f["autoPublish"] = "true or false"
    if "perDay" in d and not isint(d["perDay"], 1):
        f["perDay"] = "A whole number, 1 or more"
    if "gapMinutes" in d and not isint(d["gapMinutes"], 0):
        f["gapMinutes"] = "A whole number of minutes, 0 or more"
    if "days" in d and not (isinstance(d["days"], list) and d["days"] and all(x in DAYS for x in d["days"])):
        f["days"] = "One or more of " + ", ".join(DAYS)
    for k in ("from", "to"):
        if k in d and not (isinstance(d[k], str) and hm.match(d[k])):
            f[k] = "A time like 09:30 (24-hour, the site's timezone)"
    if not f and "from" in d and "to" in d and d["from"] >= d["to"]:
        f["to"] = "Must be later than 'from'"
    return f


def _at(day, hhmm: str, tz) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def slots(cfg: dict, tz_name: str, now: datetime, today_auto: int, last_auto: datetime | None, n: int) -> list[datetime]:
    """The next n publish times: only on the site's days, inside its hours (its own timezone), at most perDay automatic
    publishes per local day (publishing now does not count), and at least gapMinutes after the previous automatic one."""
    tz = ZoneInfo(tz_name or "UTC")
    days, per, gap = set(cfg.get("days") or []), int(cfg.get("perDay") or 0), timedelta(minutes=int(cfg.get("gapMinutes") or 0))
    if not days or per < 1 or n < 1:
        return []
    out: list[datetime] = []
    t = now.astimezone(tz)
    if last_auto:
        t = max(t, (last_auto + gap).astimezone(tz))
    today = now.astimezone(tz).date()
    d = today
    for _ in range(7 * (n + 1)):                                       # every weekday is seen at least once per 7 days
        if DAYS[d.weekday()] in days:
            start, end = _at(d, cfg["from"], tz), _at(d, cfg["to"], tz)
            used = today_auto if d == today else 0
            while used < per and len(out) < n:
                c = max(t, start)
                if c > end:
                    break
                out.append(c); used += 1; t = c + gap
        if len(out) >= n:
            break
        d += timedelta(days=1)
    return out


def reason(cfg: dict, tz_name: str, now: datetime, today_auto: int, last_auto: datetime | None, queued: int, connected: bool) -> str | None:
    """Why nothing is going out right now (None = a post is due)."""
    if not cfg.get("autoPublish"):
        return "auto_publish_off"
    if not connected:
        return "not_connected"
    if queued < 1:
        return "queue_empty"
    nxt = slots(cfg, tz_name, now, today_auto, last_auto, 1)
    if nxt and nxt[0] <= now:
        return None
    tz = ZoneInfo(tz_name or "UTC")
    lt = now.astimezone(tz)
    if DAYS[lt.weekday()] not in set(cfg.get("days") or []) or not (_at(lt.date(), cfg["from"], tz) <= lt <= _at(lt.date(), cfg["to"], tz)):
        return "outside_hours"
    if today_auto >= int(cfg.get("perDay") or 0):
        return "daily_limit"
    return "waiting_gap"


async def state(site: dict) -> dict:
    """Everything /scheduled and the scheduler need for one site."""
    cfg, _ = await cadence(site["id"])
    tz = ZoneInfo(site["timezone"] or "UTC")
    now = datetime.now(timezone.utc)
    day0 = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tz)
    kinds = (await db.settings(["publish_kinds"])).get("publish_kinds") or []
    row = await db.pool().fetchrow(
        f"""SELECT count(*) FILTER (WHERE published_at >= $2) AS today, max(published_at) AS last
            FROM {S}.posts WHERE site_id = $1 AND auto_published""", site["id"], day0)
    conn = await website.row(site["id"])
    queue = [dict(x) for x in await db.pool().fetch(
        f"SELECT * FROM {S}.posts WHERE site_id = $1 AND status IN ('approved','publishing') ORDER BY approved_at, id", site["id"])]
    srcs = [p["source_id"] for p in queue if p.get("source_id")]
    src_status = {r["id"]: r["status"] for r in await db.pool().fetch(f"SELECT id, status FROM {S}.posts WHERE id = ANY($1)", srcs)} if srcs else {}
    paired = (((conn or {}).get("config") or {}).get("arabic") or {}).get("mode") == "translatepress"
    return {"cfg": cfg, "now": now, "today": int(row["today"] or 0), "last": row["last"], "queue": queue, "kinds": kinds,
            "connected": bool(conn and conn["secret_enc"] and conn["status"] == "ok"),
            "waitsForSource": {p["id"] for p in queue if paired and p.get("source_id") and src_status.get(p["source_id"]) != "published"}}


def plan(site: dict, st: dict) -> tuple[list[dict], str | None, datetime | None]:
    """Queue positions and expected times. Only posts of a publishable kind and not on hold take a slot."""
    takes = [p for p in st["queue"] if p["status"] == "approved" and not p["hold"] and p["kind"] in st["kinds"]
             and p["id"] not in st.get("waitsForSource", set())]
    live = st["cfg"].get("autoPublish") and st["connected"]
    times = slots(st["cfg"], site["timezone"], st["now"], st["today"], st["last"], len(takes)) if live else []
    when = {p["id"]: (times[i] if i < len(times) else None) for i, p in enumerate(takes)}
    rows = []
    for i, p in enumerate(st["queue"]):
        why = ("publishing" if p["status"] == "publishing" else "on_hold" if p["hold"] else
               "no_channel" if p["kind"] not in st["kinds"] else "source_not_published" if p["id"] in st.get("waitsForSource", set()) else None)
        rows.append({"post": p, "position": i + 1, "expectedAt": when.get(p["id"]), "waiting": why})
    r = reason(st["cfg"], site["timezone"], st["now"], st["today"], st["last"], len(takes), st["connected"])
    nxt = slots(st["cfg"], site["timezone"], st["now"], st["today"], st["last"], 1) if live else []
    return rows, r, (nxt[0] if nxt else None)


# ---------------------------------------------------------------- publishing one post
async def claim(site_id: str, pid: str) -> dict | None:
    """approved, or failed while publishing -> publishing, atomically: a post is never sent twice at the same time."""
    kinds = (await db.settings(["publish_kinds"])).get("publish_kinds") or []
    r = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'publishing', updated_at = now()
            WHERE id = $1 AND site_id = $2 AND status IN ('approved','failed') AND approved_at IS NOT NULL AND kind = ANY($3)
            RETURNING *""", pid, site_id, kinds)
    return dict(r) if r else None


async def run(p: dict, *, auto: bool, job_id: str | None = None) -> dict:
    """Publish a post that is 'publishing'. Success -> 'published' + live URL, keywords covered, metered.
    Any failure -> 'failed' with a plain message (History offers Retry); raises Failed."""
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", p["site_id"]))
    conn = await website.row(site["id"])
    t = (conn or {}).get("config") or {}
    try:
        if not conn or not conn["secret_enc"] or not t.get("type"):
            raise Failed("The website is not connected. Open Settings → Website connection and save a working connection.")
        if p.get("source_id") and (t.get("arabic") or {}).get("mode") == "translatepress":
            return await _as_translation(p, site, t, security.decrypt(conn["secret_enc"]), auto, job_id)
        img = p.get("image") or None
        image = None
        if img and img.get("key"):
            ext = (img.get("ctype") or "image/jpeg").split("/")[-1]
            image = (await media.load(img["key"]), img.get("ctype") or "image/jpeg", f"{(p.get('slug') or p['id'])[:60]}.{ext}")
        res = await website.publish(t, security.decrypt(conn["secret_enc"]), {
            "title": p["title"], "slug": p["slug"], "subtitle": p["subtitle"], "body_html": p["content"], "meta_title": p["meta_title"],
            "meta_description": p["meta_description"], "keywords": p["keywords"], "category": p.get("section"), "language": p["language"],
            "image_alt": (img or {}).get("alt"), "image_credit": (img or {}).get("credit"),
            "external_id": (p.get("published") or {}).get("externalId") if (p.get("published") or {}).get("target") == t["type"] else None},
            image=image, status=(t.get("settings") or {}).get("publishAs") or "publish")
    except Exception as e:                                              # noqa: BLE001 - every failure becomes a plain message on the post
        known = isinstance(e, (Failed, website.Refused))
        msg = e.message if known else "The website could not be reached. Try again, or check Settings → Website connection."
        if not known:
            log.exception("publish %s", p["id"])
        await db.pool().execute(f"UPDATE {S}.posts SET status = 'failed', error = $2, updated_at = now() WHERE id = $1", p["id"], msg)
        if conn and isinstance(e, website.Refused):
            await db.pool().execute(f"UPDATE {S}.site_connections SET status = 'error', last_error = $2, last_checked_at = now() "
                                    f"WHERE site_id = $1 AND type = 'website'", site["id"], msg)
        raise Failed(msg) from e
    now = datetime.now(timezone.utc)
    pub = {"target": res["target"], "url": res.get("url"), "externalId": res.get("externalId"), "at": now.isoformat(),
           "status": res.get("status"), "auto": auto}
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'published', published = $2, published_at = $3, auto_published = $4, error = NULL,
               version = version + 1, updated_at = now() WHERE id = $1 RETURNING *""", p["id"], pub, now, auto)
    if p["keywords"]:
        await db.pool().execute(
            f"""UPDATE {S}.keywords SET status = 'covered', uses = uses + 1, updated_at = now()
                WHERE site_id = $1 AND lower(phrase) = ANY($2) AND status IN ('new','approved')""",
            site["id"], [k.lower() for k in p["keywords"]])
    await db.pool().execute(f"UPDATE {S}.site_connections SET status = 'ok', last_error = NULL, last_checked_at = now() "
                            f"WHERE site_id = $1 AND type = 'website'", site["id"])
    await meter.record(site["account_id"], site["id"], "publishes", 1, job_id=job_id)
    return dict(row)


async def _as_translation(p: dict, site: dict, t: dict, secret: str, auto: bool, job_id: str | None) -> dict:
    """A twin on a site whose translations are stored by a translation plugin: its pieces are stored as the translation of the
    source's published page (same address under the language's path), not as a second post."""
    src = await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1", p["source_id"])
    sp = (src["published"] or {}) if src else {}
    if not src or src["status"] != "published" or sp.get("target") != t["type"] or not sp.get("externalId"):
        raise Failed("Publish the version this is translated from first; this one is stored as its translation.")
    a, b = mirror.segments(src["content"] or ""), mirror.segments(p["content"] or "")
    if mirror.shape(src["content"] or "") != mirror.shape(p["content"] or "") or len(a) != len(b):
        raise Failed("This version no longer matches the structure of the one it translates. Ask for changes to translate it again.")
    pieces = [{"source": x, "translated": y} for x, y in zip(a, b) if mirror.has_words(x)]   # pieces without letters are not stored
    extra = [{"source": s, "translated": d} for s, d in ((src["title"], p["title"]), (src["meta_title"], p["meta_title"]),
                                                          (src["meta_description"], p["meta_description"])) if s and d]
    try:
        res = await website.ADAPTERS[t["type"]]["translate"](t, secret, sp["externalId"], p["language"], pieces, extra)
    except website.Refused as e:
        raise Failed(e.message)
    if res.get("missed"):
        raise Failed(f"{len(res['missed'])} of {res.get('sent')} pieces were not found on the published page (it may have been edited "
                     "on the website). Publish the source again, then retry this.")
    if int(res.get("stored") or 0) < int(res.get("matched") or 0):
        import json as _json
        log.warning("translation not stored for %s: %s", p["id"], _json.dumps(res.get("debug"), ensure_ascii=False)[:800])
        raise Failed(f"The website matched {res.get('matched')} pieces but stored {res.get('stored')}. Nothing is marked published. "
                     f"Details: {_json.dumps(res.get('debug'), ensure_ascii=False)[:600]}")
    now = datetime.now(timezone.utc)
    pub = {"target": "translatepress", "url": res.get("url"), "externalId": sp["externalId"], "at": now.isoformat(),
           "stored": res.get("stored"), "auto": auto}
    row = await db.pool().fetchrow(
        f"""UPDATE {S}.posts SET status = 'published', published = $2, published_at = $3, auto_published = $4, error = NULL,
               version = version + 1, updated_at = now() WHERE id = $1 RETURNING *""", p["id"], pub, now, auto)
    await meter.record(site["account_id"], site["id"], "publishes", 1, job_id=job_id)
    return dict(row)


@jobs.handler("post.publish")
async def publish_job(job: dict) -> dict:
    p = await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1", job["payload"]["postId"])
    if not p or p["status"] != "publishing":
        raise jobs.Final("The post is no longer waiting to be published.")
    try:
        row = await run(dict(p), auto=bool(job["payload"].get("auto")), job_id=job["id"])
    except Failed as e:
        raise jobs.Final(e.message)                                     # the post shows the error and offers Retry; the job does not repeat it
    return {"postId": row["id"], "status": "published", "url": (row["published"] or {}).get("url")}


# ---------------------------------------------------------------- the scheduler
async def _recover() -> None:
    """A post left 'publishing' by a restart with no job to finish it becomes 'failed' (Retry in History)."""
    m = int((await db.settings(["job_stale_minutes"])).get("job_stale_minutes") or 30)
    await db.pool().execute(
        f"""UPDATE {S}.posts p SET status = 'failed', error = 'Publishing stopped (service restart). Use Retry.', updated_at = now()
            WHERE p.status = 'publishing' AND p.updated_at < now() - make_interval(mins => $1)
              AND NOT EXISTS (SELECT 1 FROM {S}.jobs j WHERE j.kind = 'post.publish' AND j.payload->>'postId' = p.id AND j.status IN ('queued','running'))""", m)


async def tick() -> int:
    """One pass over the sites with something approved: enqueue the first post of each site whose slot is due."""
    await _recover()
    sent = 0
    for sid in [x["site_id"] for x in await db.pool().fetch(
            f"SELECT DISTINCT p.site_id FROM {S}.posts p JOIN {S}.sites s ON s.id = p.site_id JOIN {S}.accounts a ON a.id = s.account_id "
            f"WHERE p.status = 'approved' AND NOT p.hold AND s.deleted_at IS NULL AND a.status = 'active'")]:
        site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", sid))
        st = await state(site)
        if any(p["status"] == "publishing" for p in st["queue"]):
            continue                                                    # one at a time per site
        rows, why, _ = plan(site, st)
        if why is not None or await quotas.blocked(site["account_id"], sid, {"publishes": 1}):
            continue
        first = next((r["post"] for r in rows if r["waiting"] is None and r["post"]["status"] == "approved"), None)
        if first and await claim(sid, first["id"]):
            await jobs.enqueue(site["account_id"], sid, "post.publish", {"postId": first["id"], "auto": True})
            sent += 1
    return sent


async def loop() -> None:
    log.info("publishing scheduler started")
    while not _stop.is_set():
        try:
            await tick()
            wait = float((await db.settings(["scheduler_poll_seconds"])).get("scheduler_poll_seconds") or 60)
        except Exception:                                               # noqa: BLE001 - the loop never dies
            log.exception("scheduler")
            wait = 60.0
        try:
            await asyncio.wait_for(_stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def stop() -> None:
    _stop.set()
