-- 0206_ideas_drafted: a topic idea becomes 'drafted' once its draft is written (it no longer shows "Drafting…"),
-- and "Fill from my website" may read the site's links to find its articles page.
BEGIN;
ALTER TABLE ext_marketing.suggestions DROP CONSTRAINT IF EXISTS suggestions_status_check;
ALTER TABLE ext_marketing.suggestions ADD CONSTRAINT suggestions_status_check CHECK (status IN ('new','drafting','drafted','dismissed'));
UPDATE ext_marketing.suggestions g SET status = 'drafted'
 WHERE status = 'drafting' AND EXISTS (SELECT 1 FROM ext_marketing.posts p WHERE p.suggestion_id = g.id AND p.content IS NOT NULL);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('seo_propose_links', '200', 'Most links of the client''s website shown to the AI when it looks for the articles page')
ON CONFLICT (key) DO NOTHING;
COMMIT;
