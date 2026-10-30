-- 0204_p2_client_brand: a client's own look on the client dashboard: {"color": "#rrggbb", "logoUrl": "https://..."}.
BEGIN;
ALTER TABLE ext_marketing.accounts ADD COLUMN IF NOT EXISTS brand jsonb NOT NULL DEFAULT '{}'::jsonb;
COMMIT;
