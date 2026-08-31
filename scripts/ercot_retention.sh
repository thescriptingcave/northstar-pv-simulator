#!/usr/bin/env bash
# Find ERCOT's price retention boundary and print the config years to use.
#
#     ./scripts/ercot_retention.sh
#
# The public API keeps a rolling window, not the full history. NSRDB goes back
# to 2018; ERCOT does not, so weather years and price years cannot be the same
# list. This finds where prices actually start.

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
POINT="${1:-HB_WEST}"
echo "Probing retention for $POINT"
echo

available=()
for year in 2018 2019 2020 2021 2022 2023 2024 2025 2026; do
  n=$(curl -sS -L -G "${HOST}/np6-905-cd/spp_node_zone_hub" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Ocp-Apim-Subscription-Key: $ERCOT_SUBSCRIPTION_KEY" \
    --data-urlencode "settlementPoint=$POINT" \
    --data-urlencode "deliveryDateFrom=${year}-06-01" \
    --data-urlencode "deliveryDateTo=${year}-06-02" \
    --data-urlencode "size=1" \
  | python3 -c "
import sys,json
try: print(json.load(sys.stdin).get('_meta',{}).get('totalRecords',0))
except Exception: print(-1)")

  case "$n" in
    -1) echo "  $year  request failed" ;;
     0) echo "  $year  no data" ;;
     *) echo "  $year  $n records for a single day"; available+=("$year") ;;
  esac
done

echo
if [ ${#available[@]} -eq 0 ]; then
  echo "No year returned data. Check the subscription covers np6-905-cd."
  exit 1
fi

joined=$(IFS=', '; echo "${available[*]}")
echo "Available: $joined"
echo
echo "Set these in config/northstar.toml for SRC-PX-01 and SRC-PX-02:"
echo "    years = [$joined]"
echo
echo "Leave SRC-WX-01 and SRC-WX-03 as they are - NSRDB and ERA5 reach further"
echo "back than ERCOT does, and the simulator only needs prices for the years"
echo "it settles."
