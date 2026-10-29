-- 0199_ext10_site_health: site scans, findings per page, prepared and applied fixes (spec v1.1 SiteFinding, /site endpoints).
BEGIN;
ALTER TABLE ext_marketing.site_findings
  ADD COLUMN IF NOT EXISTS code text, ADD COLUMN IF NOT EXISTS detail jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS fix_data jsonb, ADD COLUMN IF NOT EXISTS error text,
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now(), ADD COLUMN IF NOT EXISTS applied_at timestamptz;
DELETE FROM ext_marketing.site_findings WHERE code IS NULL;          -- none were ever written; the key below needs a code
ALTER TABLE ext_marketing.site_findings ALTER COLUMN code SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS site_findings_key ON ext_marketing.site_findings (site_id, url, code);
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('health_max_pages', '50', 'Most pages one site scan reads'),
 ('health_link_check_max', '100', 'Most extra links one site scan checks for being broken (links to pages it did not read)'),
 ('health_fix_max_tokens', '600', 'Longest answer the AI may give when it writes one proposed fix'),
 ('health_checks', '{
   "missing_title": {"level": "critical", "fix": {"en": "Give the page a title.", "ar": "أضف عنوانًا للصفحة."}},
   "title_length": {"level": "warn", "fix": {"en": "Rewrite the title within the length band.", "ar": "أعد كتابة العنوان ضمن حدود الطول."}},
   "missing_description": {"level": "critical", "fix": {"en": "Add a meta description.", "ar": "أضف وصفًا تعريفيًا (meta description)."}},
   "description_length": {"level": "warn", "fix": {"en": "Rewrite the meta description within the length band.", "ar": "أعد كتابة الوصف التعريفي ضمن حدود الطول."}},
   "duplicate_title": {"level": "warn", "fix": {"en": "Give each page its own title.", "ar": "اجعل لكل صفحة عنوانًا خاصًا بها."}},
   "duplicate_description": {"level": "warn", "fix": {"en": "Give each page its own meta description.", "ar": "اجعل لكل صفحة وصفًا تعريفيًا خاصًا بها."}},
   "missing_h1": {"level": "warn", "fix": {"en": "Add one main heading (H1) that says what the page is about. Change this on the website.", "ar": "أضف عنوانًا رئيسيًا واحدًا (H1) يوضح موضوع الصفحة. يُغيَّر من الموقع."}},
   "many_h1": {"level": "info", "fix": {"en": "Keep one main heading (H1); make the others H2. Change this on the website.", "ar": "أبقِ عنوانًا رئيسيًا واحدًا (H1) واجعل البقية H2. يُغيَّر من الموقع."}},
   "img_alt": {"level": "warn", "fix": {"en": "Describe each image in its alt text. Change this on the website.", "ar": "صف كل صورة في نصها البديل (alt). يُغيَّر من الموقع."}},
   "noindex": {"level": "info", "fix": {"en": "This page asks search engines not to list it. Remove noindex if it should be found.", "ar": "هذه الصفحة تطلب من محركات البحث عدم إدراجها. أزل noindex إن كان يجب العثور عليها."}},
   "broken_link": {"level": "critical", "fix": {"en": "Fix or remove the links to this address on the pages listed.", "ar": "أصلح الروابط إلى هذا العنوان أو احذفها من الصفحات المذكورة."}},
   "page_unreachable": {"level": "warn", "fix": {"en": "Check that this page opens for visitors and search engines.", "ar": "تأكد أن هذه الصفحة تفتح للزوار ومحركات البحث."}}
 }', 'Site health checks: the level of each finding and the advice shown with it, per language. Titles and descriptions are measured against the site''s rules (metaTitle, metaDescription)')
ON CONFLICT (key) DO NOTHING;
COMMIT;
