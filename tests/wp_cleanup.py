"""Deletes from the site's WordPress every post this test site published (drafts), so the live test leaves nothing behind.
Usage: python wp_cleanup.py SITE_ID. Prints how many were removed."""
import asyncio, os, sys
sys.path.insert(0, os.environ.get("EXT_SRC", "/opt/nour/src"))
from ext_marketing import db, security, website


async def main(site_id: str) -> int:
    await db.open_pool()
    row = await website.row(site_id)
    t, secret = row["config"], security.decrypt(row["secret_enc"])
    ids = [r["eid"] for r in await db.pool().fetch(
        f"SELECT DISTINCT published->>'externalId' AS eid FROM {db.SCHEMA}.posts WHERE site_id = $1 AND published->>'target' = 'wordpress'", site_id) if r["eid"]]
    for i in ids:
        await website.ADAPTERS["wordpress"]["remove"](t, secret, i)
    await db.close_pool()
    return len(ids)


print(asyncio.run(main(sys.argv[1])))
