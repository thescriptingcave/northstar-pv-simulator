#!/usr/bin/env bash
# Show why ERCOT price partitions contain duplicate timestamps.
#
#     ./scripts/ercot_duplicates.sh
#
# Two distinct patterns were observed on a real backfill:
#   - November: 4 duplicates for 15-minute data, 1 for hourly. DST fall-back.
#   - LZ_WEST:  every row duplicated, in every month. Something else.

set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${ERCOT_USERNAME:?}" "${ERCOT_PASSWORD:?}" "${ERCOT_SUBSCRIPTION_KEY:?}"

CLIENT_ID="fec253ea-0d06-4272-a5e6-b478baeecd70"
TOKEN=$(curl -sS -X POST \
  "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token" \
  -d "username=$ERCOT_USERNAME" --data-urlencode "password=$ERCOT_PASSWORD" \
  -d "grant_type=password" -d "scope=openid ${CLIENT_ID} offline_access" \
  -d "client_id=${CLIENT_ID}" -d "response_type=id_token" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))")
[ -z "$TOKEN" ] && { echo "token request failed"; exit 1; }

HOST="https://api.ercot.com/api/public-reports"
RT="/np6-905-cd/spp_node_zone_hub"

ask() {
  curl -sS -L -G "${HOST}${RT}" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Ocp-Apim-Subscription-Key: $ERCOT_SUBSCRIPTION_KEY" "$@"
}

echo "=== 1. LZ_WEST, one day - are rows genuinely duplicated? ==="
ask --data-urlencode "settlementPoint=LZ_WEST" \
    --data-urlencode "deliveryDateFrom=2025-06-10" \
    --data-urlencode "deliveryDateTo=2025-06-10" \
    --data-urlencode "size=400" \
| python3 -c "
import sys, json, collections
p = json.load(sys.stdin)
cols = [f['name'] for f in p['fields']]
rows = p['data']
print(f'    rows returned: {len(rows)}  (expected 96 for one day at 15 min)')
i_pt   = cols.index('settlementPoint')
i_type = cols.index('settlementPointType')
print('    distinct settlementPoint  :', sorted({r[i_pt] for r in rows}))
print('    distinct settlementPointType:', sorted({r[i_type] for r in rows}))
key = collections.Counter((r[cols.index('deliveryDate')], r[cols.index('deliveryHour')],
                           r[cols.index('deliveryInterval')]) for r in rows)
dupes = [k for k, n in key.items() if n > 1]
print(f'    intervals appearing more than once: {len(dupes)}')
if dupes:
    k = dupes[0]
    print('    the two rows for', k, 'differ only in:')
    same = [r for r in rows if (r[cols.index(\"deliveryDate\")], r[cols.index(\"deliveryHour\")],
                                r[cols.index(\"deliveryInterval\")]) == k]
    for a, b, name in zip(same[0], same[1], cols):
        if a != b: print(f'        {name}: {a!r} vs {b!r}')
"

echo
echo "=== 2. HB_WEST across the DST fall-back (2 November 2025) ==="
ask --data-urlencode "settlementPoint=HB_WEST" \
    --data-urlencode "deliveryDateFrom=2025-11-02" \
    --data-urlencode "deliveryDateTo=2025-11-02" \
    --data-urlencode "size=400" \
| python3 -c "
import sys, json, collections
p = json.load(sys.stdin)
cols = [f['name'] for f in p['fields']]
rows = p['data']
print(f'    rows returned: {len(rows)}  (a 25-hour day has 100 intervals at 15 min)')
i_h, i_i = cols.index('deliveryHour'), cols.index('deliveryInterval')
i_dst = cols.index('DSTFlag')
key = collections.Counter((r[i_h], r[i_i]) for r in rows)
dupes = sorted(k for k, n in key.items() if n > 1)
print(f'    repeated (hour, interval) pairs: {dupes}')
for k in dupes[:4]:
    same = [r for r in rows if (r[i_h], r[i_i]) == k]
    print(f'      hour {k[0]} interval {k[1]}: DSTFlag values {[r[i_dst] for r in same]}')
print()
print('    If DSTFlag differs between the two rows, it is the disambiguator')
print('    and belongs in the uniqueness key.')
"
