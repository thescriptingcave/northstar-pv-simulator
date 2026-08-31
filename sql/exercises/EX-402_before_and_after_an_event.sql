-- EX-402 (tier 4): Before and after an event
--
-- Question: What did telemetry look like in the hour before each fault?
-- Skills:   event windows, asof reasoning, precursor analysis
--
-- Hint:     Join the event list back to telemetry on an interval, not on equality.
--
-- What the answer should tell you:
--   Precursor analysis is why events and telemetry are stored separately and joined, rather than collapsed into one table.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
WITH faults AS (
    SELECT asset_id, min(time) AS fault_time
    FROM inverter_telemetry
    WHERE operating_state = 'FAULT'
    GROUP BY asset_id
)
SELECT f.asset_id,
       f.fault_time,
       avg(t.internal_temp_c) AS mean_internal_temp_c,
       avg(t.ac_power_kw) AS mean_ac_kw
FROM faults f
JOIN inverter_telemetry t
  ON t.asset_id = f.asset_id
 AND t.time BETWEEN f.fault_time - INTERVAL 60 MINUTE AND f.fault_time
GROUP BY f.asset_id, f.fault_time
ORDER BY f.asset_id
;

-- ---------------------------------------------------------------
-- TimescaleDB / PostgreSQL
--
-- Tables live in the `telemetry` schema, not `public`. Either use
-- the qualified names below, or run once per session:
--     SET search_path TO telemetry, public;
-- ---------------------------------------------------------------
-- Uses a TimescaleDB-specific feature; compare it with the form above.
WITH faults AS (
    SELECT asset_id, min(time) AS fault_time
    FROM telemetry.inverter_telemetry
    WHERE operating_state = 'FAULT'
    GROUP BY asset_id
)
SELECT f.asset_id,
       f.fault_time,
       avg(t.internal_temp_c) AS mean_internal_temp_c,
       avg(t.ac_power_kw) AS mean_ac_kw
FROM faults f
JOIN telemetry.inverter_telemetry t
  ON t.asset_id = f.asset_id
 AND t.time BETWEEN f.fault_time - INTERVAL '60 minutes' AND f.fault_time
GROUP BY f.asset_id, f.fault_time
ORDER BY f.asset_id
;
