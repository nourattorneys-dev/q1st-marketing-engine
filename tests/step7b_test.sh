#!/bin/bash
# Step 7b: the admin data behind the portal's Usage & revenue page and upgrade requests.
set -u
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
$PSQL "delete from ext_marketing.accounts where name like 'T7b %'" >/dev/null; $PSQL "delete from ext_marketing.plans where name like 'T7b %'" >/dev/null
PM=$(curl -s -X POST $API/admin/v1/plans "${A[@]}" -d '{"name":"T7b monthly","price":600,"currency":"AED","quotas":[{"metric":"photos","limit":1,"mode":"soft"}]}' | j "d['data']['id']")
PY=$(curl -s -X POST $API/admin/v1/plans "${A[@]}" -d '{"name":"T7b yearly","price":1200,"currency":"AED","billingPeriod":"year","quotas":[]}' | j "d['data']['id']")
A1=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T7b monthly client","planId":"'$PM'"}' | j "d['data']['id']")
A2=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T7b yearly client","planId":"'$PY'","priceOverride":2400}' | j "d['data']['id']")
S1=$(curl -s -X POST $API/admin/v1/accounts/$A1/sites "${A[@]}" -d '{"name":"t7b","url":"https://www.quantum1st.com","category":"Technology","timezone":"Asia/Dubai","languages":["en"]}' | j "d['data']['id']")
K1=$(curl -s -X POST $API/admin/v1/accounts/$A1/api-keys "${A[@]}" -d '{"siteId":"'$S1'"}' | j "d['data']['key']")
# this month's usage, written as the engine writes it (2 photos on a soft limit of 1 -> 1 over; tokens with a cost)
$PSQL "insert into ext_marketing.usage_events (account_id, site_id, metric, qty, over_quota) values ('$A1','$S1','photos',1,false),('$A1','$S1','photos',1,true)" >/dev/null
$PSQL "insert into ext_marketing.usage_events (account_id, site_id, metric, qty, tokens_in, tokens_out, cost_estimate) values ('$A1','$S1','aiTokens',0,1000,500,12.5)" >/dev/null

check "T1 metrics with labels for the portal" "$(curl -s $API/admin/v1/metrics "${A[@]}" | j "all('label' in m and 'key' in m for m in d['data']) and len(d['data'])>5")" "True"
RV=$(curl -s "$API/admin/v1/revenue" "${A[@]}")
c1() { echo "$RV" | j "[c for c in d['data']['clients'] if c['accountId']=='$1'][0]$2"; }
check "T2 monthly client: price, AI cost, margin" "$(c1 $A1 "['price'], ''")" "(600.0, '')"
check "T2b ...cost and margin" "$(c1 $A1 "['aiCost'], ''")|$(c1 $A1 "['margin'], ''")" "(12.5, '')|(587.5, '')"
check "T2c ...usage against the limit, with the overage" "$(c1 $A1 "['usage'][[u['metric'] for u in [c for c in d['data']['clients'] if c['accountId']=='$A1'][0]['usage']].index('photos')]")|$(c1 $A1 "['overage'], ''")" "{'metric': 'photos', 'used': 2, 'over': 1, 'limit': 1, 'mode': 'soft'}|(1, '')"
check "T3 a yearly price counts one twelfth, the client's own price wins" "$(c1 $A2 "['price'], ''")" "(200.0, '')"
check "T4 another month: nothing used" "$(curl -s "$API/admin/v1/revenue?month=2026-01" "${A[@]}" | j "[c['usage'] for c in d['data']['clients'] if c['accountId']=='$A1'][0]")" "[]"
check "T4b a wrong month is named" "$(curl -s "$API/admin/v1/revenue?month=2026-1" "${A[@]}" | j "list(d['error']['fields'])")" "['month']"
check "T4c the totals add up" "$(echo "$RV" | j "d['data']['totals']['price'] >= 800 and d['data']['totals']['aiCost'] >= 12.5")" "True"

# upgrade requests: sent from the app, handled here
curl -s -o /dev/null -X POST $API/api/v1/account/upgrade-request -H "X-Api-Key: $K1" -H "X-Acting-User: sara" -H "Idempotency-Key: $(uid)" -H 'Content-Type: application/json' -d '{"metric":"photos","note":"More photos please"}'
UL=$(curl -s "$API/admin/v1/upgrade-requests" "${A[@]}")
UID1=$(echo "$UL" | j "[u['id'] for u in d['data'] if u['accountId']=='$A1'][0]")
check "T5 open requests across clients, with names" "$(echo "$UL" | j "[(u['accountName'], u['siteName'], u['metric'], u['by']) for u in d['data'] if u['accountId']=='$A1']")" "[('T7b monthly client', 't7b', 'photos', 'sara')]"
check "T5b the clients list counts them" "$(curl -s $API/admin/v1/accounts "${A[@]}" | j "[a['openRequests'] for a in d['data'] if a['id']=='$A1'][0]")" "1"
check "T6 marked done, with who and when" "$(curl -s -X POST $API/admin/v1/upgrade-requests/$UID1 "${A[@]}" -d '{"status":"done"}' | j "d['data']['status']")|$($PSQL "select handled_by is not null and handled_at is not null from ext_marketing.upgrade_requests where id='$UID1'")" "done|t"
check "T6b no longer in the open list" "$(curl -s "$API/admin/v1/upgrade-requests" "${A[@]}" | j "[u for u in d['data'] if u['id']=='$UID1']")" "[]"
check "T6c a wrong status is refused" "$(curl -s -X POST $API/admin/v1/upgrade-requests/$UID1 "${A[@]}" -d '{"status":"maybe"}' | j "list(d['error']['fields'])")" "['status']"
check "T6d an unknown request is 404" "$(curl -s -X POST $API/admin/v1/upgrade-requests/up_nope "${A[@]}" -d '{"status":"done"}' | j "d['error']['code']")" "not_found"
check "T7 admin routes need the admin key" "$(curl -s -o /dev/null -w '%{http_code}' $API/admin/v1/revenue)" "401"

check "T8 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id in ('$A1','$A2')" >/dev/null; $PSQL "delete from ext_marketing.plans where id in ('$PM','$PY')" >/dev/null && echo "      (test clients and plans removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
