-- WEEK 6: Predicting the next hour
--
-- The concept: a forecast is only useful if it beats persistence. Most do
-- not, and most that appear to are leaking.

-- ---------------------------------------------------------------
-- 6.1  The persistence baseline
-- ---------------------------------------------------------------
-- Persistence says "the next interval looks like this one". Any model must
-- beat it or it is not worth deploying.
WITH hourly AS (
    SELECT date_trunc('hour', time) AS hour,
           avg(grid_export_power_kw) / 1000.0 AS mw
    FROM plant_telemetry
    GROUP BY 1
),
paired AS (
    SELECT hour, mw,
           lag(mw) OVER (ORDER BY hour) AS previous_mw
    FROM hourly
)
SELECT round(avg(abs(mw - previous_mw))::numeric, 3)     AS mae_mw,
       round(sqrt(avg(power(mw - previous_mw, 2)))::numeric, 3) AS rmse_mw,
       count(*)                                          AS hours
FROM paired
WHERE previous_mw IS NOT NULL;

-- What it should tell you:
--   This is the number to beat. Report any model's skill AGAINST it, not in
--   absolute terms - an RMSE of 5 MW means nothing on its own.


-- ---------------------------------------------------------------
-- 6.2  Where forecasting is actually hard
-- ---------------------------------------------------------------
-- Average error hides the cases that matter. Rank hours by how fast output
-- was changing.
WITH hourly AS (
    SELECT date_trunc('hour', time) AS hour,
           avg(grid_export_power_kw) / 1000.0 AS mw
    FROM plant_telemetry
    GROUP BY 1
),
ramps AS (
    SELECT hour, mw, mw - lag(mw) OVER (ORDER BY hour) AS ramp_mw
    FROM hourly
)
SELECT CASE WHEN abs(ramp_mw) > 20 THEN 'steep (>20 MW/h)'
            WHEN abs(ramp_mw) > 5  THEN 'moderate'
            ELSE 'calm' END                                AS regime,
       count(*)                                            AS hours,
       round(avg(abs(ramp_mw))::numeric, 2)                AS mean_abs_ramp
FROM ramps
WHERE ramp_mw IS NOT NULL
GROUP BY 1 ORDER BY mean_abs_ramp DESC;

-- What it should tell you:
--   Calm hours dominate the count, so they dominate any average metric. A
--   model can look excellent overall and be useless in the steep decile -
--   which is the only decile anyone cares about, because that is when the
--   grid needs the forecast.


-- ---------------------------------------------------------------
-- 6.3  Leakage, deliberately
-- ---------------------------------------------------------------
-- Build a feature that could not exist at forecast time and watch the metric
-- improve. This is the most valuable thing in the whole curriculum.
WITH hourly AS (
    SELECT date_trunc('hour', time) AS hour,
           avg(grid_export_power_kw) / 1000.0 AS mw,
           avg(poa_global)                    AS poa
    FROM plant_telemetry p
    JOIN inverter_telemetry i USING (time)
    GROUP BY 1
)
-- The lag must be materialised before aggregating: a window function cannot
-- sit inside an aggregate call in either engine.
, lagged AS (
    SELECT hour, mw, poa,
           lag(poa) OVER (ORDER BY hour) AS previous_poa
    FROM hourly
)
SELECT round(corr(mw, poa)::numeric, 4)          AS corr_same_hour,
       round(corr(mw, previous_poa)::numeric, 4) AS corr_lagged_poa
FROM lagged
WHERE previous_poa IS NOT NULL;

-- What it should tell you:
--   Same-hour POA correlates near-perfectly with same-hour output, because
--   it IS the output, one step earlier in the physics. Using it as a feature
--   to predict that hour is leakage: at forecast time you do not know it.
--
--   The lagged correlation is the honest one, and it is much weaker. If your
--   model's accuracy collapses when you lag every feature, it was never
--   forecasting - it was reading the answer.
