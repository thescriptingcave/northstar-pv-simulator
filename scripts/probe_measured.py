"""Show which timestamps retain a measured poa_global."""
from pathlib import Path
from northstar_sim.storage import duckdb_connection

db = duckdb_connection(Path("datasets/winter"), "winter", "analyst")

print("=== dates where measured poa_global survives ===")
print(db.execute("""
    SELECT CAST(time AS DATE) AS day,
           count(*) AS rows,
           count(poa_global) AS non_null
    FROM inverter_telemetry
    GROUP BY 1 HAVING count(poa_global) > 0
    ORDER BY 1 LIMIT 10
""").df().to_string(index=False))

print("\n=== is it whole days, or part of each day? ===")
print(db.execute("""
    SELECT CAST(time AS DATE) AS day,
           min(time) AS first_non_null,
           max(time) AS last_non_null,
           count(*) AS n
    FROM inverter_telemetry WHERE poa_global IS NOT NULL
    GROUP BY 1 ORDER BY 1 LIMIT 5
""").df().to_string(index=False))

print("\n=== which other columns share the pattern? ===")
print(db.execute("""
    SELECT count(*) AS rows,
           count(poa_global) AS poa,
           count(poa_direct) AS direct,
           count(effective_irradiance) AS effective,
           count(cell_temperature) AS cell,
           count(dc_power_kw) AS dc,
           count(ac_power_kw) AS ac
    FROM inverter_telemetry
""").df().to_string(index=False))
db.close()
