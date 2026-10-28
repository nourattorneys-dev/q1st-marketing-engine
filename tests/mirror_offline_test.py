"""Offline: an article's pieces and structure, rebuilding the twin from translated pieces, and the translation step's measured
retry (the model is mocked). Different shapes of article on purpose: inline bold/links, lists, FAQ, tables, entities."""
import asyncio, json, os, sys
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
from ext_marketing import mirror as M, writer, db, llm, profile, rules as R
P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond
H1 = "<h2>What is <strong>VAT</strong>?</h2>\n<p>Tax &amp; you: read <a href=\"/vat\">our guide</a>.</p><ul><li>One</li><li>Two</li></ul>"
H2 = "<h2>FAQ</h2><h3>Who pays?</h3><p>Everyone.</p><table><tr><th>Rate</th><td>5%</td></tr></table>"
s1 = M.segments(H1)
ok("pieces in order, inline bold and links split", s1 == ["What is", "VAT", "?", "Tax & you: read", "our guide", ".", "One", "Two"], s1)
ok("FAQ and table cells are pieces too", M.segments(H2) == ["FAQ", "Who pays?", "Everyone.", "Rate", "5%"], M.segments(H2))
ar = ["ما هي", "ضريبة القيمة المضافة", "؟", "الضريبة وأنت: اقرأ", "دليلنا", ".", "واحد", "اثنان"]
r1 = M.rebuild(H1, ar)
ok("rebuilt twin has the same structure", M.shape(r1) == M.shape(H1))
ok("rebuilt twin keeps links and their addresses", '<a href="/vat">دليلنا</a>' in r1, r1)
ok("rebuilt twin carries the translated pieces", M.segments(r1) == ar, M.segments(r1))
ok("a translated piece with & is written safely", "&amp;" in M.rebuild("<p>x</p>", ["أ & ب"]))
ok("an extra paragraph changes the structure", M.shape(r1 + "<p>extra</p>") != M.shape(H1))
ok("a word changed inside a piece keeps the structure", M.shape(H1.replace("One", "First")) == M.shape(H1))
ok("...but changes the source fingerprint", M.fingerprint(M.segments(H1.replace("One", "First"))) != M.fingerprint(s1))
ok("a removed bold changes the structure", M.shape(H1.replace("<strong>VAT</strong>", "VAT")) != M.shape(H1))
ok("empty article: no pieces", M.segments("") == [] and M.rebuild("", []) == "")

# the translation step with a mocked model
SET = {"twin_segments_per_call": 3, "twin_max_tokens": 100, "twin_temperature": 0.2, "twin_retries": 2, "twin_punctuation": {"ar": {"?": "؟"}}, "meta_retry": 1, "writer_max_tokens_meta": 100}
async def fs(keys=None): return {k: SET.get(k) for k in (keys or SET)}
db.settings = fs
async def fr(sid): return ({"metaTitle": [10, 60], "metaDescription": [20, 160], "_scripts": {"ar": "[؀-ۿ]"}, "_minShare": 0.6, "_minLetters": 1}, 0)
R.for_site = fr
async def brief(site): return "Website: x"
profile.brief = brief
CALLS = {"pieces": 0, "fields": 0}
MODE = {}
async def chat(messages, max_tokens, temperature=0.3):
    sysm, user = messages[0]["content"], json.loads(messages[1]["content"])
    if "pieces with ids" in sysm:
        CALLS["pieces"] += 1
        CALLS.setdefault("sent", []).extend(user["pieces"].values())
        keys = list(user["pieces"])
        if MODE.get("drop_once") and "were missing" not in sysm:
            keys = keys[:-1]                                            # one id left out -> only that id is asked again
        if MODE.get("unwrapped"):
            return json.dumps({k: f"ترجمة {k}" for k in keys}), 1, 1     # the ids without the "pieces" wrapper (seen from the real model)
        if MODE.get("as_list"):
            return json.dumps({"pieces": [f"ترجمة {k}" for k in keys]}), 1, 1
        return json.dumps({"pieces": {k: f"ترجمة {k}" for k in keys}}), 1, 1
    CALLS["fields"] += 1
    return json.dumps({"title": "عنوان المقال", "subtitle": "سطر", "metaTitle": "عنوان ميتا مناسب الطول",
                       "metaDescription": "وصف ميتا مناسب الطول لهذه المقالة", "keywords": ["ضريبة القيمة المضافة"]}), 1, 1
llm.chat = chat
async def main():
    src = {"language": "en", "content": H1, "title": "What is VAT", "subtitle": "s", "meta_title": "m", "meta_description": "d", "keywords": ["vat"], "slug": "vat"}
    tw = {"language": "ar", "keywords": [], "topic": "t"}
    out = await writer.translate({"id": "s"}, src, tw)
    ok("only pieces with letters go to the model (6 of 8), in batches of 3", CALLS["pieces"] == 2 and len(CALLS["sent"]) == 6
       and "?" not in CALLS["sent"] and "." not in CALLS["sent"], CALLS)
    ok("a punctuation-only piece takes the target language's punctuation", "؟" in out["content"], out["content"])
    ok("twin structure equals the source", M.shape(out["content"]) == M.shape(H1))
    ok("twin remembers which source it was made from", out["source_hash"] == M.fingerprint(s1))
    ok("short fields translated, keywords in the twin's language", (out["title"], out["keywords"]) == ("عنوان المقال", ["ضريبة القيمة المضافة"]), out)
    CALLS.clear(); CALLS.update(pieces=0, fields=0); MODE["drop_once"] = True
    out = await writer.translate({"id": "s"}, src, tw)
    ok("a left-out piece is asked for again, alone", CALLS["pieces"] == 4 and len(CALLS["sent"]) == 8 and M.shape(out["content"]) == M.shape(H1), CALLS)
    MODE.clear(); MODE["unwrapped"] = True; CALLS.clear(); CALLS.update(pieces=0, fields=0)
    out = await writer.translate({"id": "s"}, src, tw)
    ok("an answer with the ids but without the wrapper is used", M.shape(out["content"]) == M.shape(H1) and CALLS["pieces"] == 2, CALLS)
    MODE.clear(); MODE["as_list"] = True; CALLS.clear(); CALLS.update(pieces=0, fields=0)
    out = await writer.translate({"id": "s"}, src, tw)
    ok("an answer given as a list of the right length is still used", M.shape(out["content"]) == M.shape(H1) and CALLS["pieces"] == 2, CALLS)
    MODE.clear()
    async def never(messages, max_tokens, temperature=0.3):
        return json.dumps({"pieces": {}}), 1, 1
    llm.chat = never
    try:
        await writer.translate({"id": "s"}, src, tw); ok("a translation that keeps leaving pieces out fails (no broken twin)", False)
    except ValueError:
        ok("a translation that keeps leaving pieces out fails (no broken twin)", True)
asyncio.run(main())
print("-----"); print(f"PASSED {P}   FAILED {F}")
