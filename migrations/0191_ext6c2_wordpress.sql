-- 0191_ext6c2_wordpress: the WordPress adapter. Only the body formats the adapter writes are offered (html now).
BEGIN;
UPDATE ext_marketing.settings
   SET value = (SELECT jsonb_agg(CASE WHEN d->>'type' = 'wordpress' THEN jsonb_set(d, '{bodyFormats}', '["html"]') ELSE d END)
                  FROM jsonb_array_elements(value) d),
       updated_by = 'migration 0191', updated_at = now()
 WHERE key = 'website_target_types';
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('website_timeout_seconds', '30', 'How long one call to a client website may take')
ON CONFLICT (key) DO NOTHING;
COMMIT;
