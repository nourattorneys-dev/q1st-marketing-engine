#!/bin/bash
# Step 6c-2: the WordPress adapter through a site's own WordPress (a DRAFT is published and deleted again; nothing goes live).
# Needs: WP_URL (e.g. https://fakhernco.com), WP_USER, WP_PASS (the user's application password) in the environment.
set -u
: "${WP_URL:?set WP_URL}" "${WP_USER:?set WP_USER}" "${WP_PASS:?set WP_PASS}"
API=http://127.0.0.1:8140; ENV=/opt/nour/compose/.env; AK=$(grep -E '^EXT_MKT_ADMIN_KEY=' $ENV | cut -d= -f2-)
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
A=(-H "X-Admin-Key: $AK" -H "X-Acting-User: ahmed" -H 'Content-Type: application/json')
NA=$($PSQL "select count(*) from public.marketing_approvals")
$PSQL "delete from ext_marketing.accounts where name like 'T6c2 %'" >/dev/null
AID=$(curl -s -X POST $API/admin/v1/accounts "${A[@]}" -d '{"name":"T6c2 wordpress"}' | j "d['data']['id']")
SID=$(curl -s -X POST $API/admin/v1/accounts/$AID/sites "${A[@]}" -d '{"name":"t6c2","url":"'$WP_URL'","category":"Legal","timezone":"Asia/Dubai","languages":["en","ar"]}' | j "d['data']['id']")
KEY=$(curl -s -X POST $API/admin/v1/accounts/$AID/api-keys "${A[@]}" -d '{"siteId":"'$SID'"}' | j "d['data']['key']")
U=$API/api/v1/sites/$SID
as() { curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: sara" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uid)" "$@"; }
body() { python3 -c "import json,sys; print(json.dumps({'version':int(sys.argv[1]),'type':'wordpress','settings':{'url':sys.argv[2],'username':sys.argv[3],'postType':'post'},'secret':sys.argv[4],**(json.loads(sys.argv[5]) if len(sys.argv)>5 else {})}))" "$@"; }

M=$(curl -s $API/api/v1/meta)
check "T1 /meta offers WordPress and Strapi" "$(echo "$M" | j "sorted(t['type'] for t in d['data']['targetTypes'])")" "['strapi', 'wordpress']"
check "T1b WordPress offers only the body formats it can write" "$(echo "$M" | j "[t['bodyFormats'] for t in d['data']['targetTypes'] if t['type']=='wordpress'][0]")" "['html']"
check "T2 a wrong application password is not saved" "$(as -X PUT $U/settings/website -d "$(body 0 "$WP_URL" "$WP_USER" 'wrong pass word here')" | j "d['error']['code']")" "not_connected"
check "T2b ...with a plain message" "$(as -X PUT $U/settings/website -d "$(body 0 "$WP_URL" "$WP_USER" 'wrong pass word here')" | j "'WordPress' in d['error']['message']")" "True"
check "T3 a site that is not WordPress is named" "$(as -X POST $U/settings/website/test -d "$(body 0 https://example.com "$WP_USER" x)" | j "'not answer as a WordPress' in d['data']['error']")" "True"
check "T3b http:// is refused" "$(as -X PUT $U/settings/website -d "$(body 0 http://fakhernco.com "$WP_USER" x)" | j "'settings.url' in d['error']['fields']")" "True"
check "T3c the user name is required" "$(as -X PUT $U/settings/website -d "$(body 0 "$WP_URL" '' x)" | j "list(d['error']['fields'])")" "['settings.username']"
TR=$(as -X POST $U/settings/website/test -d "$(body 0 "$WP_URL" "$WP_USER" "$WP_PASS")")
check "T4 the right password passes the test" "$(echo "$TR" | j "d['data']['ok']")" "True"
check "T4b categories read from the site" "$(echo "$TR" | j "len(d['data']['categories'])>0")" "True"
check "T4c the plugin is found: SEO fields offered, no warning" "$(echo "$TR" | j "('yoast.title' in d['data']['fields'], d['data']['note'])")" "(True, None)"
check "T4d the suggested map uses Yoast and the featured image" "$(echo "$TR" | j "(d['data']['suggestedFieldMap']['metaTitle'], d['data']['suggestedFieldMap']['image'])")" "('yoast.title', 'featuredImage')"
FM=$(echo "$TR" | j "__import__('json').dumps(d['data']['suggestedFieldMap'])")
S1=$(as -X PUT $U/settings/website -d "$(body 0 "$WP_URL" "$WP_USER" "$WP_PASS" '{"fieldMap":'"$FM"',"body":{"format":"html"},"arabic":{"mode":"translatepress"}}')")
check "T5 the connection is saved and ok" "$(echo "$S1" | j "(d['data']['type'], d['data']['status'], d['data']['secretSet'], d['data']['version'])")" "('wordpress', 'ok', True, 1)"
check "T5b the password is never returned" "$(echo "$S1" | grep -c "$WP_PASS")" "0"
check "T5c the plugin and Yoast versions are remembered" "$($PSQL "select (config->'learned'->>'plugin') is not null and (config->'learned'->>'yoast') is not null from ext_marketing.site_connections where site_id='$SID' and type='website'")" "t"
check "T6 categories endpoint" "$(as $U/website/categories | j "len(d['data'])>0 and all('slug' in x and 'name' in x for x in d['data'])")" "True"
check "T6b a body format WordPress does not offer is refused" "$(as -X PUT $U/settings/website -d '{"version":1,"body":{"format":"blocks"}}' | j "list(d['error']['fields'])")" "['body.format']"

echo "      publishing a DRAFT with a photo, updating it, reading it back, deleting it..."
# the probe runs with exactly the running service's environment (database address, encryption key), read from its process
MPID=$(systemctl show -p MainPID --value nour-mkt-ext)
R=$(sudo env -i $(sudo tr '\0' '\n' < /proc/$MPID/environ | grep -E '^(POSTGRES_DSN|EXT_MKT_|MINIO_|PEXELS_)' | xargs) EXT_SRC=/opt/nour/src \
      /opt/nour/venv/bin/python /tmp/wp_publish_probe.py $SID 2>&1 | tail -1)
echo "      $R"
check "T7 a draft is created" "$(echo "$R" | j "(d['created']['status'], d['created']['target'])")" "('draft', 'wordpress')"
check "T7b it is not visible to the public" "$(echo "$R" | j "d['publicStatus'] in (401, 403, 404)")" "True"
check "T8 updating writes to the same post" "$(echo "$R" | j "(d['updatedSameId'], d['readBack']['title'])")" "(True, 'Q1st test draft (updated)')"
check "T9 SEO title, description and focus keyword written to Yoast" "$(echo "$R" | j "(d['readBack']['seoTitle'], d['readBack']['seoDescription'], d['readBack']['focusKeyword'])")" "('Q1st test meta title', 'Q1st test meta description.', 'q1st test keyword')"
check "T10 excerpt, body, category written" "$(echo "$R" | j "(d['readBack']['excerpt'], d['readBack']['contentHasHeading'], d['readBack']['categoryCount'])")" "('A test of the publishing connection.', True, 1)"
check "T11 featured photo uploaded with its alt text" "$(echo "$R" | j "(d['readBack']['hasImage'], d['readBack']['imageAlt'])")" "(True, 'Blue test picture')"
check "T12 the test post is deleted from the site" "$(echo "$R" | j "d['afterDelete']")" "404"

check "T13 Nour untouched" "$($PSQL "select count(*) from public.marketing_approvals")" "$NA"
check "T14 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
$PSQL "delete from ext_marketing.accounts where id='$AID'" >/dev/null && echo "      (test client removed)"
echo "-----"; echo "PASSED $P   FAILED $F"
