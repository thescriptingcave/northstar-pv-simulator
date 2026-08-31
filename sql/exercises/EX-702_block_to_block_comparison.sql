-- EX-702 (tier 7): Block-to-block comparison
--
-- Question: Do the ten power blocks perform equally?
-- Skills:   aggregation, peer comparison at scale, spread analysis
--
-- Hint:     Compare energy and loading together; a block can be low on one and normal on the other.
--
-- What the answer should tell you:
--   Daily block energy spread is small even under broken cloud - weather averages out over a day. Persistent block differences come from soiling and equipment, which is what makes them detectable.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT asset_id AS block_id,
       sum(ac_power_kw) / 60.0 / 1000.0 AS energy_mwh,
       avg(transformer_loading_pct) AS mean_loading_pct,
       max(transformer_loading_pct) AS peak_loading_pct
FROM block_telemetry
GROUP BY block_id
ORDER BY energy_mwh
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
SELECT asset_id AS block_id,
       sum(ac_power_kw) / 60.0 / 1000.0 AS energy_mwh,
       avg(transformer_loading_pct) AS mean_loading_pct,
       max(transformer_loading_pct) AS peak_loading_pct
FROM telemetry.block_telemetry
GROUP BY block_id
ORDER BY energy_mwh
;
