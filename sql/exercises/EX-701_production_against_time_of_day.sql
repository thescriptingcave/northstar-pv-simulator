-- EX-701 (tier 7): Production against time of day
--
-- Question: When does the plant produce, in local terms?
-- Skills:   timezone conversion, time extraction, shape analysis
--
-- Hint:     Storage is UTC. Local solar-day analysis requires a conversion.
--
-- What the answer should tell you:
--   This shape is why merchant solar earns below the average price: the plant produces hardest in the hours when every other solar plant in the region is also producing hardest.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT extract('hour' FROM time AT TIME ZONE 'America/Chicago') AS local_hour,
       avg(grid_export_power_kw) AS mean_export_kw,
       max(grid_export_power_kw) AS peak_export_kw
FROM plant_telemetry
GROUP BY local_hour
ORDER BY local_hour
;

-- ---------------------------------------------------------------
-- TimescaleDB / PostgreSQL
--
-- Tables live in the `telemetry` schema, not `public`. Either use
-- the qualified names below, or run once per session:
--     SET search_path TO telemetry, public;
-- ---------------------------------------------------------------
-- Same logic, schema-qualified. This query needs no
-- time-series-specific feature - only the table names differ.
SELECT extract('hour' FROM time AT TIME ZONE 'America/Chicago') AS local_hour,
       avg(grid_export_power_kw) AS mean_export_kw,
       max(grid_export_power_kw) AS peak_export_kw
FROM telemetry.plant_telemetry
GROUP BY local_hour
ORDER BY local_hour
;
