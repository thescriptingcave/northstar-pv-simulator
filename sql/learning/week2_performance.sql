-- WEEK 2: Performance ratio, and the temperature trap
--
-- The concept: PR asks "how much of the available energy did we capture?"
-- It is the single most-quoted PV metric and the easiest to compute wrongly.

-- ---------------------------------------------------------------
-- 2.1  Performance ratio, the naive way
-- ---------------------------------------------------------------
-- PR = actual energy / (POA energy x nameplate / 1000 W/m2)
--
-- Compute it daily and look at the seasonal shape.
SELECT CAST(i.time AS DATE) AS day,
       round((sum(i.ac_power_kw) / 60.0 / 1000.0)::numeric, 2)      AS mwh,
       round(avg(i.poa_global)::numeric, 1)                          AS mean_poa,
       round((sum(i.ac_power_kw) / nullif(sum(i.poa_global), 0)
              * 1000.0 / 2500.0)::numeric, 4)                        AS crude_pr
FROM inverter_telemetry i
WHERE i.poa_global > 50 AND i.ac_power_kw IS NOT NULL
GROUP BY 1 ORDER BY 1 LIMIT 30;

-- What it should tell you:
--   PR is HIGHER in winter. That is not better maintenance - it is colder
--   silicon. Modules lose roughly 0.4% per degree above 25 C, so a plant in
--   July looks worse than the same plant in January.


-- ---------------------------------------------------------------
-- 2.2  The confounder, made visible
-- ---------------------------------------------------------------
-- Bucket by cell temperature and watch PR fall as the modules heat up.
-- floor() rather than width_bucket(): portable across DuckDB and
-- PostgreSQL, and the arithmetic is visible.
SELECT floor(cell_temperature / 10) * 10 AS temp_band_c,
       count(*)                                       AS samples,
       round(avg(cell_temperature)::numeric, 1)       AS mean_cell_c,
       round(avg(ac_power_kw / nullif(poa_global, 0)
                 * 1000.0 / 2500.0)::numeric, 4)      AS mean_pr
FROM inverter_telemetry
WHERE poa_global > 400 AND ac_power_kw > 0
GROUP BY 1 ORDER BY 1;

-- What it should tell you:
--   A clean monotonic decline. Filtering to poa > 400 matters: at low
--   irradiance the inverter's own efficiency curve dominates and you would be
--   measuring two effects at once.


-- ---------------------------------------------------------------
-- 2.3  Temperature-corrected PR
-- ---------------------------------------------------------------
-- Correct each interval to 25 C before aggregating. The correction is
--   PR_corrected = PR / (1 + gamma * (T_cell - 25))
-- with gamma = -0.0043 for this module.
--
-- Correct FIRST, then aggregate. Correcting an aggregate uses a mean
-- temperature that no interval actually had.
SELECT CAST(time AS DATE) AS day,
       round(avg(ac_power_kw / nullif(poa_global, 0)
                 * 1000.0 / 2500.0)::numeric, 4)                     AS raw_pr,
       round(avg(ac_power_kw / nullif(poa_global, 0) * 1000.0 / 2500.0
                 / (1 + (-0.0043) * (cell_temperature - 25)))::numeric, 4)
                                                                     AS corrected_pr
FROM inverter_telemetry
WHERE poa_global > 400 AND ac_power_kw > 0
GROUP BY 1 ORDER BY 1 LIMIT 30;

-- What it should tell you:
--   The corrected series is flatter across the year. Residual seasonality
--   after correction is real - soiling, snow, spectrum - and that is exactly
--   what you wanted to see before the temperature signal buried it.
