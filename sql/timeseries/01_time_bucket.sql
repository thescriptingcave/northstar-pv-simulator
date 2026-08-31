-- TS-101 .. TS-103 : time_bucket, first/last, and the solar day
--
-- These are the queries that need TimescaleDB. Everything in sql/exercises/
-- runs on plain PostgreSQL; nothing here does.
--
-- Run against a loaded database:
--     psql "$NORTHSTAR_DSN" -f sql/timeseries/01_time_bucket.sql

\echo '=== TS-101  Downsample with first() and last() ==='
-- avg() tells you the level; first() and last() tell you the *shape* within
-- the bucket. Both are needed: a bucket averaging 400 W/m2 could be a steady
-- overcast hour or a clear hour half-eaten by cloud.
--
-- first(value, time) is not the same as min(value). It is the value at the
-- earliest timestamp, which is what you want for an opening reading.
SELECT time_bucket('1 hour', time)                        AS hour,
       round(avg(poa_global)::numeric, 1)                 AS mean_poa,
       round(first(poa_global, time)::numeric, 1)         AS poa_at_open,
       round(last(poa_global, time)::numeric, 1)          AS poa_at_close,
       round(max(ac_power_kw)::numeric, 1)                AS peak_kw
FROM telemetry.inverter_telemetry
WHERE asset_id = 'NORTHSTA-BLK01-INV1'
  AND time >= '2023-06-22 12:00' AND time < '2023-06-23'
GROUP BY hour ORDER BY hour;


\echo ''
\echo '=== TS-102  The UTC-day trap ==='
-- Bucketing a West Texas plant by UTC day splits the solar day in half: local
-- solar noon is around 18:50 UTC, so afternoon and morning land on different
-- days.
--
-- The symptom is unmistakable once you look for it - a "generating span" of
-- 23:59, because the day starts with the plant already producing and ends with
-- it still producing.
SELECT time_bucket('1 day', time) AS utc_day,
       last(time, time)  FILTER (WHERE ac_power_kw > 0)
         - first(time, time) FILTER (WHERE ac_power_kw > 0) AS generating_span
FROM telemetry.inverter_telemetry
WHERE asset_id = 'NORTHSTA-BLK01-INV1'
GROUP BY utc_day ORDER BY utc_day;


\echo ''
\echo '=== TS-103  The fix: time_bucket with a timezone ==='
-- time_bucket accepts a timezone, and this is the reason to use it over
-- date_trunc. The bucket boundary follows local midnight, including across
-- daylight saving transitions, which date_trunc('day', time AT TIME ZONE ...)
-- gets subtly wrong twice a year.
--
-- Sunrise and sunset now sit where a human expects them, and daily energy is
-- stable instead of alternating between half-days.
SELECT time_bucket('1 day', time, 'America/Chicago')      AS solar_day,
       first(time, time) FILTER (WHERE ac_power_kw > 0)
         AT TIME ZONE 'America/Chicago'                   AS first_generation,
       last(time, time) FILTER (WHERE ac_power_kw > 0)
         AT TIME ZONE 'America/Chicago'                   AS last_generation,
       round((sum(ac_power_kw) / 60 / 1000)::numeric, 2)  AS mwh
FROM telemetry.inverter_telemetry
WHERE asset_id = 'NORTHSTA-BLK01-INV1'
GROUP BY solar_day ORDER BY solar_day;
