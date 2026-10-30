-- 0203_p1c_client_delete: a deleted client leaves the Clients list but its account row stays, so revenue, usage and the
-- audit log keep their history.
BEGIN;
ALTER TABLE ext_marketing.accounts ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
COMMIT;
