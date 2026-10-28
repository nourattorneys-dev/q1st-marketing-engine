-- 0194_ext6d_pieces: translation pieces are sent with ids and only missing ids are asked again; pieces without letters are
-- kept, with the target language's punctuation.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('twin_retries', '2', 'How many times the pieces a translation left out are asked for again'),
 ('twin_punctuation', '{"ar": {"?": "؟", ",": "،", ";": "؛"}}', 'Punctuation written differently in a language, used for pieces that are only punctuation')
ON CONFLICT (key) DO NOTHING;
COMMIT;
