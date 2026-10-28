-- 0175_ext6a_reader: a page with less body text than this is read from its title, meta description, Open Graph,
-- headings and structured data (sites built by JavaScript); how many sitemap URLs are considered.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('site_min_body_chars', '400', 'Below this much body text a page is read from its meta tags, headings and structured data'),
 ('site_sitemap_urls', '200', 'URLs read from sitemap.xml when looking for pages of a client site')
ON CONFLICT (key) DO NOTHING;
COMMIT;
