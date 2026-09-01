-- WEEK 4: Finding faults without being told where they are
--
-- The concept: you cannot detect a fault by looking at one inverter, because
-- output falls for weather too. Detection is always comparative.

-- ---------------------------------------------------------------
-- 4.1  Peer normalisation - the core technique
-- ---------------------------------------------------------------
-- Normalise each inverter against its BLOCK, not the plant. Blocks share
-- weather closely; the plant does not.
WITH peers AS (
    SELECT time, asset_id,
           substr(asset_id, 1, 14) AS block_id,
           ac_power_kw,
           avg(ac_power_kw) OVER (
               PARTITION BY time, substr(asset_id, 1, 14)
           ) AS block_mean
    FROM inverter_telemetry
    WHERE poa_global > 300 AND ac_power_kw IS NOT NULL
)
SELECT asset_id,
       count(*)                                                   AS samples,
       round(avg(ac_power_kw / nullif(block_mean, 0))::numeric, 4) AS peer_ratio,
       round(min(ac_power_kw / nullif(block_mean, 0))::numeric, 4) AS worst
FROM peers
GROUP BY asset_id
ORDER BY peer_ratio
LIMIT 10;

-- What it should tell you:
--   Healthy inverters sit near 1.00. A sustained ratio below ~0.95 is worth
--   investigating. Normalising against the PLANT mean instead manufactures
--   underperformers out of the cloud field.


-- ---------------------------------------------------------------
-- 4.2  Sustained, not instantaneous
-- ---------------------------------------------------------------
-- One bad minute is noise. Fifteen consecutive is an event. This is the
-- gaps-and-islands pattern, and it is worth learning properly.
WITH ratios AS (
    SELECT time, asset_id,
           ac_power_kw / nullif(avg(ac_power_kw) OVER (
               PARTITION BY time, substr(asset_id, 1, 14)), 0) AS ratio
    FROM inverter_telemetry
    WHERE poa_global > 300 AND ac_power_kw IS NOT NULL
),
flagged AS (
    SELECT time, asset_id, ratio, ratio < 0.75 AS bad
    FROM ratios
),
islands AS (
    SELECT time, asset_id, ratio, bad,
           sum(CASE WHEN bad THEN 0 ELSE 1 END)
               OVER (PARTITION BY asset_id ORDER BY time) AS island
    FROM flagged
)
SELECT asset_id, min(time) AS started, max(time) AS ended,
       count(*) AS minutes, round(avg(ratio)::numeric, 3) AS mean_ratio
FROM islands
WHERE bad
GROUP BY asset_id, island
HAVING count(*) >= 15
ORDER BY minutes DESC
LIMIT 15;

-- What it should tell you:
--   These are candidate events. Score them: how many did you find, and how
--   many were real? `northstar-sim score` does exactly this against the truth
--   tree, and a naive threshold recovers about 39% of injected faults at 82%
--   precision. Beating that is the exercise.


-- ---------------------------------------------------------------
-- 4.3  Faults that peer analysis cannot see
-- ---------------------------------------------------------------
-- A stuck tracker reduces the irradiance the modules receive. Output falls,
-- but so does POA - so the power/POA ratio looks normal.
SELECT asset_id,
       round(avg(tracker_angle_deg)::numeric, 2)              AS mean_angle,
       round(stddev(tracker_angle_deg)::numeric, 2)           AS angle_stddev,
       round(avg(poa_global)::numeric, 1)                     AS mean_poa
FROM inverter_telemetry
WHERE poa_global > 200
GROUP BY asset_id
ORDER BY angle_stddev
LIMIT 10;

-- What it should tell you:
--   A tracker that should sweep +/-60 degrees over a day has a large angle
--   standard deviation. A small one means it stopped moving. Peer power
--   analysis misses this entirely - which is why detection needs more than
--   one method.
