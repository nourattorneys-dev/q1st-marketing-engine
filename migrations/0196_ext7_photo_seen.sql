-- 0196_ext7_photo_seen: the photos a site's search returned, so picking one needs only its id (spec v1.1 {pexelsId, alt?}).
BEGIN;
CREATE TABLE IF NOT EXISTS ext_marketing.photo_seen (
  site_id text NOT NULL REFERENCES ext_marketing.sites(id) ON DELETE CASCADE, pexels_id bigint NOT NULL,
  full_url text NOT NULL, photographer text, alt text, seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (site_id, pexels_id));
COMMIT;
