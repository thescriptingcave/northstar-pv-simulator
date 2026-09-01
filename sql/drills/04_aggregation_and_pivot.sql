-- DRILL 4: Aggregation, conditional logic and reshaping
--
-- Interviews test whether you can reshape without a spreadsheet.

-- ---------------------------------------------------------------
-- 4.1  Conditional aggregation - the pivot you should reach for first
-- ---------------------------------------------------------------
-- Interview shape: "sales by region as columns, not rows"
--
-- CASE inside an aggregate beats any dialect-specific PIVOT: it is portable,
-- readable, and works everywhere. FILTER is the SQL-standard spelling and is
-- supported by both DuckDB and PostgreSQL.
SELECT CAST(time AS DATE) AS day,
       count(*) FILTER (WHERE operating_state = 'RUNNING')   AS running,
       count(*) FILTER (WHERE operating_state = 'STANDBY')   AS standby,
       count(*) FILTER (WHERE operating_state = 'CURTAILED') AS curtailed,
       count(*) FILTER (WHERE operating_state = 'FAULT')     AS fault,
       round(100.0 * count(*) FILTER (WHERE operating_state = 'FAULT')
             / count(*), 3)                                  AS pct_fault
FROM inverter_telemetry
GROUP BY 1 ORDER BY 1;


-- ---------------------------------------------------------------
-- 4.2  HAVING against WHERE
-- ---------------------------------------------------------------
-- Interview shape: "departments with more than 5 employees earning over 100k"
--
-- WHERE filters ROWS before grouping. HAVING filters GROUPS after. Both
-- appear here and swapping them changes the answer, not just the plan.
SELECT asset_id,
       count(*)                                    AS bright_minutes,
       round(avg(ac_power_kw)::numeric, 1)         AS mean_kw
FROM inverter_telemetry
WHERE poa_global > 600                     -- rows: only bright intervals
GROUP BY asset_id
HAVING avg(ac_power_kw) < 2000             -- groups: only weak inverters
ORDER BY mean_kw
LIMIT 10;


-- ---------------------------------------------------------------
-- 4.3  Percentiles and the median
-- ---------------------------------------------------------------
-- Interview shape: "median order value", "p95 latency"
--
-- AVG is not the median and hides skew. Both engines support
-- PERCENTILE_CONT, which interpolates - use PERCENTILE_DISC if you need an
-- actual observed value.
SELECT asset_id,
       round(avg(ac_power_kw)::numeric, 1)                              AS mean_kw,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ac_power_kw)::numeric, 1)
                                                                        AS median_kw,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY ac_power_kw)::numeric, 1)
                                                                        AS p95_kw
FROM inverter_telemetry
WHERE poa_global > 300 AND ac_power_kw IS NOT NULL
GROUP BY asset_id
ORDER BY median_kw
LIMIT 10;

-- Mean well below median means a long left tail - outages dragging the
-- average down while most intervals are healthy. That distinction matters and
-- an average alone cannot show it.


-- ---------------------------------------------------------------
-- 4.4  UNPIVOT: wide to long, portably
-- ---------------------------------------------------------------
-- Interview shape: "turn twelve month columns into rows"
--
-- DuckDB has UNPIVOT; PostgreSQL does not. LATERAL over a VALUES list works
-- in both, and being able to write it is worth more than knowing one dialect's
-- shortcut.
SELECT v.channel,
       count(*)                        AS samples,
       round(avg(v.value)::numeric, 2) AS mean_value
FROM inverter_telemetry i
CROSS JOIN LATERAL (VALUES
    ('poa_global',       i.poa_global),
    ('cell_temperature', i.cell_temperature),
    ('dc_power_kw',      i.dc_power_kw),
    ('ac_power_kw',      i.ac_power_kw)
) AS v(channel, value)
WHERE i.poa_global > 300 AND v.value IS NOT NULL
GROUP BY v.channel
ORDER BY v.channel;


-- ---------------------------------------------------------------
-- 4.5  GROUPING SETS: several aggregations in one pass
-- ---------------------------------------------------------------
-- Interview shape: "totals by region, by product, and overall - one query"
--
-- Beats UNION ALL of three queries: one table scan instead of three, and the
-- optimiser can share the work.
SELECT COALESCE(CAST(CAST(time AS DATE) AS VARCHAR), 'ALL DAYS') AS day,
       COALESCE(operating_state, 'ALL STATES')                   AS state,
       count(*)                                                  AS minutes
FROM inverter_telemetry
GROUP BY GROUPING SETS (
    (CAST(time AS DATE), operating_state),
    (CAST(time AS DATE)),
    ()
)
ORDER BY day, state
LIMIT 20;
