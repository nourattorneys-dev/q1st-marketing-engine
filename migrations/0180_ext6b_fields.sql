-- 0180_ext6b_fields: each short field is language-checked on its own; social posts come as alternatives of different lengths.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('language_min_letters', '8', 'A field needs at least this many letters before its language is checked'),
 ('social_alternatives', '3', 'Versions of different length the writer gives for a social post; the first inside the word band is used')
ON CONFLICT (key) DO NOTHING;
COMMIT;
