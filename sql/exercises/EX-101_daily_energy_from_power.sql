-- EX-101 (tier 1): Daily energy from power
--
-- Question: How much energy did the plant export each day?
-- Skills:   time extraction, GROUP BY, integration from power
--
-- Hint:     Energy is integrated from power. At 1-minute samples each row is 1/60 of an hour.
--
-- What the answer should tell you:
--   Energy must never be stored independently of power. If the two disagree, one of them is wrong and reconciliation will not tell you which.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
SELECT date_trunc('day', time) AS day,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
FROM plant_telemetry
GROUP BY day
ORDER BY day
;

-- ---------------------------------------------------------------
-- TimescaleDB / PostgreSQL
--
-- Tables live in the `telemetry` schema, not `public`. Either use
-- the qualified names below, or run once per session:
--     SET search_path TO telemetry, public;
-- ---------------------------------------------------------------
-- Uses a TimescaleDB-specific feature; compare it with the form above.
SELECT time_bucket(INTERVAL '1 day', time) AS day,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
FROM telemetry.plant_telemetry
GROUP BY day
ORDER BY day
;
