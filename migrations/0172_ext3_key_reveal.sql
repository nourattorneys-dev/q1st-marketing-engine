-- 0172_ext3_key_reveal: a new key is revealed ONCE through a single-use, short-lived, encrypted copy (refresh never re-issues).
BEGIN;
ALTER TABLE ext_marketing.api_keys ADD COLUMN IF NOT EXISTS reveal_enc bytea;
ALTER TABLE ext_marketing.api_keys ADD COLUMN IF NOT EXISTS reveal_until timestamptz;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('key_reveal_seconds', '300', 'How long a newly issued key can be shown (once) to the admin who issued it')
ON CONFLICT (key) DO NOTHING;
COMMIT;
