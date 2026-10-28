"""Writes one post for one site from facts only: the site's profile, its own pages, web research, the site's rules.
A blog article is written section by section, then a small JSON call writes title/slug/meta/photo idea about the
finished article. A social post is one JSON call. Nothing names Nour or assumes any category."""
from __future__ import annotations
import json, re

import markdown as _md

from . import db, llm, profile, research, rules as R, sitepages

LANG = {"en": "English", "ar": "Arabic (Modern Standard, natural for a Gulf audience)"}


def _slug(t: str) -> str:
    return re.sub(r"[^\w]+", "-", (t or "").lower(), flags=re.U).strip("-")[:80]


def pick(options: list, band, measure) -> str | None:
    """The model's alternatives for one length-limited field: the first that measures inside the band, else the closest one.
    The model writes every option; the code only measures them."""
    opts = [o.strip() for o in options if isinstance(o, str) and o.strip()]
    if not opts:
        return None
    if not band:
        return opts[0]
    lo, hi = band
    for o in opts:
        if lo <= measure(o) <= hi:
            return o
    return min(opts, key=lambda o: (max(lo - measure(o), measure(o) - hi, 0), opts.index(o)))


async def write(site: dict, post: dict, note: str | None = None, previous: dict | None = None, fix: list | None = None, ctx: dict | None = None) -> dict:
    """fix = measured findings of the writer's own previous version (a revision pass); ctx carries the site pages and research
    of the first pass so a revision does not fetch them again."""
    s = await db.settings(["writer_site_pages", "writer_max_tokens_blog", "writer_max_tokens_meta", "writer_max_tokens_social",
                           "writer_temperature", "social_char_limits", "meta_alternatives", "writer_length_target", "writer_max_tokens_section",
                           "writer_max_tokens_outline", "meta_retry", "writer_section_min_share"])
    alts = int(s.get("meta_alternatives") or 3)
    seo, _ = await profile.seo(site["id"])
    rules, _ = await R.for_site(site["id"])
    facts = await profile.brief(site)
    lang, kind, kws = post["language"], post["kind"], post.get("keywords") or []
    tin = tout = 0
    if ctx is None:
        own, _probs = await sitepages.read(seo.get("siteUrl") or site["url"], int(s["writer_site_pages"]))
        ctx = {"own": own, "sources": await research.find(f"{post['topic']} {site.get('category') or ''}".strip(), lang) if kind == "blog" else []}
    own, sources = ctx["own"], ctx["sources"]
    own_txt = "\n\n".join(f"[{p['title'] or p['url']}]\n{p['text'][:2500]}" for p in own) or "(the site could not be read)"
    ev = "\n\n".join(f"[{i + 1}] {x['title']} ({x['url']})\n{x['text']}" for i, x in enumerate(sources)) or "(no web research available)"
    redo = ""
    kshare = R.language_share(" ".join(kws), lang, {**rules, "_minLetters": 1}) if kws else None
    if kshare is not None and rules.get("_minShare") is not None and kshare < float(rules["_minShare"]):
        raw, a, b = await llm.chat([
            {"role": "system", "content": f"Give the search keywords people in this market type in {LANG.get(lang, lang)} for the same intent as each keyword below - "
                                          "the way they really search, not a word-for-word translation. Answer ONLY JSON: {\"keywords\": [\"...\"]} in the same order."},
            {"role": "user", "content": f"{facts}\nKEYWORDS:\n" + "\n".join(kws)}], max_tokens=int(s["writer_max_tokens_meta"]), temperature=0.2)
        tin += a; tout += b
        try:
            adapted = [k for k in (llm.json_from(raw).get("keywords") or []) if isinstance(k, str) and k.strip()]
        except ValueError:
            adapted = []
        if adapted:
            kws = adapted[: len(kws)] or kws
    other = R.language_share(post["topic"] + " " + " ".join(kws), lang, rules)
    if other is not None and rules.get("_minShare") is not None and other < float(rules["_minShare"]):
        redo += (f"\n\nThe topic and keywords are given in another language. Write EVERYTHING (title, text, headings) in {LANG.get(lang, lang)}, "
                 f"and use the keywords as people in that market search for them in {LANG.get(lang, lang)} - not a word-for-word translation.")
    if fix and previous:
        redo += ("\n\nYour previous version was measured and has these problems; write the complete piece again so that every one is fixed: "
                 + "; ".join(fix) + f".\nPrevious version:\n{(previous.get('content') or '')[:6000]}")
    if note:
        failing = [f["message"] for f in (previous or {}).get("findings") or [] if f["level"] in ("critical", "warn")]
        redo += (f"\n\nThe reviewer asked for changes - follow this exactly: {note}"
                + (f"\nThe previous version failed these checks; the new one must pass them: {'; '.join(failing)}." if failing else "")
                + f"\nPrevious version:\n{((previous or {}).get('content') or '')[:4000]}")
    common = (f"You write for the website below, in {LANG.get(lang, lang)}. Tone: {post.get('tone') or seo.get('tone') or 'clear and helpful'}.\n{facts}\n"
              "Write only what the site's own pages and the research support; no invented statistics, prices, dates, names, events, studies or quotes. "
              "If the topic itself states a figure, event or study that the pages and research do not support, leave that claim out (also from the title). "
              "Never mention AI, models, tools or how the text was made.")
    if kind == "blog":
        return await _article(site, post, s, rules, seo, facts, common, own_txt, sources, ev, redo, kws, lang, ctx, alts, tin, tout)
    limit = (s.get("social_char_limits") or {}).get(post["platform"])
    n_alt = int((await db.settings(["social_alternatives"])).get("social_alternatives") or 1)
    band = (rules.get("social") or {}).get("words")
    cnt = [tin, tout]
    text, d = await alternatives([
        {"role": "system", "content": common + "\n" + R.brief(rules, "social", seo.get("cta")) +
         (f"\nThe platform allows at most {limit} characters." if limit else "") +
         (f"\nWrite {n_alt} versions of the post, about " + ", ".join(str(int(band[0] + (band[1] - band[0]) * (i + 1) / (n_alt + 1))) for i in range(n_alt))
          + " words long, in that order." if band else f"\nWrite {n_alt} versions of the post that differ in length.") +
         " Answer ONLY JSON: {{\"versions\": [\"post text\", ...], \"hashtags\": [\"#...\"], "
         "\"photoIdea\": \"what a fitting stock photo shows, in English\", "
         f"\"imageAlt\": \"describes that photo in {LANG.get(lang, lang)}\"}}" + redo + f"\n\nTHE SITE'S OWN PAGES:\n{own_txt}"},
        {"role": "user", "content": f"Platform: {post['platform']}\nTopic: {post['topic']}\nKeywords: {', '.join(kws) or '(choose from the topic)'}"}],
        "versions", band, R.words, (await db.settings(["meta_retry"])).get("meta_retry"), int(s["writer_max_tokens_social"]) * max(1, n_alt),
        float(s["writer_temperature"]), cnt, prefer=(lambda v: len(v) <= limit) if limit else None)
    tin, tout = cnt
    text = (text or "").strip()[: limit] if limit else (text or "").strip()

    return {"title": post["topic"][:255], "subtitle": "", "content": text, "slug": "", "meta_title": "", "meta_description": "",
            "keywords": kws, "photo_idea": (d.get("photoIdea") or post["topic"])[:200], "image_alt": (d.get("imageAlt") or "")[:300],
            "hashtags": [h if h.startswith("#") else "#" + h for h in (d.get("hashtags") or []) if isinstance(h, str)][:15], "sources": [], "tokens": (tin, tout), "ctx": ctx}


async def alternatives(messages: list, key: str, band, measure, retries: int, max_tokens, temperature, counter, prefer=None) -> tuple[str | None, dict]:
    """Ask for versions under `key`, keep the first inside the band (preferring those `prefer` accepts); when none fits or none
    came back, ask again with the measured values. Returns (chosen, the last JSON answer)."""
    note, got = "", {}
    chosen = None
    for _ in range(1 + int(retries or 0)):
        msgs = [dict(messages[0], content=messages[0]["content"] + note)] + messages[1:]
        try:
            got = llm.json_from(await _ask(msgs, max_tokens, temperature, counter))
        except ValueError:
            got = {}
        opts = [v.strip() for v in (got.get(key) or []) if isinstance(v, str) and v.strip()]
        pool = ([v for v in opts if prefer(v)] or opts) if prefer else opts
        if pool:
            chosen = pick(pool, band, measure)
        if chosen and (not band or band[0] <= measure(chosen) <= band[1]):
            break
        note = (f"\nYour previous versions measured {[measure(v) for v in opts]}; each must be {band[0]}-{band[1]}. Give new versions that fit."
                if opts and band else "\nYour previous answer had no usable versions. Answer ONLY the JSON asked for.")
    return chosen, got


async def _ask(messages, max_tokens, temperature, counter):
    text, a, b = await llm.chat(messages, max_tokens=int(max_tokens), temperature=float(temperature))
    counter[0] += a; counter[1] += b
    return text


async def _article(site, post, s, rules, seo, facts, common, own_txt, sources, ev, redo, kws, lang, ctx, alts, tin, tout) -> dict:
    """Section by section: outline -> opening (alternatives, the one inside the word band is kept) -> each section with its share of
    the word target (measured; one retry when it comes back short) -> FAQ answers + closing -> meta fields (alternatives, measured).
    The model writes every word; the code sets each part's target from the site's rules and measures what came back.
    The parts are kept in out['_w'] so a revision repairs only the part a finding is about (see repair)."""
    lo, hi = rules["blog"]["words"]
    w = {"s": s, "rules": rules, "seo": seo, "facts": facts, "common": common, "lang": lang, "lang_name": LANG.get(lang, lang),
         "main": kws[0] if kws else post["topic"], "temp": float(s["writer_temperature"]), "alts": alts, "redo": redo, "kws": kws,
         "sources": sources, "ctx": ctx, "tk": [tin, tout], "lo": lo, "target": lo + (hi - lo) * float(s.get("writer_length_target") or 0.35),
         "ground": f"\n\nTHE SITE'S OWN PAGES:\n{own_txt}\n\nWEB RESEARCH (use only what helps; never copy sentences):\n{ev}"}
    fq = rules.get("faq") or {}
    nfaq_lo, nfaq_hi = fq["count"] if fq.get("required") else (0, 0)
    fa = fq.get("answerWords") or [0, 0]
    n_sec = max(int(rules.get("h2Min") or 3), 1)
    # 1) outline
    raw = await _ask([
        {"role": "system", "content": common + f"\nPlan an article in {w['lang_name']}. Answer ONLY JSON: {{\"title\": \"...\", \"sections\": "
                                      f"[{{\"heading\": \"...\", \"points\": [\"...\"]}}], \"faq\": [\"question\", ...]}}. "
                                      f"{n_sec} to {n_sec + 2} sections, each heading phrased the way a reader would search; {nfaq_lo}-{nfaq_hi} FAQ questions."
                                      f" Main keyword: '{w['main']}'." + redo + w["ground"]},
        {"role": "user", "content": f"Topic: {post['topic']}"}], s["writer_max_tokens_outline"], 0.3, w["tk"])
    try:
        plan = llm.json_from(raw)
    except ValueError:                                                  # answered a bare list of sections instead of the object asked for
        try:
            plan = {"sections": llm.json_from(raw, want=list)}
        except ValueError:
            plan = {}
    w["secs"] = [x for x in (plan.get("sections") or []) if isinstance(x, dict) and x.get("heading")][: n_sec + 2] or [{"heading": post["topic"], "points": []}]
    faqs = [q for q in (plan.get("faq") or []) if isinstance(q, str)][:nfaq_hi] if nfaq_hi else []
    w["title"] = (plan.get("title") or post["topic"]).strip()
    # 2) opening
    w["intro"] = await _intro(w)
    # 3) sections, each with its share of what is left of the target
    per = max(1, int((w["target"] - R.words(w["intro"]) - len(faqs) * sum(fa) / 2) / len(w["secs"])))
    w["parts"] = [await _section(w, i, per) for i in range(len(w["secs"]))]
    # 4) FAQ answers + closing call to action
    w["faq_md"], w["closing"] = "", ""
    if faqs or seo.get("cta"):
        raw = await _ask([
            {"role": "system", "content": common + f"\nIn {w['lang_name']}, answer each question in {fa[0]}-{fa[1]} words" +
                                          (f", and write a one-sentence closing that ends with this call to action: {seo.get('cta')}" if seo.get("cta") else "") +
                                          ". Answer ONLY JSON: {\"answers\": [\"...\"], \"closing\": \"...\"}." + w["ground"]},
            {"role": "user", "content": "Questions:\n" + "\n".join(faqs) if faqs else "(no questions)"}], s["writer_max_tokens_section"], w["temp"], w["tk"])
        d = llm.json_from(raw)
        ans = [a for a in (d.get("answers") or []) if isinstance(a, str)]
        if faqs:
            head = "الأسئلة الشائعة" if lang == "ar" else "FAQ"
            w["faq_md"] = f"## {head}\n\n" + "\n\n".join(f"### {q}\n\n{a}" for q, a in zip(faqs, ans))
        w["closing"] = (d.get("closing") or "").strip()
    return await _finish(post, w)


async def _intro(w: dict, extra: str = "") -> str:
    iw = w["rules"]["introWords"]
    text, _ = await alternatives([
        {"role": "system", "content": w["common"] + f"\nWrite the opening paragraph of the article '{w['title']}' in {w['lang_name']}: it answers the reader's "
                                      f"question directly and uses the keyword '{w['main']}' naturally. Give {w['alts']} versions of different length around "
                                      f"{iw[0]}-{iw[1]} words. Answer ONLY JSON: {{\"versions\": [\"...\"]}}." + w["redo"] + extra + w["ground"]},
        {"role": "user", "content": f"Outline: {[x['heading'] for x in w['secs']]}"}], "versions", iw, R.words, w["s"].get("meta_retry"),
        w["s"]["writer_max_tokens_section"], w["temp"], w["tk"], prefer=lambda v: w["main"].lower() in v.lower())
    return text or ""


async def _section(w: dict, i: int, target: int) -> str:
    """One section's body, about `target` words. Measured: when it comes back under writer_section_min_share of the target it is
    asked again with the measured count, and the longer answer is kept."""
    sec, secs = w["secs"][i], w["secs"]
    share = float(w["s"].get("writer_section_min_share") or 0)
    base = (w["common"] + f"\nYou are writing ONE section of the article '{w['title']}' in {w['lang_name']}. Write only the body of the section "
            f"'{sec['heading']}' (do not repeat the heading, no introduction to the whole article, no conclusion) in about {target} words, "
            "in short paragraphs; you may use ### sub-headings and lists. Plain Markdown, no JSON."
            + (f" Cover: {'; '.join(p for p in sec.get('points') or [] if isinstance(p, str))}." if sec.get("points") else "")
            + f" Other sections (do not repeat them): {[x['heading'] for j, x in enumerate(secs) if j != i]}.")
    note, best = "", ""
    for _ in range(2):
        raw = (await _ask([{"role": "system", "content": base + note + w["redo"] + w["ground"]},
                           {"role": "user", "content": f"Section: {sec['heading']}"}], w["s"]["writer_max_tokens_section"], w["temp"], w["tk"])).strip()
        if R.words(raw) > R.words(best):
            best = raw
        if R.words(best) >= target * share:
            break
        note = (f"\nYour previous answer for this section measured {R.words(raw)} words; it must be about {target} words. Write it again in full, "
                "with more concrete detail, examples and steps from the pages and research - no padding and no repetition.")
    return best


def _body(w: dict) -> str:
    parts = [f"## {x['heading']}\n\n{b}" for x, b in zip(w["secs"], w["parts"])]
    return "\n\n".join(x for x in [w["intro"], *parts, w["faq_md"], w["closing"]] if x)


async def _finish(post: dict, w: dict, extra: str = "") -> dict:
    """Assemble the parts and write the meta fields about the finished article (alternatives, measured; one retry)."""
    s, rules, lang_name = w["s"], w["rules"], w["lang_name"]
    body = _body(w)
    html = R.clean_html(_md.markdown(body, extensions=["extra", "sane_lists"]))
    slug_n = lambda x: len([t for t in _slug(x).split("-") if t])   # noqa: E731
    bands = {"metaTitles": (rules.get("metaTitle"), len), "metaDescriptions": (rules.get("metaDescription"), len), "slugs": (rules.get("slugWords"), slug_n)}
    meta, chosen, note = {}, {}, extra
    for attempt in range(1 + int(s.get("meta_retry") or 0)):
        raw = await _ask([
            {"role": "system", "content": f"You write SEO fields for a finished article, in {lang_name}. Answer ONLY JSON with keys: subtitle (one sentence stating the "
                                          f"article's main point), metaTitles, metaDescriptions, slugs (each a list of {w['alts']} alternatives that differ in length; slugs are "
                                          f"lowercase words joined by '-'), keywords (list in {lang_name}, as people search in that language; the main one first), "
                                          f"photoIdea (what a fitting stock photo shows, in English), imageAlt (describes that photo, in {lang_name}). "
                                          f"Limits: meta title {rules.get('metaTitle')} characters, meta description {rules.get('metaDescription')} characters, "
                                          f"slug {rules.get('slugWords')} words." + note},
            {"role": "user", "content": f"{w['facts']}\nMain keyword: {w['main']}\nTitle: {w['title']}\n\nARTICLE:\n{body[:6000]}"}], s["writer_max_tokens_meta"], 0.2, w["tk"])
        got = llm.json_from(raw)
        meta = {**meta, **{k: v for k, v in got.items() if v}}
        misses = []
        for key, (band, measure) in bands.items():
            opts = [o for o in (got.get(key) or []) if isinstance(o, str)] or ([chosen[key]] if chosen.get(key) else [])
            chosen[key] = pick(opts, band, measure)
            if band and chosen[key] and not (band[0] <= measure(chosen[key]) <= band[1]):
                misses.append(f"{key}: your options measured {[measure(o) for o in opts]}; each must be {band[0]}-{band[1]}")
        if not misses:
            break
        note = extra + "\nYour previous options did not fit: " + "; ".join(misses) + ". Give new options that fit."
    tin, tout = w["tk"]
    return {"title": w["title"][:255], "subtitle": (meta.get("subtitle") or "")[:300], "content": html,
            "slug": _slug(chosen.get("slugs") or w["title"]) or _slug(post["topic"]), "meta_title": (chosen.get("metaTitles") or "")[:255],
            "meta_description": (chosen.get("metaDescriptions") or "")[:400],
            "keywords": _own_language(meta.get("keywords"), w["kws"], w["lang"], rules),
            "photo_idea": (meta.get("photoIdea") or post["topic"])[:200], "image_alt": (meta.get("imageAlt") or "")[:300], "hashtags": [],
            "sources": [{"title": x["title"], "url": x["url"]} for x in w["sources"]], "tokens": (tin, tout), "ctx": w["ctx"], "_w": w}


REPAIRABLE = {"length", "mixed", "keyword", "intro"}


async def repair(post: dict, out: dict, findings: list) -> dict | None:
    """A revision pass for an article: only the parts the measured findings are about are written again (a short article gets its
    shortest sections expanded, stray foreign words are rewritten in the paragraph they sit in, a missing keyword gets a new title
    or opening), then the meta fields are written again about the result. None when no finding is one this can repair - the caller
    then asks for a complete new version."""
    w0 = out.get("_w")
    refs = {f["ref"] for f in findings if f["level"] in ("critical", "warn")}
    if not w0 or not refs & REPAIRABLE:
        return None
    w = {**w0, "parts": list(w0["parts"]), "secs": list(w0["secs"]), "tk": [0, 0]}
    rules, lang, ln = w["rules"], w["lang"], w["lang_name"]
    kw = (out.get("keywords") or [w["main"]])[0]
    if "length" in refs:
        n = R.words(R.text_of(out["content"]))
        if n < w["lo"]:
            order = sorted(range(len(w["parts"])), key=lambda i: R.words(w["parts"][i]))
            for j, i in enumerate(order):                               # shortest first; measured after each, stops once the target is met
                left = w["target"] - R.words(R.text_of(_md.markdown(_body(w))))
                if left <= 0:
                    break
                w["parts"][i] = await _section(w, i, int(R.words(w["parts"][i]) + left / (len(order) - j)))
    if "mixed" in refs:
        async def clean(text: str) -> str:
            stray = R.foreign_words(text, lang, rules)
            if not stray:
                return text
            raw = (await _ask([
                {"role": "system", "content": f"Rewrite the text below in {ln}, keeping its meaning, its length and its Markdown exactly, but write these words "
                                              f"in {ln}: {', '.join(stray)}. Names, brands and acronyms stay as they are. Answer with the rewritten text only."},
                {"role": "user", "content": text}], w["s"]["writer_max_tokens_section"], 0.2, w["tk"])).strip()
            ok = raw and len(R.foreign_words(raw, lang, rules)) < len(stray) and abs(R.words(raw) - R.words(text)) <= max(5, R.words(text) * 0.2)
            return raw if ok else text                                  # measured: kept only when it has fewer stray words and the same length
        w["title"] = await clean(w["title"])
        w["intro"] = await clean(w["intro"])
        w["parts"] = [await clean(b) for b in w["parts"]]
        w["faq_md"] = await clean(w["faq_md"]) if w["faq_md"] else ""
        w["closing"] = await clean(w["closing"]) if w["closing"] else ""
    if "keyword" in refs and rules.get("keywordInTitle") and kw.lower() not in w["title"].lower():
        t, _ = await alternatives([
            {"role": "system", "content": w["common"] + f"\nGive {w['alts']} titles in {ln} for the article below; each one contains the exact phrase '{kw}' "
                                          "and says what the reader gets. Answer ONLY JSON: {\"versions\": [\"...\"]}."},
            {"role": "user", "content": f"Current title: {w['title']}\nOutline: {[x['heading'] for x in w['secs']]}"}], "versions", None, len,
            w["s"].get("meta_retry"), w["s"]["writer_max_tokens_meta"], 0.3, w["tk"], prefer=lambda v: kw.lower() in v.lower())
        if t and kw.lower() in t.lower():
            w["title"] = t.strip()
    iw = rules.get("introWords") or [0, 10 ** 6]
    if ("keyword" in refs and rules.get("keywordInIntro") and kw.lower() not in w["intro"].lower()) or ("intro" in refs and not iw[0] <= R.words(w["intro"]) <= iw[1]):
        w["main"] = kw
        w["intro"] = await _intro(w, f"\nThe opening must contain the exact phrase '{kw}'.") or w["intro"]
    res = await _finish(post, w)
    res["tokens"] = tuple(w["tk"])
    return res


def _own_language(proposed, fallback: list, lang: str, rules: dict) -> list:
    """The meta step's keywords that are in the post's language; when none are, the (already adapted) keywords the article was written for."""
    ms = rules.get("_minShare")
    ok = [k for k in (proposed or []) if isinstance(k, str) and k.strip()
          and (ms is None or (R.language_share(k, lang, {**rules, "_minLetters": 1}) or 0) >= float(ms))]
    return ok[:10] or fallback


# ---------------------------------------------------------------- the twin in the other language (mirrors the source)
async def translate(site: dict, source: dict, twin: dict, note: str | None = None) -> dict:
    """Writes the twin of an article from its source: every text piece of the source's HTML is translated in order and put
    back into the same HTML, so the structure matches by construction. The model translates; the code counts what came back
    and asks again (with the counts) when a batch has a different number of pieces."""
    from . import mirror as M
    s = await db.settings(["twin_segments_per_call", "twin_max_tokens", "twin_temperature", "twin_retries", "twin_punctuation",
                           "meta_retry", "writer_max_tokens_meta"])
    rules, _ = await R.for_site(site["id"])
    facts = await profile.brief(site)
    lang, src_lang = twin["language"], source["language"]
    ln, sn = LANG.get(lang, lang), LANG.get(src_lang, src_lang)
    pieces = M.segments(source["content"] or "")
    size = max(1, int(s.get("twin_segments_per_call") or 40))
    tk = [0, 0]
    # pieces without letters (punctuation, numbers, symbols) are not language: kept, with the target language's punctuation
    punct = (s.get("twin_punctuation") or {}).get(lang) or {}
    words = [i for i, p in enumerate(pieces) if M.has_words(p)]
    out = ["".join(punct.get(ch, ch) for ch in p) for p in pieces]
    guide = (f"You translate a website article from {sn} into {ln} for the website below. Write natural, fluent {ln} as a native "
             "writer would, keeping the meaning, facts, names and numbers exactly; never add or drop content. The text comes as "
             "pieces with ids, in reading order; some pieces are parts of one sentence (split around bold text or links): "
             "translate each piece so that the pieces, read in order, form a natural sentence. Answer ONLY JSON: "
             "{\"pieces\": {\"<id>\": \"translation\", ...}} with every id given, each translated on its own.\n" + facts
             + (f"\nThe reviewer asked for this - follow it while keeping every piece: {note}" if note else ""))
    for b0 in range(0, len(words), size):
        batch = words[b0:b0 + size]
        todo = {f"p{i + 1}": pieces[i] for i in batch}
        done: dict = {}
        for attempt in range(1 + int(s.get("twin_retries") or 0)):
            ask = {k: v for k, v in todo.items() if k not in done}
            if not ask:
                break
            first = int(next(iter(ask))[1:]) - 1
            ctx = [pieces[j] for j in range(max(0, first - 3), first)]
            raw = await _ask([{"role": "system", "content": guide + ("" if not attempt else
                               f"\nThese ids were missing from your previous answer: {', '.join(ask)}. Translate exactly these.")},
                              {"role": "user", "content": json.dumps({"before": ctx, "pieces": ask}, ensure_ascii=False)}],
                             s["twin_max_tokens"], float(s.get("twin_temperature") or 0.2), tk)
            try:
                obj = llm.json_from(raw)
            except ValueError:
                obj = {}
            # the ids may come wrapped as asked ({"pieces": {...}}) or as the object itself ({"p1": ...}): either is the answer
            got = obj.get("pieces") if isinstance(obj.get("pieces"), (dict, list)) else {k: v for k, v in obj.items() if k in ask}
            if isinstance(got, list):                                   # answered a list: only usable when it lines up one to one
                got = dict(zip(ask, got)) if len(got) == len(ask) else {}
            done.update({k: str(v).strip() for k, v in got.items() if k in ask and isinstance(v, (str, int, float)) and str(v).strip()})
        missing = [k for k in todo if k not in done]
        if missing:
            raise ValueError(f"the translation left out {len(missing)} of the article's pieces after every retry")
        for k, v in done.items():
            out[int(k[1:]) - 1] = v
    content = M.rebuild(source["content"] or "", out)
    # the short fields: translated together, lengths measured against the site's bands (one retry with the measured values)
    fields = {"title": source.get("title") or "", "subtitle": source.get("subtitle") or "", "metaTitle": source.get("meta_title") or "",
              "metaDescription": source.get("meta_description") or "", "keywords": source.get("keywords") or []}
    bands = {"metaTitle": rules.get("metaTitle"), "metaDescription": rules.get("metaDescription")}
    note, short = "", {}
    for _ in range(1 + int(s.get("meta_retry") or 0)):
        raw = await _ask([
            {"role": "system", "content": f"Translate these fields of the same article from {sn} into {ln}, naturally. keywords: give them as "
                                          f"people in this market search in {ln} for the same intent, not word for word. Limits: metaTitle "
                                          f"{bands['metaTitle']} characters, metaDescription {bands['metaDescription']} characters. "
                                          "Answer ONLY JSON with the same keys." + note + "\n" + facts},
            {"role": "user", "content": json.dumps(fields, ensure_ascii=False)}], s["writer_max_tokens_meta"], 0.2, tk)
        try:
            short = llm.json_from(raw)
        except ValueError:
            short = {}
        miss = [f"{k} is {len(short.get(k) or '')} characters; it must be {b[0]}-{b[1]}" for k, b in bands.items()
                if b and short.get(k) and not b[0] <= len(short[k]) <= b[1]]
        if short.get("title") and not miss:
            break
        note = "\nYour previous answer did not fit: " + ("; ".join(miss) or "no title") + ". Answer again."
    kws = _own_language(short.get("keywords"), twin.get("keywords") or [], lang, rules)
    return {"title": (short.get("title") or twin.get("topic") or "")[:255], "subtitle": (short.get("subtitle") or "")[:300], "content": content,
            "slug": _slug(short.get("title") or "") or source.get("slug") or "", "meta_title": (short.get("metaTitle") or "")[:255],
            "meta_description": (short.get("metaDescription") or "")[:400], "keywords": kws, "hashtags": [],
            "source_hash": M.fingerprint(pieces), "tokens": tuple(tk)}
