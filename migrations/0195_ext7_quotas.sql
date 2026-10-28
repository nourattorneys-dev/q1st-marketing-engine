-- 0195_ext7_quotas: plan and usage for the site's app, upgrade requests to our team, hard limits enforced.
BEGIN;
CREATE TABLE IF NOT EXISTS ext_marketing.upgrade_requests (
  id text PRIMARY KEY, account_id text NOT NULL REFERENCES ext_marketing.accounts(id) ON DELETE CASCADE,
  site_id text REFERENCES ext_marketing.sites(id) ON DELETE SET NULL, metric text REFERENCES ext_marketing.metrics(key),
  note text, requested_by text, status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','declined')),
  handled_by text, handled_at timestamptz, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS upgrade_requests_open_idx ON ext_marketing.upgrade_requests (status, created_at);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('usage_max_months', '24', 'Most months one usage report may cover')
ON CONFLICT (key) DO NOTHING;
COMMIT;
