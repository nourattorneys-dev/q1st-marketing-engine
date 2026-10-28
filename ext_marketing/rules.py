"""The article rule set: default (a setting) overridden by the site's own rules (site_settings 'rules'), plus the site's
avoid list. Checks only MEASURE the text and report facts to the reviewer; the model writes, a person decides.
Levels: critical (blocks Approve unless a reason is given), warn (reported), human (a checklist item, never auto-passed)."""
from __future__ import annotations
import html as _h, re

from . import db, profile

S = db.SCHEMA


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


async def for_site(site_id: str) -> tuple[dict, int]:
    base = (await db.settings(["default_rules"])).get("default_rules") or {}
    r = await db.pool().fetchrow(f"SELECT value, version FROM {S}.site_settings WHERE site_id = $1 AND grp = 'rules'", site_id)
    seo, _ = await profile.seo(site_id)
    rules = _merge(base, (r["value"] if r else {}) or {})
    rules["avoidPhrases"] = list(dict.fromkeys((rules.get("avoidPhrases") or []) + (seo.get("avoid") or [])))
    s = await db.settings(["language_scripts", "language_min_share", "language_min_letters"])
    rules["_scripts"], rules["_minShare"], rules["_minLetters"] = s.get("language_scripts") or {}, s.get("language_min_share"), s.get("language_min_letters")
    return rules, (r["version"] if r else 0)


def brief(rules: dict, kind: str, cta: str | None) -> str:
    """The rules as plain instructions for the writer (the same numbers the checks measure)."""
    w = (rules.get(kind) or {}).get("words") or []
    lines = []
    if w:
        lines.append(f"Length: {w[0]}-{w[1]} words.")
    if kind == "blog":
        iw = rules.get("introWords") or []
        if iw:
            lines.append(f"Open with a {iw[0]}-{iw[1]} word paragraph that answers the reader's question directly.")
        if rules.get("h2Min"):
            lines.append(f"Use at least {rules['h2Min']} section headings (## in Markdown), each phrased the way a reader would search.")
        fq = rules.get("faq") or {}
        if fq.get("required"):
            c = fq.get("count") or [3, 6]
            lines.append(f"End with a section '## FAQ' of {c[0]}-{c[1]} questions as ### headings, each answered briefly.")
        if rules.get("keywordInIntro"):
            lines.append("Use the main keyword naturally in the opening paragraph.")
    if rules.get("avoidPhrases"):
        lines.append("Never use these words or claims: " + "; ".join(rules["avoidPhrases"]) + ".")
    if cta:
        lines.append(f"Close with this call to action, in the article's language: {cta}")
    return "\n".join(lines)


def clean_html(h: str) -> str:
    """No scripts, frames, embedded objects or inline event handlers in HTML that goes to a client's website."""
    h = re.sub(r"<\s*(script|iframe|object|embed|style)\b[^>]*>.*?<\s*/\s*\1\s*>", "", h or "", flags=re.I | re.S)
    h = re.sub(r"<\s*(script|iframe|object|embed|style)\b[^>]*/?>", "", h, flags=re.I)
    h = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    return re.sub(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r"\1=\2#\2", h, flags=re.I)


def language_share(text: str, lang: str | None, rules: dict) -> float | None:
    """Share of letters written in the post's own language's script (None when the script is not configured)."""
    pat = (rules.get("_scripts") or {}).get(lang or "")
    letters = re.findall(r"[^\W\d_]", text or "")
    if not pat or len(letters) < int(rules.get("_minLetters") or 20):
        return None
    return len(re.findall(pat, "".join(letters))) / len(letters)


def foreign_words(text: str, lang: str | None, rules: dict, limit: int | None = 8) -> list:
    """Ordinary lowercase words written in another script inside the text (e.g. 'automate' in Arabic). Names, brands and acronyms
    (a capital letter or a digit: Quantum1st, AI, WhatsApp) are left out - the reviewer sees the list and decides."""
    pat = (rules.get("_scripts") or {}).get(lang or "")
    if not pat:
        return []
    own = re.compile(pat)
    out = []
    for w in re.findall(r"[^\W\d_][\w'-]*", text or ""):
        if own.search(w) or any(c.isupper() or c.isdigit() for c in w) or len(w) < 3:
            continue
        if w.lower() not in out:
            out.append(w.lower())
    return out[:limit] if limit else out


def text_of(html_body: str) -> str:
    return _h.unescape(re.sub(r"<[^>]+>", " ", html_body or ""))


def words(t: str) -> int:
    return len(re.findall(r"\w+", t or ""))


def check(post: dict, rules: dict) -> list[dict]:
    out: list = []
    add = lambda lv, ref, msg: out.append({"level": lv, "ref": ref, "message": msg})   # noqa: E731
    kind, body = post.get("kind"), post.get("content") or ""
    txt = text_of(body) if kind == "blog" else body
    n = words(txt)
    w = (rules.get(kind) or {}).get("words") or []
    if w:
        if n < w[0]:
            add("critical", "length", f"{n} words; the site's minimum is {w[0]}")
        elif n > w[1]:
            add("warn", "length", f"{n} words; the site's maximum is {w[1]}")
    lg, ms = post.get("language"), rules.get("_minShare")
    fields = [("language", "the text", txt), ("language", "the title", post.get("title"))]
    if kind == "blog":
        fields += [("language", "the meta title", post.get("meta_title")), ("language", "the meta description", post.get("meta_description")),
                   ("language", "the main keyword", (post.get("keywords") or [""])[0])]
    for ref, label, val in fields:                                      # every field on its own: a long body cannot hide a wrong-language title
        share = language_share(val or "", lg, rules)
        if share is not None and ms is not None and share < float(ms):
            add("critical", ref, f"{label} is not in {lg} ({round(share * 100)}% of its letters); write it in {lg}")
    stray = foreign_words(txt, lg, rules)
    if stray:
        add("warn", "mixed", f"words in another script inside the {lg} text: {', '.join(stray)} - write them in {lg}")
    kw = (post.get("keywords") or [None])[0]
    low = txt.lower()
    for ph in rules.get("avoidPhrases") or []:
        if ph and re.search(r"(?<!\w)" + re.escape(ph.lower()) + r"(?!\w)", low + " " + (post.get("title") or "").lower()):
            add("critical", "avoid", f"uses '{ph}', which the site never says")
    if kind == "blog":
        if not (post.get("title") or "").strip():
            add("critical", "title", "no title")
        mt, md = post.get("meta_title") or "", post.get("meta_description") or ""
        for label, val, band in (("meta title", mt, rules.get("metaTitle")), ("meta description", md, rules.get("metaDescription"))):
            if not val.strip():
                add("critical", label.replace(" ", ""), f"no {label}")
            elif band and len(val) > band[1]:
                add("critical", label.replace(" ", ""), f"{label} is {len(val)} characters; the maximum is {band[1]}")
            elif band and len(val) < band[0]:
                add("warn", label.replace(" ", ""), f"{label} is {len(val)} characters; aim for at least {band[0]}")
        sw = rules.get("slugWords") or []
        slug_n = len([x for x in (post.get("slug") or "").split("-") if x])
        if not slug_n:
            add("critical", "slug", "no URL slug")
        elif sw and not (sw[0] <= slug_n <= sw[1]):
            add("warn", "slug", f"URL slug has {slug_n} words; aim for {sw[0]}-{sw[1]}")
        h2 = len(re.findall(r"<h2\b", body))
        if rules.get("h2Min") and h2 < rules["h2Min"]:
            add("warn", "headings", f"{h2} section headings; the site asks for at least {rules['h2Min']}")
        fq = rules.get("faq") or {}
        if fq.get("required") and not re.search(r"<h2[^>]*>\s*(faq|frequently asked|الأسئلة الشائعة|أسئلة)", body, re.I):
            add("warn", "faq", "no FAQ section")
        first = re.search(r"<p>(.*?)</p>", body, re.S)
        iw = rules.get("introWords") or []
        if first and iw:
            fw = words(text_of(first.group(1)))
            if not (iw[0] <= fw <= iw[1]):
                add("warn", "intro", f"opening paragraph is {fw} words; the site asks for {iw[0]}-{iw[1]}")
        if kw:
            if rules.get("keywordInTitle") and kw.lower() not in (post.get("title") or "").lower():
                add("warn", "keyword", f"the main keyword '{kw}' is not in the title")
            if rules.get("keywordInIntro") and first and kw.lower() not in text_of(first.group(1)).lower():
                add("warn", "keyword", f"the main keyword '{kw}' is not in the opening paragraph")
    img = post.get("image") or {}
    if not img:
        add("warn", "photo", "no photo")
    elif rules.get("altRequired") and not (img.get("alt") or "").strip():
        add("warn", "photo", "the photo has no alt text")
    for h in rules.get("humanChecks") or []:
        add("human", "review", h)
    order = {"critical": 0, "warn": 1, "human": 2}
    return sorted(out, key=lambda f: order[f["level"]])


async def score(findings: list) -> int:
    wt = (await db.settings(["seo_score_weights"])).get("seo_score_weights") or {}
    return max(0, 100 - sum(int(wt.get(f["level"], 0)) for f in findings))
