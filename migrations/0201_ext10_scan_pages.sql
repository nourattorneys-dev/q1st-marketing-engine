-- 0201_ext10_scan_pages: a site scan reads up to 300 pages, so a whole small or medium site is checked (50 left most pages unread).
BEGIN;
UPDATE ext_marketing.settings SET value = '300' WHERE key = 'health_max_pages';
COMMIT;
