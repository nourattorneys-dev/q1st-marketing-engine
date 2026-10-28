-- 0170_ext3_billing: the time zone that defines a client's monthly usage period (setting, not code).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('billing_timezone', '"Asia/Dubai"', 'Time zone in which a usage month starts and ends')
ON CONFLICT (key) DO NOTHING;
COMMIT;
