-- EX-201 (tier 2): Steepest ramps
--
-- Question: When did plant output change fastest, and by how much?
-- Skills:   LAG, window functions, rate of change
--
-- Hint:     LAG over an ordered window gives the previous sample.
--
-- What the answer should tell you:
--   The steepest ramps cluster on broken-cloud days, not clear ones. Clear days have the largest output and the gentlest ramps.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
WITH ramps AS (
    SELECT time,
           grid_export_power_kw,
           grid_export_power_kw
               - lag(grid_export_power_kw) OVER (ORDER BY time) AS ramp_kw_min
    FROM plant_telemetry
)
SELECT time, grid_export_power_kw, ramp_kw_min
FROM ramps
WHERE ramp_kw_min IS NOT NULL
ORDER BY abs(ramp_kw_min) DESC
LIMIT 20
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
WITH ramps AS (
    SELECT time,
           grid_export_power_kw,
           grid_export_power_kw
               - lag(grid_export_power_kw) OVER (ORDER BY time) AS ramp_kw_min
    FROM telemetry.plant_telemetry
)
SELECT time, grid_export_power_kw, ramp_kw_min
FROM ramps
WHERE ramp_kw_min IS NOT NULL
ORDER BY abs(ramp_kw_min) DESC
LIMIT 20
;
