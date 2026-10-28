"""Offline: the publishing cadence maths (slots, reason), the settings check and the post buttons - many different cases."""
import os, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, os.environ.get("EXT_SRC", os.path.join(os.path.dirname(__file__), "..")))
from ext_marketing import publishing as PB, posts
P = F = 0
def ok(name, cond, got=""):
    global P, F
    print(("PASS " if cond else "FAIL "), name, "" if cond else f"(got {got})"); P += bool(cond); F += not cond
DXB, LON = ZoneInfo("Asia/Dubai"), ZoneInfo("Europe/London")
C = {"autoPublish": True, "perDay": 2, "days": ["sun", "mon", "tue", "wed", "thu"], "from": "10:00", "to": "17:00", "gapMinutes": 180}
L = lambda y, mo, d, h, mi, tz=DXB: datetime(y, mo, d, h, mi, tzinfo=tz)   # noqa: E731
loc = lambda xs, tz=DXB: [x.astimezone(tz).strftime("%a %H:%M") for x in xs]   # noqa: E731
# 2026-10-21 is a Wednesday
s = PB.slots(C, "Asia/Dubai", L(2026, 10, 21, 8, 0), 0, None, 3)
ok("before hours: first slot at opening, then the gap, then next day", loc(s) == ["Wed 10:00", "Wed 13:00", "Thu 10:00"], loc(s))
s = PB.slots(C, "Asia/Dubai", L(2026, 10, 21, 11, 20), 0, None, 2)
ok("inside hours, nothing yet today: due now", s[0] == L(2026, 10, 21, 11, 20) and loc(s)[1] == "Wed 14:20", loc(s))
s = PB.slots(C, "Asia/Dubai", L(2026, 10, 21, 11, 0), 2, L(2026, 10, 21, 10, 0), 1)
ok("daily limit used: next working day", loc(s) == ["Thu 10:00"], loc(s))
s = PB.slots(C, "Asia/Dubai", L(2026, 10, 22, 16, 0), 1, L(2026, 10, 22, 15, 0), 1)
ok("gap would pass closing time: skips the weekend (fri, sat off) to Sunday", loc(s) == ["Sun 10:00"], loc(s))
s = PB.slots(C, "Asia/Dubai", L(2026, 10, 21, 12, 0), 1, L(2026, 10, 21, 11, 0), 1)
ok("last automatic one an hour ago, gap 3h: two hours from now", loc(s) == ["Wed 14:00"], loc(s))
s = PB.slots({**C, "days": ["fri"], "perDay": 1}, "Asia/Dubai", L(2026, 10, 21, 9, 0), 0, None, 3)
ok("one day a week, one per day: three Fridays", loc(s) == ["Fri 10:00"] * 3 and (s[1] - s[0]).days == 7, loc(s))
s = PB.slots(C, "Europe/London", L(2026, 10, 21, 9, 0, timezone.utc), 0, None, 1)
ok("the site's own timezone decides the hours", loc(s, LON) == ["Wed 10:00"], loc(s, LON))
ok("no days -> no slots", PB.slots({**C, "days": []}, "Asia/Dubai", L(2026, 10, 21, 9, 0), 0, None, 2) == [])
s = PB.slots({**C, "gapMinutes": 0, "perDay": 3}, "Asia/Dubai", L(2026, 10, 21, 10, 0), 0, None, 3)
ok("gap 0: several due at once up to the daily limit", loc(s) == ["Wed 10:00"] * 3, loc(s))
R = lambda now, today=0, last=None, q=1, conn=True, cfg=C: PB.reason(cfg, "Asia/Dubai", now, today, last, q, conn)   # noqa: E731
ok("reason: auto publish off", R(L(2026, 10, 21, 11, 0), cfg={**C, "autoPublish": False}) == "auto_publish_off")
ok("reason: not connected", R(L(2026, 10, 21, 11, 0), conn=False) == "not_connected")
ok("reason: queue empty", R(L(2026, 10, 21, 11, 0), q=0) == "queue_empty")
ok("reason: outside hours (evening)", R(L(2026, 10, 21, 19, 0)) == "outside_hours")
ok("reason: outside hours (weekend day)", R(L(2026, 10, 23, 11, 0)) == "outside_hours")
ok("reason: daily limit", R(L(2026, 10, 21, 15, 0), today=2, last=L(2026, 10, 21, 11, 0)) == "daily_limit")
ok("reason: waiting for the gap", R(L(2026, 10, 21, 11, 0), today=1, last=L(2026, 10, 21, 10, 0)) == "waiting_gap")
ok("reason: none - a post is due", R(L(2026, 10, 21, 14, 0), today=1, last=L(2026, 10, 21, 10, 0)) is None)
ok("settings: a good body passes", PB.check_cadence(C) == {})
e = PB.check_cadence({"autoPublish": "yes", "perDay": 0, "days": ["friday"], "from": "9am", "to": "25:00", "gapMinutes": -5})
ok("settings: each wrong field named", sorted(e) == ["autoPublish", "days", "from", "gapMinutes", "perDay", "to"], sorted(e))
ok("settings: 'to' must be after 'from'", PB.check_cadence({**C, "from": "17:00", "to": "10:00"}) == {"to": "Must be later than 'from'"})
ok("settings: true is not a number", "perDay" in PB.check_cadence({"perDay": True}))
A = lambda st, **k: posts.actions({"status": st, "kind": k.get("kind", "blog"), "hold": k.get("hold", False), "approved_at": k.get("ap")}, ["pending"], ["blog"])   # noqa: E731
ok("buttons: pending", A("pending") == ["edit", "photo", "approve", "changes", "reject", "delete"], A("pending"))
ok("buttons: approved article", A("approved", ap=1) == ["publishNow", "hold", "changes", "reject"], A("approved", ap=1))
ok("buttons: approved on hold -> resume", "resume" in A("approved", hold=True, ap=1) and "hold" not in A("approved", hold=True, ap=1))
ok("buttons: approved social post has no publish yet", "publishNow" not in A("approved", kind="social", ap=1))
ok("buttons: publish failed -> retry", A("failed", ap=1) == ["retry", "changes", "reject", "delete"], A("failed", ap=1))
ok("buttons: draft failed -> ask again, no publish", A("failed") == ["changes", "delete"], A("failed"))
ok("buttons: published / publishing -> none", A("published", ap=1) == [] and A("publishing", ap=1) == [], A("published", ap=1))
print("-----"); print(f"PASSED {P}   FAILED {F}")
