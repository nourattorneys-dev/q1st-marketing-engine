#!/bin/bash
# Step 10: site health on the live engine, against the contract test site (fakhernco.com). Scanning only reads the site.
# APPLY=1 also writes ONE prepared fix to the live page, checks it, then puts the old text back (needs WP_USER, WP_PASS).
set -u
API=http://127.0.0.1:8140; SITE=${SITE:-site_0a3b334c404b573c}; KEY=${KEY:?set KEY to the contract site key}
PSQL="docker exec nour-postgres psql -U nour_user -d nour_db -Atc"; P=0; F=0; T0=$(date '+%Y-%m-%d %H:%M:%S')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
check() { if [ "$2" == "$3" ]; then echo "PASS  $1"; P=$((P+1)); else echo "FAIL  $1  (got: $2  want: $3)"; F=$((F+1)); fi; }
uid() { cat /proc/sys/kernel/random/uuid; }
as() { curl -s -H "X-Api-Key: $KEY" -H "X-Acting-User: ahmed" -H "Idempotency-Key: $(uid)" -H 'Content-Type: application/json' "$@"; }
U=$API/api/v1/sites/$SITE
wait_job() { for i in $(seq 1 120); do S=$(as $API/api/v1/jobs/$1 | j "d['data']['status']"); [ "$S" == "done" -o "$S" == "failed" ] && break; sleep 3; done; as $API/api/v1/jobs/$1; }

JID=$(as -X POST $U/site/scan -d '{}' | j "d['data']['jobId']")
check "T1 a scan starts" "$(echo $JID | cut -c1-2)" "j_"
check "T1b asking again while it runs gives the same job" "$(as -X POST $U/site/scan -d '{}' | j "d['data']['jobId']")" "$JID"
JR=$(wait_job $JID)
check "T2 the scan finishes and read pages" "$(echo "$JR" | j "(d['data']['status'], d['data']['result']['pages'] > 0)")" "('done', True)"
echo "      scan: $(echo "$JR" | j "d['data']['result']")"
FL=$(as "$U/site/findings?pageSize=100")
check "T3 findings in the spec shape, known levels and statuses" "$(echo "$FL" | j "all({'id','url','level','issue','fix','status'} <= set(f) and f['level'] in ('critical','warn','info') and f['status'] in ('open','prepared','applied','dismissed') for f in d['data'])")" "True"
check "T3b every finding is on the site's own address" "$(echo "$FL" | j "all('fakhernco.com' in f['url'] for f in d['data'])")" "True"
echo "      by check: $(echo "$FL" | j "__import__('collections').Counter(f['code'] for f in d['data']).most_common()")"
IDS1=$(echo "$FL" | j "sorted(f['id'] for f in d['data'] if f['status']=='open')")
JR2=$(wait_job $(as -X POST $U/site/scan -d '{}' | j "d['data']['jobId']"))
IDS2=$(as "$U/site/findings?status=open&pageSize=100" | j "sorted(f['id'] for f in d['data'])")
check "T4 a second scan of the unchanged site keeps the same findings (same ids)" "$IDS2" "$IDS1"
PICK=$(echo "$FL" | j "[f['id'] for f in sorted(d['data'], key=lambda f: __import__('urllib.parse').parse.urlparse(f['url']).path.strip('/') == '') if f['code'] in ('missing_description','description_length','title_length','missing_title','duplicate_title','duplicate_description') and f['status'] in ('open','prepared')][:2]")   # inner pages first: the home page's SEO text lives in the SEO plugin's settings
if [ "$PICK" != "[]" ]; then
  PJ=$(echo "$PICK" | tr "'" '"'); N=$(echo "$PJ" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
  FJ=$(wait_job $(as -X POST $U/site/fixes -d "{\"ids\": $PJ}" | j "d['data']['jobId']"))
  check "T5 prepare answers for every picked title/description" "$(echo "$FJ" | j "(d['data']['status'], len(d['data']['result']['prepared']) + len(d['data']['result']['failed']))")" "('done', $N)"
  BANDS=$($PSQL "select value from ext_marketing.settings where key='default_rules'")
  check "T5b each proposal is inside the site's length band" "$(as "$U/site/findings?status=prepared&pageSize=100" | BANDS="$BANDS" python3 -c "
import sys, json, os
d = json.load(sys.stdin)['data']; b = json.loads(os.environ['BANDS'])
band = lambda code: b['metaTitle' if 'title' in code else 'metaDescription']
print(len(d) > 0 and all(band(f['code'])[0] <= len(f['fix'][len('Proposed: '):]) <= band(f['code'])[1] for f in d))")" "True"
  echo "      proposals: $(as "$U/site/findings?status=prepared&pageSize=5" | j "[(f['url'], f['fix']) for f in d['data']]")"
else
  echo "SKIP  T5 the site has no title/description finding to prepare"
fi
H1=$(echo "$FL" | j "[f['id'] for f in d['data'] if f['code'] in ('missing_h1','many_h1','img_alt','broken_link') and f['status']=='open'][:1]")
if [ "$H1" != "[]" ]; then
  check "T6 apply on a website-only finding is refused with advice" "$(as -X POST $U/site/findings/bulk -d "{\"action\":\"apply\",\"ids\": $(echo $H1 | tr "'" '"')}" | j "d['data']['failed'][0]['code']")" "not_prepared"
fi
check "T7 an unknown action is refused" "$(as -X POST $U/site/findings/bulk -d '{"action":"burn","ids":["x"]}' | j "list(d['error']['fields'])")" "['action']"
if [ "${APPLY:-0}" == "1" ]; then
  : "${WP_USER:?set WP_USER}" "${WP_PASS:?set WP_PASS}"
  FID=$(as "$U/site/findings?status=prepared&pageSize=100" | j "[f['id'] for f in d['data'] if __import__('urllib.parse').parse.urlparse(f['url']).path.strip('/')][0]")
  check "T8 APPLY: one prepared fix is written to the live page" "$(as -X POST $U/site/findings/bulk -d "{\"action\":\"apply\",\"ids\":[\"$FID\"]}" | j "d['data']['done']")" "['$FID']"
  OUT=$($PSQL "select fix_data::text from ext_marketing.site_findings where id='$FID'" | python3 -c "
import sys, json, os, base64, urllib.request
f = json.load(sys.stdin)
key = {'metaTitle': '_yoast_wpseo_title', 'metaDescription': '_yoast_wpseo_metadesc'}[f['field']]
auth = 'Basic ' + base64.b64encode(f\"{os.environ['WP_USER']}:{os.environ['WP_PASS']}\".encode()).decode()
def call(path, body=None):
    r = urllib.request.Request('https://fakhernco.com/wp-json/wp/v2/' + path, data=json.dumps(body).encode() if body else None,
                               headers={'X-Q1st-Auth': auth, 'Content-Type': 'application/json'}, method='POST' if body else 'GET')
    return json.load(urllib.request.urlopen(r, timeout=30))
for t in ('pages', 'posts'):
    try:
        live = call(f\"{t}/{f['externalId']}?context=edit&_fields=meta\")['meta'][key]; break
    except Exception:
        continue
back = call(f\"{t}/{f['externalId']}\", {'meta': {key: f['before'][f['field']]}})['meta'][key]
print(live == f['value'], back == f['before'][f['field']])")
  check "T8b the live page had exactly the proposed text, and the old text is back" "$OUT" "True True"
  $PSQL "update ext_marketing.site_findings set status='prepared', applied_at=null where id='$FID'" >/dev/null
else
  echo "SKIP  T8 APPLY (writes one live page and puts it back): run again with APPLY=1 when you agree"
fi
check "T9 no errors in the journal" "$(sudo journalctl -u nour-mkt-ext --since "$T0" --no-pager | grep -c ' ERROR \|Traceback')" "0"
echo "-----"; echo "PASSED $P   FAILED $F"
