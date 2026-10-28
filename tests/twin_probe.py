"""Diagnostic: runs the twin translation on a sample article with the REAL model and prints, per call, what was sent and what
came back (ids asked, ids answered, the first characters of the raw answer). Usage: python twin_probe.py"""
import asyncio, json, os, sys
sys.path.insert(0, os.environ.get("EXT_SRC", "/opt/nour/src"))
from ext_marketing import db, writer, llm, rules as R

HTML = """<p>End-of-service gratuity is a payment an employer in the UAE owes when employment ends, under <strong>Federal Decree-Law No. 33 of 2021</strong>.</p>
<h2>How is gratuity calculated?</h2><p>It is based on the <strong>basic salary</strong> and the years of service.</p>
<ul><li>21 days of basic pay for each of the first five years</li><li>30 days for each year after that</li></ul>
<h2>FAQ</h2><h3>Does a part-time worker get gratuity?</h3><p>Yes, in proportion to the hours worked.</p>
<p>Read <a href="/contact">our guide</a> or book a consultation.</p>"""


async def main():
    await db.open_pool()
    real = llm.chat
    async def spy(messages, max_tokens, temperature=0.3):
        text, a, b = await real(messages, max_tokens=max_tokens, temperature=temperature)
        try:
            sent = json.loads(messages[1]["content"]).get("pieces")
        except Exception:                                               # noqa: BLE001
            sent = None
        try:
            got = llm.json_from(text).get("pieces")
        except Exception as e:                                          # noqa: BLE001
            got = f"<no JSON: {e}>"
        print(json.dumps({"sentIds": list(sent) if isinstance(sent, dict) else None,
                          "answerType": type(got).__name__, "answerIds": list(got) if isinstance(got, dict) else (len(got) if isinstance(got, list) else got),
                          "maxTokens": max_tokens, "tokensOut": b, "raw": text[:400], "rawChars": len(text)}, ensure_ascii=False))
        return text, a, b
    llm.chat = spy
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {db.SCHEMA}.sites LIMIT 1") or {"id": "none", "name": "Probe", "url": "https://example.com",
                                                                                           "category": "Legal", "profile": {}, "languages": ["en", "ar"], "timezone": "Asia/Dubai", "default_language": "en"})
    async def fr(sid): return ({**(await db.settings(["default_rules"]))["default_rules"], "_scripts": {}, "_minShare": None, "_minLetters": 1}, 0)
    R.for_site = fr
    src = {"language": "en", "content": HTML, "title": "UAE end-of-service gratuity", "subtitle": "How it is calculated.",
           "meta_title": "UAE gratuity: how it is calculated", "meta_description": "What an employer owes at the end of employment.",
           "keywords": ["end of service gratuity uae"], "slug": "uae-gratuity"}
    try:
        out = await writer.translate(site, src, {"language": "ar", "keywords": [], "topic": "t"})
        print("RESULT ok:", out["title"], "|", out["content"][:300])
    except Exception as e:                                              # noqa: BLE001
        print("RESULT failed:", e)
    await db.close_pool()

asyncio.run(main())
