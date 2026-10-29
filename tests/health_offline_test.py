"""Offline: the site health measurements, on many different pages (English, Arabic, odd markup)."""
import os, sys
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
from ext_marketing.health import measure, across
P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond
RULES = {"metaTitle": [30, 60], "metaDescription": [70, 155], "altRequired": True}
D = "A clear description of what this page offers its visitors, long enough to sit inside the band."   # 98 chars
def page(title="Employment law advice for companies in Dubai", desc=D, body="<h1>Main</h1>", head="", lang="en"):
    t = f"<title>{title}</title>" if title is not None else ""
    d = f'<meta name="description" content="{desc}">' if desc is not None else ""
    return f'<html lang="{lang}"><head>{t}{d}{head}</head><body>{body}</body></html>'
codes = lambda h, r=RULES: sorted(f["code"] for f in measure("https://x.com/p", h, r)[0])   # noqa: E731

ok("a good page has no findings", codes(page()) == [], codes(page()))
ok("no <title> -> missing_title", codes(page(title=None)) == ["missing_title"], codes(page(title=None)))
ok("an empty <title> (spaces only) -> missing_title", codes(page(title="   ")) == ["missing_title"], codes(page(title="   ")))
ok("short title -> title_length", codes(page(title="Home")) == ["title_length"], codes(page(title="Home")))
long_t = "Employment law advice for companies and employees in Dubai and Abu Dhabi"
ok("long title -> title_length, with the measured length", [f["issue"] for f in measure("u", page(title=long_t), RULES)[0]] == [f"The title is {len(long_t)} characters; the band is 30–60."])
ok("title at exactly the band edges passes", codes(page(title="x" * 30)) == [] and codes(page(title="y" * 60)) == [])
ok("whitespace inside a title is counted once", codes(page(title="Employment   law\n advice   for companies in Dubai")) == [])
ok("no description -> missing_description", codes(page(desc=None)) == ["missing_description"])
ok("description name in capitals is still read", codes(page(desc=None, head=f'<meta name="Description" content="{D}">')) == [])
ok("long description -> description_length", codes(page(desc="w " * 100)) == ["description_length"])
ar_t = "استشارات قانون العمل للشركات في دبي والإمارات"
ok("Arabic title inside the band passes (counted in characters, not bytes)", codes(page(title=ar_t, lang="ar")) == [], (len(ar_t), codes(page(title=ar_t))))
ok("no H1 -> missing_h1", codes(page(body="<h2>Only a sub heading</h2>")) == ["missing_h1"])
ok("an empty H1 counts as none", codes(page(body="<h1> </h1>")) == ["missing_h1"])
ok("two H1 -> many_h1", codes(page(body="<h1>A</h1><h1>B</h1>")) == ["many_h1"])
ok("image without alt -> img_alt with the count", (lambda f: (f["code"], f["detail"]["count"]))(measure("u", page(body='<h1>a</h1><img src="1.jpg"><img src="2.jpg" alt="">'), RULES)[0][0]) == ("img_alt", 2))
ok("decorative images (role=presentation / aria-hidden) are not counted", codes(page(body='<h1>a</h1><img src="1.jpg" role="presentation"><img src="2.jpg" aria-hidden="true"><img src="3.jpg" alt="A team meeting">')) == [])
ok("alt not required by the site's rules -> no img_alt", codes(page(body='<h1>a</h1><img src="1.jpg">'), {**RULES, "altRequired": False}) == [])
ok("noindex -> noindex", codes(page(head='<meta name="robots" content="noindex, follow">')) == ["noindex"])
ok("robots index,follow -> nothing", codes(page(head='<meta name="robots" content="index, follow">')) == [])
ok("a site with no title band set measures no length", codes(page(title="Hi"), {"metaDescription": [70, 155]}) == [])
_, rd = measure("u", page(lang="ar-AE"), RULES)
ok("what was read: title, description, language, first H1", rd == {"title": "Employment law advice for companies in Dubai", "description": D, "lang": "ar", "h1": "Main"}, rd)
pages = [{"url": "a", "read": {"title": "Same Title", "description": "one"}}, {"url": "b", "read": {"title": "same title", "description": "two"}},
         {"url": "c", "read": {"title": "Other", "description": "one"}}]
x = across(pages)
ok("duplicates across pages, ignoring case: each page names the others", sorted((u, f["code"], tuple(f["detail"]["samePages"])) for u, f in x) == [
    ("a", "duplicate_description", ("c",)), ("a", "duplicate_title", ("b",)), ("b", "duplicate_title", ("a",)), ("c", "duplicate_description", ("a",))], x)
bm = '<html><head><title>Employment law advice for companies in Dubai</title><body><div><h1>x<p>unclosed <img src=a.jpg alt="Our office"'
ok("unclosed tags still measured (no head close, no H1 close)", codes(bm) == ["missing_description"], codes(bm))
print(f"\nPASSED {P}   FAILED {F}")
sys.exit(1 if F else 0)
