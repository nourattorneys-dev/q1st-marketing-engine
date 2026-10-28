#!/bin/bash
# Step 6c-1: website connection (general record, Strapi adapter), idempotent changes, editing locks.
set -u
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
SU=$(grep -E '^STRAPI_URL=' $ENV | cut -d= -f2- | tr -d '"'); ST=$(grep -E '^STRAPI_API_TOKEN=' $ENV | cut -d= -f2- | tr -d '"')
SC=$(grep -E '^STRAPI_COLLECTION=' $ENV | cut -d= -f2- | tr -d '"'); SC=${SC:-articles}
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
NA=$($PSQL "select count(*) from public.marketing_approvals")
$PSQL "delete from ext_marketing.accounts where name like 'T6c1 %'" >/dev/null
AID=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T6c1 site"}' | j "d['data']['id']")
SID=$(curl -s -X POST $API/admin/v1/accounts/$AID/sites "${A[@]}" -d '{"name":"t6c1","url":"https://www.quantum1st.com","category":"Technology","timezone":"Asia/Dubai","languages":["en","ar"]}' | j "d['data']['id']")
KEY=$(curl -s -X POST $API/admin/v1/accounts/$AID/api-keys "${A[@]}" -d '{"siteId":"'$SID'"}' | j "d['data']['key']")
U=$API/api/v1/sites/$SID
H=(-H "X-Api-Key: $KEY" -H 'Content-Type: application/json')
as() { local who=$1; shift; curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: $who" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uid)" "$@"; }

# ---- /meta and the moved connections
M=$(curl -s $API/api/v1/meta)
check "T1 /meta offers only types with an adapter (strapi now)" "$(echo "$M" | j "[t['type'] for t in d['data']['targetTypes']]")" "['strapi']"
check "T1b /meta limits.lockMinutes and features from settings" "$(echo "$M" | j "(d['data']['limits']['lockMinutes'], d['data']['features'])")" "(15, [])"
check "T2 no Strapi-only connection rows left (moved to 'website')" "$($PSQL "select count(*) from ext_marketing.site_connections where type='strapi'")" "0"
check "T2b a moved row keeps its address and field map" "$($PSQL "select count(*) from ext_marketing.site_connections where type='website' and config->>'type'='strapi' and (config->'settings'->>'url' is null or config->'fieldMap' is null)")" "0"

# ---- idempotency
check "T3 a change without Idempotency-Key is refused" "$(curl -s -X POST "${H[@]}" $U/keywords -d '{"phrases":"idem test one","language":"en"}' | j "d['error']['code']")" "idempotency_key_required"
K1=$(uid)
R1=$(curl -s -D /tmp/h1 -X POST "${H[@]}" -H "Idempotency-Key: $K1" $U/keywords -d '{"phrases":"idem test two","language":"en"}')
R2=$(curl -s -D /tmp/h2 -X POST "${H[@]}" -H "Idempotency-Key: $K1" $U/keywords -d '{"phrases":"idem test two","language":"en"}')
check "T4 the same key twice returns the first answer" "$([ "$R1" == "$R2" ] && grep -qi '^idempotent-replay: true' /tmp/h2 && echo same)" "same"
check "T4b ...and the change happened once" "$($PSQL "select count(*) from ext_marketing.keywords where site_id='$SID' and phrase='idem test two'")" "1"
check "T5 the same key on another action is refused" "$(curl -s -X POST "${H[@]}" -H "Idempotency-Key: $K1" $U/keywords/topics -d '{}' | j "d['error']['code']")" "idempotency_mismatch"
check "T5b reads need no key" "$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" $U/keywords)" "200"

# ---- website connection
check "T6 a new site starts with no connection" "$(as sara $U/settings/website | j "(d['data']['type'], d['data']['status'], d['data']['version'], d['data']['secretSet'])")" "(None, 'missing', 0, False)"
check "T7 an unknown type is refused" "$(as sara -X PUT $U/settings/website -d '{"version":0,"type":"joomla"}' | j "list(d['error']['fields'])")" "['type']"
check "T8 the type's required fields are named" "$(as sara -X PUT $U/settings/website -d '{"version":0,"type":"strapi","settings":{}}' | j "sorted(d['error']['fields'])")" "['settings.collection', 'settings.url']"
check "T9 http:// is refused" "$(as sara -X PUT $U/settings/website -d '{"version":0,"type":"strapi","settings":{"url":"http://cms.example.com","collection":"x"},"secret":"x"}' | j "'settings.url' in d['error']['fields']")" "True"
check "T9b a format the type does not have is refused" "$(as sara -X PUT $U/settings/website -d '{"version":0,"type":"strapi","settings":{"url":"'$SU'","collection":"'$SC'"},"body":{"format":"boldbuilder"},"secret":"x"}' | j "list(d['error']['fields'])")" "['body.format']"
check "T10 a wrong token is not saved (422 not_connected)" "$(as sara -X PUT $U/settings/website -d '{"version":0,"type":"strapi","settings":{"url":"'$SU'","collection":"'$SC'"},"secret":"wrong-token"}' | j "d['error']['code']")|$(as sara $U/settings/website | j "d['data']['version']")" "not_connected|0"
TR=$(as sara -X POST $U/settings/website/test -d '{"type":"strapi","settings":{"url":"'$SU'","collection":"'$SC'"},"secret":"'$ST'"}')
check "T11 test with unsaved values reads the fields and suggests a map" "$(echo "$TR" | j "(d['data']['ok'], len(d['data']['fields'])>0, 'title' in d['data']['suggestedFieldMap'])")" "(True, True, True)"
check "T11b the test saved nothing" "$(as sara $U/settings/website | j "d['data']['version']")" "0"
FM=$(echo "$TR" | j "__import__('json').dumps(d['data']['suggestedFieldMap'])")
S1=$(as sara -X PUT $U/settings/website -d '{"version":0,"type":"strapi","settings":{"url":"'$SU'","collection":"'$SC'"},"secret":"'$ST'","fieldMap":'"$FM"',"body":{"format":"blocks"},"arabic":{"mode":"locale"}}')
check "T12 a working connection is saved, tested, secret never returned" "$(echo "$S1" | j "(d['data']['status'], d['data']['secretSet'], d['data']['version'], 'secret' in d['data']['settings'], '$ST' in str(d))")" "('ok', True, 1, False, False)"
check "T12b the secret is stored encrypted" "$($PSQL "select position('$ST' in encode(secret_enc,'escape'))=0 from ext_marketing.site_connections where site_id='$SID' and type='website'")" "t"
check "T13 a field-map change alone saves without a new test" "$(as sara -X PUT $U/settings/website -d '{"version":1,"fieldMap":{"title":"title"}}' | j "(d['data']['version'], d['data']['fieldMap'])")" "(2, {'title': 'title'})"
check "T13b a stale version is refused" "$(as sara -X PUT $U/settings/website -d '{"version":1,"fieldMap":{}}' | j "d['error']['code']")" "version_conflict"
check "T14 categories come from the connected website" "$(as sara $U/website/categories | j "d['ok'] and isinstance(d['data'], list)")" "True"
check "T15 the site shows connections.website" "$(as sara $U | j "d['data']['connections']['website']")" "ok"
check "T15b the old Strapi routes are gone" "$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" $U/settings/strapi)" "404"

# ---- editing locks (a post row made directly: no model needed here)
PID=p_t6c1_$(date +%s)
$PSQL "insert into ext_marketing.posts (id, site_id, kind, platform, language, status, topic, title, content) values ('$PID','$SID','blog','website','en','pending','t','Lock test','<p>x</p>')" >/dev/null
V() { $PSQL "select version from ext_marketing.posts where id='$PID'"; }
L=$(as sara -X POST $U/posts/$PID/lock)
check "T16 opening the editor takes the lock" "$(echo "$L" | j "d['data']['by']")" "sara"
check "T16b the lock lasts lock_minutes" "$($PSQL "select round(extract(epoch from lock_expires_at - now())/60) from ext_marketing.posts where id='$PID'")" "15"
check "T17 another person's edit is refused while it is held" "$(as omar -X PUT $U/posts/$PID -d '{"version":'$(V)',"title":"omar"}' | j "(d['error']['code'], d['error']['details']['lockedBy'])")" "('locked', 'sara')"
check "T18 another person cannot take the lock" "$(as omar -X POST $U/posts/$PID/lock | j "d['error']['code']")" "locked"
check "T18b photo changes are refused too" "$(as omar -X DELETE $U/posts/$PID/image | j "d['error']['code']")" "locked"
check "T19 the holder can edit" "$(as sara -X PUT $U/posts/$PID -d '{"version":'$(V)',"title":"sara edit"}' | j "d['data']['title']")" "sara edit"
check "T19b renewing keeps the holder" "$(as sara -X POST $U/posts/$PID/lock | j "d['data']['by']")" "sara"
check "T20 take over moves the lock" "$(as omar -X POST $U/posts/$PID/takeover | j "d['data']['lock']['by']")" "omar"
check "T20b ...and the old holder is now refused" "$(as sara -X PUT $U/posts/$PID -d '{"version":'$(V)',"title":"x"}' | j "d['error']['code']")" "locked"
check "T21 leaving the editor releases it" "$(as omar -X DELETE $U/posts/$PID/lock | j "d['data']['released']")|$(as sara $U/posts/$PID | j "d['data']['lock']")" "True|None"
check "T21b only the holder can release" "$(as omar -X POST $U/posts/$PID/lock >/dev/null; as sara -X DELETE $U/posts/$PID/lock >/dev/null; as sara $U/posts/$PID | j "d['data']['lock']['by']")" "omar"
$PSQL "update ext_marketing.posts set lock_expires_at = now() - interval '1 minute' where id='$PID'" >/dev/null
check "T22 an expired lock counts as free" "$(as sara -X PUT $U/posts/$PID -d '{"version":'$(V)',"title":"after expiry"}' | j "d['data']['title']")" "after expiry"
check "T22b ...and anyone can take it" "$(as sara -X POST $U/posts/$PID/lock | j "d['data']['by']")" "sara"

check "T23 Nour untouched" "$($PSQL "select count(*) from public.marketing_approvals")" "$NA"
check "T24 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
echo "      (test site kept: $SID. Remove later: $PSQL \"delete from ext_marketing.accounts where id='$AID'\")"
echo "-----"; echo "PASSED $P   FAILED $F"
