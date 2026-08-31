-- EX-502 (tier 5): Curtailment looks like a fault
--
-- Question: Find intervals with high irradiance and near-zero output.
-- Skills:   filtering, the discriminating triple, misattribution
--
-- Hint:     Compare available against commanded power before concluding anything is broken.
--
-- What the answer should tell you:
--   High irradiance with zero output and no fault code is economic curtailment. An analyst who stops at the telemetry will dispatch a technician to a working inverter.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT asset_id,
       count(*) AS minutes,
       avg(poa_global) AS mean_poa,
       avg(available_power_kw) AS mean_available_kw,
       avg(commanded_power_kw) AS mean_commanded_kw,
       avg(ac_power_kw) AS mean_ac_kw
FROM inverter_telemetry
WHERE poa_global > 600
  AND ac_power_kw < 50
GROUP BY asset_id
ORDER BY minutes DESC
LIMIT 10
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
SELECT asset_id,
       count(*) AS minutes,
       avg(poa_global) AS mean_poa,
       avg(available_power_kw) AS mean_available_kw,
       avg(commanded_power_kw) AS mean_commanded_kw,
       avg(ac_power_kw) AS mean_ac_kw
FROM telemetry.inverter_telemetry
WHERE poa_global > 600
  AND ac_power_kw < 50
GROUP BY asset_id
ORDER BY minutes DESC
LIMIT 10
;
