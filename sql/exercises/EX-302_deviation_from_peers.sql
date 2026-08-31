-- EX-302 (tier 3): Deviation from peers
--
-- Question: Which inverter deviates most from its block peers, and when?
-- Skills:   window partition, peer baseline, self-join alternative
--
-- Hint:     A window partitioned by time and block gives each row its own peer group without a self-join.
--
-- What the answer should tell you:
--   Peer ratio is the workhorse of fault detection. It removes the weather, which is the dominant signal, leaving equipment behaviour.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
WITH with_block AS (
    SELECT time, asset_id,
           substr(asset_id, 1, 14) AS block_id,
           ac_power_kw
    FROM inverter_telemetry
    WHERE ac_power_kw > 50
),
peers AS (
    SELECT time, asset_id, block_id, ac_power_kw,
           avg(ac_power_kw) OVER (PARTITION BY time, block_id) AS peer_mean
    FROM with_block
)
SELECT asset_id,
       count(*) AS samples,
       avg(ac_power_kw / nullif(peer_mean, 0)) AS peer_ratio
FROM peers
GROUP BY asset_id
ORDER BY peer_ratio
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
WITH with_block AS (
    SELECT time, asset_id,
           substr(asset_id, 1, 14) AS block_id,
           ac_power_kw
    FROM telemetry.inverter_telemetry
    WHERE ac_power_kw > 50
),
peers AS (
    SELECT time, asset_id, block_id, ac_power_kw,
           avg(ac_power_kw) OVER (PARTITION BY time, block_id) AS peer_mean
    FROM with_block
)
SELECT asset_id,
       count(*) AS samples,
       avg(ac_power_kw / nullif(peer_mean, 0)) AS peer_ratio
FROM peers
GROUP BY asset_id
ORDER BY peer_ratio
LIMIT 10
;
