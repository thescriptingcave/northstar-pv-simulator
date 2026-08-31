#!/usr/bin/env bash
# Compare the LZ and LZEW price series for a load zone.
#
#     ./scripts/ercot_lz_compare.sh [LZ_WEST] [2025-06]
#
# ERCOT reports a load zone under two settlement point types. On a single
# sampled day they agreed; across a full year they do not. This shows how far
# apart they are, so the choice is made on evidence rather than on which one
# the API happened to list first.

set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${ERCOT_USERNAME:?}" "${ERCOT_PASSWORD:?}" "${ERCOT_SUBSCRIPTION_KEY:?}"

POINT="${1:-LZ_WEST}"
MONTH="${2:-2025-06}"
CLIENT_ID="fec253ea-0d06-4272-a5e6-b478baeecd70"

TOKEN=$(curl -sS -X POST \
  "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token" \
  -d "username=$ERCOT_USERNAME" --data-urlencode "password=$ERCOT_PASSWORD" \
  -d "grant_type=password" -d "scope=openid ${CLIENT_ID} offline_access" \
  -d "client_id=${CLIENT_ID}" -d "response_type=id_token" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))")
[ -z "$TOKEN" ] && { echo "token request failed"; exit 1; }

echo "Comparing LZ and LZEW for $POINT over $MONTH"
echo

python3 - "$TOKEN" "$ERCOT_SUBSCRIPTION_KEY" "$POINT" "$MONTH" <<'PY'
import sys, json, urllib.parse, urllib.request, collections, statistics

token, key, point, month = sys.argv[1:5]
year, mon = month.split("-")
import calendar
last = calendar.monthrange(int(year), int(mon))[1]

rows, page = [], 1
while True:
    q = urllib.parse.urlencode({
        "settlementPoint": point,
        "deliveryDateFrom": f"{month}-01",
        "deliveryDateTo": f"{month}-{last:02d}",
        "size": 1000, "page": page,
    })
    req = urllib.request.Request(
        f"https://api.ercot.com/api/public-reports/np6-905-cd/spp_node_zone_hub?{q}",
        headers={"Authorization": f"Bearer {token}",
                 "Ocp-Apim-Subscription-Key": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        p = json.load(r)
    cols = [f["name"] for f in p["fields"]]
    rows.extend(p["data"])
    meta = p.get("_meta", {})
    if page >= int(meta.get("totalPages", 1)):
        break
    page += 1

i_d, i_h, i_i = cols.index("deliveryDate"), cols.index("deliveryHour"), cols.index("deliveryInterval")
i_t, i_p = cols.index("settlementPointType"), cols.index("settlementPointPrice")

by_type = collections.defaultdict(dict)
for r in rows:
    by_type[r[i_t]][(r[i_d], r[i_h], r[i_i])] = float(r[i_p])

types = sorted(by_type)
print(f"  types present : {types}")
for t in types:
    v = list(by_type[t].values())
    print(f"    {t:<6} n={len(v):>6}  mean={statistics.mean(v):8.2f}  "
          f"min={min(v):9.2f}  max={max(v):9.2f}  negatives={sum(1 for x in v if x<0)}")

if len(types) == 2:
    a, b = types
    shared = set(by_type[a]) & set(by_type[b])
    diffs = [by_type[a][k] - by_type[b][k] for k in shared]
    identical = sum(1 for d in diffs if d == 0)
    print()
    print(f"  shared intervals : {len(shared):,}")
    print(f"  identical prices : {identical:,} ({identical/len(shared):.1%})")
    if identical < len(shared):
        nz = [d for d in diffs if d != 0]
        print(f"  where they differ: n={len(nz):,}  mean diff={statistics.mean(nz):+.3f}  "
              f"max |diff|={max(abs(d) for d in nz):.2f}")
        print()
        print("  A small, mostly-zero difference means the two are the same product")
        print("  with rounding or a settlement revision. A large or structural one")
        print("  means they are different products and the choice matters.")
PY
