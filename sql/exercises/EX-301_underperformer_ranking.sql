-- EX-301 (tier 3): Underperformer ranking
--
-- Question: Which inverters produced least per unit of irradiance?
-- Skills:   peer normalisation, GROUP BY, ranking
--
-- Hint:     Normalise by the resource each inverter actually saw, not by the plant average.
--
-- What the answer should tell you:
--   Normalising by plant-average irradiance instead of per-asset irradiance manufactures underperformers out of the spatial cloud field. The worst-ranked inverter may simply have been under a cloud.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT asset_id,
       sum(ac_power_kw) / 60.0 / 1000.0 AS energy_mwh,
       sum(ac_power_kw) / nullif(sum(poa_global), 0) AS normalised_output
FROM inverter_telemetry
WHERE poa_global > 50
GROUP BY asset_id
ORDER BY normalised_output
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
       sum(ac_power_kw) / 60.0 / 1000.0 AS energy_mwh,
       sum(ac_power_kw) / nullif(sum(poa_global), 0) AS normalised_output
FROM telemetry.inverter_telemetry
WHERE poa_global > 50
GROUP BY asset_id
ORDER BY normalised_output
LIMIT 10
;
