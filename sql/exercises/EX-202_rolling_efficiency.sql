-- EX-202 (tier 2): Rolling efficiency
--
-- Question: How does DC-to-AC conversion efficiency vary with load?
-- Skills:   rolling window, derived metrics, load filtering
--
-- Hint:     Bucket by DC power and average the ratio within each bucket.
--
-- What the answer should tell you:
--   Efficiency is not constant. It rises steeply at low load, flattens, then appears to fall at the top - the last part is clipping, not a less efficient inverter.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT round(dc_power_kw / 250.0) * 250 AS dc_band_kw,
       count(*) AS samples,
       avg(ac_power_kw / nullif(dc_power_kw, 0)) AS efficiency
FROM inverter_telemetry
WHERE dc_power_kw > 100
GROUP BY dc_band_kw
ORDER BY dc_band_kw
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
SELECT round(dc_power_kw / 250.0) * 250 AS dc_band_kw,
       count(*) AS samples,
       avg(ac_power_kw / nullif(dc_power_kw, 0)) AS efficiency
FROM telemetry.inverter_telemetry
WHERE dc_power_kw > 100
GROUP BY dc_band_kw
ORDER BY dc_band_kw
;
