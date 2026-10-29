"""Site health (spec v1.1 /site-health): a scan reads the site's pages and MEASURES them (titles and descriptions against the
site's own rules, headings, image alt text, noindex, duplicates, broken links). Each finding keeps its key (page, check) across
scans: a dismissed one stays dismissed, one that is gone is removed, an applied one that comes back is opened again.
Prepare: the AI writes a proposed title or description for the findings picked; the length is measured, never assumed.
Apply: writes a prepared fix to the website through its connection, then reads it back."""
from __future__ import annotations
import logging

import httpx

from . import db, jobs, llm, meter, netguard, profile, rules as R, security, sitepages, webhooks, website

S = db.SCHEMA
log = logging.getLogger("ext_marketing.health")
WRITES = {"missing_title": "metaTitle", "title_length": "metaTitle", "duplicate_title": "metaTitle",
          "missing_description": "metaDescription", "description_length": "metaDescription", "duplicate_description": "metaDescription"}
BAND = {"metaTitle": "the title", "metaDescription": "the meta description"}


def _band(n: int, band) -> bool:
    return not band or band[0] <= n <= band[1]


def measure(page_url: str, html: str, rules: dict) -> tuple[list[dict], dict]:
    """The findings of one page, as facts, plus what was read (title, description) for the duplicate check and the fixer."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[dict] = []
    add = lambda code, issue, **detail: out.append({"code": code, "issue": issue, "detail": detail})   # noqa: E731
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split())
    md = soup.find("meta", attrs={"name": lambda v: (v or "").lower() == "description"})
    desc = " ".join(((md.get("content") if md else "") or "").split())
    for code_missing, code_len, val, band, name in (("missing_title", "title_length", title, rules.get("metaTitle"), "title"),
                                                    ("missing_description", "description_length", desc, rules.get("metaDescription"), "meta description")):
        if not val:
            add(code_missing, f"The page has no {name}.")
        elif not _band(len(val), band):
            add(code_len, f"The {name} is {len(val)} characters; the band is {band[0]}–{band[1]}.", value=val, length=len(val))
    h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    if not [h for h in h1 if h]:
        add("missing_h1", "The page has no main heading (H1).")
    elif len(h1) > 1:
        add("many_h1", f"The page has {len(h1)} main headings (H1).", headings=h1[:5])
    if rules.get("altRequired"):
        bare = [i.get("src") or i.get("data-src") or "" for i in soup.find_all("img")
                if not (i.get("alt") or "").strip() and i.get("role") != "presentation" and i.get("aria-hidden") != "true"]
        if bare:
            add("img_alt", f"{len(bare)} image{'s' if len(bare) > 1 else ''} without alt text.", images=bare[:10], count=len(bare))
    rb = soup.find("meta", attrs={"name": lambda v: (v or "").lower() == "robots"})
    if rb and "noindex" in (rb.get("content") or "").lower():
        add("noindex", "The page asks search engines not to list it (noindex).")
    lang = (soup.html.get("lang") if soup.html else None) or ""
    return out, {"title": title, "description": desc, "lang": lang.split("-")[0].lower(), "h1": (h1 or [""])[0]}


def across(pages: list[dict]) -> list[tuple[str, dict]]:
    """Titles and descriptions shared by more than one page: every page of the group gets the finding, naming the others."""
    out = []
    for key, code, name in (("title", "duplicate_title", "title"), ("description", "duplicate_description", "meta description")):
        groups: dict = {}
        for p in pages:
            v = (p["read"].get(key) or "").strip().lower()
            if v:
                groups.setdefault(v, []).append(p["url"])
        for urls in groups.values():
            if len(urls) > 1:
                for u in urls:
                    out.append((u, {"code": code, "issue": f"{len(urls) - 1} other page{'s' if len(urls) > 2 else ''} use the same {name}.",
                                    "detail": {"samePages": [x for x in urls if x != u][:10]}}))
    return out


async def _check_links(urls: list[str], limit: int, timeout: float) -> tuple[list[dict], list[str]]:
    """Links to pages the scan did not read: each is asked once (HEAD, then GET when HEAD is refused). (broken, asked)"""
    out, asked = [], []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        for u in urls[:limit]:
            asked.append(u); first = u
            try:
                for _ in range(5):
                    await netguard.check(u, require_https=False)
                    r = await c.head(u)
                    if r.status_code in (405, 501):
                        r = await c.get(u)
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        u = str(r.url.join(r.headers["location"]))
                        continue
                    break
                if r.status_code in (404, 410):
                    out.append({"url": first, "status": r.status_code})
            except (netguard.Blocked, httpx.HTTPError):
                continue
    return out, asked


def _advice(checks: dict, code: str, lang: str) -> str | None:
    f = (checks.get(code) or {}).get("fix") or {}
    return f.get(lang) or f.get("en")


def shape(r: dict) -> dict:
    return {"id": r["id"], "url": r["url"], "level": r["level"], "issue": r["issue"], "fix": r["fix"], "status": r["status"],
            "code": r["code"], "error": r.get("error"), "updatedAt": r["updated_at"]}


@jobs.handler("site.scan")
async def scan(job: dict) -> dict:
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", job["site_id"]))
    s = await db.settings(["health_max_pages", "health_link_check_max", "health_checks", "site_fetch_timeout_seconds"])
    checks = s.get("health_checks") or {}
    rules, _ = await R.for_site(site["id"])
    seo, _ = await profile.seo(site["id"])
    pages, problems, graph = await sitepages.read(seo.get("siteUrl") or site["url"], int(s["health_max_pages"]), raw=True)
    if not pages:
        why = "; ".join(f"{p['url']}: {p['reason']}" for p in problems[:3]) or "no page answered"
        raise jobs.Final(f"The site could not be read ({why}). Nothing was changed.")
    await jobs.progress(job["id"], 0.6)
    found: list[tuple[str, dict]] = []
    for p in pages:
        f, p["read"] = measure(p["url"], p["html"], rules)
        found += [(p["url"], x) for x in f]
    found += across(pages)
    for pr in problems:
        if pr.get("status") in (404, 410):
            found.append((pr["url"], {"code": "broken_link", "issue": f"This address answers {pr['status']} (not found); "
                                      f"{len(pr['linkedFrom'])} page{'s' if len(pr['linkedFrom']) != 1 else ''} link to it.",
                                      "detail": {"linkedFrom": pr["linkedFrom"][:10], "status": pr["status"]}}))
        else:
            found.append((pr["url"], {"code": "page_unreachable", "issue": f"The page could not be read: {pr['reason']}.", "detail": {}}))
    broken, asked = await _check_links(graph["unvisited"], int(s["health_link_check_max"]), float(s.get("site_fetch_timeout_seconds") or 20))
    for b in broken:
        lf = graph["refs"].get(sitepages.key(b["url"]), [])
        found.append((b["url"], {"code": "broken_link", "issue": f"This address answers {b['status']} (not found); "
                                 f"{len(lf)} page{'s' if len(lf) != 1 else ''} link to it.", "detail": {"linkedFrom": lf[:10], "status": b["status"]}}))
    await jobs.progress(job["id"], 0.85)
    lang = site["default_language"]
    keys = set()
    async with db.pool().acquire() as con, con.transaction():
        for url, f in found:
            if f["code"] not in checks or (url, f["code"]) in keys:
                continue
            keys.add((url, f["code"]))
            await con.execute(
                f"""INSERT INTO {S}.site_findings (id, site_id, url, code, level, issue, fix, detail)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT (site_id, url, code) DO UPDATE SET level = $5, issue = $6, detail = $8, last_seen_at = now(), updated_at = now(),
                        status = CASE WHEN {S}.site_findings.status = 'applied' THEN 'open' ELSE {S}.site_findings.status END,
                        fix = CASE WHEN {S}.site_findings.status IN ('prepared') THEN {S}.site_findings.fix ELSE $7 END,
                        fix_data = CASE WHEN {S}.site_findings.status IN ('prepared') THEN {S}.site_findings.fix_data END""",
                security.new_id("sf"), site["id"], url, f["code"], checks[f["code"]].get("level") or "warn", f["issue"],
                _advice(checks, f["code"], lang), f["detail"])
        read = [p["url"] for p in pages] + [p["url"] for p in problems] + asked   # only what this scan looked at can be resolved
        gone = await con.fetchval(
            f"""WITH d AS (DELETE FROM {S}.site_findings WHERE site_id = $1 AND status IN ('open','prepared') AND url = ANY($2)
                           AND NOT ((url, code) IN (SELECT * FROM unnest($3::text[], $4::text[]))) RETURNING 1) SELECT count(*) FROM d""",
            site["id"], read, [k[0] for k in keys], [k[1] for k in keys])
    open_n = await db.pool().fetchval(f"SELECT count(*) FROM {S}.site_findings WHERE site_id = $1 AND status = 'open'", site["id"])
    await meter.record(site["account_id"], site["id"], "siteScans", 1, job_id=job["id"])
    await webhooks.emit(site["id"], "site.scan_finished", {"findings": open_n})
    return {"pages": len(pages), "problems": len(problems), "findings": len(keys), "open": open_n, "resolved": gone}


async def _write_fix(site: dict, f: dict, rules: dict, page: dict, s: dict) -> tuple[str | None, int, int, str | None]:
    """(value, tokens in, tokens out, note). The value is measured against the band; one more try when it is outside."""
    field = WRITES[f["code"]]
    band = rules.get(field) or []
    others = (f["detail"] or {}).get("samePages") or []
    what = BAND[field]
    ask = [{"role": "system", "content": f"You write {what} of one page of the website below, for search results. Write it in the page's own "
                                         f"language, true to what the page says, specific to this page"
                                         f"{' and different from the other pages that share it' if others else ''}. "
                                         f"Length: {band[0]}–{band[1]} characters. Answer ONLY JSON: {{\"value\": \"...\"}}."},
           {"role": "user", "content": f"{await profile.brief(site)}\nPAGE: {f['url']}\nLANGUAGE (html lang): {page.get('lang') or 'unknown'}\n"
                                       f"CURRENT TITLE: {page.get('title') or '(none)'}\nCURRENT DESCRIPTION: {page.get('description') or '(none)'}\n"
                                       f"MAIN HEADING: {page.get('h1') or '(none)'}\nPAGE TEXT:\n{(page.get('text') or '')[:4000]}"}]
    tin = tout = 0
    best, note = None, None
    for _ in range(2):
        raw, a, b = await llm.chat(ask, max_tokens=int(s["health_fix_max_tokens"]))
        tin += a; tout += b
        v = " ".join(str(llm.json_from(raw).get("value") or "").split())
        if v and (best is None or abs(len(v) - sum(band) / 2) < abs(len(best) - sum(band) / 2)):
            best = v
        if v and _band(len(v), band):
            return v, tin, tout, None
        ask += [{"role": "assistant", "content": raw},
                {"role": "user", "content": f"That is {len(v)} characters; it must be {band[0]}–{band[1]}. Write it again."}]
    if best:
        note = f"The proposal is {len(best)} characters; the band is {band[0]}–{band[1]}. Edit it before applying."
    return best, tin, tout, note


@jobs.handler("site.fixes")
async def prepare(job: dict) -> dict:
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {S}.sites WHERE id = $1", job["site_id"]))
    s = await db.settings(["health_fix_max_tokens"])
    rules, _ = await R.for_site(site["id"])
    rows = [dict(x) for x in await db.pool().fetch(
        f"SELECT * FROM {S}.site_findings WHERE site_id = $1 AND id = ANY($2) AND status IN ('open','prepared')", site["id"], job["payload"]["ids"])]
    todo = [r for r in rows if r["code"] in WRITES]
    tin = tout = 0
    done, manual, failed = [], [r["id"] for r in rows if r["code"] not in WRITES], []
    cache: dict = {}
    for i, f in enumerate(todo):
        if f["url"] not in cache:                                       # the page as it is now, read once
            got, _probs, _g = await sitepages.read(f["url"], 1, raw=True)
            cache[f["url"]] = (lambda p: {**measure(p["url"], p["html"], rules)[1], "text": p["text"]})(got[0]) if got else None
        page = cache[f["url"]]
        if page is None:
            failed.append(f["id"])
            await db.pool().execute(f"UPDATE {S}.site_findings SET error = 'The page could not be read now. Scan again.', updated_at = now() WHERE id = $1", f["id"])
            continue
        try:
            v, a, b, note = await _write_fix(site, f, rules, page, s)
        except Exception as e:                                          # noqa: BLE001 - one finding never stops the others
            log.exception("health fix %s", f["id"])
            v, a, b, note = None, 0, 0, f"The proposal could not be written ({type(e).__name__}). Try again."
        tin += a; tout += b
        if not v:
            failed.append(f["id"])
            await db.pool().execute(f"UPDATE {S}.site_findings SET error = $2, updated_at = now() WHERE id = $1", f["id"], note)
            continue
        field = WRITES[f["code"]]
        await db.pool().execute(
            f"""UPDATE {S}.site_findings SET status = 'prepared', fix = $2, fix_data = $3, error = $4, updated_at = now() WHERE id = $1""",
            f["id"], f"Proposed: {v}", {"field": field, "value": v}, note)
        done.append(f["id"])
        await jobs.progress(job["id"], (i + 1) / max(1, len(todo)))
    if tin or tout:
        await meter.record(site["account_id"], site["id"], "siteScans", 0, tokens_in=tin, tokens_out=tout, job_id=job["id"])
    return {"prepared": done, "manual": manual, "failed": failed}


async def apply(site: dict, f: dict) -> dict:
    """Writes one prepared fix to the website and returns what it replaced; raises website.Refused with a plain reason."""
    conn = await website.row(site["id"])
    t = (conn or {}).get("config") or {}
    if not conn or not conn["secret_enc"] or not t.get("type"):
        raise website.Refused("The website is not connected. Open Settings → Website connection.")
    fn = website.ADAPTERS[t["type"]].get("set_seo")
    if not fn:
        raise website.Refused(f"Fixes cannot be written to a {t['type']} site yet. Change it on the website, then dismiss this.")
    fd = f["fix_data"] or {}
    return await fn(t, security.decrypt(conn["secret_enc"]), f["url"], {fd["field"]: fd["value"]})
