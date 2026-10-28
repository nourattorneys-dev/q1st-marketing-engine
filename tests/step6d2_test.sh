#!/bin/bash
# Step 6d-2: a twin on a TranslatePress site is stored as the translation of its published source (no second post).
# The source is published to WordPress AS A DRAFT and deleted after. Needs WP_URL, WP_USER, WP_PASS.
set -u
: "${WP_URL:?set WP_URL}" "${WP_USER:?set WP_USER}" "${WP_PASS:?set WP_PASS}"
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
NA=$($PSQL "select count(*) from public.marketing_approvals")
$PSQL "delete from ext_marketing.accounts where name like 'T6d2 %'" >/dev/null
AID=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T6d2 translation"}' | j "d['data']['id']")
SID=$(curl -s -X POST $API/admin/v1/accounts/$AID/sites "${A[@]}" -d '{"name":"t6d2","url":"'$WP_URL'","category":"Legal","timezone":"Asia/Dubai","languages":["en","ar"]}' | j "d['data']['id']")
KEY=$(curl -s -X POST $API/admin/v1/accounts/$AID/api-keys "${A[@]}" -d '{"siteId":"'$SID'"}' | j "d['data']['key']")
U=$API/api/v1/sites/$SID
as() { curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: sara" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uid)" "$@"; }
MPID=$(systemctl show -p MainPID --value nour-mkt-ext)
py() { sudo env -i $(sudo cat /proc/$MPID/environ | tr '\0' '\n' | grep -E '^(POSTGRES_DSN|EXT_MKT_|MINIO_|PEXELS_)' | xargs) EXT_SRC=/opt/nour/src /opt/nour/venv/bin/python "$@" 2>&1 | tail -1; }
WB=$(python3 -c "import json,sys; print(json.dumps({'version':0,'type':'wordpress','settings':{'url':sys.argv[1],'username':sys.argv[2],'postType':'post','publishAs':'draft'},'secret':sys.argv[3],'fieldMap':{'title':'title','slug':'slug','subtitle':'excerpt','metaTitle':'yoast.title','metaDescription':'yoast.description'},'body':{'format':'html'},'arabic':{'mode':'translatepress'}}))" "$WP_URL" "$WP_USER" "$WP_PASS")
check "T0 website connected; plugin 0.4.0 or newer (stores translations)" "$(as -X PUT $U/settings/website -d "$WB" | j "d['data']['status']")|$($PSQL "select string_to_array(config->'learned'->>'plugin','.')::int[] >= '{0,4,0}' from ext_marketing.site_connections where site_id='$SID' and type='website'")" "ok|t"

# a source and its twin, written directly (the model is not needed here): straight quotes and -- on purpose, which the
# website turns into typographic characters when it renders the page
TAG=$(date +%s)
sudo /opt/nour/venv/bin/python - "$SID" "$TAG" > /tmp/t6d2.sql <<'PY'
import sys, json; sys.path.insert(0, "/opt/nour/src")
from ext_marketing import mirror as M
sid, tag = sys.argv[1], sys.argv[2]
en = (f"<p>Q1st test {tag}: the company's assets -- what \"protection\" means, under <strong>Federal Law No. 32</strong>.</p>"
      "<h2>Who decides?</h2><p>The partners decide, see <a href=\"/contact\">our contact page</a>.</p>"
      "<ul><li>First step</li><li>Second step</li></ul><h2>FAQ</h2><h3>Is it quick?</h3><p>Usually, yes.</p>")
ar = ["اختبار Q1st " + tag + ": أصول الشركة -- ما تعنيه \"الحماية\"، بموجب", "القانون الاتحادي رقم 32", ".",
      "من يقرر؟", "يقرر الشركاء، راجع", "صفحة التواصل", ".", "الخطوة الأولى", "الخطوة الثانية", "الأسئلة الشائعة", "هل هو سريع؟", "عادةً، نعم."]
assert len(ar) == len(M.segments(en)), (len(ar), M.segments(en))
arh = M.rebuild(en, ar)
q = lambda s: "'" + s.replace("'", "''") + "'"   # noqa: E731
print(f"""insert into ext_marketing.posts (id, site_id, kind, platform, language, pair_id, status, topic, title, meta_title, meta_description, slug, content, findings)
 values ('p_src', '{sid}', 'blog', 'website', 'en', 'pair_t', 'pending', 't', {q("Q1st test "+tag+" — please ignore")}, {q("Q1st test meta "+tag)}, {q("Q1st test description "+tag)}, {q("q1st-test-"+tag)}, {q(en)}, '[]');
insert into ext_marketing.posts (id, site_id, kind, platform, language, pair_id, source_id, source_hash, status, topic, title, meta_title, meta_description, content, findings)
 values ('p_tw', '{sid}', 'blog', 'website', 'ar', 'pair_t', 'p_src', {q(M.fingerprint(M.segments(en)))}, 'pending', 't', {q("اختبار Q1st "+tag)}, {q("عنوان ميتا "+tag)}, {q("وصف ميتا "+tag)}, {q(arh)}, '[]');""")
PY
docker exec -i nour-postgres psql -U nour_user -d nour_db -q < /tmp/t6d2.sql
check "T1 a source and its mirrored twin exist" "$($PSQL "select count(*) from ext_marketing.posts where site_id='$SID'")" "2"
as -X POST $U/posts/p_src/approve >/dev/null; as -X POST $U/posts/p_tw/approve >/dev/null
check "T2 the twin cannot go before its source" "$(as -X POST $U/posts/p_tw/publish | j "(d['error']['code'], 'first' in d['error']['message'])")" "('publish_failed', True)"
as -X POST $U/posts/p_tw/approve >/dev/null; $PSQL "update ext_marketing.posts set status='approved', error=null where id='p_tw'" >/dev/null
SP=$(as -X POST $U/posts/p_src/publish)
check "T3 the source is published to WordPress (draft)" "$(echo "$SP" | j "(d['data']['status'], d['data']['published']['target'])")" "('published', 'wordpress')"
check "T4 the twin no longer waits in the queue" "$(as $U/scheduled | j "[x['waiting'] for x in d['data']['posts'] if x['id']=='p_tw'][0]")" "None"
TP=$(as -X POST $U/posts/p_tw/publish)
echo "      $(echo "$TP" | j "d['data']['published'] if d.get('ok') else d['error']")"
check "T5 the twin is stored as the translation, not a second post" "$(echo "$TP" | j "(d['data']['status'], d['data']['published']['target'], d['data']['published']['externalId']=='$(echo "$SP" | j "d['data']['published']['externalId']")')")" "('published', 'translatepress', True)"
check "T5b every piece plus title and SEO fields stored as reviewed" "$(echo "$TP" | j "d['data']['published']['stored']>=10")" "True"
check "T5c its address is the source's under /ar/" "$(echo "$TP" | j "'/ar/' in d['data']['published']['url']")" "True"
check "T6 one publish counted for each" "$($PSQL "select coalesce(sum(qty),0) from ext_marketing.usage_events where site_id='$SID' and metric='publishes'")" "2"
# a twin whose structure no longer matches is refused before anything is written
$PSQL "update ext_marketing.posts set status='approved', content=content||'<p>زائد</p>', published=null where id='p_tw'" >/dev/null
check "T7 a twin that no longer matches is refused with a plain message" "$(as -X POST $U/posts/p_tw/publish | j "(d['error']['code'], 'structure' in d['error']['message'])")" "('publish_failed', True)"

echo "      removing the test draft from WordPress..."
check "T8 the test draft removed from WordPress" "$(py /tmp/wp_cleanup.py $SID)" "1"
check "T9 Nour untouched" "$($PSQL "select count(*) from public.marketing_approvals")" "$NA"
check "T10 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id='$AID'" >/dev/null && echo "      (test client removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
