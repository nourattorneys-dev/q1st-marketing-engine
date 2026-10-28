-- 0173_ext5_strapi: knobs of the per-site Strapi connector (settings, editable in Engine settings).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('strapi_timeout_seconds', '30', 'Longest wait for one call to a client''s Strapi'),
 ('strapi_require_https', 'true', 'Refuse Strapi addresses that are not https'),
 ('strapi_sample_size', '3', 'How many existing entries Test connection reads to learn the site''s fields'),
 ('strapi_field_synonyms', '{"title":["title","name","heading","headline"],"slug":["slug","handle","url"],"body":["content","body","text","article","richText","html"],"subtitle":["subtitle","excerpt","summary","standfirst","lead","intro"],"metaTitle":["seo.metaTitle","metaTitle","seo.title","meta_title"],"metaDescription":["seo.metaDescription","metaDescription","seo.description","meta_description","description"],"keywords":["seo.keywords","keywords","tags"],"image":["cover","image","featuredImage","thumbnail","coverImage","heroImage","photo"],"category":["category","categories"],"author":["author","authors","writer"]}',
  'Field names Test connection looks for when it suggests the field map (our field -> names a site may use)')
ON CONFLICT (key) DO NOTHING;
COMMIT;
