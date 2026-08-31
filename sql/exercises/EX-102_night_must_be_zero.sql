-- EX-102 (tier 1): Night must be zero
--
-- Question: Does the plant produce anything when the sun is down?
-- Skills:   filtering, aggregate sanity checks
--
-- Hint:     Look at the minimum as well as the maximum.
--
-- What the answer should tell you:
--   Minimum AC is negative overnight, not zero: an energised inverter draws its own standby load. A daylight filter that keeps these rows will bias every efficiency calculation slightly negative.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT count(*) AS night_samples,
       max(ac_power_kw) AS max_ac_kw,
       min(ac_power_kw) AS min_ac_kw
FROM inverter_telemetry
WHERE poa_global < 1.0
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
SELECT count(*) AS night_samples,
       max(ac_power_kw) AS max_ac_kw,
       min(ac_power_kw) AS min_ac_kw
FROM telemetry.inverter_telemetry
WHERE poa_global < 1.0
;
