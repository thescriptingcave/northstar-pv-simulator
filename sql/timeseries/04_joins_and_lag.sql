-- TS-109 .. TS-111 : cross-series alignment, spatial lag, asset comparison
--
--     psql "$NORTHSTAR_DSN" -f sql/timeseries/04_joins_and_lag.sql

\echo '=== TS-109  Aligning 1-minute telemetry to 15-minute settlement ==='
-- Two series at different cadences. The join key is the settlement bucket, not
-- the timestamp - a direct equality join returns almost nothing, because a
-- 1-minute reading rarely lands exactly on a 15-minute boundary.
--
-- Energy is summed within the bucket, then valued once at the bucket's price.
-- Valuing each minute at a 15-minute price and summing gives the same answer
-- here only because the price is constant across the bucket; that is a
-- property of settlement, not of arithmetic, and it stops being true the
-- moment you use a price that varies within the interval.
SELECT time_bucket('15 minutes', time)                          AS settlement_interval,
       round((sum(grid_export_power_kw) / 60 / 1000)::numeric, 3) AS mwh,
       round(avg(curtailed_power_kw / 1000)::numeric, 2)          AS mean_curtailed_mw
FROM telemetry.plant_telemetry
WHERE time >= '2023-06-22 17:00' AND time < '2023-06-22 19:00'
GROUP BY settlement_interval ORDER BY settlement_interval;


\echo ''
\echo '=== TS-110  Recovering wind direction from irradiance alone ==='
-- Clouds cross the site, so a downwind asset sees the same shadow later. The
-- lag between two assets is distance along the wind divided by wind speed, and
-- the correlation peak recovers it - without ever reading the anemometer.
--
-- **You must detrend first.** Correlating raw irradiance gives 0.998 at every
-- offset from -12 to +12 minutes: the diurnal cycle is common to both assets
-- and swamps the cloud signal completely. The result looks like a strong
-- correlation and carries no information at all.
--
-- Subtract the plant-mean irradiance at each timestamp. What remains is the
-- spatial deviation - which asset is brighter or darker than the fleet right
-- now - and that is the only part carrying the advection signature.
--
-- The peak here is -9 minutes at r = 0.141. Low absolute correlation is
-- expected and correct: after removing the diurnal signal, only the cloud
-- field is left. A clean interior peak matters, not a large number.
WITH plant_mean AS (
    SELECT time, avg(poa_global) AS mean_poa
    FROM telemetry.inverter_telemetry
    WHERE poa_global > 100
    GROUP BY time
),
residual AS (
    SELECT i.time, i.asset_id, i.poa_global - p.mean_poa AS deviation
    FROM telemetry.inverter_telemetry i
    JOIN plant_mean p USING (time)
    WHERE i.asset_id IN ('NORTHSTA-BLK01-INV1', 'NORTHSTA-BLK10-INV4')
      AND i.poa_global > 100
),
upwind   AS (SELECT time, deviation FROM residual
             WHERE asset_id = 'NORTHSTA-BLK01-INV1'),
downwind AS (SELECT time, deviation FROM residual
             WHERE asset_id = 'NORTHSTA-BLK10-INV4'),
offsets  AS (SELECT generate_series(-20, 20) AS lag_minutes)
SELECT lag_minutes,
       round(corr(upwind.deviation, downwind.deviation)::numeric, 4) AS residual_corr
FROM offsets
JOIN upwind ON true
JOIN downwind ON downwind.time = upwind.time
                 + (lag_minutes || ' minutes')::interval
GROUP BY lag_minutes
ORDER BY residual_corr DESC
LIMIT 5;

-- Check the peak is in the interior of your search range. A peak at the edge
-- means the true lag is outside it and you are reading a truncated curve.


\echo '=== TS-111  Peer comparison at bucket resolution ==='
-- Normalise each inverter against its block mean at every bucket. This removes
-- the weather, which is the dominant signal, leaving equipment behaviour.
--
-- Normalising against the *plant* mean instead manufactures underperformers
-- out of the spatial cloud field: an inverter under a cloud is not a faulty
-- inverter.
WITH bucketed AS (
    SELECT time_bucket('15 minutes', time) AS bucket,
           asset_id,
           substr(asset_id, 1, 14)         AS block_id,
           avg(ac_power_kw)                AS kw
    FROM telemetry.inverter_telemetry
    WHERE ac_power_kw > 50
    GROUP BY bucket, asset_id, block_id
),
peers AS (
    SELECT bucket, asset_id, kw,
           avg(kw) OVER (PARTITION BY bucket, block_id) AS block_mean
    FROM bucketed
)
SELECT asset_id,
       count(*)                                              AS buckets,
       round(avg(kw / nullif(block_mean, 0))::numeric, 4)    AS mean_peer_ratio,
       round(min(kw / nullif(block_mean, 0))::numeric, 4)    AS worst_bucket
FROM peers
GROUP BY asset_id
ORDER BY mean_peer_ratio
LIMIT 8;
