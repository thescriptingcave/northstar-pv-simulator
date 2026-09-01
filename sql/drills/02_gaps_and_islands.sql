-- DRILL 2: Gaps and islands
--
-- Consecutive runs of a condition. Asked constantly - "longest login streak",
-- "consecutive days above target", "sessions from an event log" - and it has
-- exactly two idioms worth memorising.

-- ---------------------------------------------------------------
-- 2.1  The difference-of-row-numbers trick
-- ---------------------------------------------------------------
-- Interview shape: "find consecutive days a user logged in"
-- Here:            consecutive minutes an inverter ran
--
-- The idea: for rows in a consecutive run, (row_number over the whole set)
-- minus (row_number within the group) is CONSTANT. That constant becomes the
-- group key. Once you have seen it, you have it forever.
WITH numbered AS (
    SELECT time, asset_id, operating_state,
           row_number() OVER (PARTITION BY asset_id ORDER BY time) AS overall,
           row_number() OVER (
               PARTITION BY asset_id, operating_state ORDER BY time
           ) AS within_state
    FROM inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK01-INV1'
)
SELECT operating_state,
       min(time) AS started,
       max(time) AS ended,
       count(*)  AS minutes
FROM numbered
GROUP BY asset_id, operating_state, overall - within_state
HAVING count(*) > 30
ORDER BY started
LIMIT 15;


-- ---------------------------------------------------------------
-- 2.2  The running-sum-of-a-flag trick
-- ---------------------------------------------------------------
-- Interview shape: "group events into sessions separated by 30-min gaps"
--
-- Often clearer than 2.1 and generalises better: mark where a new run
-- STARTS, then take a running sum of that marker as the group id.
WITH flagged AS (
    SELECT time, asset_id, ac_power_kw,
           CASE WHEN ac_power_kw > 100 THEN 0 ELSE 1 END AS is_break
    FROM inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK01-INV1' AND ac_power_kw IS NOT NULL
),
grouped AS (
    SELECT time, ac_power_kw, is_break,
           sum(is_break) OVER (ORDER BY time) AS run_id
    FROM flagged
)
SELECT run_id,
       min(time) AS started,
       max(time) AS ended,
       count(*)  AS minutes,
       round(avg(ac_power_kw)::numeric, 1) AS mean_kw
FROM grouped
WHERE is_break = 0
GROUP BY run_id
HAVING count(*) > 60
ORDER BY started
LIMIT 15;


-- ---------------------------------------------------------------
-- 2.3  Finding the GAPS rather than the islands
-- ---------------------------------------------------------------
-- Interview shape: "find missing invoice numbers", "detect data outages"
--
-- Compare each row's timestamp against the previous one. Anything beyond the
-- expected cadence is a gap. Note this finds gaps in the ROWS - a row present
-- with NULL values is a different problem and needs a different query.
WITH stepped AS (
    SELECT time, asset_id,
           lag(time) OVER (PARTITION BY asset_id ORDER BY time) AS previous
    FROM inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK01-INV1'
)
SELECT previous AS gap_started,
       time     AS resumed,
       time - previous AS gap_length
FROM stepped
WHERE previous IS NOT NULL
  AND time - previous > INTERVAL '1 minute'
ORDER BY gap_length DESC
LIMIT 10;


-- ---------------------------------------------------------------
-- 2.4  Islands with a tolerance
-- ---------------------------------------------------------------
-- Real data is noisy. One good minute inside a bad run should not split it.
-- Smooth first with a moving average, then island the smoothed series.
WITH smoothed AS (
    SELECT time, asset_id, ac_power_kw,
           avg(ac_power_kw) OVER (
               PARTITION BY asset_id ORDER BY time
               ROWS BETWEEN 4 PRECEDING AND 4 FOLLOWING
           ) AS smooth_kw
    FROM inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK01-INV1'
      AND poa_global > 300 AND ac_power_kw IS NOT NULL
),
flagged AS (
    SELECT time, smooth_kw,
           CASE WHEN smooth_kw < 500 THEN 0 ELSE 1 END AS is_break
    FROM smoothed
),
grouped AS (
    SELECT time, smooth_kw, is_break,
           sum(is_break) OVER (ORDER BY time) AS run_id
    FROM flagged
)
SELECT run_id, min(time) AS started, count(*) AS minutes,
       round(avg(smooth_kw)::numeric, 1) AS mean_kw
FROM grouped
WHERE is_break = 0
GROUP BY run_id
HAVING count(*) >= 10
ORDER BY minutes DESC
LIMIT 10;

-- Say out loud why you smoothed. "To avoid splitting a genuine event on a
-- single noisy sample" is the answer an interviewer wants; "it looked better"
-- is not.
