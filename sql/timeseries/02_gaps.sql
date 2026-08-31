-- TS-104 .. TS-105 : gapfill, locf, and why they may not help
--
--     psql "$NORTHSTAR_DSN" -f sql/timeseries/02_gaps.sql

\echo '=== TS-104  gapfill + locf + interpolate over a real outage ==='
-- NORTHSTA-BLK05-INV4 loses telemetry for 170 minutes in full daylight.
--
-- Watch what locf and interpolate return. The answer is instructive and is not
-- what most people expect.
SELECT bucket,
       round(measured::numeric, 1)      AS measured,
       round(locf_fill::numeric, 1)     AS locf_fill,
       round(interp::numeric, 1)        AS interpolated
FROM (
    SELECT time_bucket_gapfill('15 minutes', time)  AS bucket,
           avg(ac_power_kw)                          AS measured,
           locf(avg(ac_power_kw))                    AS locf_fill,
           interpolate(avg(ac_power_kw))             AS interp
    FROM telemetry.inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK05-INV4'
      AND time >= '2023-06-24 11:30' AND time < '2023-06-24 15:15'
    GROUP BY bucket
) q ORDER BY bucket;

-- Note also: locf() and interpolate() must be the OUTERMOST call. Wrapping
-- them - round(locf(...)) - fails with "locf must be toplevel function call".
-- Hence the subquery.


\echo ''
\echo '=== TS-105  Why locf returned nothing, and what does work ==='
-- locf fills buckets that gapfill *created*. It does not fill NULL aggregates
-- inside buckets that already exist.
--
-- This outage writes 165 rows with every column NULL - which is correct, and
-- required by doc 19 section 5.3: missing must never be zero-filled, because
-- zero irradiance is indistinguishable from night. But it means the buckets
-- exist, gapfill creates nothing, and locf has nothing to fill.
--
-- Confirm it for yourself:
--   SELECT count(*), count(ac_power_kw) FROM telemetry.inverter_telemetry
--   WHERE asset_id='NORTHSTA-BLK05-INV4'
--     AND time >= '2023-06-24 12:00' AND time < '2023-06-24 14:45';
--   -> 165 rows, 0 non-null
--
-- The technique for carrying a value across NULLs is a counting window:
-- count() ignores NULLs, so it increments only on real values and therefore
-- labels each NULL run with the index of the last real value before it.
WITH buckets AS (
    SELECT time_bucket('15 minutes', time) AS bucket, avg(ac_power_kw) AS kw
    FROM telemetry.inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK05-INV4'
      AND time >= '2023-06-24 11:30' AND time < '2023-06-24 15:15'
    GROUP BY bucket
),
islands AS (
    SELECT bucket, kw, count(kw) OVER (ORDER BY bucket) AS grp FROM buckets
)
SELECT bucket,
       round(kw::numeric, 1) AS measured,
       round(first_value(kw) OVER (PARTITION BY grp ORDER BY bucket)::numeric, 1)
         AS carried_forward
FROM islands ORDER BY bucket;

-- Now look at what you just produced. It carries -0.7 kW - the overnight
-- standby draw - across three hours of full daylight, because that was the
-- last real value before the outage began.
--
-- Carrying forward is the wrong answer here. The honest options are to leave
-- the gap NULL, or to estimate from a peer inverter that kept reporting. That
-- an operation is available does not make it correct.
