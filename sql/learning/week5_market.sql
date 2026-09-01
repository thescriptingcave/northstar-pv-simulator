-- WEEK 5: What the energy was worth
--
-- The concept: megawatt-hours are not fungible. One produced at 5pm in August
-- is worth several produced at noon in April. Every physical metric in weeks
-- 1-4 is blind to this.

-- ---------------------------------------------------------------
-- 5.1  Capture rate - the number that exposes the problem
-- ---------------------------------------------------------------
-- Capture rate = generation-weighted price / time-weighted price.
--
-- Below 1.0 means you produce when energy is cheap. For solar this is
-- structural: everyone's panels peak at the same moment.
--
-- Needs a price series joined at settlement grain - see week 6 for the join.
-- Here, look at the production shape that causes it.
SELECT date_part('hour', time) AS hour_utc,
       round((sum(grid_export_power_kw) / 60.0 / 1000.0)::numeric, 1) AS mwh,
       round(avg(grid_export_power_kw / 1000.0)::numeric, 2)          AS mean_mw
FROM plant_telemetry
WHERE grid_export_power_kw > 0
GROUP BY 1 ORDER BY 1;

-- What it should tell you:
--   Production is a narrow midday hump. Prices in a high-solar market are a
--   trough at exactly that hour and a peak after sunset, when you produce
--   nothing. That mismatch IS the capture rate.
--
--   Measured on real 2025 ERCOT prices at this plant's node: 82.2%.


-- ---------------------------------------------------------------
-- 5.2  Curtailment economics
-- ---------------------------------------------------------------
-- Curtailed energy has a revenue impact. The sign is the interesting part.
SELECT CAST(time AS DATE) AS day,
       round((sum(curtailed_power_kw) / 60.0 / 1000.0)::numeric, 2) AS curtailed_mwh,
       round((sum(grid_export_power_kw) / 60.0 / 1000.0)::numeric, 1) AS exported_mwh
FROM plant_telemetry
WHERE curtailed_power_kw > 0
GROUP BY 1 ORDER BY curtailed_mwh DESC LIMIT 15;

-- What it should tell you:
--   Join these days to prices and compute what the curtailed energy would
--   have earned. On this dataset the answer is NEGATIVE - curtailing made
--   money, because those intervals cleared below the production tax credit
--   floor. Generating into them would have cost more than stopping.
--
--   Any pipeline that reports "lost revenue" as unconditionally positive
--   breaks on this, and finding out why is the point.


-- ---------------------------------------------------------------
-- 5.3  Settlement grain, not sample grain
-- ---------------------------------------------------------------
-- The market settles in 15-minute intervals. Telemetry is 1-minute. Valuing
-- each minute at its interval's price and summing is correct; valuing the
-- interval mean is also correct; mixing the two is not.
SELECT date_trunc('hour', time)                                      AS hour,
       round((sum(grid_export_power_kw) / 60.0 / 1000.0)::numeric, 3) AS mwh,
       count(*)                                                       AS minutes
FROM plant_telemetry
WHERE grid_export_power_kw > 0
GROUP BY 1 ORDER BY 1 LIMIT 24;

-- What it should tell you:
--   Every full hour has 60 minutes. Partial hours at the record edges do not,
--   and a mean-based aggregate silently over-weights them. Weight by the
--   sample count, always.
