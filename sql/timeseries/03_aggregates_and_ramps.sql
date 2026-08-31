-- TS-106 .. TS-108 : continuous aggregates, distributions, ramp analysis
--
--     psql "$NORTHSTAR_DSN" -f sql/timeseries/03_aggregates_and_ramps.sql

\echo '=== TS-106  A continuous aggregate must agree with its source ==='
-- An aggregate that disagrees with the raw data is worse than no aggregate:
-- it is fast and wrong, and nothing downstream notices.
--
-- Weight by samples, not a plain avg of avgs. Buckets can hold different
-- numbers of samples, and averaging averages silently overweights the sparse
-- ones - most visibly at the first and last bucket of a record.
SELECT 'continuous aggregate' AS source,
       count(*)                                             AS rows_scanned,
       round((sum(value * samples) / 60 / 1000)::numeric, 3) AS mwh
FROM telemetry.plant_5min
UNION ALL
SELECT 'raw hypertable',
       count(*),
       round((sum(grid_export_power_kw) / 60 / 1000)::numeric, 3)
FROM telemetry.plant_telemetry WHERE grid_export_power_kw IS NOT NULL;

-- 2,017 rows against 10,081 for an identical answer. That ratio is the whole
-- argument for continuous aggregates, and it grows with the record.


\echo ''
\echo '=== TS-107  Where does an inverter actually spend its time? ==='
-- histogram() returns a single array: the first element is the underflow
-- bucket, the last is overflow, and the rest are the requested bins.
--
-- Expect a strong peak in the top bin. That is clipping - the inverter pinned
-- at its AC limit - and it is a design consequence, not a fault.
SELECT histogram(ac_power_kw, 0, 2500, 10) AS loading_distribution
FROM telemetry.inverter_telemetry
WHERE poa_global > 50;


\echo ''
\echo '=== TS-108  The steepest ramps are commercial, not meteorological ==='
-- Rank 5-minute plant ramps and ask what caused them.
--
-- Curtailment switches output between full and zero within one settlement
-- interval; no cloud does that to a 3.26 km site. Measured on this dataset,
-- curtailed buckets ramp at 3.53 MW on average against 0.85 MW for
-- weather-driven ones - four times steeper.
--
-- An analyst hunting cloud ramps who does not join the curtailment signal will
-- find the controller instead, and conclude the site has extraordinary weather.
WITH bucketed AS (
    SELECT time_bucket('5 minutes', time)   AS bucket,
           avg(grid_export_power_kw) / 1000 AS mw,
           avg(curtailed_power_kw) / 1000   AS curtailed_mw
    FROM telemetry.plant_telemetry
    GROUP BY bucket
),
ramps AS (
    SELECT bucket, mw, curtailed_mw,
           mw - lag(mw) OVER (ORDER BY bucket) AS ramp_mw
    FROM bucketed
)
SELECT CASE WHEN curtailed_mw > 1 THEN 'curtailment active'
            ELSE 'weather only' END              AS cause,
       count(*)                                  AS buckets,
       round(max(abs(ramp_mw))::numeric, 1)      AS worst_ramp_mw,
       round(avg(abs(ramp_mw))::numeric, 2)      AS mean_abs_ramp_mw
FROM ramps
WHERE ramp_mw IS NOT NULL
GROUP BY cause ORDER BY worst_ramp_mw DESC;
