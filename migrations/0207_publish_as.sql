-- 0207_publish_as: how the website keeps a published article (live, or a draft someone publishes in WordPress) is a
-- choice on the Website connection form, never a hidden value. Existing connections keep what they have.
BEGIN;
UPDATE ext_marketing.settings SET value = (
  SELECT jsonb_agg(CASE WHEN e->>'type' = 'wordpress' AND NOT (e->'fields' @> '[{"key": "publishAs"}]')
    THEN jsonb_set(e, '{fields}', (e->'fields') || '[{"key": "publishAs", "label": "Articles appear on the site as", "kind": "choice",
          "required": false, "options": [{"value": "publish", "label": "Published (live at once)"},
                                         {"value": "draft", "label": "Draft (someone publishes it in WordPress)"}]}]'::jsonb)
    ELSE e END ORDER BY o)
  FROM jsonb_array_elements(value) WITH ORDINALITY AS x(e, o))
WHERE key = 'website_target_types';
COMMIT;
