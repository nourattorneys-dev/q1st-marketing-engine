-- 0197_ext7_arabic_names: Arabic names for lists stored as data (metered items, site categories), so the portal shows them in the chosen language.
BEGIN;
ALTER TABLE ext_marketing.metrics ADD COLUMN IF NOT EXISTS label_ar text;
UPDATE ext_marketing.metrics m SET label_ar = v.ar FROM (VALUES
 ('articles','المقالات المكتوبة'),('socialPosts','منشورات التواصل المكتوبة'),('photos','الصور المختارة أو المرفوعة'),
 ('publishes','عمليات النشر'),('siteScans','فحوصات الموقع'),('keywordProposals','مرات اقتراح الكلمات المفتاحية'),
 ('aiTokens','رموز الذكاء الاصطناعي'),('sites','المواقع'),('topicRuns','مرات اقتراح المواضيع')) AS v(k, ar)
WHERE m.key = v.k AND m.label_ar IS NULL;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('site_category_names', '{"Automotive":{"ar":"السيارات"},"Beauty & wellness":{"ar":"التجميل والعناية"},"Construction":{"ar":"البناء والمقاولات"},"Education":{"ar":"التعليم"},"Finance & insurance":{"ar":"المال والتأمين"},"Food & restaurants":{"ar":"الأغذية والمطاعم"},"Government":{"ar":"الجهات الحكومية"},"Healthcare":{"ar":"الرعاية الصحية"},"Hospitality & travel":{"ar":"الضيافة والسفر"},"Legal":{"ar":"القانون"},"Logistics":{"ar":"الخدمات اللوجستية"},"Manufacturing":{"ar":"التصنيع"},"Non-profit":{"ar":"القطاع غير الربحي"},"Real estate":{"ar":"العقارات"},"Retail & e-commerce":{"ar":"التجزئة والتجارة الإلكترونية"},"Technology":{"ar":"التقنية"},"Other":{"ar":"أخرى"}}',
  'Names of each site category in other languages, keyed by the category as written in site_categories; a category without a name here shows as written')
ON CONFLICT (key) DO NOTHING;
COMMIT;
