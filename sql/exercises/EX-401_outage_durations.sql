-- EX-401 (tier 4): Outage durations
--
-- Question: How long did each inverter outage last?
-- Skills:   gaps and islands, window functions, event reconstruction
--
-- Hint:     The difference between two row numbers is constant within a run of identical values.
--
-- What the answer should tell you:
--   Gaps-and-islands reconstructs discrete events from continuous state. Compare the result against the events table: they should agree, and where they do not, one of them is wrong.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
WITH flagged AS (
    SELECT time, asset_id,
           CASE WHEN operating_state = 'FAULT' THEN 1 ELSE 0 END AS faulted
    FROM inverter_telemetry
),
grouped AS (
    SELECT time, asset_id, faulted,
           row_number() OVER (PARTITION BY asset_id ORDER BY time)
             - row_number() OVER (PARTITION BY asset_id, faulted ORDER BY time)
             AS island
    FROM flagged
)
SELECT asset_id,
       min(time) AS started,
       max(time) AS ended,
       count(*) AS minutes
FROM grouped
WHERE faulted = 1
GROUP BY asset_id, island
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
WITH flagged AS (
    SELECT time, asset_id,
           CASE WHEN operating_state = 'FAULT' THEN 1 ELSE 0 END AS faulted
    FROM telemetry.inverter_telemetry
),
grouped AS (
    SELECT time, asset_id, faulted,
           row_number() OVER (PARTITION BY asset_id ORDER BY time)
             - row_number() OVER (PARTITION BY asset_id, faulted ORDER BY time)
             AS island
    FROM flagged
)
SELECT asset_id,
       min(time) AS started,
       max(time) AS ended,
       count(*) AS minutes
FROM grouped
WHERE faulted = 1
GROUP BY asset_id, island
ORDER BY minutes DESC
;
