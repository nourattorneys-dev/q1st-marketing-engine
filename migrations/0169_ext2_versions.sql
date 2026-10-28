-- 0169_ext2_versions: optimistic-lock version on editable ext_marketing rows (spec: 409 version_conflict).
BEGIN;
ALTER TABLE ext_marketing.sites    ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1;
ALTER TABLE ext_marketing.accounts ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1;
ALTER TABLE ext_marketing.plans    ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1;
COMMIT;

-- account-level overrides have site_id NULL; a plain UNIQUE treats NULLs as different -> duplicates. Use an expression index.
BEGIN;
ALTER TABLE ext_marketing.quota_overrides DROP CONSTRAINT IF EXISTS quota_overrides_account_id_site_id_metric_key;
CREATE UNIQUE INDEX IF NOT EXISTS quota_overrides_uq ON ext_marketing.quota_overrides (account_id, (coalesce(site_id, '')), metric);
COMMIT;

-- each site's own app calls the API with that site's key; no client logins on our side
BEGIN;
ALTER TABLE ext_marketing.api_keys ADD COLUMN IF NOT EXISTS site_id text REFERENCES ext_marketing.sites(id) ON DELETE CASCADE;
DROP TABLE IF EXISTS ext_marketing.users;
DELETE FROM ext_marketing.settings WHERE key IN ('roles','token_ttl_minutes');
COMMIT;
