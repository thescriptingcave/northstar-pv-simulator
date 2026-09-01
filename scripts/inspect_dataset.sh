#!/usr/bin/env bash
# Inspect a generated dataset when an acceptance check reports "no data".
#
#     ./scripts/inspect_dataset.sh datasets/observed observed

set -uo pipefail
cd "$(dirname "$0")/.."
DATASET="${1:-datasets/observed}"
RUN_ID="${2:-observed}"

uv run python - "$DATASET" "$RUN_ID" <<'PY'
import sys
from pathlib import Path
from northstar_sim.storage import duckdb_connection

dataset, run_id = sys.argv[1], sys.argv[2]
db = duckdb_connection(Path(dataset), run_id, "analyst")

print(f"Dataset: {dataset} (run_id={run_id})\n")
rows = db.execute("SELECT count(*) FROM inverter_telemetry").fetchone()[0]
print(f"  inverter rows            {rows:,}")

for label, query in (
    ("non-null poa_global", "SELECT count(poa_global) FROM inverter_telemetry"),
    ("poa_global > 100", "SELECT count(*) FROM inverter_telemetry WHERE poa_global > 100"),
    ("max poa_global", "SELECT max(poa_global) FROM inverter_telemetry"),
    ("non-null ac_power_kw", "SELECT count(ac_power_kw) FROM inverter_telemetry"),
    ("distinct timestamps", "SELECT count(DISTINCT time) FROM inverter_telemetry"),
    ("distinct assets", "SELECT count(DISTINCT asset_id) FROM inverter_telemetry"),
):
    value = db.execute(query).fetchone()[0]
    print(f"  {label:<24} {value if value is not None else 'NULL'}")

print()
span = db.execute("SELECT min(time), max(time) FROM inverter_telemetry").fetchone()
print(f"  time span                {span[0]} .. {span[1]}")

# The truth tree tells you whether the physics ran or the sensors nulled it.
truth = duckdb_connection(Path(dataset), run_id, "truth")
tables = {r[0] for r in truth.execute("SHOW TABLES").fetchall()}
if "inverter_truth" in tables:
    t = truth.execute("SELECT count(poa_global) FROM inverter_truth").fetchone()[0]
    print(f"  non-null poa in TRUTH    {t:,}")
    print()
    if t and not db.execute(
        "SELECT count(poa_global) FROM inverter_telemetry"
    ).fetchone()[0]:
        print("  Truth has POA but the analyst tree does not: the sensor layer")
        print("  nulled it. A single NaN in POA truth poisons the sensor's")
        print("  cumulative state and nulls the whole measured series.")
PY
