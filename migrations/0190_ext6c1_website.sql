-- 0190_ext6c1_website: one general website connection (WordPress or Strapi) per site, idempotent changes, editing locks.
BEGIN;
-- the Strapi-only connection becomes the website connection {type, settings, fieldMap, body, arabic, languages, learned}
UPDATE ext_marketing.site_connections SET type = 'website', config = jsonb_build_object(
    'type', 'strapi',
    'settings', jsonb_strip_nulls(jsonb_build_object(
        'url', config->'url', 'collection', config->'collection', 'articlePath', config->'articlePath', 'siteUrl', config->'siteUrl',
        'categoryCollection', config->'categoryCollection', 'categoryNameField', config->'categoryNameField',
        'categorySlugField', config->'categorySlugField', 'defaultCategory', config->'defaultCategory',
        'fixedFields', config->'fixedFields', 'defaultLocale', config->'defaultLocale')),
    'fieldMap', coalesce(config->'fieldMap', '{}'::jsonb),
    'body', jsonb_build_object('format', 'blocks'),
    'arabic', jsonb_build_object('mode', 'locale'),
    'languages', coalesce(config->'locales', '[]'::jsonb),
    'learned', jsonb_strip_nulls(jsonb_build_object('fieldKinds', config->'fieldKinds', 'strapiVersion', config->'strapiVersion')))
 WHERE type = 'strapi';

CREATE TABLE IF NOT EXISTS ext_marketing.idempotency (
  scope text NOT NULL,                       -- hash of the caller's key: a key is never shared between sites
  key text NOT NULL, method text NOT NULL, path text NOT NULL,
  status int, body bytea, content_type text,  -- NULL status = the first request is still running
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (scope, key));
CREATE INDEX IF NOT EXISTS idempotency_age_idx ON ext_marketing.idempotency (created_at);

ALTER TABLE ext_marketing.posts ADD COLUMN IF NOT EXISTS lock_expires_at timestamptz;

INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('idempotency_hours', '24', 'A change repeated with the same Idempotency-Key within this many hours returns the first result'),
 ('idempotency_required', 'true', 'Changes without an Idempotency-Key are refused'),
 ('lock_minutes', '15', 'How long an editing lock lasts without being renewed'),
 ('features', '[]', 'Optional features this engine offers (import, repurpose, performance, legalChecks); the portal shows only these'),
 ('website_target_types', '[
   {"type": "wordpress", "label": "WordPress",
    "fields": [{"key": "url", "label": "Site address", "kind": "url", "required": true},
               {"key": "username", "label": "Publishing user", "kind": "text", "required": true},
               {"key": "secret", "label": "Application password", "kind": "secret", "required": true},
               {"key": "postType", "label": "Post type", "kind": "text", "required": false}],
    "bodyFormats": ["html", "boldbuilder"], "arabicModes": ["translatepress", "separate"]},
   {"type": "strapi", "label": "Strapi",
    "fields": [{"key": "url", "label": "Strapi address", "kind": "url", "required": true},
               {"key": "secret", "label": "API token", "kind": "secret", "required": true},
               {"key": "collection", "label": "Collection", "kind": "text", "required": true},
               {"key": "articlePath", "label": "Article path on the website", "kind": "text", "required": false}],
    "bodyFormats": ["blocks"], "arabicModes": ["locale"]}]',
  'Website types a site can connect to and the fields of their form (only types with an adapter are offered)')
ON CONFLICT (key) DO NOTHING;
COMMIT;
