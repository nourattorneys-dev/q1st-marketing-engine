-- 0192_ext6c3_publish: review (approve / changes / reject / hold), the publishing queue on each site's cadence, publishing
-- through the site's website connection (WordPress or Strapi).
BEGIN;
ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS published_at timestamptz;
ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS auto_published boolean NOT NULL DEFAULT false;
ALTER TABLE ext_marketing.posts DROP CONSTRAINT IF EXISTS posts_status_check;
ALTER TABLE ext_marketing.posts ADD CONSTRAINT posts_status_check
  CHECK (status IN ('drafting','pending','approved','publishing','published','failed','rejected'));
CREATE INDEX IF NOT EXISTS posts_queue_idx ON ext_marketing.posts (site_id, status, approved_at);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('publishing_defaults', '{"autoPublish": false, "perDay": 2, "days": ["sun","mon","tue","wed","thu"], "from": "10:00", "to": "17:00", "gapMinutes": 180}',
  'A site''s automatic publishing cadence until it saves its own (times in the site''s timezone)'),
 ('scheduler_poll_seconds', '60', 'How often the publishing queue checks whether a site has a slot due'),
 ('publish_kinds', '["blog"]', 'Post kinds that can be published now (social posts wait until their channel is connected)')
ON CONFLICT (key) DO NOTHING;
COMMIT;
