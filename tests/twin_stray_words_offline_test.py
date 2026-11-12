"""Offline: a translation that leaves ordinary words in the source language is asked again for exactly those pieces, with
the words named; names and acronyms are not; a worse answer is never kept. Different articles and languages."""
import asyncio, json, os, re, sys
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
from ext_marketing import db, llm, profile, rules as R, writer as W
P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond

SET = {"twin_segments_per_call": 40, "twin_max_tokens": 2000, "twin_temperature": 0.2, "twin_retries": 2, "twin_punctuation": {},
       "meta_retry": 0, "writer_max_tokens_meta": 500}
async def settings(keys=None): return {k: v for k, v in SET.items() if not keys or k in keys}
RULES = {"_scripts": {"ar": "[؀-ۿ]", "en": "[A-Za-z]"}, "metaTitle": None, "metaDescription": None}
async def for_site(_): return RULES, None
async def brief(_): return ""
db.settings, R.for_site, profile.brief = settings, for_site, brief

def run(source_html, lang, first, fixes, src_lang="en"):
    """first: translation per piece text on the first pass; fixes: list of {piece text: answer} per retry round."""
    calls = {"retry": [], "n": 0}
    async def chat(messages, max_tokens=0, temperature=0):
        sysm, user = messages[0]["content"], json.loads(messages[1]["content"])
        if "pieces" not in user:                                      # the short fields
            return json.dumps({"title": "عنوان" if lang == "ar" else "Title", "keywords": []}), 1, 1
        if "left words untranslated" in sysm:
            rnd = len(calls["retry"]); calls["retry"].append((sysm, user["pieces"]))
            fx = fixes[rnd] if rnd < len(fixes) else {}
            return json.dumps({"pieces": {k: fx.get(v, first[v]) for k, v in user["pieces"].items()}}, ensure_ascii=False), 1, 1
        return json.dumps({"pieces": {k: first[v] for k, v in user["pieces"].items()}}, ensure_ascii=False), 1, 1
    llm.chat = chat
    out = asyncio.run(W.translate({"id": "s"}, {"language": src_lang, "content": source_html, "title": "T"}, {"language": lang, "keywords": []}))
    return out, calls["retry"]

# 1) legal article, Arabic: two pieces leak English, one keeps a name (Dubai Courts) - only the two are asked again, words named
src = "<p>Choose the mainland or a free zone.</p><p>Disputes go to Dubai Courts.</p><ul><li>Align the contract with your intentions.</li></ul>"
first = {"Choose the mainland or a free zone.": "اختر mainland أو منطقة حرة.", "Disputes go to Dubai Courts.": "تُحال النزاعات إلى Dubai Courts.",
         "Align the contract with your intentions.": "اجعل العقد aligns مع intentions الخاصة بك."}
fix = {"Choose the mainland or a free zone.": "اختر البر الرئيسي أو منطقة حرة.", "Align the contract with your intentions.": "اجعل العقد متوافقًا مع نواياك."}
out, retries = run(src, "ar", first, [fix])
txt = R.text_of(out["content"])
ok("legal/ar: leaked words are gone", not R.foreign_words(txt, "ar", RULES, None), R.foreign_words(txt, "ar", RULES, None))
ok("legal/ar: one retry, only the two leaking pieces", len(retries) == 1 and len(retries[0][1]) == 2, retries)
ok("legal/ar: the retry names the exact words", "mainland" in retries[0][0] and "aligns" in retries[0][0] and "intentions" in retries[0][0], retries[0][0][-200:])
ok("legal/ar: names stay (Dubai Courts not asked again)", "Dubai Courts" in txt and all("Dubai" not in v for v in retries[0][1].values()), txt)
ok("legal/ar: structure kept", out["content"].count("<p>") == 2 and "<li>" in out["content"], out["content"])

# 2) travel article: the model gets worse on the retry - the better first answer is kept
src2 = "<h2>Best beaches</h2><p>Swim early in the morning.</p>"
first2 = {"Best beaches": "أفضل الشواطئ", "Swim early in the morning.": "اسبح early في الصباح."}
worse = {"Swim early in the morning.": "swim early in the morning please"}
out2, r2 = run(src2, "ar", first2, [worse, worse])
ok("travel/ar: a worse answer is not kept", "اسبح" in R.text_of(out2["content"]), out2["content"])
ok("travel/ar: tried as many rounds as allowed", len(r2) == SET["twin_retries"], len(r2))

# 3) the other direction (Arabic -> English) with Arabic left in: same rule
src3 = "<p>الضرائب على الشركات</p>"
first3 = {"الضرائب على الشركات": "Corporate الضرائب"}
fix3 = {"الضرائب على الشركات": "Corporate taxes"}
out3, r3 = run(src3, "en", first3, [fix3], src_lang="ar")
ok("tax/en: Arabic left in English is asked again and fixed", R.text_of(out3["content"]).strip() == "Corporate taxes" and len(r3) == 1, (out3["content"], r3))

# 4) a clean translation makes no extra call
out4, r4 = run("<p>Hello world.</p>", "ar", {"Hello world.": "مرحبا بالعالم."}, [])
ok("clean: no extra call", r4 == [], r4)
print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)
