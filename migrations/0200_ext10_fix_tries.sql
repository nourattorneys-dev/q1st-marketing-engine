-- 0200_ext10_fix_tries: how many times the AI may try to write a site-health fix that fits its length band.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('health_fix_tries', '3', 'Tries the AI gets to write a site-health title or description inside its length band; one that never fits is not prepared')
ON CONFLICT (key) DO NOTHING;
COMMIT;
