-- 0202_p1c_dashboard: how long the engine waits for the client dashboard when the staff portal sets a site up there.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('dashboard_timeout_seconds', '20', 'Seconds the engine waits for the client dashboard to answer when a site is set up, renamed or removed there')
ON CONFLICT (key) DO NOTHING;
COMMIT;
