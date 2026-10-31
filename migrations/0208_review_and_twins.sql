-- 0208_review_and_twins: approving with critical findings needs a reason only where the client asks for one (it is always
-- recorded how many there were); a translation stored on its source's page is published after its source.
BEGIN;
ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS approved_critical integer NOT NULL DEFAULT 0;
UPDATE ext_marketing.posts SET approved_critical = jsonb_array_length(coalesce((SELECT jsonb_agg(f) FROM jsonb_array_elements(findings) f
  WHERE f->>'level' = 'critical'), '[]'::jsonb)) WHERE override_reason IS NOT NULL;
UPDATE ext_marketing.settings SET value = value || '{"overrideReasonRequired": false}'::jsonb
 WHERE key = 'publishing_defaults' AND NOT value ? 'overrideReasonRequired';
COMMIT;
