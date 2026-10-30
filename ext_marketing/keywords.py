"""Keywords of one site: add, review, and AI proposals. Tools bring facts (the site's own pages, SEMrush volume and
difficulty for the site's country); the model proposes; a person approves. Every proposal arrives as 'new'."""
from __future__ import annotations
import asyncio, logging

from . import db, jobs, llm, meter, profile, security, sitepages, webhooks

S = db.SCHEMA
log = logging.getLogger("ext_marketing.keywords")


def _person(who: str | None) -> dict | None:
    """Who added it, as {id, name} (spec v1.1): the person the site's app named (X-Acting-User), else the key's label."""
    if not who:
        return None
    name = who.split(" via ")[0].removeprefix("key:")
    return {"id": name, "name": name}


def shape(r: dict) -> dict:
    return {"id": r["id"], "phrase": r["phrase"], "language": r["language"], "source": r["source"], "status": r["status"],
            "rationale": r["rationale"], "uses": r["uses"], "searchVolume": r.get("search_volume"), "difficulty": r.get("difficulty"),
            "addedBy": _person(r["added_by"]), "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


async def add(site_id: str, phrases: list, language: str, source: str, by: str | None, rationale: str | None = None,
              volume: int | None = None, difficulty: int | None = None) -> tuple[list, list]:
    added, skipped = [], []
    for p in phrases:
        p = " ".join(str(p or "").split())[:200]
        if not p:
            continue
        row = await db.pool().fetchrow(
            f"""INSERT INTO {S}.keywords (id, site_id, phrase, language, source, rationale, added_by, search_volume, difficulty)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING RETURNING *""",
            security.new_id("kw"), site_id, p, language, source, rationale, by, volume, difficulty)
        (added if row else skipped).append(shape(dict(row)) if row else p)
    return added, skipped


async def _semrush(seeds: list, database: str, limit: int) -> tuple[list, str]:
    """Related keywords with volume + difficulty. ('', reason) when SEMrush is not available - the run goes on without it."""
    import os
    key = os.environ.get("SEMRUSH_API_KEY")
    if not key:
        return [], "SEMrush not configured on this server"
    try:
        from semrush_connector.semrush_api import phrase_all
    except Exception:                                                   # noqa: BLE001
        return [], "SEMrush module not available"
    out, errors = [], 0
    for s in seeds:
        try:
            for k in await phrase_all(key, s, database, limit) or []:
                out.append({"phrase": k.get("Keyword") or k.get("Ph") or k.get("phrase"), "volume": _int(k.get("Search Volume") or k.get("Nq") or k.get("volume")),
                            "difficulty": _int(k.get("Keyword Difficulty Index") or k.get("Kd") or k.get("difficulty"))})
        except Exception:                                               # noqa: BLE001
            errors += 1
    return [x for x in out if x["phrase"]], (f"SEMrush failed for {errors} of {len(seeds)} seeds" if errors else "")


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


@jobs.handler("keywords.propose")
async def propose(job: dict) -> dict:
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1 AND deleted_at IS NULL", job["site_id"]) or {})
    if not site:
        raise jobs.Final("The site no longer exists.")
    s = await db.settings(["kw_propose_pages", "kw_propose_count", "kw_propose_seeds", "kw_semrush_limit", "kw_semrush_database_default", "kw_max_tokens"])
    seo, _ = await profile.seo(site["id"])
    langs = [job["payload"].get("language")] if job["payload"].get("language") else site["languages"]
    facts = await profile.brief(site)
    pages, problems = await sitepages.read(seo.get("siteUrl") or site["url"], int(s["kw_propose_pages"]))
    if not pages and problems:
        reader = (await db.settings(["site_reader_name"])).get("site_reader_name") or "Quantum1st-MarketingBot"
        why = problems[0]["reason"]
        read_note = (f"The website {why}, so keywords were proposed from the site's profile only. "
                     + (f"Ask the site owner to allow the reader '{reader}' in the website firewall." if "403" in why or "429" in why else ""))
    else:
        read_note = None
    await jobs.progress(job["id"], 0.25)
    existing = [r["phrase"] for r in await db.pool().fetch(f"SELECT phrase FROM {S}.keywords WHERE site_id = $1", site["id"])]
    site_txt = "\n\n".join(f"[{p['title'] or p['url']}]\n{p['text']}" for p in pages) or "(the site could not be read)"
    tin = tout = 0
    # 1) the model names seed phrases from what the site actually offers
    raw, a, b = await llm.chat([
        {"role": "system", "content": "You are an SEO strategist. From the website's own pages and facts, name the search phrases its real customers type. "
                                      "Answer ONLY JSON: {\"seeds\": [\"...\"]}."},
        {"role": "user", "content": f"{facts}\nLanguages: {', '.join(langs)}\nGive {int(s['kw_propose_seeds'])} short seed phrases in the site's main language.\n\nWEBSITE PAGES:\n{site_txt}"}],
        max_tokens=int(s["kw_max_tokens"]))
    tin += a; tout += b
    seeds = [x for x in (llm.json_from(raw).get("seeds") or []) if isinstance(x, str)][: int(s["kw_propose_seeds"])]
    await jobs.progress(job["id"], 0.45)
    market, note = await _semrush(seeds, seo.get("semrushDatabase") or s["kw_semrush_database_default"], int(s["kw_semrush_limit"]))
    await jobs.progress(job["id"], 0.65)
    # 2) the model proposes the list, seeing the market data; it decides, the code only checks the shape
    n_total = int(s["kw_propose_count"])
    per = {lg: n_total // len(langs) + (1 if i < n_total % len(langs) else 0) for i, lg in enumerate(langs)}   # derived from the site's languages
    split = ", ".join(f"{n} in {lg}" for lg, n in per.items())
    mk = "\n".join(f"- {m['phrase']} | volume {m['volume']} | difficulty {m['difficulty']}" for m in market[:200]) or "(no market data)"
    raw, a, b = await llm.chat([
        {"role": "system", "content": "You are an SEO strategist for the website below. Propose keywords it can realistically rank for and that match what it offers. "
                                      "Prefer phrases with real search volume when market data is given; include long-tail phrases. Do not propose any phrase in the EXISTING list. "
                                      "Answer ONLY JSON: {\"keywords\": [{\"phrase\": \"...\", \"language\": \"" + "|".join(langs) + "\", \"rationale\": \"one short line\"}]}."},
        {"role": "user", "content": f"{facts}\nHow many: {n_total} in total - {split}. Each phrase is written natively in its language (as people in that market search), not translated word for word. A phrase is written wholly in its own language and script, place names included; only acronyms and brand names people type in Latin letters (for example DIFC) stay as they are\n\nMARKET DATA (SEMrush):\n{mk}\n\n"
                                    f"EXISTING (do not repeat):\n{', '.join(existing[:400]) or '(none)'}\n\nWEBSITE PAGES:\n{site_txt[:12000]}"}],
        max_tokens=int(s["kw_max_tokens"]))
    tin += a; tout += b
    items = llm.json_from(raw).get("keywords") or []
    vol = {m["phrase"].lower(): m for m in market}
    added = skipped = 0
    new_ids: list = []
    for it in items[: int(s["kw_propose_count"]) * 2]:
        if not isinstance(it, dict) or not isinstance(it.get("phrase"), str):
            continue
        lg = it.get("language") if it.get("language") in site["languages"] else site["default_language"]
        m = vol.get(it["phrase"].strip().lower(), {})
        a2, sk = await add(site["id"], [it["phrase"]], lg, "ai", "engine", (it.get("rationale") or "")[:300], m.get("volume"), m.get("difficulty"))
        added += len(a2); skipped += len(sk); new_ids += [x["id"] for x in a2]
        if added >= int(s["kw_propose_count"]):
            break
    await meter.record(job["account_id"], site["id"], "keywordProposals", 1, tokens_in=tin, tokens_out=tout, job_id=job["id"])
    if new_ids:
        await webhooks.emit(site["id"], "keywords.proposed", {"ids": new_ids})
    by_lang = {lg: await db.pool().fetchval(f"SELECT count(*) FROM {S}.keywords WHERE site_id=$1 AND source='ai' AND language=$2", site["id"], lg) for lg in langs}
    return {"added": added, "skipped": skipped, "pagesRead": len(pages), "readFromMeta": sum(1 for p in pages if p.get("read") == "meta"), "byLanguage": by_lang, "seeds": seeds, "marketData": len(market), "note": " ".join(x for x in (read_note, note) if x) or None, "readProblems": problems[:10]}


@jobs.handler("seo.propose")
async def propose_seo(job: dict) -> dict:
    """Registered with the job runner here, beside the other proposals; the work is in profile.py."""
    return await profile.propose_from_site(job)
