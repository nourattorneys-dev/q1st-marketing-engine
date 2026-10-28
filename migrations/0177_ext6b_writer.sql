-- 0177_ext6b_writer: the writer, research, photos and the default rule set (every number is a setting; a site can override the rules).
BEGIN;
INSERT INTO ext_marketing.settings (key, value, description) VALUES
 ('default_rules', '{"blog":{"words":[900,1600]},"social":{"words":[40,220]},"introWords":[40,70],"metaTitle":[30,60],"metaDescription":[70,155],"slugWords":[3,6],"h2Min":3,"faq":{"required":true,"count":[3,6]},"keywordInTitle":true,"keywordInIntro":true,"altRequired":true,"humanChecks":["Every fact, figure and date is checked against a source","The article says something useful the site''s other pages do not"],"avoidPhrases":[]}',
  'The rule set every site starts with; a site can override any part in its own rules'),
 ('research_results', '6', 'Web results looked at for one article'),
 ('research_pages', '3', 'Web pages actually read for facts for one article'),
 ('research_page_chars', '3000', 'Characters of each research page given to the writer'),
 ('writer_site_pages', '3', 'Pages of the client site read so the article matches what the site offers'),
 ('writer_max_tokens_blog', '6000', 'Longest article the model may write in one call'),
 ('writer_max_tokens_meta', '900', 'Longest answer for the title/meta/photo step'),
 ('writer_max_tokens_social', '900', 'Longest social post answer'),
 ('writer_temperature', '0.4', 'How freely the writer words things (0 = most literal)'),
 ('social_char_limits', '{"linkedin":3000,"instagram":2200}', 'Longest post each platform accepts'),
 ('photo_auto', 'true', 'Attach the best stock photo to every new draft (the reviewer can change it)'),
 ('photo_candidates', '3', 'Stock photos tried for the automatic pick'),
 ('photo_orientation', '{"website":"landscape","linkedin":"landscape","instagram":"square"}', 'Photo shape per platform'),
 ('photo_hosts', '["images.pexels.com"]', 'Hosts a picked stock photo may be downloaded from'),
 ('media_bucket', '"ext-marketing"', 'Storage bucket for client photos (separate from Nour''s)'),
 ('topics_count', '10', 'Topics one suggestion run proposes'),
 ('topics_max_tokens', '2000', 'Longest answer for one topic suggestion run'),
 ('seo_score_weights', '{"critical":25,"warn":8}', 'Points one finding takes off the 100-point SEO score'),
 ('editable_statuses', '["pending"]', 'Post statuses a site may still edit')
ON CONFLICT (key) DO NOTHING;
INSERT INTO ext_marketing.metrics (key, label) VALUES ('topicRuns', 'Topic suggestion runs') ON CONFLICT (key) DO NOTHING;
COMMIT;
