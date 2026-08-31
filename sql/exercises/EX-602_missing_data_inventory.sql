-- EX-602 (tier 6): Missing data inventory
--
-- Question: Which assets and signals have gaps, and how large?
-- Skills:   null handling, completeness metrics, availability
--
-- Hint:     Missing means NULL. If you see zeros where you expect gaps, something upstream zero-filled them.
--
-- What the answer should tell you:
--   Report data availability alongside every performance figure. A performance ratio of 84% from 91% availability is not the same claim as 84% from 99.8%.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT asset_id,
       count(*) AS expected_samples,
       sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS missing_samples,
       1.0 - sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END)
             / count(*)::DOUBLE AS availability
FROM inverter_telemetry
GROUP BY asset_id
ORDER BY availability
LIMIT 10
;

-- ---------------------------------------------------------------
-- TimescaleDB / PostgreSQL
--
-- Tables live in the `telemetry` schema, not `public`. Either use
-- the qualified names below, or run once per session:
--     SET search_path TO telemetry, public;
-- ---------------------------------------------------------------
-- Uses a TimescaleDB-specific feature; compare it with the form above.
SELECT asset_id,
       count(*) AS expected_samples,
       sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS missing_samples,
       1.0 - sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END)::double precision
             / count(*) AS availability
FROM telemetry.inverter_telemetry
GROUP BY asset_id
ORDER BY availability
LIMIT 10
;
