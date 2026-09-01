-- DRILL 3: Joins, deduplication and NULL semantics
--
-- Where most SQL interviews are actually lost. Not the exotic stuff - the
-- join that silently multiplies rows and the NULL that eats a comparison.

-- ---------------------------------------------------------------
-- 3.1  Self-join for period comparison
-- ---------------------------------------------------------------
-- Interview shape: "compare each month to the same month last year"
-- Here:            compare each inverter to its block sibling
--
-- The join predicate `a.asset_id < b.asset_id` gives each PAIR once. Using
-- `!=` gives every pair twice, which is a classic silent doubling.
WITH daily AS (
    SELECT substr(asset_id, 1, 14) AS block_id, asset_id,
           sum(ac_power_kw) / 60.0 / 1000.0 AS mwh
    FROM inverter_telemetry
    WHERE ac_power_kw > 0 GROUP BY 1, 2
)
SELECT a.block_id, a.asset_id AS inverter_a, b.asset_id AS inverter_b,
       round(a.mwh::numeric, 2) AS mwh_a,
       round(b.mwh::numeric, 2) AS mwh_b,
       round((100.0 * (a.mwh - b.mwh) / nullif(b.mwh, 0))::numeric, 2) AS pct_diff
FROM daily a
JOIN daily b ON a.block_id = b.block_id AND a.asset_id < b.asset_id
ORDER BY abs(a.mwh - b.mwh) DESC
LIMIT 10;


-- ---------------------------------------------------------------
-- 3.2  Deduplication - keep one row per key
-- ---------------------------------------------------------------
-- Interview shape: "remove duplicate customers, keeping the most recent"
--
-- ROW_NUMBER in a CTE, filter to 1. Know why DISTINCT is not enough: DISTINCT
-- dedupes IDENTICAL rows, but here the rows differ in the column you want to
-- choose between.
WITH ranked AS (
    SELECT time, asset_id, ac_power_kw,
           row_number() OVER (
               PARTITION BY asset_id, CAST(time AS DATE)
               ORDER BY ac_power_kw DESC
           ) AS rn
    FROM inverter_telemetry
    WHERE ac_power_kw IS NOT NULL
)
SELECT CAST(time AS DATE) AS day, asset_id,
       round(ac_power_kw::numeric, 1) AS peak_kw, time AS peak_at
FROM ranked
WHERE rn = 1
ORDER BY day, asset_id
LIMIT 12;


-- ---------------------------------------------------------------
-- 3.3  NULL semantics - the quiet killer
-- ---------------------------------------------------------------
-- Interview shape: "why does this WHERE clause drop rows?"
--
-- NULL is not a value; it is the absence of one. NULL != 100 is NULL, not
-- true, so the row is excluded. COUNT(col) skips NULLs; COUNT(*) does not.
-- These three columns should differ, and you should be able to say why.
SELECT count(*)                                          AS all_rows,
       count(ac_power_kw)                                AS non_null_power,
       count(*) FILTER (WHERE ac_power_kw IS NULL)       AS null_power,
       count(*) FILTER (WHERE ac_power_kw != 0)          AS explicitly_nonzero,
       round(avg(ac_power_kw)::numeric, 2)               AS mean_skips_nulls
FROM inverter_telemetry;


-- ---------------------------------------------------------------
-- 3.4  Anti-join: rows with no match
-- ---------------------------------------------------------------
-- Interview shape: "customers who never ordered"
-- Here:            timestamps with plant data but no inverter data
--
-- Three ways to write it. NOT EXISTS is usually safest: NOT IN returns NOTHING
-- if the subquery yields a single NULL, which is a genuinely nasty bug.
SELECT p.time, round(p.grid_export_power_kw::numeric, 1) AS export_kw
FROM plant_telemetry p
WHERE NOT EXISTS (
    SELECT 1 FROM inverter_telemetry i WHERE i.time = p.time
)
ORDER BY p.time
LIMIT 10;


-- ---------------------------------------------------------------
-- 3.5  Aggregate then join, or join then aggregate?
-- ---------------------------------------------------------------
-- Joining first multiplies rows before you aggregate, inflating any SUM on
-- the one-side of a one-to-many. Aggregating first avoids it.
--
-- This is the fan-out trap, and it is the most expensive mistake on this list
-- because the query still returns a plausible number.
WITH inverter_daily AS (
    SELECT CAST(time AS DATE) AS day,
           sum(ac_power_kw) / 60.0 / 1000.0 AS inverter_mwh
    FROM inverter_telemetry WHERE ac_power_kw > 0 GROUP BY 1
),
plant_daily AS (
    SELECT CAST(time AS DATE) AS day,
           sum(grid_export_power_kw) / 60.0 / 1000.0 AS plant_mwh
    FROM plant_telemetry WHERE grid_export_power_kw > 0 GROUP BY 1
)
SELECT p.day,
       round(i.inverter_mwh::numeric, 1) AS inverter_mwh,
       round(p.plant_mwh::numeric, 1)    AS plant_mwh,
       round((100.0 * (i.inverter_mwh - p.plant_mwh)
              / nullif(p.plant_mwh, 0))::numeric, 2) AS pct_loss
FROM plant_daily p
JOIN inverter_daily i ON i.day = p.day
ORDER BY p.day;

-- The difference is real: transformer and line losses between the inverters
-- and the meter. Expect a couple of percent. If you had joined the raw tables
-- first, each plant row would have been multiplied by 40 inverters.
