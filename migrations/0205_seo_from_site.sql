-- 0205_seo_from_site: "Fill from my website" reads the client's own pages and proposes the whole SEO profile.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('seo_propose_pages', '8', 'Pages of the client''s website read to propose its SEO profile (home, about, services first)'),
 ('seo_propose_max_tokens', '1800', 'Most tokens the AI may write for one SEO profile proposal')
ON CONFLICT (key) DO NOTHING;
INSERT INTO ext_marketing.metrics (key, label, label_ar, unit, active) VALUES
 ('profileProposals', 'Profile fills from the website', 'تعبئة الملف من الموقع', 'count', true)
ON CONFLICT (key) DO NOTHING;
COMMIT;
