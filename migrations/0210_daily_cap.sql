-- 0210_daily_cap: the most articles an account may publish a day (an article with its translation is one), set per account
-- in the staff portal; empty = the default below (null = no daily cap). Automatic and Publish now both respect it.
BEGIN;
ALTER TABLE ext_marketing.accounts ADD COLUMN IF NOT EXISTS max_publishes_per_day integer CHECK (max_publishes_per_day >= 0);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('publish_daily_cap_default', '3', 'Most articles an account may publish a day when its own limit is empty (an article with its translation counts once); null = no limit')
ON CONFLICT (key) DO NOTHING;
COMMIT;
