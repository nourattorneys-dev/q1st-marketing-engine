#!/bin/bash
# Step 8: webhooks on the live engine (a test client; a public echo service plays the site's app).
set -u
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
$PSQL "delete from ext_marketing.accounts where name like 'T8 %'" >/dev/null; $PSQL "delete from ext_marketing.plans where name like 'T8 %'" >/dev/null
P1=$(curl -s -X POST $API/admin/v1/plans "${A[@]}" -d '{"name":"T8 plan one","quotas":[]}' | j "d['data']['id']")
P2=$(curl -s -X POST $API/admin/v1/plans "${A[@]}" -d '{"name":"T8 plan two","quotas":[]}' | j "d['data']['id']")
A1=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T8 client","planId":"'$P1'"}' | j "d['data']['id']")
S1=$(curl -s -X POST $API/admin/v1/accounts/$A1/sites "${A[@]}" -d '{"name":"t8","url":"https://www.quantum1st.com","category":"Technology","timezone":"Asia/Dubai","languages":["en"]}' | j "d['data']['id']")
K1=$(curl -s -X POST $API/admin/v1/accounts/$A1/api-keys "${A[@]}" -d '{"siteId":"'$S1'"}' | j "d['data']['key']")
put() { curl -s -X PUT $API/api/v1/sites/$S1/webhook -H "X-Api-Key: $K1" -H "X-Acting-User: sara" -H "Idempotency-Key: $(uid)" -H 'Content-Type: application/json' -d "$1"; }
last() { $PSQL "select event||'|'||status||'|'||attempts||'|'||coalesce(last_code::text,'-') from ext_marketing.webhook_deliveries where site_id='$S1' order by created_at desc limit 1"; }
OKURL=https://httpbin.org/status/200; BADURL=https://httpbin.org/status/500

R=$(put '{"url":"'$OKURL'"}')
SEC=$(echo "$R" | j "d['data']['secret']")
check "T1 first save: secret returned once, all events by default" "$(echo "$SEC" | cut -c1-6)|$(echo "$R" | j "len(d['data']['events'])")" "whsec_|$($PSQL "select jsonb_array_length(value) from ext_marketing.settings where key='webhook_events'")"
check "T2 GET: address and events, never the secret" "$(curl -s $API/api/v1/sites/$S1/webhook -H "X-Api-Key: $K1" | j "(d['data']['url'], 'secret' in d['data'])")" "('$OKURL', False)"
check "T3 a private address is refused" "$(put '{"url":"https://127.0.0.1/hook"}' | j "list(d['error']['fields'])")" "['url']"
check "T3b a plain http address is refused" "$(put '{"url":"http://httpbin.org/status/200"}' | j "list(d['error']['fields'])")" "['url']"
check "T3c an unknown event is refused" "$(put '{"url":"'$OKURL'","events":["post.nope"]}' | j "list(d['error']['fields'])")" "['events']"
check "T4 the stored secret is encrypted (not readable in the table)" "$($PSQL "select position('$SEC' in encode(secret_enc,'escape'))=0 from ext_marketing.webhooks where site_id='$S1'")" "t"
TR=$(curl -s -X POST $API/admin/v1/sites/$S1/webhook/test "${A[@]}")
check "T5 admin test reaches the address (200) and the state is Working" "$(echo "$TR" | j "(d['data']['recent'][0]['event'], d['data']['recent'][0]['status'], d['data']['recent'][0]['code'], d['data']['status'])")" "('webhook.test', 'sent', 200, 'ok')"
V=$(curl -s $API/admin/v1/accounts/$A1 "${A[@]}" | j "d['data']['account']['version']")
curl -s -o /dev/null -X PUT $API/admin/v1/accounts/$A1 "${A[@]}" -d '{"version":'$V',"planId":"'$P2'"}'
for i in 1 2 3 4 5 6; do [ "$(last | cut -d'|' -f2)" == "sent" ] && break; sleep 1; done
check "T6 a real event: changing the plan sends plan.changed, delivered" "$(last)" "plan.changed|sent|1|200"
check "T6b its body is {event, siteId, at (+04:00), data:{planId}}" "$($PSQL "select body::jsonb->>'event', body::jsonb->>'siteId', right(body::jsonb->>'at',6), body::jsonb->'data'->>'planId' from ext_marketing.webhook_deliveries where site_id='$S1' order by created_at desc limit 1")" "plan.changed|$S1|+04:00|$P2"
put '{"url":"'$BADURL'"}' >/dev/null
TR=$(curl -s -X POST $API/admin/v1/sites/$S1/webhook/test "${A[@]}")
check "T7 an address answering 500: kept for another try, state Failing with the reason" "$(echo "$TR" | j "(d['data']['recent'][0]['status'], d['data']['recent'][0]['attempts'], d['data']['recent'][0]['code'], d['data']['status'], d['data']['lastError'])")" "('pending', 1, 500, 'failing', 'The address answered 500.')"
check "T7b the next try waits the first backoff" "$($PSQL "select abs(extract(epoch from next_at - now()) - (select (value->>0)::numeric from ext_marketing.settings where key='webhook_backoff_seconds')) < 10 from ext_marketing.webhook_deliveries where site_id='$S1' and status='pending' limit 1")" "t"
check "T8 the client page data carries the webhook state" "$(curl -s $API/admin/v1/accounts/$A1 "${A[@]}" | j "(d['data']['webhooks']['$S1']['status'], d['data']['webhooks']['$S1']['waiting'])")" "('failing', 1)"
check "T9 an empty address removes it; waiting deliveries are dropped" "$(put '{"url":""}' | j "d['data']")|$($PSQL "select count(*) from ext_marketing.webhook_deliveries where site_id='$S1' and status='pending'")" "{'url': None, 'events': []}|0"
check "T10 another site's key cannot read this webhook" "$(curl -s $API/api/v1/sites/site_0a3b334c404b573c/webhook -H "X-Api-Key: $K1" | j "d['error']['code']")" "not_found"
check "T11 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id = '$A1'" >/dev/null; $PSQL "delete from ext_marketing.plans where id in ('$P1','$P2')" >/dev/null && echo "      (test client and plans removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
