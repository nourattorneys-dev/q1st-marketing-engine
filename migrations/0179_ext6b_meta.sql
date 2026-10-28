-- 0179_ext6b_meta: two revision passes (measured: one pass left most articles short); alternatives per length-limited field.
BEGIN;
UPDATE ext_marketing.settings SET value = '2', updated_by = 'migration 0179', updated_at = now() WHERE key = 'writer_fix_passes';
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('meta_alternatives', '3', 'Alternatives the writer gives for each length-limited field (meta title, meta description, slug); the first that fits is used')
ON CONFLICT (key) DO NOTHING;
COMMIT;
