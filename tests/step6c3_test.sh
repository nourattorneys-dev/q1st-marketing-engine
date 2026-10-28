#!/bin/bash
# Step 6c-3: review (approve / override / changes / reject / hold), the queue and its cadence, publish now, retry after a
# failure, automatic publishing, bulk. Publishes to the site's WordPress AS DRAFTS (settings.publishAs) and deletes them after.
# Needs WP_URL, WP_USER, WP_PASS in the environment.
set -u
: "${WP_URL:?set WP_URL}" "${WP_USER:?set WP_USER}" "${WP_PASS:?set WP_PASS}"
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
NA=$($PSQL "select count(*) from public.marketing_approvals")
$PSQL "delete from ext_marketing.accounts where name like 'T6c3 %'" >/dev/null
AID=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T6c3 publish"}' | j "d['data']['id']")
SID=$(curl -s -X POST $API/admin/v1/accounts/$AID/sites "${A[@]}" -d '{"name":"t6c3","url":"'$WP_URL'","category":"Legal","timezone":"Asia/Dubai","languages":["en","ar"]}' | j "d['data']['id']")
KEY=$(curl -s -X POST $API/admin/v1/accounts/$AID/api-keys "${A[@]}" -d '{"siteId":"'$SID'"}' | j "d['data']['key']")
U=$API/api/v1/sites/$SID
as() { local who=$1; shift; curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: $who" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uid)" "$@"; }
MPID=$(systemctl show -p MainPID --value nour-mkt-ext)
py() { sudo env -i $(sudo cat /proc/$MPID/environ | tr '\0' '\n' | grep -E '^(POSTGRES_DSN|EXT_MKT_|MINIO_|PEXELS_)' | xargs) EXT_SRC=/opt/nour/src /opt/nour/venv/bin/python "$@" 2>&1 | tail -1; }
# the site's WordPress, publishing as drafts so nothing goes live
WB=$(python3 -c "import json,sys; print(json.dumps({'version':0,'type':'wordpress','settings':{'url':sys.argv[1],'username':sys.argv[2],'postType':'post','publishAs':'draft'},'secret':sys.argv[3],'fieldMap':{'title':'title','slug':'slug','subtitle':'excerpt','metaTitle':'yoast.title','metaDescription':'yoast.description','keywords':'yoast.focuskw'},'body':{'format':'html'},'arabic':{'mode':'translatepress'}}))" "$WP_URL" "$WP_USER" "$WP_PASS")
check "T0 website connected (publishing as drafts)" "$(as sara -X PUT $U/settings/website -d "$WB" | j "d['data']['status']")" "ok"
mk() {  # id title findings-json [kind]
  $PSQL "insert into ext_marketing.posts (id, site_id, kind, platform, language, status, topic, title, subtitle, slug, meta_title, meta_description, keywords, content, findings)
         values ('$1','$SID','${4:-blog}','$([ "${4:-blog}" = blog ] && echo website || echo linkedin)','en','pending','t','$2','Test standfirst.','q1st-test-$1','Q1st test $1','Q1st test description.','{\"q1st test kw $1\"}','<h2>Test</h2><p>Test paragraph.</p>','$3')" >/dev/null; }
as sara -X POST $U/keywords -d '{"phrases":"q1st test kw p_a","language":"en"}' >/dev/null
mk p_a "Q1st test A — please ignore" '[]'
mk p_b "Q1st test B — please ignore" '[{"level":"critical","ref":"length","message":"500 words; the minimum is 900"}]'
mk p_c "Q1st test C — please ignore" '[{"level":"critical","code":"structure_mismatch","overridable":false,"message":"the Arabic does not mirror the English"}]'
mk p_d "Q1st test D — please ignore" '[]'
mk p_e "Q1st test E — please ignore" '[]'
mk p_s "Q1st social — please ignore" '[]' social

# ---- the cadence settings
check "T1 the cadence starts from the defaults" "$(as sara $U/settings/publishing | j "(d['data']['autoPublish'], d['data']['version'])")" "(False, 0)"
check "T1b wrong values are named" "$(as sara -X PUT $U/settings/publishing -d '{"version":0,"perDay":0,"days":["friday"],"from":"9am"}' | j "sorted(d['error']['fields'])")" "['days', 'from', 'perDay']"
check "T1c an unknown setting is refused" "$(as sara -X PUT $U/settings/publishing -d '{"version":0,"colour":"red"}' | j "list(d['error']['fields'])")" "['colour']"
check "T1d saved, the rest kept from the defaults" "$(as sara -X PUT $U/settings/publishing -d '{"version":0,"perDay":3}' | j "(d['data']['perDay'], d['data']['gapMinutes'], d['data']['version'])")" "(3, 180, 1)"

# ---- review
check "T2 a critical finding blocks approval without a reason" "$(as sara -X POST $U/posts/p_b/approve -d '{}' | j "(d['error']['code'], d['error']['details']['overridable'])")" "('framework_blocked', True)"
check "T2b ...and goes through with a reason, kept on the post" "$(as sara -X POST $U/posts/p_b/approve -d '{"overrideReason":"Short on purpose: a news update"}' | j "(d['data']['status'], d['data']['overrideReason'])")" "('approved', 'Short on purpose: a news update')"
check "T2c a non-overridable finding blocks even with a reason" "$(as sara -X POST $U/posts/p_c/approve -d '{"overrideReason":"please"}' | j "(d['error']['code'], d['error']['details']['overridable'])")" "('framework_blocked', False)"
check "T3 a clean post is approved, buttons follow" "$(as sara -X POST $U/posts/p_a/approve | j "(d['data']['status'], d['data']['actions'], d['data']['reviewedBy'])")" "('approved', ['publishNow', 'hold', 'changes', 'reject'], 'sara')"
check "T3b approving twice is refused" "$(as sara -X POST $U/posts/p_a/approve | j "d['error']['code']")" "wrong_status"
as omar -X POST $U/posts/p_d/lock >/dev/null
check "T4 a post someone else edits cannot be approved" "$(as sara -X POST $U/posts/p_d/approve | j "d['error']['code']")" "locked"
as omar -X DELETE $U/posts/p_d/lock >/dev/null
check "T5 hold and resume an approved post" "$(as sara -X POST $U/posts/p_a/hold -d '{"hold":true}' | j "d['data']['schedule']['hold']")|$(as sara -X POST $U/posts/p_a/hold -d '{"hold":false}' | j "d['data']['schedule']['hold']")" "True|False"
check "T5b a post waiting for review cannot be held" "$(as sara -X POST $U/posts/p_d/hold -d '{"hold":true}' | j "d['error']['code']")" "wrong_status"
check "T6 reject with a note" "$(as sara -X POST $U/posts/p_e/reject -d '{"note":"Off topic"}' | j "(d['data']['status'], d['data']['reviewNote'], d['data']['actions'])")" "('rejected', 'Off topic', ['delete'])"
check "T7 asking for changes needs a note" "$(as sara -X POST $U/posts/p_d/changes -d '{}' | j "list(d['error']['fields'])")" "['note']"
CH=$(as sara -X POST $U/posts/p_d/changes -d '{"note":"Shorter please"}')
check "T7b ...with a note it is redrafted" "$(echo "$CH" | j "(d['data']['status'], d['data']['jobId'][:2])")" "('drafting', 'j_')"
$PSQL "update ext_marketing.jobs set status='failed', error='stopped by the test' where id='$(echo "$CH" | j "d['data']['jobId']")' and status='queued'" >/dev/null
$PSQL "update ext_marketing.posts set status='pending', review_note=null where id='p_d'" >/dev/null

# ---- the queue
as sara -X POST $U/posts/p_s/approve >/dev/null
SQ=$(as sara $U/scheduled)
check "T8 auto-publish off: the queue says why" "$(echo "$SQ" | j "(d['data']['autoPublish'], d['data']['reason'], d['data']['nextSlotAt'])")" "(False, 'auto_publish_off', None)"
check "T8b queue order and the social post waiting for its channel" "$(echo "$SQ" | j "[(x['id'], x['position'], x['waiting']) for x in d['data']['items']]")" "[('p_b', 1, None), ('p_a', 2, None), ('p_s', 3, 'no_channel')]"
check "T8c the social post has no Publish button" "$(as sara $U/posts/p_s | j "'publishNow' in d['data']['actions']")" "False"
check "T8d publishing it now says why" "$(as sara -X POST $U/posts/p_s/publish | j "d['error']['code']")" "no_channel"

# ---- publish now, then a failure and retry
PUB=$(as sara -X POST $U/posts/p_a/publish -d '{}')
check "T9 publish now: published to WordPress" "$(echo "$PUB" | j "(d['data']['status'], d['data']['published']['target'], d['data']['published']['status'], d['data']['published']['auto'])")" "('published', 'wordpress', 'draft', False)"
check "T9b the WordPress post ID and address are kept" "$(echo "$PUB" | j "d['data']['published']['externalId'].isdigit() and d['data']['published']['url'].startswith('$WP_URL')")" "True"
check "T9c its keyword is marked covered" "$($PSQL "select status from ext_marketing.keywords where site_id='$SID' and phrase='q1st test kw p_a'")" "covered"
check "T9d one publish is counted" "$($PSQL "select coalesce(sum(qty),0) from ext_marketing.usage_events where site_id='$SID' and metric='publishes'")" "1"
check "T9e publishing it again is refused" "$(as sara -X POST $U/posts/p_a/publish | j "d['error']['code']")" "wrong_status"
$PSQL "update ext_marketing.site_connections set config = jsonb_set(config, '{settings,url}', '\"https://example.com\"') where site_id='$SID' and type='website'" >/dev/null
FL=$(as sara -X POST $U/posts/p_b/publish)
check "T10 a failure answers 502 with a plain message" "$(echo "$FL" | j "(d['error']['code'], len(d['error']['message'])>20)")" "('publish_failed', True)"
check "T10b the post is failed with Retry" "$(as sara $U/posts/p_b | j "(d['data']['status'], d['data']['actions'][0], bool(d['data']['error']))")" "('failed', 'retry', True)"
$PSQL "update ext_marketing.site_connections set config = jsonb_set(config, '{settings,url}', '\"$WP_URL\"'), status='ok' where site_id='$SID' and type='website'" >/dev/null
check "T10c retry publishes it" "$(as sara -X POST $U/posts/p_b/publish | j "(d['data']['status'], d['data']['error'])")" "('published', None)"

# ---- automatic publishing on the cadence
as sara -X POST $U/posts/p_d/approve >/dev/null
DOW=$(TZ=Asia/Dubai date +%a | tr 'A-Z' 'a-z')
V=$(as sara $U/settings/publishing | j "d['data']['version']")
as sara -X PUT $U/settings/publishing -d '{"version":'$V',"autoPublish":true,"perDay":1,"days":["'$DOW'"],"from":"00:00","to":"23:59","gapMinutes":0}' >/dev/null
check "T11 a slot is due now" "$(as sara $U/scheduled | j "(d['data']['reason'], d['data']['items'][0]['id'])")" "(None, 'p_d')"
echo "      waiting for the scheduler to publish p_d (checks every scheduler_poll_seconds)..."
for i in $(seq 1 30); do st=$($PSQL "select status from ext_marketing.posts where id='p_d'"); [ "$st" = published ] || [ "$st" = failed ] && break; sleep 5; done
check "T11b the scheduler published it on its own" "$($PSQL "select status||'|'||auto_published from ext_marketing.posts where id='p_d'")" "published|true"
mk p_f "Q1st test F — please ignore" '[]'
as sara -X POST $U/posts/p_f/approve >/dev/null
check "T12 one a day: the next one waits for the next allowed day" "$(as sara $U/scheduled | j "(d['data']['reason'], d['data']['publishedToday'], d['data']['items'][0]['expectedAt'] is not None)")" "('daily_limit', 1, True)"
check "T12b publish now does not use the day's allowance" "$(as sara -X POST $U/posts/p_f/publish | j "d['data']['status']")|$(as sara $U/scheduled | j "d['data']['publishedToday']")" "published|1"

# ---- bulk
mk p_g "Q1st test G — please ignore" '[]'
mk p_h "Q1st test H — please ignore" '[{"level":"critical","ref":"length","message":"too short"}]'
check "T13 bulk approve reports each post" "$(as sara -X POST $U/posts/bulk -d '{"action":"approve","ids":["p_g","p_h"]}' | j "(d['data']['done'], [(x['id'], x['code']) for x in d['data']['failed']])")" "(['p_g'], [('p_h', 'framework_blocked')])"
check "T13b an unknown bulk action is named" "$(as sara -X POST $U/posts/bulk -d '{"action":"explode","ids":["p_g"]}' | j "list(d['error']['fields'])")" "['action']"

echo "      removing the test drafts from WordPress..."
N=$(py /tmp/wp_cleanup.py $SID)
check "T14 every test draft removed from WordPress" "$N" "4"
check "T15 Nour untouched" "$($PSQL "select count(*) from public.marketing_approvals")" "$NA"
check "T16 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id='$AID'" >/dev/null && echo "      (test client removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
