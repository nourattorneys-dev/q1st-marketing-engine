# Quantum1st Marketing Engine (ext_marketing)

The multi-site marketing API behind `https://api.q1st.io/api/v1`: keywords, topics, AI drafting, checks, photos, publishing to each site's CMS.
One FastAPI service (`nour-mkt-ext`, port 8140) with its own Postgres schema `ext_marketing`. Never touches Nour's tables.

- `ext_marketing/` – the service (deployed to `/opt/nour/src/ext_marketing`)
- `migrations/` – SQL, applied in order (copies kept in `/opt/nour/migrations`)
- `tests/` – offline tests (`*_offline_test.py`) and live tests (`*_test.sh`, run on the server)
- API contract: Marketing Portal Build Spec v1.1 (in the `q1st-marketing-app` repo, `docs/specs/`)
