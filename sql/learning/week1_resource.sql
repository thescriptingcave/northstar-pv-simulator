-- WEEK 1: The resource, and why irradiance is not one number
--
-- Runs in DuckDB, TablePlus/psql, Jupyter and Grafana.
--   DuckDB      : views are unqualified  (inverter_telemetry)
--   PostgreSQL  : SET search_path TO telemetry, public;
--
-- The concept: a PV plant converts *plane-of-array* irradiance, not the
-- horizontal irradiance a weather service reports. Everything downstream
-- depends on getting that distinction right.

-- ---------------------------------------------------------------
-- 1.1  GHI is not POA, and the gap is the tracker
-- ---------------------------------------------------------------
-- A horizontal sensor and a tracked module see different amounts of the same
-- sun. Ask by how much, and when.
--
-- Expect POA to exceed GHI most in the morning and evening, least at solar
-- noon. That is the tracker doing its job: it is pointing at a sun the
-- horizontal sensor sees obliquely.
SELECT date_part('hour', w.time) AS hour_utc,
       round(avg(w.ghi)::numeric, 1)                       AS mean_ghi,
       round(avg(i.poa_global)::numeric, 1)                AS mean_poa,
       round(avg(i.poa_global / nullif(w.ghi, 0))::numeric, 3) AS poa_over_ghi
FROM weather_telemetry w
JOIN inverter_telemetry i ON i.time = w.time
WHERE w.ghi > 50 AND i.asset_id = 'NORTHSTA-BLK01-INV1'
GROUP BY 1 ORDER BY 1;

-- What it should tell you:
--   The ratio is a U-shape across the day. If you see a flat ratio, you are
--   probably looking at a fixed-tilt array or a bug.


-- ---------------------------------------------------------------
-- 1.2  Clearness index: separating weather from geometry
-- ---------------------------------------------------------------
-- Raw irradiance confounds two things: how high the sun is, and how much
-- cloud is in the way. The clearness index divides one out.
--
-- kt = GHI / clear-sky GHI. Near 1.0 is a clear sky; 0.2 is heavy overcast.
-- Because the geometry is divided out, kt is comparable across hours and
-- seasons in a way GHI never is.
SELECT CAST(time AS DATE)                          AS day,
       round(avg(ghi)::numeric, 1)                 AS mean_ghi,
       round(max(ghi)::numeric, 1)                 AS peak_ghi,
       count(*) FILTER (WHERE ghi > 50)            AS daylight_samples
FROM weather_telemetry
GROUP BY 1 ORDER BY 1 LIMIT 20;

-- What it should tell you:
--   Mean GHI conflates day length with cloudiness. A short clear winter day
--   and a long cloudy summer day can report the same mean. This is why
--   performance work normalises before it compares.


-- ---------------------------------------------------------------
-- 1.3  The three weather stations disagree, and that is correct
-- ---------------------------------------------------------------
-- Three sensors on one site should never agree exactly. Cloud shadows are
-- smaller than the site.
SELECT time,
       round(max(ghi)::numeric - min(ghi)::numeric, 1) AS spread,
       round(avg(ghi)::numeric, 1)                     AS mean_ghi,
       count(*)                                        AS stations
FROM weather_telemetry
WHERE ghi > 200
GROUP BY time
HAVING max(ghi) - min(ghi) > 100
ORDER BY spread DESC
LIMIT 15;

-- What it should tell you:
--   The largest disagreements are cloud edges crossing the site. If two
--   stations NEVER disagree, one of them is a copy of the other - which is a
--   real failure mode in plant SCADA.
