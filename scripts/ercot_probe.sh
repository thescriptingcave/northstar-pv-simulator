#!/usr/bin/env bash
# Probe the ERCOT Public API directly, mirroring what the fetch client does.
#
#     ./scripts/ercot_probe.sh
#
# Reads ERCOT_USERNAME, ERCOT_PASSWORD and ERCOT_SUBSCRIPTION_KEY from .env.
# Prints response bodies so an empty result can be told from an error, which
# the fetch client cannot do once it has parsed the payload into a frame.

set -uo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a
: "${ERCOT_USERNAME:?set in .env}" "${ERCOT_PASSWORD:?set in .env}"
: "${ERCOT_SUBSCRIPTION_KEY:?set in .env}"

CLIENT_ID="fec253ea-0d06-4272-a5e6-b478baeecd70"
TOKEN_URL="https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
HOST="https://api.ercot.com/api/public-reports"
RT="/np6-905-cd/spp_node_zone_hub"

echo "1. Acquiring token"
TOKEN=$(curl -sS -X POST "$TOKEN_URL" \
  -d "username=$ERCOT_USERNAME" \
  --data-urlencode "password=$ERCOT_PASSWORD" \
  -d "grant_type=password" \
  -d "scope=openid ${CLIENT_ID} offline_access" \
  -d "client_id=${CLIENT_ID}" \
  -d "response_type=id_token" | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

if [ -z "$TOKEN" ]; then echo "   FAILED to get a token"; exit 1; fi
echo "   ok (${#TOKEN} chars)"

probe() {
  local label="$1"; shift
  echo
  echo "=== $label ==="
  echo "    $*"
  # Capture the status separately: a non-JSON body is an error page, and the
  # status code is the only thing that says which error.
  local body status
  body=$(curl -sS -G "${HOST}${RT}" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Ocp-Apim-Subscription-Key: $ERCOT_SUBSCRIPTION_KEY" \
    -w "\n__STATUS__%{http_code}" "$@")
  status="${body##*__STATUS__}"
  body="${body%__STATUS__*}"
  echo "    HTTP $status"
  printf '%s' "$body" | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
except Exception:
    sys.stdin.seek(0)
    print('    non-JSON body:', sys.stdin.read()[:400]); raise SystemExit
if 'data' in p:
    meta = p.get('_meta', {})
    print(f\"    rows={len(p['data'])}  totalRecords={meta.get('totalRecords')}  \"
          f\"page={meta.get('currentPage')} of {meta.get('totalPages')}\")
    if p['data']:
        print('    first row:', p['data'][0])
    fields = [f['name'] for f in p.get('fields', [])]
    print('    fields:', fields[:8])
else:
    print('    payload keys:', list(p)[:8])
    print('    body:', json.dumps(p)[:300])
"
}

# What the client sends today.
probe "A. as the client sends it (Jan 2019, HB_WEST)" \
  --data-urlencode "settlementPoint=HB_WEST" \
  --data-urlencode "deliveryDateFrom=2019-01-01" \
  --data-urlencode "deliveryDateTo=2019-01-31" \
  --data-urlencode "size=10000"

# Hypothesis 1: the window predates retention.
probe "B. recent window instead (last month)" \
  --data-urlencode "settlementPoint=HB_WEST" \
  --data-urlencode "deliveryDateFrom=$(date -v-40d +%Y-%m-%d 2>/dev/null || date -d '40 days ago' +%Y-%m-%d)" \
  --data-urlencode "deliveryDateTo=$(date -v-10d +%Y-%m-%d 2>/dev/null || date -d '10 days ago' +%Y-%m-%d)"

# Hypothesis 2: the filter name or point identifier is wrong.
probe "C. no settlementPoint filter at all (recent)" \
  --data-urlencode "deliveryDateFrom=$(date -v-3d +%Y-%m-%d 2>/dev/null || date -d '3 days ago' +%Y-%m-%d)" \
  --data-urlencode "deliveryDateTo=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d '1 day ago' +%Y-%m-%d)" \
  --data-urlencode "size=5"

# Hypothesis 3: the plant node rather than the hub.
probe "D. the plant node HRNT_SLR_RN (recent)" \
  --data-urlencode "settlementPoint=HRNT_SLR_RN" \
  --data-urlencode "deliveryDateFrom=$(date -v-40d +%Y-%m-%d 2>/dev/null || date -d '40 days ago' +%Y-%m-%d)" \
  --data-urlencode "deliveryDateTo=$(date -v-10d +%Y-%m-%d 2>/dev/null || date -d '10 days ago' +%Y-%m-%d)"

# Does the working point have historical coverage?
probe "E. HRNT_SLR_RN in Jan 2019 (retention?)" \
  --data-urlencode "settlementPoint=HRNT_SLR_RN" \
  --data-urlencode "deliveryDateFrom=2019-01-01" \
  --data-urlencode "deliveryDateTo=2019-01-31"

# Is size=10000 accepted, or does it exceed the maximum page size?
probe "F. size=10000 on a known-good point" \
  --data-urlencode "settlementPoint=HRNT_SLR_RN" \
  --data-urlencode "deliveryDateFrom=$(date -v-5d +%Y-%m-%d 2>/dev/null || date -d '5 days ago' +%Y-%m-%d)" \
  --data-urlencode "deliveryDateTo=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d '1 day ago' +%Y-%m-%d)" \
  --data-urlencode "size=10000"

# What hub identifiers actually exist?
echo
echo "=== G. hub-type settlement points present in a recent day ==="
curl -sS -G "${HOST}${RT}" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Ocp-Apim-Subscription-Key: $ERCOT_SUBSCRIPTION_KEY" \
  --data-urlencode "deliveryDateFrom=$(date -v-2d +%Y-%m-%d 2>/dev/null || date -d '2 days ago' +%Y-%m-%d)" \
  --data-urlencode "deliveryDateTo=$(date -v-2d +%Y-%m-%d 2>/dev/null || date -d '2 days ago' +%Y-%m-%d)" \
  --data-urlencode "settlementPointType=HU" \
  --data-urlencode "size=200" \
| python3 -c "
import sys, json
try: p = json.load(sys.stdin)
except Exception: print('    could not parse'); raise SystemExit
names = sorted({r[3] for r in p.get('data', [])})
print('    hub points found:', names or '(none - try settlementPointType=HU or a different report)')
"

echo
echo "Reading the result:"
echo "  A empty, B populated  -> 2019 is outside retention; move the price years forward"
echo "  A and B empty, C populated -> the settlementPoint filter name or value is wrong"
echo "  everything empty      -> wrong endpoint, or the subscription lacks this product"
echo "  totalRecords > rows   -> pagination; the client reads only the first page"
