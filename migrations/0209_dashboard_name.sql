-- 0209_dashboard_name: each client's own dashboard address, <name>.<domain> (e.g. fakher.q1st.io), set in the staff portal.
BEGIN;
ALTER TABLE ext_marketing.accounts ADD COLUMN IF NOT EXISTS dashboard_name text;
CREATE UNIQUE INDEX IF NOT EXISTS accounts_dashboard_name_ux ON ext_marketing.accounts (dashboard_name) WHERE dashboard_name IS NOT NULL AND deleted_at IS NULL;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('dashboard_client_domain', '"q1st.io"', 'The domain under which each client''s dashboard has its own address (shown in the staff portal; the dashboard''s CLIENT_DOMAIN)')
ON CONFLICT (key) DO NOTHING;
COMMIT;
