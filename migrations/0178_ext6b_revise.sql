-- 0178_ext6b_revise: one measured revision pass after drafting; the language check (which script each language is written in).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('writer_fix_passes', '1', 'Revision passes after drafting: the measured findings go back to the writer, the better version is kept'),
 ('language_scripts', '{"ar":"[\\u0600-\\u06FF]","en":"[A-Za-z]"}', 'The letters each language is written in (for the right-language check)'),
 ('language_min_share', '0.6', 'Share of letters that must be in the post''s own language')
ON CONFLICT (key) DO NOTHING;
COMMIT;
