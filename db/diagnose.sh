#!/usr/bin/env bash
# Diagnose an empty Grafana dashboard, or a database that will not answer.
#
#   ./db/diagnose.sh
#
# Override the connection with NORTHSTAR_DSN if you changed the credentials.
#
# Every check prints the *actual* error. An earlier version of this script
# redirected stderr to /dev/null, which meant it reported "permission denied"
# with no indication of what was denied, to whom, or by which server. A
# diagnostic that hides the diagnosis is worse than no diagnostic.

DSN="${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}"
fail=0

# Redact the password when echoing the DSN back.
safe_dsn() { echo "$DSN" | sed -E 's#(//[^:]+:)[^@]*@#\1****@#'; }

echo "Connection: $(safe_dsn)"
echo

echo "0. Is a psql client available?"
if command -v psql >/dev/null 2>&1; then
    echo "   ok - $(psql --version)"
else
    echo "   FAIL - psql is not on PATH."
    echo "   macOS: brew install libpq && brew link --force libpq"
    echo "   Everything below needs it. Grafana and TablePlus do not."
    exit 1
fi

echo
echo "1. Can we connect at all?"
err=$(psql "$DSN" -tAc "SELECT 1" 2>&1 >/dev/null)
if [ -z "$err" ]; then
    echo "   ok"
else
    echo "   FAIL - $err"
    echo
    echo "   If Grafana and a GUI client both connect, they are almost"
    echo "   certainly not using this DSN. Check:"
    echo "     - a .env setting POSTGRES_USER / POSTGRES_PASSWORD"
    echo "     - another PostgreSQL already on port 5432 (Homebrew, Postgres.app)"
    echo "       compare: psql \"$DSN\" -tAc 'SHOW server_version'"
    echo "       against the container: docker exec northstar-timescaledb \\"
    echo "                              psql -U \${POSTGRES_USER:-northstar} \\"
    echo "                              -d northstar -tAc 'SHOW server_version'"
    exit 1
fi

echo
echo "   Connected as:"
psql "$DSN" -tAc "SELECT '     user=' || current_user
                       || '  db=' || current_database()
                       || '  server=' || setting
                  FROM pg_settings WHERE name = 'server_version'" 2>&1

echo
echo "   Is this the TimescaleDB container, or another server?"
psql "$DSN" -tAc "SELECT CASE WHEN count(*) > 0
                    THEN '     timescaledb ' || max(extversion) || ' present'
                    ELSE '     NO timescaledb extension - wrong server?' END
                  FROM pg_extension WHERE extname = 'timescaledb'" 2>&1

echo
echo "2. Does the schema exist?"
err=$(psql "$DSN" -tAc "SELECT 1 FROM information_schema.schemata
                        WHERE schema_name = 'telemetry'" 2>&1)
if echo "$err" | grep -q "^1$"; then
    echo "   ok"
else
    echo "   FAIL - no 'telemetry' schema. ${err}"
    echo "   The init scripts only run on an EMPTY volume."
    echo "   Fix: docker compose -f db/docker-compose.yml down -v && make db-up"
    fail=1
fi

echo
echo "3. Can this user read the tables?"
out=$(psql "$DSN" -tAc "SELECT count(*) FROM telemetry.plant_telemetry" 2>&1)
if echo "$out" | grep -qE "^[0-9]+$"; then
    if [ "$out" -gt 0 ]; then
        echo "   ok - $out rows in plant_telemetry"
    else
        echo "   FAIL - table exists but is EMPTY."
        echo "   'make db-up' creates the schema only. Fix: make db-load"
        fail=1
    fi
else
    echo "   FAIL - $out"
    echo "   Owner of the table:"
    psql "$DSN" -tAc "SELECT '     ' || tableowner FROM pg_tables
                      WHERE schemaname='telemetry' AND tablename='plant_telemetry'" 2>&1
    echo "   Your roles:"
    psql "$DSN" -tAc "SELECT '     ' || string_agg(rolname, ', ')
                      FROM pg_roles WHERE pg_has_role(current_user, oid, 'member')" 2>&1
    fail=1
fi

echo
echo "4. Is the Grafana datasource provisioned?"
if [ -f db/grafana/datasources/northstar.yaml ]; then
    echo "   ok - $(grep -m1 'uid:' db/grafana/datasources/northstar.yaml | tr -d ' ')"
    if grep -E '^\s*(user|password):' db/grafana/datasources/northstar.yaml \
         | grep -q ':-'; then
        echo "   FAIL - credentials use shell '\${VAR:-default}' expansion."
        echo "   Grafana does not implement it. Fix: make db-load"
        fail=1
    fi
else
    echo "   FAIL - db/grafana/datasources/northstar.yaml missing. Fix: make db-load"
    fail=1
fi

echo
echo "5. Do the dashboards point at the data, or at 'now'?"
window=$(python3 -c "
import json,glob
f=sorted(glob.glob('dashboards/northstar-*.json'))
print(json.load(open(f[0]))['time'] if f else 'no dashboards found')" 2>&1)
echo "   $window"
if echo "$window" | grep -q "now-"; then
    echo "   FAIL - pointing at 'now'; the dataset is historical. Fix: make db-load"
    fail=1
fi

if echo "$out" | grep -qE "^[0-9]+$" && [ "$out" -gt 0 ]; then
    echo
    echo "   Data actually spans:"
    psql "$DSN" -tAc "SELECT '   ' || min(time) || '  to  ' || max(time)
                      FROM telemetry.plant_telemetry" 2>&1
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "All checks pass. If panels are still blank, hard-refresh the browser:"
    echo "Grafana caches dashboard JSON, and a time range saved on your session"
    echo "overrides the dashboard default."
else
    echo "Fix the FAIL items above, then re-run."
fi
exit $fail
