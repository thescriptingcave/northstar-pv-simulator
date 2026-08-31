-- EX-601 (tier 6): Find stuck sensors
--
-- Question: Which signals stopped changing while the plant kept running?
-- Skills:   UNPIVOT, run detection, false-positive control
--
-- Hint:     A running sum over a change flag numbers each run of identical values. Scan every channel, and think hard about which constants are legitimate before you flag anything.
--
-- What the answer should tell you:
--   Three traps, in the order you will hit them. Scanning only ac_power_kw finds nothing - a stuck channel can be any of them. Scanning without a daylight filter returns overnight standby and night-time constants at the top. And a curtailed inverter genuinely holds output at exactly 0.0 kW for hours, which is not a frozen sensor. Filtered properly this query recovers the injected stuck signals with no false positives - check it against the truth schema. Do not trust the quality column either: roughly half of injected defects carry no flag, and drift carries one 5% of the time.

-- ---------------------------------------------------------------
-- DuckDB over the Parquet export (no server required)
-- ---------------------------------------------------------------
WITH daylight AS (
    -- Filter to operating hours FIRST. An unfiltered scan returns overnight
    -- standby (-0.7 kW held for 646 minutes) and constant night-time internal
    -- temperature, drowning the real signals in legitimate constants.
    SELECT time, asset_id, ac_power_kw, dc_power_kw, ac_preclip_kw,
           internal_temp_c, cell_temperature, dc_voltage_v
    FROM inverter_telemetry
    WHERE poa_global > 100 AND ac_power_kw > 1
),
long AS (
    -- UNPIVOT scans every channel. Hand-listing them misses whichever one
    -- actually froze, which is not knowable in advance.
    UNPIVOT daylight
    ON ac_power_kw, dc_power_kw, ac_preclip_kw,
       internal_temp_c, cell_temperature, dc_voltage_v
    INTO NAME signal VALUE value
),
runs AS (
    SELECT time, asset_id, signal, value,
           CASE WHEN value = lag(value)
                     OVER (PARTITION BY asset_id, signal ORDER BY time)
                THEN 0 ELSE 1 END AS changed
    FROM long WHERE value IS NOT NULL
),
islands AS (
    SELECT asset_id, signal, value,
           sum(changed) OVER (PARTITION BY asset_id, signal ORDER BY time)
             AS island
    FROM runs
)
SELECT asset_id, signal, round(value, 4) AS frozen_value,
       count(*) AS repeated_minutes
FROM islands
GROUP BY asset_id, signal, value, island
HAVING count(*) > 15
ORDER BY repeated_minutes DESC
LIMIT 15
;

-- ---------------------------------------------------------------
-- TimescaleDB / PostgreSQL
--
-- Tables live in the `telemetry` schema, not `public`. Either use
-- the qualified names below, or run once per session:
--     SET search_path TO telemetry, public;
-- ---------------------------------------------------------------
-- Uses a TimescaleDB-specific feature; compare it with the form above.
WITH daylight AS (
    SELECT time, asset_id, ac_power_kw, dc_power_kw, ac_preclip_kw,
           internal_temp_c, cell_temperature, dc_voltage_v
    FROM telemetry.inverter_telemetry
    WHERE poa_global > 100 AND ac_power_kw > 1
),
long AS (
    -- PostgreSQL has no UNPIVOT. LATERAL over a VALUES list is the idiom.
    SELECT d.time, d.asset_id, v.signal, v.value
    FROM daylight d
    CROSS JOIN LATERAL (VALUES
        ('ac_power_kw',      d.ac_power_kw),
        ('dc_power_kw',      d.dc_power_kw),
        ('ac_preclip_kw',    d.ac_preclip_kw),
        ('internal_temp_c',  d.internal_temp_c),
        ('cell_temperature', d.cell_temperature),
        ('dc_voltage_v',     d.dc_voltage_v)
    ) AS v(signal, value)
),
runs AS (
    SELECT time, asset_id, signal, value,
           CASE WHEN value = lag(value)
                     OVER (PARTITION BY asset_id, signal ORDER BY time)
                THEN 0 ELSE 1 END AS changed
    FROM long WHERE value IS NOT NULL
),
islands AS (
    SELECT asset_id, signal, value,
           sum(changed) OVER (PARTITION BY asset_id, signal ORDER BY time)
             AS island
    FROM runs
)
SELECT asset_id, signal, round(value::numeric, 4) AS frozen_value,
       count(*) AS repeated_minutes
FROM islands
GROUP BY asset_id, signal, value, island
HAVING count(*) > 15
ORDER BY repeated_minutes DESC
LIMIT 15
;
