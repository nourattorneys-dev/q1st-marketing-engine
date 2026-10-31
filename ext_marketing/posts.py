"""Posts (articles and social posts) of one site: create drafts (one per language, linked as a pair), the drafting job,
topic suggestions, and the exact JSON shape the site's app receives."""
from __future__ import annotations
import logging

from . import db, jobs, llm, locks, media, meter, mirror, profile, quotas, rules as R, security, webhooks, website, writer

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
        t = await db.pool().fetchrow(f"SELECT id, language, status, source_id FROM {S}.posts WHERE pair_id = $1 AND id <> $2", p["pair_id"], p["id"])
        twin = dict(t) if t else None
    acts = actions(p, ed, kinds)
    waits = publish_twin = False
    if twin and await _arabic_mode(site_id) == "translatepress":       # the translation is stored on its source's page
        if p.get("source_id") == twin["id"] and twin["status"] != "published":
            waits = True                                                # the source must go live first
            acts = [a for a in acts if a not in ("publishNow", "retry")]
        publish_twin = twin.get("source_id") == p["id"] and twin["status"] == "approved"
    from . import publishing as PB
    cfg, _ = await PB.cadence(site_id)
    if twin:
        twin.pop("source_id", None)
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
           "sourceId": p.get("source_id"), "lock": locks.shape(p), "version": p["version"], "actions": acts,
           "waitsForSource": waits, "canPublishTwin": publish_twin, "approvedWithCritical": p.get("approved_critical") or 0,
           "reasonRequired": bool(cfg.get("overrideReasonRequired"))}
    if not full:
        out.pop("content", None)
    else:
        out["content"] = p["content"]
    return out


async def create(site: dict, *, topic: str, kind: str, platform: str, language: str, tone: str | None, keywords: list,
                 by: str, suggestion_id: str | None = None) -> tuple[list, list]:
    """One drafting post per language (language 'both' = every site language), linked by pair_id; one job each."""
    langs = list(site["languages"]) if language == "both" else [language]
    await quotas.check(site["account_id"], site["id"], {"articles" if kind == "blog" else "socialPosts": len(langs), "aiTokens": 0})
    pair = security.new_id("pair") if len(langs) > 1 else None
    src = await source_language(site) if pair and kind == "blog" else None
    if src not in langs:
        src = langs[0] if src else None
    order = ([src] + [x for x in langs if x != src]) if src else langs
    ids, jids, source_id = [], [], None
    for lg in order:
        pid = security.new_id("p")
        twin = bool(src) and lg != src                                  # an article's twin is translated from the source when it is drafted
        await db.pool().execute(
            f"""INSERT INTO {S}.posts (id, site_id, kind, platform, language, pair_id, suggestion_id, status, topic, tone, keywords, requested_by, source_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'drafting',$8,$9,$10,$11,$12)""",
            pid, site["id"], kind, platform, lg, pair, suggestion_id, topic[:500], tone, keywords, by, source_id if twin else None)
        if not twin:
            jids.append(await jobs.enqueue(site["account_id"], site["id"], "post.draft", {"postId": pid}))
        if lg == src:
            source_id = pid
        ids.append(pid)
    return ids, jids


async def source_language(site: dict) -> str:
    """The language an article pair is written in first: the website's own main language when its connection reports one
    (a translation plugin translates from it), else the site's default language."""
    row = await website.row(site["id"])
    tp = (((row or {}).get("config") or {}).get("learned") or {}).get("translatepress") or {}
    main = (tp.get("defaultLanguage") or "").split("_")[0].lower()
    return main if main in (site["languages"] or []) else site["default_language"]


async def _arabic_mode(site_id: str) -> str | None:
    row = await website.row(site_id)
    return ((((row or {}).get("config") or {}).get("arabic")) or {}).get("mode")


async def twin_findings(p: dict, findings: list) -> list:
    """A twin's checks: what it inherits from its source is left to the source, and its structure must still match the source
    (and the source must not have changed since it was translated). On a site whose translations are paired by these pieces
    (TranslatePress) a mismatch is critical and cannot be overridden; elsewhere it is a warning."""
    if not p.get("source_id"):
        return findings
    s = await db.pool().fetchrow(f"SELECT language, content FROM {S}.posts WHERE id = $1", p["source_id"])
    if not s:
        return findings
    inherited = set((await db.settings(["twin_inherited_checks"])).get("twin_inherited_checks") or [])
    out = [f for f in findings if f.get("ref") not in inherited]
    hard = await _arabic_mode(p["site_id"]) == "translatepress"
    lvl = "critical" if hard else "warn"
    if mirror.shape(s["content"] or "") != mirror.shape(p.get("content") or ""):
        out.append({"level": lvl, "ref": "structure", "code": "structure_mismatch", "overridable": not hard,
                    "message": f"this version no longer has the same headings, paragraphs and lists as the {s['language']} version, in the same "
                               "order - edit it to match, or ask for changes to translate it again"})
    elif p.get("source_hash") and mirror.fingerprint(mirror.segments(s["content"] or "")) != p["source_hash"]:
        out.append({"level": lvl, "ref": "structure", "code": "source_changed", "overridable": not hard,
                    "message": f"the {s['language']} version changed after this was translated - ask for changes to translate it again"})
    return out


async def refresh_twins(source_id: str) -> None:
    """After the source changed: twins still in review are translated again; approved or published twins get their checks redone."""
    for t in await db.pool().fetch(f"SELECT * FROM {S}.posts WHERE source_id = $1", source_id):
        t = dict(t)
        if t["status"] == "pending":
            await db.pool().execute(f"UPDATE {S}.posts SET status = 'drafting', updated_at = now() WHERE id = $1 AND status = 'pending'", t["id"])
            site = await db.pool().fetchrow(f"SELECT account_id FROM {S}.sites WHERE id = $1", t["site_id"])
            await jobs.enqueue(site["account_id"], t["site_id"], "post.twin", {"postId": t["id"]})
        elif t["status"] in ("approved", "failed", "published") and t["content"]:
            f, seo = await recheck(t)
            await db.pool().execute(f"UPDATE {S}.posts SET findings = $2, seo = $3, updated_at = now() WHERE id = $1", t["id"], f, seo)


async def _auto_photo(site: dict, p: dict, idea: str, alt: str) -> dict | None:
    s = await db.settings(["photo_auto", "photo_candidates", "photo_orientation"])
    if not s.get("photo_auto") or await quotas.blocked(site["account_id"], site["id"], {"photos": 1}):
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


async def settle_suggestion(sid: str | None) -> None:
    """A topic idea leaves 'drafting' once its drafts are done: 'drafted' when one was written, back to 'new' when every one failed."""
    if not sid:
        return
    await db.pool().execute(
        f"""UPDATE {S}.suggestions g SET status = CASE WHEN EXISTS (SELECT 1 FROM {S}.posts WHERE suggestion_id = g.id AND content IS NOT NULL)
                                                  THEN 'drafted' ELSE 'new' END
            WHERE g.id = $1 AND g.status = 'drafting'
              AND NOT EXISTS (SELECT 1 FROM {S}.posts WHERE suggestion_id = g.id AND status = 'drafting' AND source_id IS NULL)""", sid)


async def recheck(p: dict) -> tuple[list, dict]:
    rl, _ = await R.for_site(p["site_id"])
    f = await twin_findings(p, R.check(p, rl))
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
            msg = f"Drafting failed ({type(e).__name__}). Ask for a new draft, or tell support if it repeats."
            if await db.pool().fetchval(f"UPDATE {S}.posts SET status='failed', error=$2, updated_at=now() WHERE id=$1 AND status='drafting' RETURNING id",
                                        job["payload"]["postId"], msg):
                await webhooks.emit(job["site_id"], "post.failed", {"postId": job["payload"]["postId"], "error": msg})
            await db.pool().execute(f"UPDATE {S}.posts SET status='failed', error=$2, updated_at=now() WHERE source_id=$1 AND status='drafting' AND content IS NULL",
                                    job["payload"]["postId"], "The version this is translated from could not be drafted. Ask for changes on it, then here.")
            await settle_suggestion(await db.pool().fetchval(f"SELECT suggestion_id FROM {S}.posts WHERE id = $1", job["payload"]["postId"]))
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
    await webhooks.emit(site["id"], "post.drafted", {"postId": pid, "status": "pending"})
    await settle_suggestion(p["suggestion_id"])
    for t in await db.pool().fetch(f"SELECT id FROM {S}.posts WHERE source_id = $1 AND status = 'drafting'", pid):
        await jobs.enqueue(site["account_id"], site["id"], "post.twin", {"postId": t["id"]})
    await refresh_twins(pid)
    return {"postId": pid, "status": "pending", "words": R.words(R.text_of(out["content"])), "critical": sum(1 for f in findings if f["level"] == "critical"),
            "revisions": revisions, "firstPass": rank(first), "kept": rank([f for f in findings if f["level"] in ("critical", "warn")]),
            "strayWords": [stray_first, stray(merged)],
            "photo": bool(img), "sources": len(out["sources"])}


@jobs.handler("post.twin")
async def twin(job: dict) -> dict:
    """Translates an article's twin from its source; on the LAST attempt any failure leaves the twin 'failed' with a plain message."""
    try:
        return await _twin(job)
    except jobs.Final:
        raise
    except Exception as e:                                              # noqa: BLE001
        if job["attempts"] >= int((await db.settings(["job_max_attempts"])).get("job_max_attempts") or 1):
            msg = f"Translating failed ({type(e).__name__}). Ask for changes to try again, or tell support if it repeats."
            if await db.pool().fetchval(f"UPDATE {S}.posts SET status='failed', error=$2, updated_at=now() WHERE id=$1 AND status='drafting' RETURNING id",
                                        job["payload"]["postId"], msg):
                await webhooks.emit(job["site_id"], "post.failed", {"postId": job["payload"]["postId"], "error": msg})
        raise


async def _twin(job: dict) -> dict:
    pid = job["payload"]["postId"]
    p = await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1", pid)
    if not p or p["status"] != "drafting" or not p["source_id"]:
        raise jobs.Final("The post is no longer waiting to be translated.")
    p = dict(p)
    src = await db.pool().fetchrow(f"SELECT * FROM {S}.posts WHERE id = $1", p["source_id"])
    if not src or not src["content"] or src["status"] not in ("pending", "approved", "publishing", "published", "failed"):
        raise jobs.Final("The version this is translated from is not written yet.")
    src = dict(src)
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", p["site_id"]))
    out = await writer.translate(site, src, p, note=p["review_note"])
    await jobs.progress(job["id"], 0.8)
    img = p["image"]
    if not img and src["image"]:                                        # the same photo, its own copy (removing one never removes the other)
        si = src["image"]
        key = await media.store(site["id"], pid, await media.load(si["key"]), si["ctype"])
        img = {**si, "key": key, "alt": si.get("alt")}
    F = ("title", "subtitle", "content", "slug", "meta_title", "meta_description", "keywords", "hashtags")
    merged = {**p, **{k: out[k] for k in F}, "image": img, "source_hash": out["source_hash"]}
    findings, seo = await recheck(merged)
    await db.pool().execute(
        f"""UPDATE {S}.posts SET title=$2, subtitle=$3, content=$4, slug=$5, meta_title=$6, meta_description=$7, keywords=$8, hashtags=$9,
               image=$10, sources=$11, findings=$12, seo=$13, source_hash=$14, photo_idea=$15, status='pending', pending_since=now(), error=NULL,
               review_note=NULL, version=version+1, updated_at=now() WHERE id=$1""",
        pid, out["title"], out["subtitle"], out["content"], out["slug"], out["meta_title"], out["meta_description"], out["keywords"],
        out["hashtags"], img, src["sources"] or [], findings, seo, out["source_hash"], src["photo_idea"])
    await meter.record(site["account_id"], site["id"], "articles", 0 if (p["review_note"] or p["content"]) else 1, tokens_in=out["tokens"][0],
                       tokens_out=out["tokens"][1], job_id=job["id"])
    await webhooks.emit(site["id"], "post.drafted", {"postId": pid, "status": "pending"})
    return {"postId": pid, "status": "pending", "pieces": len(mirror.segments(out["content"])),
            "sourcePieces": len(mirror.segments(src["content"])), "critical": sum(1 for f in findings if f["level"] == "critical")}


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
    added, new_ids = 0, []
    seen = {t.strip().lower() for t in done if t}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("title"), str) or it["title"].strip().lower() in seen:
            continue
        lg = it.get("language") if it.get("language") in site["languages"] else site["default_language"]
        kd = it.get("kind") if it.get("kind") in ("blog", "social") else "blog"
        sid = security.new_id("sg"); new_ids.append(sid)
        await db.pool().execute(f"INSERT INTO {S}.suggestions (id, site_id, title, kind, language, keywords, why) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                                sid, site["id"], it["title"].strip()[:300], kd, lg,
                                [k for k in (it.get("keywords") or []) if isinstance(k, str)][:8], (it.get("why") or "")[:300])
        seen.add(it["title"].strip().lower()); added += 1
    await meter.record(site["account_id"], site["id"], "topicRuns", 1, tokens_in=a, tokens_out=b, job_id=job["id"])
    if new_ids:
        await webhooks.emit(site["id"], "suggestions.created", {"ids": new_ids})
    return {"added": added}
