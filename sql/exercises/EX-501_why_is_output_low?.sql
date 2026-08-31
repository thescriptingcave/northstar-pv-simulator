-- EX-501 (tier 5): Why is output low?
--
-- Question: Separate clipping, curtailment, derating and low resource.
-- Skills:   conditional aggregation, CASE, multi-signal reasoning
--
-- Hint:     Order the CASE branches carefully - the conditions overlap.
--
-- What the answer should tell you:
--   Curtailment and clipping both hold output flat at high irradiance. Only commanded_power_kw separates them, and only the price series explains why the command was given. Note which conditions are absent: a window with no curtailment and no derating tells you about the weather and the market, not about the query.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT CASE
         WHEN curtailed_power_kw > 0 THEN 'curtailed'
         WHEN thermal_derate_factor < 0.999 THEN 'thermal derate'
         WHEN ac_preclip_kw >= 2497.5 THEN 'clipping'
         WHEN poa_global < 200 THEN 'low resource'
         ELSE 'normal'
       END AS condition,
       count(*) AS minutes,
       avg(ac_power_kw) AS mean_ac_kw,
       avg(poa_global) AS mean_poa
FROM inverter_telemetry
WHERE poa_global > 5
GROUP BY condition
ORDER BY minutes DESC
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
SELECT CASE
         WHEN curtailed_power_kw > 0 THEN 'curtailed'
         WHEN thermal_derate_factor < 0.999 THEN 'thermal derate'
         WHEN ac_preclip_kw >= 2497.5 THEN 'clipping'
         WHEN poa_global < 200 THEN 'low resource'
         ELSE 'normal'
       END AS condition,
       count(*) AS minutes,
       avg(ac_power_kw) AS mean_ac_kw,
       avg(poa_global) AS mean_poa
FROM telemetry.inverter_telemetry
WHERE poa_global > 5
GROUP BY condition
ORDER BY minutes DESC
;
