-- WEEK 3: Where the energy went
--
-- The concept: a loss waterfall accounts for every megawatt-hour between the
-- irradiance arriving and the energy exported. Getting one term wrong makes
-- the total still balance, which is what makes waterfalls dangerous.

-- ---------------------------------------------------------------
-- 3.1  Clipping: a design decision, not a fault
-- ---------------------------------------------------------------
-- This plant is 124.66 MWp DC behind 100 MW AC - a DC/AC ratio of 1.25. On
-- bright cool days the array produces more than the inverters can pass.
SELECT CAST(time AS DATE) AS day,
       round((sum(ac_preclip_kw - ac_power_kw) / 60.0 / 1000.0)::numeric, 3)
                                                        AS clipped_mwh,
       count(*) FILTER (WHERE ac_preclip_kw - ac_power_kw > 1)
                                                        AS clipped_minutes,
       round(max(cell_temperature)::numeric, 1)         AS peak_cell_c
FROM inverter_telemetry
WHERE ac_preclip_kw IS NOT NULL
GROUP BY 1 HAVING sum(ac_preclip_kw - ac_power_kw) > 0
ORDER BY clipped_mwh DESC LIMIT 15;

-- What it should tell you:
--   Clipping is worst on cool bright days, not hot ones. Heat reduces DC
--   output, so it reduces clipping. The two losses are NOT additive, and
--   estimating them separately then summing over-counts.


-- ---------------------------------------------------------------
-- 3.2  Thermal derating suppresses clipping
-- ---------------------------------------------------------------
-- Compare clipped energy against ambient conditions directly.
-- floor() rather than width_bucket(): portable across both engines.
SELECT floor(cell_temperature / 10) * 10 AS cell_band_c,
       count(*)                                            AS samples,
       round((sum(ac_preclip_kw - ac_power_kw) / 60.0 / 1000.0)::numeric, 2)
                                                           AS clipped_mwh,
       round(avg(thermal_derate_factor)::numeric, 4)       AS mean_derate
FROM inverter_telemetry
WHERE poa_global > 700
GROUP BY 1 ORDER BY 1;

-- What it should tell you:
--   Clipped energy peaks in the middle temperature bands and falls at the
--   top, where derating has already reduced output below the AC limit. Two
--   mechanisms competing for the same megawatt-hours.


-- ---------------------------------------------------------------
-- 3.3  Curtailment is not a loss, it is a choice
-- ---------------------------------------------------------------
-- Curtailed energy looks exactly like an outage in a power trace. The
-- difference is the operating state and, ultimately, the price.
SELECT operating_state,
       count(*)                                              AS minutes,
       round((sum(curtailed_power_kw) / 60.0 / 1000.0)::numeric, 1)
                                                             AS curtailed_mwh,
       round(avg(poa_global)::numeric, 1)                    AS mean_poa
FROM inverter_telemetry
WHERE poa_global > 100
GROUP BY 1 ORDER BY minutes DESC;

-- What it should tell you:
--   CURTAILED intervals have high irradiance and low output - the signature
--   of a fault. Without the state column you would report a fleet-wide
--   equipment problem that never happened.
--
--   Then ask the harder question: was curtailing correct? Join prices and
--   find out whether those intervals would have earned or cost money.
