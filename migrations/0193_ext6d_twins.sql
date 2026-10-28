-- 0193_ext6d_twins: an article's twin in the other language is translated from its source piece by piece, so both have the
-- same structure (translation plugins such as TranslatePress pair the pieces).
BEGIN;
ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS source_id text REFERENCES ext_marketing.posts(id) ON DELETE SET NULL;
ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS source_hash text;
CREATE INDEX IF NOT EXISTS posts_source_idx ON ext_marketing.posts (source_id);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('twin_segments_per_call', '40', 'How many text pieces of an article are translated in one call'),
 ('twin_max_tokens', '4000', 'Longest answer for one translation call'),
 ('twin_temperature', '0.2', 'How freely the translation may word things (0 = most literal)'),
 ('twin_inherited_checks', '["length", "intro", "headings", "faq"]', 'Checks a translated twin leaves to its source (its structure and length are the source''s)')
ON CONFLICT (key) DO NOTHING;
COMMIT;
