-- 0174_ext6a_engine: job queue knobs, the model, site reading, keyword proposals; keyword volume/difficulty columns.
BEGIN;
ALTER TABLE ext_marketing.keywords ADD COLUMN IF NOT EXISTS search_volume integer;
ALTER TABLE ext_marketing.keywords ADD COLUMN IF NOT EXISTS difficulty integer;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('llm_engine', '"nemotron"', 'The model server the client engine writes with (shared with Nour; client jobs run one at a time)'),
 ('job_stale_minutes', '30', 'A running job older than this is treated as crashed and retried'),
 ('site_fetch_timeout_seconds', '20', 'Longest wait for one page of a client website'),
 ('site_fetch_max_bytes', '2000000', 'Largest client page read'),
 ('site_page_chars', '6000', 'Characters of each client page given to the model'),
 ('kw_propose_pages', '6', 'Pages of the client site read for a keyword proposal (home + linked pages on the same site)'),
 ('kw_propose_count', '20', 'Keywords one proposal run suggests'),
 ('kw_propose_seeds', '5', 'Seed phrases checked in SEMrush per proposal run (each is one paid call)'),
 ('kw_semrush_limit', '10', 'Related keywords SEMrush returns per seed'),
 ('kw_semrush_database_default', '"ae"', 'SEMrush country database used when a site sets none'),
 ('kw_max_tokens', '2500', 'Longest model answer for one keyword step'),
 ('site_profile_fields', '["tone","audience","locations","avoid","focusKeywords","cta","services"]', 'Profile fields the writer is given about a site')
ON CONFLICT (key) DO NOTHING;
COMMIT;
