-- 0212_twin_retries_text: the same retries now also cover pieces that came back with words still in the source language.
UPDATE ext_marketing.settings SET description = 'How many times a translation''s pieces are asked for again: pieces left out, and pieces that still have words in the original language'
WHERE key = 'twin_retries';
