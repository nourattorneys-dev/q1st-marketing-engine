-- 0176_ext6a_reader2: sitemap names tried after robots.txt; heavy pages are read up to site_fetch_max_bytes (not discarded).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('site_sitemap_paths', '["/sitemap.xml","/sitemap_index.xml","/wp-sitemap.xml","/sitemap-index.xml"]', 'Sitemap addresses tried after the ones a site declares in robots.txt'),
 ('site_reader_name', '"Quantum1st-MarketingBot"', 'The name our website reader announces (site owners allow this name in their firewall)')
ON CONFLICT (key) DO NOTHING;
UPDATE ext_marketing.settings SET description = 'Bytes of one client page read (a heavier page is read up to this size, not skipped)' WHERE key = 'site_fetch_max_bytes';
COMMIT;
