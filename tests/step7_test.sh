#!/bin/bash
# Step 7: /account/plan, /account/usage, upgrade requests, hard limits refused (429 quota_exceeded), soft limits recorded.
set -u
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
MPID=$(systemctl show -p MainPID --value nour-mkt-ext)
py() { sudo env -i $(sudo cat /proc/$MPID/environ | tr '\0' '\n' | grep -E '^(POSTGRES_DSN|EXT_MKT_|MINIO_|PEXELS_)' | xargs) EXT_SRC=/opt/nour/src /opt/nour/venv/bin/python -c "$1" 2>&1 | tail -1; }
$PSQL "delete from ext_marketing.accounts where name like 'T7 %'" >/dev/null; $PSQL "delete from ext_marketing.plans where name = 'T7 plan'" >/dev/null
PL=$(curl -s -X POST $API/admin/v1/plans "${A[@]}" -d '{"name":"T7 plan","price":500,"currency":"AED","quotas":[{"metric":"photos","limit":1,"mode":"hard"},{"metric":"topicRuns","limit":0,"mode":"hard"},{"metric":"articles","limit":0,"mode":"hard"},{"metric":"publishes","limit":0,"mode":"soft","warnAt":0.5}]}' | j "d['data']['id']")
AID=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T7 quotas","planId":"'$PL'"}' | j "d['data']['id']")
SID=$(curl -s -X POST $API/admin/v1/accounts/$AID/sites "${A[@]}" -d '{"name":"t7","url":"https://www.quantum1st.com","category":"Technology","timezone":"Asia/Dubai","languages":["en","ar"]}' | j "d['data']['id']")
KEY=$(curl -s -X POST $API/admin/v1/accounts/$AID/api-keys "${A[@]}" -d '{"siteId":"'$SID'"}' | j "d['data']['key']")
U=$API/api/v1/sites/$SID
as() { curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: sara" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uid)" "$@"; }
G() { curl -s -H "X-Api-Key: $KEY" "$@"; }

# ---- the plan
PN=$(G $API/api/v1/account/plan)
check "T1 the plan, its period and every metric" "$(echo "$PN" | j "(d['data']['plan']['name'], d['data']['period']['end'][8:10] in ('28','29','30','31'), 'photos' in d['data']['metrics'])")" "('T7 plan', True, True)"
check "T1b a quota row: limit, used, scope, mode" "$(echo "$PN" | j "[(q['limit'], q['used'], q['scope'], q['mode']) for q in d['data']['quotas'] if q['metric']=='photos'][0]")" "(1, 0, 'account', 'hard')"
check "T1c a metric with no limit is unlimited (null)" "$(echo "$PN" | j "[q['limit'] for q in d['data']['quotas'] if q['metric']=='socialPosts'][0]")" "None"
check "T1d a soft limit keeps its warning share" "$(echo "$PN" | j "[(q['mode'], q['warnAt']) for q in d['data']['quotas'] if q['metric']=='publishes'][0]")" "('soft', 0.5)"
curl -s -o /dev/null -X PUT $API/admin/v1/accounts/$AID/quota-overrides "${A[@]}" -d '{"metric":"photos","limit":2,"siteId":"'$SID'","reason":"test"}'
check "T2 a site override shows as the site's limit" "$(G $API/api/v1/account/plan | j "[(q['limit'], q['scope']) for q in d['data']['quotas'] if q['metric']=='photos'][0]")" "(2, 'site')"

# ---- hard limits refuse before any work starts
R=$(as -X POST $U/keywords/topics -d '{}')
check "T3 a hard limit at 0 refuses with 429 and the numbers" "$(echo "$R" | j "(d['error']['code'], d['error']['details'])")" "('quota_exceeded', {'metric': 'topicRuns', 'used': 0, 'limit': 0})"
check "T3b ...with a plain message" "$(echo "$R" | j "'limit' in d['error']['message'] and 'upgrade' in d['error']['message']")" "True"
check "T3c ...and no job was queued" "$($PSQL "select count(*) from ext_marketing.jobs where site_id='$SID'")" "0"
check "T4 drafting an article over its limit is refused, nothing created" "$(as -X POST $U/drafts -d '{"topic":"t","kind":"blog","language":"en"}' | j "d['error']['code']")|$($PSQL "select count(*) from ext_marketing.posts where site_id='$SID'")" "quota_exceeded|0"
check "T4b 'both' languages count as two articles" "$(curl -s -o /dev/null -X PUT $API/admin/v1/accounts/$AID/quota-overrides "${A[@]}" -d '{"metric":"articles","limit":1,"reason":"test"}'; as -X POST $U/drafts -d '{"topic":"t","kind":"blog","language":"both"}' | j "d['error']['details']")" "{'metric': 'articles', 'used': 0, 'limit': 1}"
$PSQL "insert into ext_marketing.posts (id, site_id, kind, platform, language, status, topic, title, content) values ('p_t7','$SID','blog','website','en','pending','t','Photo test','<p>x</p>')" >/dev/null
PH=$(G "$U/photos/search?q=dubai%20office" | j "d['data'][0]['pexelsId']")
check "T5c a photo that was not in this site's search is refused" "$(as -X POST $U/posts/p_t7/image/pick -d '{"pexelsId":1}' | j "list(d['error']['fields'])")" "['pexelsId']"
check "T5 photos up to the site's limit are allowed (pick by id, answer is the image)" "$(as -X POST $U/posts/p_t7/image/pick -d '{"pexelsId":'$PH'}' | j "(d['data']['source'], d['data']['pexelsId']==$PH)")|$(as -X POST $U/posts/p_t7/image/pick -d '{"pexelsId":'$PH',"alt":"An office"}' | j "d['data']['alt']")" "('pexels', True)|An office"
check "T5b the next one is refused (the site's own count)" "$(as -X POST $U/posts/p_t7/image/pick -d '{"pexelsId":'$PH'}' | j "(d['error']['code'], d['error']['details']['used'], d['error']['details']['limit'])")" "('quota_exceeded', 2, 2)"

# ---- soft limits are recorded, not refused
py "import asyncio,sys; sys.path.insert(0,'/opt/nour/src'); from ext_marketing import db, meter
async def m():
    await db.open_pool(); await meter.record('$AID','$SID','publishes',1); await db.close_pool()
asyncio.run(m()); print('ok')" >/dev/null
check "T6 over a soft limit: recorded and marked as overage" "$($PSQL "select over_quota from ext_marketing.usage_events where site_id='$SID' and metric='publishes'")" "t"
check "T6b the scheduler holds back work a hard limit would refuse" "$(py "import asyncio,sys; sys.path.insert(0,'/opt/nour/src'); from ext_marketing import db, quotas
async def m():
    await db.open_pool(); r = (await quotas.blocked('$AID','$SID',{'photos':1}), await quotas.blocked('$AID','$SID',{'publishes':1})); await db.close_pool(); print(r)
asyncio.run(m())")" "(True, False)"

# ---- usage by month (this site only)
M=$(TZ=Asia/Dubai date +%Y-%m)
US=$(G "$API/api/v1/account/usage?from=$M&to=$M")
check "T7 usage for this month, this site" "$(echo "$US" | j "(d['data']['months'][0]['month'], d['data']['months'][0]['sites'][0]['siteId']=='$SID', d['data']['months'][0]['sites'][0]['metrics'].get('photos'))")" "('$M', True, 2)"
check "T7b results for the range" "$(echo "$US" | j "list(d['data']['results'][0])")" "['siteId', 'published', 'keywordsCovered']"
check "T7c several months, each listed" "$(G "$API/api/v1/account/usage?from=2026-01&to=2026-03" | j "[m['month'] for m in d['data']['months']]")" "['2026-01', '2026-02', '2026-03']"
check "T7d a wrong month is named" "$(G "$API/api/v1/account/usage?from=2026-13&to=2026-14" | j "sorted(d['error']['fields'])")" "['from', 'to']"
check "T7e too many months at once is refused" "$(G "$API/api/v1/account/usage?from=2020-01&to=2026-01" | j "list(d['error']['fields'])")" "['from']"

# ---- asking for more
check "T8 an upgrade request reaches our team" "$(as -X POST $API/api/v1/account/upgrade-request -d '{"metric":"photos","note":"We need more photos"}' | j "d['data']['requested']")|$(curl -s $API/admin/v1/accounts/$AID "${A[@]}" | j "[(x['metric'], x['note'], x['by'], x['status']) for x in d['data']['upgradeRequests']]")" "True|[('photos', 'We need more photos', 'sara', 'open')]"
check "T8b an unknown metric is refused" "$(as -X POST $API/api/v1/account/upgrade-request -d '{"metric":"unicorns"}' | j "list(d['error']['fields'])")" "['metric']"

check "T9 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id='$AID'" >/dev/null; $PSQL "delete from ext_marketing.plans where id='$PL'" >/dev/null && echo "      (test client and plan removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
