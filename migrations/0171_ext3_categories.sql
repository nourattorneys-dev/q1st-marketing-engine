-- 0171_ext3_categories: the site categories offered in the portal dropdown (a setting; admins add more in Engine settings).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('site_categories', '["Automotive","Beauty & wellness","Construction","Education","Finance & insurance","Food & restaurants","Government","Healthcare","Hospitality & travel","Legal","Logistics","Manufacturing","Non-profit","Real estate","Retail & e-commerce","Technology","Other"]',
  'Categories a site can be in (dropdown). The AI writes for the chosen category.')
ON CONFLICT (key) DO NOTHING;
COMMIT;
