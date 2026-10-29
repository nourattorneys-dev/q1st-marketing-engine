"""Read-only probe: reads the site twice the way a scan does and reports which pages it read, and how one page measures.
Run (on the server, as the service runs): see tests/README or the command given with it. Writes nothing."""
import asyncio, sys
from ext_marketing import db, health, rules as R, sitepages

async def main(site_id, needle):
    await db.open_pool()
    site = dict(await db.pool().fetchrow(f"SELECT * FROM {db.SCHEMA}.sites WHERE id = $1", site_id))
    n = int((await db.settings(["health_max_pages"]))["health_max_pages"])
    rules, _ = await R.for_site(site_id)
    runs = []
    for i in (1, 2):
        pages, problems, graph = await sitepages.read(site["url"], n, raw=True)
        urls = [p["url"] for p in pages]
        runs.append(urls)
        hit = [p for p in pages if needle in p["url"]]
        print(f"run {i}: {len(pages)} pages, {len(problems)} problems, {len(graph['unvisited'])} links left unread; '{needle}' read: {[p['url'] for p in hit]}")
        for p in hit:
            f, rd = health.measure(p["url"], p["html"], rules)
            print(f"   measured: {[x['code'] for x in f]}  title={len(rd['title'])}  desc={len(rd['description'])}")
    a, b = runs
    print("same pages both runs:", set(a) == set(b), "| same order:", a == b)
    print("only in run 1:", sorted(set(a) - set(b))[:10]); print("only in run 2:", sorted(set(b) - set(a))[:10])
    rows = await db.pool().fetch(f"SELECT url, code, status FROM {db.SCHEMA}.site_findings WHERE site_id = $1 AND url LIKE $2", site_id, f"%{needle}%")
    print("stored findings for it:", [tuple(r) for r in rows])

asyncio.run(main(sys.argv[1], sys.argv[2]))
