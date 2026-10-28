-- 0181_ext6b_sections: articles are written section by section (measured: one-shot drafts stayed at 550-800 words whatever the minimum).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('writer_length_target', '0.35', 'Where in the site''s word band an article aims (0 = the minimum, 1 = the maximum)'),
 ('writer_max_tokens_section', '1600', 'Longest answer for one section of an article'),
 ('writer_max_tokens_outline', '900', 'Longest answer for an article outline'),
 ('meta_retry', '1', 'When no alternative fits its length band, ask again this many times with the measured lengths')
ON CONFLICT (key) DO NOTHING;
UPDATE ext_marketing.settings SET value = jsonb_set(value, '{faq,answerWords}', '[30,60]'::jsonb, true), updated_by = 'migration 0181', updated_at = now()
 WHERE key = 'default_rules' AND NOT (value->'faq' ? 'answerWords');
COMMIT;
