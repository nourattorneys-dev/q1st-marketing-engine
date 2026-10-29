-- 0198_ext8_webhooks: one webhook per site (spec v1.1 §8) and the log of what was sent, with retries.
BEGIN;
-- the first draft kept one webhook per account; spec v1.1 has one per site. The old parts were never used.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ext_marketing' AND table_name = 'webhook_deliveries' AND column_name = 'account_id') THEN
    IF (SELECT count(*) FROM ext_marketing.webhook_deliveries) = 0 THEN DROP TABLE ext_marketing.webhook_deliveries;
    ELSE ALTER TABLE ext_marketing.webhook_deliveries RENAME TO webhook_deliveries_account_draft; END IF;
  END IF;
END $$;
ALTER TABLE ext_marketing.accounts DROP COLUMN IF EXISTS webhook_url, DROP COLUMN IF EXISTS webhook_events, DROP COLUMN IF EXISTS webhook_secret_enc;
CREATE TABLE IF NOT EXISTS ext_marketing.webhooks (
  site_id text PRIMARY KEY REFERENCES ext_marketing.sites(id) ON DELETE CASCADE,
  url text NOT NULL, events text[] NOT NULL DEFAULT '{}', secret_enc bytea NOT NULL,
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','ok','failing')), last_error text, last_delivery_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ext_marketing.webhook_deliveries (
  id text PRIMARY KEY, site_id text NOT NULL REFERENCES ext_marketing.sites(id) ON DELETE CASCADE,
  event text NOT NULL, body text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','dropped')),
  attempts int NOT NULL DEFAULT 0, last_code int, last_error text,
  next_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz);
CREATE INDEX IF NOT EXISTS webhook_deliveries_due_idx ON ext_marketing.webhook_deliveries (next_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS webhook_deliveries_site_idx ON ext_marketing.webhook_deliveries (site_id, created_at DESC);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('webhook_events', '["job.finished","post.drafted","post.published","post.failed","suggestions.created","keywords.proposed","connection.changed","performance.refresh_suggested","site.scan_finished","quota.warning","quota.exceeded","plan.changed"]',
  'Events a site''s app may ask to receive on its webhook'),
 ('webhook_timeout_seconds', '10', 'Seconds to wait for a webhook address to answer'),
 ('webhook_batch', '20', 'Most webhook deliveries sent in one pass'),
 ('webhook_keep_days', '30', 'Days a sent or failed webhook delivery stays in the log'),
 ('webhook_require_https', 'true', 'Webhook addresses must start with https://')
ON CONFLICT (key) DO NOTHING;
UPDATE ext_marketing.settings SET description = 'How many times one webhook delivery is tried before it is marked failed' WHERE key = 'webhook_max_attempts';
UPDATE ext_marketing.settings SET description = 'Seconds to wait before each new try of a failed webhook delivery, in order; the last wait repeats' WHERE key = 'webhook_backoff_seconds';
COMMIT;
