# Staff portal: Marketing clients section

Our own admin pages for the marketing engine, inside Nour's staff portal (`/opt/nour/src/staff_portal`). They hold no data:
every read and write goes to the engine's `/admin/v1` API with the admin key, naming the signed-in staff user.

| File here | Deployed to |
|---|---|
| `routers/mkt_clients.py` | `/opt/nour/src/staff_portal/routers/mkt_clients.py` |
| `strings_mktclients.py` | `/opt/nour/src/staff_portal/strings_mktclients.py` |
| `templates/mktc_*.html` | `/opt/nour/src/staff_portal/templates/` |
| `static/noreload.js` | `/opt/nour/src/staff_portal/static/noreload.js` (shared by the whole portal) |

`wiring.txt`: where the portal's own files (`main.py`, `shell.py`) register this section; those files are Nour's and are not kept here.
