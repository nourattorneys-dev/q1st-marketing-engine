#!/bin/bash
# Step 6d: an article pair is written source-first, the twin translated piece by piece (same structure), structure checks,
# a source edit re-translates the twin, the queue waits for the source. Real model. Needs WP_URL, WP_USER, WP_PASS.
set -u
: "${WP_URL:?set WP_URL}" "${WP_USER:?set WP_USER}" "${WP_PASS:?set WP_PASS}"
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
NA=$($PSQL "select count(*) from public.marketing_approvals")
$PSQL "delete from ext_marketing.accounts where name like 'T6d %'" >/dev/null
AID=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T6d twins"}' | j "d['data']['id']")
SID=$(curl -s -X POST $API/admin/v1/accounts/$AID/sites "${A[@]}" -d '{"name":"t6d","url":"'$WP_URL'","category":"Legal","timezone":"Asia/Dubai","languages":["en","ar"]}' | j "d['data']['id']")
KEY=$(curl -s -X POST $API/admin/v1/accounts/$AID/api-keys "${A[@]}" -d '{"siteId":"'$SID'"}' | j "d['data']['key']")
U=$API/api/v1/sites/$SID
as() { local who=$1; shift; curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: $who" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uid)" "$@"; }
mir() { sudo /opt/nour/venv/bin/python -c "import sys; sys.path.insert(0,'/opt/nour/src'); from ext_marketing import mirror as M; import json; a,b=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$@"; }
wait_post() { for i in $(seq 1 120); do st=$($PSQL "select status from ext_marketing.posts where id='$1'"); printf "\r      $(date +%-I:%M:%S\ %p) $1: $st   "; [ "$st" != drafting ] && break; sleep 5; done; echo; }
WB=$(python3 -c "import json,sys; print(json.dumps({'version':0,'type':'wordpress','settings':{'url':sys.argv[1],'username':sys.argv[2],'postType':'post','publishAs':'draft'},'secret':sys.argv[3],'fieldMap':{'title':'title','slug':'slug','subtitle':'excerpt','metaTitle':'yoast.title','metaDescription':'yoast.description','keywords':'yoast.focuskw','image':'featuredImage'},'body':{'format':'html'},'arabic':{'mode':'translatepress'}}))" "$WP_URL" "$WP_USER" "$WP_PASS")
check "T0 website connected (TranslatePress site)" "$(as sara -X PUT $U/settings/website -d "$WB" | j "(d['data']['status'], d['data']['arabic']['mode'])")" "('ok', 'translatepress')"
check "T0b the plugin reports its new version and TranslatePress' own functions" "$(as sara -X POST $U/settings/website/test | j "d['data']['ok']")|$($PSQL "select config->'learned'->>'plugin' from ext_marketing.site_connections where site_id='$SID' and type='website'")" "True|0.2.0"

# ---- a pair: the source is drafted first, the twin waits for it
D=$(as sara -X POST $U/drafts -d '{"topic":"End-of-service gratuity in the UAE: how it is calculated","kind":"blog","language":"both"}')
SRC=$(echo "$D" | j "d['data']['postIds'][0]"); TW=$(echo "$D" | j "d['data']['postIds'][1]")
check "T1 'both' makes two posts but one drafting job (the source)" "$(echo "$D" | j "(len(d['data']['postIds']), len(d['data']['jobIds']))")" "(2, 1)"
check "T1b the source is the website's main language, the twin points to it" "$($PSQL "select string_agg(language||':'||coalesce(source_id,'-'),',' order by language) from ext_marketing.posts where id in ('$SRC','$TW')")" "ar:$SRC,en:-"
echo "      drafting the English source with the real model..."; wait_post $SRC
echo "      translating the Arabic twin..."; wait_post $TW
check "T2 both are waiting for review" "$($PSQL "select string_agg(status,',' order by language) from ext_marketing.posts where id in ('$SRC','$TW')")" "pending,pending"
$PSQL "select json_build_array((select content from ext_marketing.posts where id='$SRC'),(select content from ext_marketing.posts where id='$TW'))" > /tmp/t6d_pair.json
check "T3 the twin has exactly the source's structure" "$(mir /tmp/t6d_pair.json "M.shape(a)==M.shape(b) and len(M.segments(a))>10")" "True"
echo "      pieces: source $(mir /tmp/t6d_pair.json "len(M.segments(a))") / twin $(mir /tmp/t6d_pair.json "len(M.segments(b))")"
echo "      twin opening: $(mir /tmp/t6d_pair.json "' '.join(M.segments(b)[:3])[:200]")"
G=$(as sara $U/posts/$TW)
check "T4 the twin is written in Arabic (no language findings)" "$(echo "$G" | j "[f['message'] for f in d['data']['findings'] if f['ref']=='language']")" "[]"
check "T4b the twin's title and keyword are Arabic" "$(echo "$G" | j "all(any('؀'<=ch<='ۿ' for ch in x) for x in (d['data']['title'], d['data']['keywords'][0]))")" "True"
check "T5 checks the twin leaves to its source are not on it; no mismatch" "$(echo "$G" | j "sorted({f['ref'] for f in d['data']['findings']} & {'length','intro','headings','faq','structure'})")" "[]"
check "T6 the twin has the source's photo as its own copy" "$($PSQL "select (t.image->>'pexelsId') = (s.image->>'pexelsId') and (t.image->>'key') <> (s.image->>'key') from ext_marketing.posts t, ext_marketing.posts s where t.id='$TW' and s.id='$SRC'")" "t"
check "T6b both drafts are counted as articles" "$($PSQL "select coalesce(sum(qty),0) from ext_marketing.usage_events where site_id='$SID' and metric='articles'")" "2"

# ---- the structure check
V=$(as sara $U/posts/$TW | j "d['data']['version']")
E=$(as sara -X PUT $U/posts/$TW -d "$(as sara $U/posts/$TW | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(json.dumps({'version':d['version'],'content':d['content']+'<p>فقرة إضافية</p>'}))")")
check "T7 an extra paragraph on the twin is a critical, non-overridable mismatch" "$(echo "$E" | j "[(f['code'], f['level'], f['overridable']) for f in d['data']['findings'] if f.get('code')]")" "[('structure_mismatch', 'critical', False)]"
check "T8 ...which cannot be approved even with a reason" "$(as sara -X POST $U/posts/$TW/approve -d '{"overrideReason":"looks fine"}' | j "(d['error']['code'], d['error']['details']['overridable'])")" "('framework_blocked', False)"
CH=$(as sara -X POST $U/posts/$TW/changes -d '{"note":"Keep the same structure as the English"}')
check "T9 asking for changes on the twin translates it again" "$(echo "$CH" | j "d['data']['status']")|$($PSQL "select kind from ext_marketing.jobs where id='$(echo "$CH" | j "d['data']['jobId']")'")" "drafting|post.twin"
wait_post $TW
check "T9b ...and the mismatch is gone" "$(as sara $U/posts/$TW | j "[f['code'] for f in d['data']['findings'] if f.get('code')]")" "[]"
check "T9c a translation asked again is not counted twice" "$($PSQL "select coalesce(sum(qty),0) from ext_marketing.usage_events where site_id='$SID' and metric='articles'")" "2"

# ---- editing the source re-translates a twin still in review
H0=$($PSQL "select source_hash from ext_marketing.posts where id='$TW'")
SE=$(as sara $U/posts/$SRC | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; c=d['content']; i=c.find('</p>'); print(json.dumps({'version':d['version'],'content':c[:i]+' This sentence was added by the reviewer.'+c[i:]}))")
as sara -X PUT $U/posts/$SRC -d "$SE" >/dev/null
check "T10 editing the source sends its twin back to translation" "$($PSQL "select status from ext_marketing.posts where id='$TW'")" "drafting"
wait_post $TW
check "T10b the twin now matches the edited source" "$($PSQL "select source_hash <> '$H0' and status='pending' from ext_marketing.posts where id='$TW'")" "t"

# ---- the queue: on a TranslatePress site the twin waits until its source is published
as sara -X POST $U/posts/$SRC/approve -d '{"overrideReason":"test"}' >/dev/null
as sara -X POST $U/posts/$TW/approve -d '{"overrideReason":"test"}' >/dev/null
check "T11 the approved twin waits for its source to be published" "$(as sara $U/scheduled | j "[x['waiting'] for x in d['data']['items'] if x['id']=='$TW'][0]")" "source_not_published"

check "T12 Nour untouched" "$($PSQL "select count(*) from public.marketing_approvals")" "$NA"
check "T13 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id='$AID'" >/dev/null && echo "      (test client removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
