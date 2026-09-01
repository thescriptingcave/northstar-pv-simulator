-- DRILL 1: Window functions
--
-- The single most-tested area in data engineering interviews, and the one
-- most people half-know. Every query here has an interview equivalent.
--
--   DuckDB     : views are unqualified
--   PostgreSQL : SET search_path TO telemetry, public;

-- ---------------------------------------------------------------
-- 1.1  ROW_NUMBER vs RANK vs DENSE_RANK
-- ---------------------------------------------------------------
-- Interview shape: "rank employees by salary within each department"
-- Here:            rank inverters by output within each block
--
-- Know the difference cold. With ties at 2nd place:
--   ROW_NUMBER  1 2 3 4   arbitrary tiebreak, always distinct
--   RANK        1 2 2 4   ties share, then a GAP
--   DENSE_RANK  1 2 2 3   ties share, NO gap
WITH daily AS (
    SELECT substr(asset_id, 1, 14)          AS block_id,
           asset_id,
           sum(ac_power_kw) / 60.0 / 1000.0 AS mwh
    FROM inverter_telemetry
    WHERE ac_power_kw > 0
    GROUP BY 1, 2
)
SELECT block_id, asset_id, round(mwh::numeric, 2) AS mwh,
       row_number() OVER (PARTITION BY block_id ORDER BY mwh DESC) AS rn,
       rank()       OVER (PARTITION BY block_id ORDER BY mwh DESC) AS rnk,
       dense_rank() OVER (PARTITION BY block_id ORDER BY mwh DESC) AS dense
FROM daily
ORDER BY block_id, rn
LIMIT 12;


-- ---------------------------------------------------------------
-- 1.2  Top-N per group
-- ---------------------------------------------------------------
-- Interview shape: "the top 2 earners in each department"
-- Here:            the 2 worst inverters in each block
--
-- The pattern is always: window in a subquery, filter outside. You cannot
-- filter on a window function in WHERE, because WHERE runs before the window
-- is computed. That is the actual thing being tested.
WITH daily AS (
    SELECT substr(asset_id, 1, 14)          AS block_id,
           asset_id,
           sum(ac_power_kw) / 60.0 / 1000.0 AS mwh
    FROM inverter_telemetry
    WHERE ac_power_kw > 0
    GROUP BY 1, 2
),
ranked AS (
    SELECT block_id, asset_id, mwh,
           row_number() OVER (PARTITION BY block_id ORDER BY mwh ASC) AS rn
    FROM daily
)
SELECT block_id, asset_id, round(mwh::numeric, 2) AS mwh
FROM ranked
WHERE rn <= 2
ORDER BY block_id, mwh
LIMIT 12;


-- ---------------------------------------------------------------
-- 1.3  Running total and moving average
-- ---------------------------------------------------------------
-- Interview shape: "cumulative revenue by month", "7-day moving average"
--
-- The frame clause is the part people get wrong:
--   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW   running total
--   ROWS BETWEEN 6 PRECEDING AND CURRENT ROW           trailing 7
--   RANGE  is value-based, ROWS is position-based - they differ with ties
SELECT day,
       round(mwh::numeric, 1)                                     AS mwh,
       round(sum(mwh) OVER (ORDER BY day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::numeric, 1)
                                                                  AS cumulative,
       round(avg(mwh) OVER (ORDER BY day
             ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)::numeric, 2)
                                                                  AS moving_avg_7
FROM (
    SELECT CAST(time AS DATE) AS day,
           sum(grid_export_power_kw) / 60.0 / 1000.0 AS mwh
    FROM plant_telemetry
    GROUP BY 1
) d
ORDER BY day;


-- ---------------------------------------------------------------
-- 1.4  LAG, LEAD and period-over-period change
-- ---------------------------------------------------------------
-- Interview shape: "month-over-month growth", "days since last order"
--
-- Watch the NULL on the first row, and decide deliberately what it means.
SELECT day,
       round(mwh::numeric, 1)                                    AS mwh,
       round(lag(mwh) OVER (ORDER BY day)::numeric, 1)           AS previous_day,
       round((mwh - lag(mwh) OVER (ORDER BY day))::numeric, 1)   AS change,
       round((100.0 * (mwh - lag(mwh) OVER (ORDER BY day))
              / nullif(lag(mwh) OVER (ORDER BY day), 0))::numeric, 1)
                                                                 AS pct_change
FROM (
    SELECT CAST(time AS DATE) AS day,
           sum(grid_export_power_kw) / 60.0 / 1000.0 AS mwh
    FROM plant_telemetry
    GROUP BY 1
) d
ORDER BY day;


-- ---------------------------------------------------------------
-- 1.5  FIRST_VALUE, LAST_VALUE and the frame trap
-- ---------------------------------------------------------------
-- LAST_VALUE is the classic interview trap. The DEFAULT frame is
--   RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
-- so LAST_VALUE returns the CURRENT row, not the last one. You must state
-- the frame explicitly.
SELECT day,
       round(mwh::numeric, 1) AS mwh,
       round(first_value(mwh) OVER w::numeric, 1)  AS first_in_window,
       round(last_value(mwh) OVER (
           ORDER BY day
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       )::numeric, 1)                              AS last_correct,
       round(last_value(mwh) OVER (ORDER BY day)::numeric, 1)
                                                   AS last_wrong
FROM (
    SELECT CAST(time AS DATE) AS day,
           sum(grid_export_power_kw) / 60.0 / 1000.0 AS mwh
    FROM plant_telemetry
    GROUP BY 1
) d
WINDOW w AS (ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
ORDER BY day;

-- `last_wrong` equals `mwh` on every row. That is the trap, and it is asked
-- often enough to be worth recognising instantly.
