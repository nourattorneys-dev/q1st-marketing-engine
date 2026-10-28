"""Posts (articles and social posts) of one site: create drafts (one per language, linked as a pair), the drafting job,
topic suggestions, and the exact JSON shape the site's app receives."""
from __future__ import annotations
import logging

from . import db, jobs, llm, locks, media, meter, profile, rules as R, security, writer

S = db.SCHEMA
log = logging.getLogger("ext_marketing.posts")


def actions(p: dict, editable: list, kinds: list | None = None) -> list:
    """The buttons a site's app shows for this post (the app never works the rules out itself)."""
    st, a = p["status"], []
    pub_fail = st == "failed" and p.get("approved_at") is not None    # failed while publishing (a failed draft has no approval)
    if st in editable:
        a += ["edit", "photo"]
    if st == "pending":
        a += ["approve", "changes", "reject"]
    if st == "approved":
        a += (["publishNow"] if p["kind"] in (kinds or []) else []) + ["resume" if p.get("hold") else "hold", "changes", "reject"]
    if pub_fail:
        a += (["retry"] if p["kind"] in (kinds or []) else []) + ["changes", "reject"]
    elif st == "failed":
        a += ["changes"]
    if st in ("pending", "rejected", "failed") or st in editable:
        a += ["delete"]
    return list(dict.fromkeys(a))


async def shape(p: dict, site_id: str, *, full: bool = True) -> dict:
    s = await db.settings(["editable_statuses", "publish_kinds"])
    ed, kinds = s.get("editable_statuses") or ["pending"], s.get("publish_kinds") or []
    img = p.get("image") or None
    twin = None
    if p.get("pair_id"):
        t = await db.pool().fetchrow(f"SELECT id, language, status FROM {S}.posts WHERE pair_id = $1 AND id <> $2", p["pair_id"], p["id"])
        twin = dict(t) if t else None
    out = {"id": p["id"], "kind": p["kind"], "platform": p["platform"], "language": p["language"], "pairId": p["pair_id"], "twin": twin,
           "status": p["status"], "topic": p["topic"], "tone": p["tone"], "title": p["title"], "subtitle": p["subtitle"], "slug": p["slug"],
           "metaTitle": p["meta_title"], "metaDescription": p["meta_description"], "keywords": p["keywords"], "hashtags": p["hashtags"],
           "section": p["section"], "photoIdea": p["photo_idea"],
           "image": ({"url": f"/api/v1/sites/{site_id}/media/{p['id']}?v={p['version']}", "alt": img.get("alt"), "credit": img.get("credit"),
                      "source": img.get("source"), "pexelsId": img.get("pexelsId")} if img else None),
           "sources": p["sources"], "findings": p["findings"], "seo": p["seo"], "reviewNote": p["review_note"], "error": p["error"],
           "schedule": {"hold": p["hold"], "approvedAt": p.get("approved_at")} if p["status"] in ("approved", "publishing") else None,
           "published": p["published"], "publishedAt": p.get("published_at"), "overrideReason": p.get("override_reason"),
           "requestedBy": p["requested_by"], "reviewedBy": p["reviewed_by"], "createdAt": p["created_at"], "updatedAt": p["updated_at"],
           "lock": locks.shape(p), "version": p["version"], "actions": actions(p, ed, kinds)}
    if not full:
        out.pop("content", None)
    else:
        out["content"] = p["content"]
    return out


async def create(site: dict, *, topic: str, kind: str, platform: str, language: str, tone: str | None, keywords: list,
                 by: str, suggestion_id: str | None = None) -> tuple[list, list]:
    """One drafting post per language (language 'both' = every site language), linked by pair_id; one job each."""
    langs = list(site["languages"]) if language == "both" else [language]
    pair = security.new_id("pair") if len(langs) > 1 else None
    ids, jids = [], []
    for lg in langs:
        pid = security.new_id("p")
        await db.pool().execute(
            f"""INSERT INTO {S}.posts (id, site_id, kind, platform, language, pair_id, suggestion_id, status, topic, tone, keywords, requested_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'drafting',$8,$9,$10,$11)""",
            pid, site["id"], kind, platform, lg, pair, suggestion_id, topic[:500], tone, keywords, by)
        jids.append(await jobs.enqueue(site["account_id"], site["id"], "post.draft", {"postId": pid}))
        ids.append(pid)
    return ids, jids


async def _auto_photo(site: dict, p: dict, idea: str, alt: str) -> dict | None:
    s = await db.settings(["photo_auto", "photo_candidates", "photo_orientation"])
    if not s.get("photo_auto"):
        return None
    orient = (s.get("photo_orientation") or {}).get(p["platform"], "landscape")
    for ph in await media.search(idea, orient, int(s.get("photo_candidates") or 3)):
        try:
            data, ct = await media.download(ph["full"])
        except ValueError:
            continue
        key = await media.store(site["id"], p["id"], data, ct)
        return {"key": key, "ctype": ct, "bytes": len(data), "alt": alt or ph.get("alt"), "credit": f"Photo by {ph.get('photographer')} on Pexels",
                "source": "pexels", "pexelsId": ph["pexelsId"]}
    return None


async def recheck(p: dict) -> tuple[list, dict]:
    rl, _ = await R.for_site(p["site_id"])
    f = R.check(p, rl)
    return f, {"score": await R.score(f), "checks": [x for x in f if x["level"] != "human"]}


@jobs.handler("post.draft")
async def draft(job: dict) -> dict:
    """Any failure on the LAST attempt leaves the post 'failed' with a plain message - never stuck in 'drafting'."""
    try:
        return await _draft(job)
    except jobs.Final:
        raise
    except Exception as e:                                              # noqa: BLE001
        if job["attempts"] >= int((await db.settings(["job_max_attempts"])).get("job_max_attempts") or 1):
            await db.pool().execute(f"UPDATE {S}.posts SET status='failed', error=$2, updated_at=now() WHERE id=$1 AND status='drafting'",
                                    job["payload"]["postId"], f"Drafting failed ({type(e).__name__}). Ask for a new draft, or tell support if it repeats.")
        raise


async def _draft(job: dict) -> dict:
    pid = job["payload"]["postId"]
    p = await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1", pid)
    if not p or p["status"] not in ("drafting",):
        raise jobs.Final("The post is no longer waiting to be drafted.")
    p = dict(p)
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", p["site_id"]))
    previous = {"content": p["content"], "findings": p["findings"]} if p["review_note"] else None
    out = await writer.write(site, p, note=p["review_note"], previous=previous)
    await jobs.progress(job["id"], 0.6)
    img = p["image"]
    if not img:
        img = await _auto_photo(site, p, out["photo_idea"], out["image_alt"])
    F = ("title", "subtitle", "content", "slug", "meta_title", "meta_description", "keywords", "hashtags")
    merged = {**p, **{k: out[k] for k in F}, "image": img}
    findings, seo = await recheck(merged)
    first = [f for f in findings if f["level"] in ("critical", "warn")]
    revisions, tin, tout = 0, *out["tokens"]
    rules_l, _ = await R.for_site(site["id"])
    stray = lambda m: len(R.foreign_words(R.text_of(m["content"]) if m["kind"] == "blog" else m["content"], m["language"], rules_l, None))   # noqa: E731
    rank = lambda fs: (sum(f["level"] == "critical" for f in fs), sum(f["level"] == "warn" for f in fs))   # noqa: E731
    better = lambda f2, m2: (*rank(f2), stray(m2)) < (*rank(findings), stray(merged))   # noqa: E731 - same count of findings: fewer stray words still wins
    stray_first = stray(merged)
    for _ in range(int((await db.settings(["writer_fix_passes"])).get("writer_fix_passes") or 0)):
        todo = [f["message"] for f in findings if f["level"] in ("critical", "warn") and f["ref"] != "photo"]
        if not todo:
            break
        try:
            alt = (await writer.repair(p, out, findings) if p["kind"] == "blog" else None) \
                or await writer.write(site, p, note=p["review_note"], previous={"content": out["content"]}, fix=todo, ctx=out["ctx"])
        except Exception:                                               # noqa: BLE001 - a failed revision keeps the first version
            log.exception("revision failed for %s", pid)
            break
        revisions += 1; tin += alt["tokens"][0]; tout += alt["tokens"][1]
        m2 = {**p, **{k: alt[k] for k in F}, "image": img}
        f2, s2 = await recheck(m2)
        if better(f2, m2):                                              # keep whichever version measures better
            out, merged, findings, seo = {**alt, "sources": out["sources"]}, m2, f2, s2
    await jobs.progress(job["id"], 0.85)
    await db.pool().execute(
        f"""UPDATE {S}.posts SET title=$2, subtitle=$3, content=$4, slug=$5, meta_title=$6, meta_description=$7, keywords=$8, hashtags=$9,
               photo_idea=$10, image=$11, sources=$12, findings=$13, seo=$14, status='pending', pending_since=now(), error=NULL,
               review_note=NULL, version=version+1, updated_at=now() WHERE id=$1""",
        pid, out["title"], out["subtitle"], out["content"], out["slug"], out["meta_title"], out["meta_description"], out["keywords"],
        out["hashtags"], out["photo_idea"], img, out["sources"], findings, seo)
    await meter.record(site["account_id"], site["id"], "articles" if p["kind"] == "blog" else "socialPosts", 0 if p["review_note"] else 1,
                       tokens_in=tin, tokens_out=tout, job_id=job["id"])
    if img and not p["image"]:
        await meter.record(site["account_id"], site["id"], "photos", 1, job_id=job["id"])
    return {"postId": pid, "status": "pending", "words": R.words(R.text_of(out["content"])), "critical": sum(1 for f in findings if f["level"] == "critical"),
            "revisions": revisions, "firstPass": rank(first), "kept": rank([f for f in findings if f["level"] in ("critical", "warn")]),
            "strayWords": [stray_first, stray(merged)],
            "photo": bool(img), "sources": len(out["sources"])}


@jobs.handler("topics.suggest")
async def suggest(job: dict) -> dict:
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", job["site_id"]))
    s = await db.settings(["topics_count", "topics_max_tokens"])
    ids = job["payload"].get("keywordIds") or []
    q = f"SELECT phrase, language FROM {S}.keywords WHERE site_id = $1 AND " + ("id = ANY($2)" if ids else "status = 'approved'")
    kws = [dict(x) for x in await db.pool().fetch(q, site["id"], *([ids] if ids else []))]
    if not kws:
        raise jobs.Final("No approved keywords to suggest topics for. Approve some keywords first.")
    done = [r["t"] for r in await db.pool().fetch(
        f"SELECT title AS t FROM {S}.suggestions WHERE site_id=$1 UNION SELECT coalesce(title, topic) FROM {S}.posts WHERE site_id=$1", site["id"])]
    raw, a, b = await llm.chat([
        {"role": "system", "content": "You plan content for the website below. Propose article and social post topics a real customer of this site would want, "
                                      "each built around one or more of the given keywords, in that keyword's language. Never repeat or rephrase a topic in DONE. "
                                      "A topic is a subject, not a claim: no figures, percentages, numbers of companies, events, studies, rankings or dates that you "
                                      "cannot point to in the site's facts - the writer must be able to support every word of the title. "
                                      "Answer ONLY JSON: {\"topics\": [{\"title\": \"...\", \"kind\": \"blog|social\", \"language\": \"" + "|".join(site["languages"]) +
                                      "\", \"keywords\": [\"...\"], \"why\": \"one short line\"}]}."},
        {"role": "user", "content": f"{await profile.brief(site)}\nHow many: {int(s['topics_count'])}\nKEYWORDS:\n" +
                                    "\n".join(f"- {k['phrase']} [{k['language']}]" for k in kws[:200]) + f"\n\nDONE:\n{chr(10).join(done[:300]) or '(none)'}"}],
        max_tokens=int(s["topics_max_tokens"]))
    items = llm.json_from(raw).get("topics") or []
    added = 0
    seen = {t.strip().lower() for t in done if t}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("title"), str) or it["title"].strip().lower() in seen:
            continue
        lg = it.get("language") if it.get("language") in site["languages"] else site["default_language"]
        kd = it.get("kind") if it.get("kind") in ("blog", "social") else "blog"
        await db.pool().execute(f"INSERT INTO {S}.suggestions (id, site_id, title, kind, language, keywords, why) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                                security.new_id("sg"), site["id"], it["title"].strip()[:300], kd, lg,
                                [k for k in (it.get("keywords") or []) if isinstance(k, str)][:8], (it.get("why") or "")[:300])
        seen.add(it["title"].strip().lower()); added += 1
    await meter.record(site["account_id"], site["id"], "topicRuns", 1, tokens_in=a, tokens_out=b, job_id=job["id"])
    return {"added": added}
