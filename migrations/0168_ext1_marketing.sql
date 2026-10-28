-- 0168_ext1_marketing: separate schema for OUTSIDE client sites (multi-site marketing).
-- Nour's own marketing tables (public.marketing_approvals, seo_keywords, seo_settings) are NOT touched.
-- Every limit/number lives in ext_marketing.settings or plan_quotas -- nothing hard-coded in code.
BEGIN;
CREATE SCHEMA IF NOT EXISTS ext_marketing;
SET search_path = ext_marketing;

CREATE TABLE IF NOT EXISTS settings (            -- system-wide knobs, editable by admins
  key text PRIMARY KEY, value jsonb NOT NULL, description text,
  updated_by text, updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS plans (
  id text PRIMARY KEY, name text NOT NULL, price numeric(12,2), currency text,
  billing_period text NOT NULL DEFAULT 'month' CHECK (billing_period IN ('month','year')),
  queue_priority int NOT NULL DEFAULT 0, active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS metrics (             -- what can be metered; UI reads this list
  key text PRIMARY KEY, label text NOT NULL, unit text NOT NULL DEFAULT 'count', active boolean NOT NULL DEFAULT true);

CREATE TABLE IF NOT EXISTS plan_quotas (
  plan_id text REFERENCES plans(id) ON DELETE CASCADE, metric text REFERENCES metrics(key),
  limit_value bigint,                            -- NULL = unlimited
  mode text NOT NULL DEFAULT 'hard' CHECK (mode IN ('hard','soft')),
  warn_at numeric(4,3),                          -- NULL = use settings.quota_warn_at
  PRIMARY KEY (plan_id, metric));

CREATE TABLE IF NOT EXISTS accounts (
  id text PRIMARY KEY, name text NOT NULL, plan_id text REFERENCES plans(id),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','closed')),
  price_override numeric(12,2), notes text,
  webhook_url text, webhook_events text[], webhook_secret_enc bytea,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS quota_overrides (     -- raise/lower any limit per account or per site
  id bigserial PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  site_id text, metric text NOT NULL REFERENCES metrics(key),
  limit_value bigint, mode text CHECK (mode IN ('hard','soft')), warn_at numeric(4,3),
  reason text, set_by text, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, site_id, metric));

CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  email text NOT NULL, name text, role text NOT NULL CHECK (role IN ('admin','publisher','reviewer')),
  password_hash text, active boolean NOT NULL DEFAULT true, last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS users_email_uq ON users (lower(email));

CREATE TABLE IF NOT EXISTS api_keys (
  id text PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  key_hash text NOT NULL UNIQUE, key_prefix text NOT NULL, label text,
  created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz, revoked_at timestamptz);

CREATE TABLE IF NOT EXISTS sites (
  id text PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  name text NOT NULL, url text NOT NULL, category text, timezone text NOT NULL,
  languages text[] NOT NULL, default_language text NOT NULL,
  profile jsonb NOT NULL DEFAULT '{}',           -- tone, audience, locations, avoid... fed to the AI
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz);
CREATE INDEX IF NOT EXISTS sites_account_ix ON sites (account_id) WHERE deleted_at IS NULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'quota_overrides_site_fk') THEN
    ALTER TABLE quota_overrides ADD CONSTRAINT quota_overrides_site_fk FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE;
  END IF; END $$;

CREATE TABLE IF NOT EXISTS site_settings (       -- seo / publishing / rules, one json per group
  site_id text REFERENCES sites(id) ON DELETE CASCADE, grp text NOT NULL CHECK (grp IN ('seo','publishing','rules')),
  value jsonb NOT NULL DEFAULT '{}', version int NOT NULL DEFAULT 1,
  updated_by text, updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (site_id, grp));

CREATE TABLE IF NOT EXISTS site_connections (    -- strapi / linkedin / instagram per site
  site_id text REFERENCES sites(id) ON DELETE CASCADE, type text NOT NULL,
  config jsonb NOT NULL DEFAULT '{}',            -- non-secret: url, collection, fieldMap...
  secret_enc bytea,                              -- token, encrypted by the service (never returned)
  status text NOT NULL DEFAULT 'missing' CHECK (status IN ('ok','expiring','error','missing')),
  expires_at timestamptz, last_error text, last_checked_at timestamptz, version int NOT NULL DEFAULT 1,
  PRIMARY KEY (site_id, type));

CREATE TABLE IF NOT EXISTS keywords (
  id text PRIMARY KEY, site_id text NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  phrase text NOT NULL, language text NOT NULL,
  source text NOT NULL CHECK (source IN ('ai','person','search_console')),
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','approved','covered','dismissed','off_strategy')),
  rationale text, uses int NOT NULL DEFAULT 0, added_by text,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS keywords_uq ON keywords (site_id, lower(phrase), language);
CREATE INDEX IF NOT EXISTS keywords_status_ix ON keywords (site_id, status);

CREATE TABLE IF NOT EXISTS suggestions (
  id text PRIMARY KEY, site_id text NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  title text NOT NULL, kind text NOT NULL CHECK (kind IN ('blog','social')), language text NOT NULL,
  keywords text[] NOT NULL DEFAULT '{}', why text,
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','drafting','dismissed')),
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS suggestions_ix ON suggestions (site_id, status);

CREATE TABLE IF NOT EXISTS posts (
  id text PRIMARY KEY, site_id text NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('blog','social')), platform text NOT NULL, language text NOT NULL,
  pair_id text, suggestion_id text REFERENCES suggestions(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'drafting'
    CHECK (status IN ('drafting','pending','approved','published','failed','rejected')),
  topic text, tone text, title text, subtitle text, slug text, meta_title text, meta_description text,
  keywords text[] NOT NULL DEFAULT '{}', content text, hashtags text[] NOT NULL DEFAULT '{}', section text,
  image jsonb, photo_idea text, sources jsonb NOT NULL DEFAULT '[]',
  findings jsonb NOT NULL DEFAULT '[]', seo jsonb, review_note text, override_reason text, error text,
  hold boolean NOT NULL DEFAULT false, pending_since timestamptz, published jsonb,
  requested_by text, reviewed_by text, locked_by text, locked_at timestamptz,
  version int NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS posts_status_ix ON posts (site_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS posts_pair_ix ON posts (pair_id) WHERE pair_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS site_findings (
  id text PRIMARY KEY, site_id text NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  url text NOT NULL, level text NOT NULL CHECK (level IN ('critical','warn','info')),
  issue text NOT NULL, fix text,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','prepared','applied','dismissed')),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS jobs (
  id text PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  site_id text REFERENCES sites(id) ON DELETE CASCADE, kind text NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  priority int NOT NULL DEFAULT 0, progress real NOT NULL DEFAULT 0,
  payload jsonb NOT NULL DEFAULT '{}', result jsonb, error text, attempts int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz);
CREATE INDEX IF NOT EXISTS jobs_queue_ix ON jobs (status, priority DESC, created_at) WHERE status IN ('queued','running');

CREATE TABLE IF NOT EXISTS usage_events (        -- the meter: every billable action + its cost
  id bigserial PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  site_id text REFERENCES sites(id) ON DELETE SET NULL, metric text NOT NULL REFERENCES metrics(key),
  qty bigint NOT NULL DEFAULT 1, tokens_in bigint NOT NULL DEFAULT 0, tokens_out bigint NOT NULL DEFAULT 0,
  gpu_ms bigint NOT NULL DEFAULT 0, cost_estimate numeric(14,6), over_quota boolean NOT NULL DEFAULT false,
  job_id text, at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS usage_period_ix ON usage_events (account_id, at);
CREATE INDEX IF NOT EXISTS usage_site_ix ON usage_events (site_id, metric, at);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id bigserial PRIMARY KEY, account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  event_id text NOT NULL UNIQUE, event text NOT NULL, payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
  attempts int NOT NULL DEFAULT 0, next_at timestamptz NOT NULL DEFAULT now(), last_error text,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS webhook_due_ix ON webhook_deliveries (next_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS audit_log (
  id bigserial PRIMARY KEY, account_id text, site_id text, actor text NOT NULL,
  action text NOT NULL, target text, detail jsonb, at timestamptz NOT NULL DEFAULT now());

-- seed: metered items (labels editable; admin can add more)
INSERT INTO metrics (key,label) VALUES
 ('articles','Articles drafted'),('socialPosts','Social posts drafted'),('photos','Photos picked or uploaded'),
 ('publishes','Publishes'),('siteScans','Site scans'),('keywordProposals','Keyword proposal runs'),
 ('aiTokens','AI tokens'),('sites','Sites')
ON CONFLICT (key) DO NOTHING;
UPDATE metrics SET unit='tokens' WHERE key='aiTokens';

-- seed: system knobs (starting values, all editable by admins; code reads them, never hard-codes)
INSERT INTO settings (key,value,description) VALUES
 ('quota_warn_at','0.8','Share of a limit at which a quota warning is sent'),
 ('queue_max_concurrent','1','Client AI jobs running at the same time (Nour is never queued here)'),
 ('queue_poll_seconds','2','How often the worker checks for queued jobs'),
 ('job_max_attempts','3','Retries before a job is marked failed'),
 ('client_poll_seconds','3','Poll interval the portal should use (served in /meta)'),
 ('page_size_default','25','Default list page size'),
 ('page_size_max','100','Largest list page size'),
 ('photo_max_bytes','8388608','Largest uploaded photo'),
 ('photo_types','["image/jpeg","image/png","image/webp"]','Allowed photo types'),
 ('meta_title_max','60','Meta title length shown in the editor'),
 ('meta_description_max','160','Meta description length shown in the editor'),
 ('webhook_max_attempts','8','Webhook delivery retries'),
 ('webhook_backoff_seconds','[30,120,600,1800,3600,7200,21600,43200]','Wait before each retry'),
 ('token_ttl_minutes','720','Portal user session length'),
 ('cost_per_1k_tokens','null','Estimated cost per 1,000 AI tokens; set by admin for margin reports'),
 ('cost_per_gpu_second','null','Estimated cost per GPU second; set by admin for margin reports'),
 ('currency','"AED"','Currency for prices and costs'),
 ('languages','["en","ar"]','Languages the engine writes in'),
 ('kinds','["blog","social"]','Content types'),
 ('platforms','["website","linkedin","instagram"]','Publishing targets with a connector'),
 ('roles','["admin","publisher","reviewer"]','Client user roles')
ON CONFLICT (key) DO NOTHING;

COMMIT;
