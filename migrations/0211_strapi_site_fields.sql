-- 0211_strapi_site_fields: the Strapi connection form gains the website's own address (so a published article's live
-- link can be shown) and a default category (used when an article is published without one, e.g. automatically).
-- Fields the engine fills by type (publish date, allowed values) need no setting. Existing connections are unchanged.
BEGIN;
UPDATE ext_marketing.settings SET value = (
  SELECT jsonb_agg(CASE WHEN e->>'type' = 'strapi' AND NOT (e->'fields' @> '[{"key": "siteUrl"}]')
    THEN jsonb_set(e, '{fields}', (e->'fields') || '[
      {"key": "siteUrl", "label": "Website address", "kind": "url", "required": false},
      {"key": "defaultCategory", "label": "Default category", "kind": "text", "required": false}]'::jsonb)
    ELSE e END ORDER BY o)
  FROM jsonb_array_elements(value) WITH ORDINALITY AS x(e, o))
WHERE key = 'website_target_types';
COMMIT;
