-- 0182_ext6b_repair: sections are measured (one retry when short); revisions repair only the parts a finding is about.
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('writer_section_min_share', '0.85', 'A section shorter than this share of its word target is asked again once with the measured count')
ON CONFLICT (key) DO NOTHING;
COMMIT;
