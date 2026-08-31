-- Three-way reconciliation - doc 15 section 11.
--
-- Legs 1 and 2 (raw hypertable vs continuous aggregate) are checked here.
-- Leg 3 (DuckDB over Parquet) is checked by `northstar-sim storage-gate`.
-- All three were measured in agreement to 1e-7 MWh; see doc 39.
--
--     psql -d northstar -v ON_ERROR_STOP=1 -f db/tests/test_reconciliation.sql

\set ON_ERROR_STOP on

DO $$
DECLARE
    raw_mwh numeric;
    agg_mwh numeric;
    hier_mwh numeric;
    tolerance numeric := 1e-4;
BEGIN
    SELECT sum(grid_export_power_kw) / 60 / 1000 INTO raw_mwh
    FROM telemetry.plant_telemetry WHERE grid_export_power_kw IS NOT NULL;

    SELECT sum(value * samples) / 60 / 1000 INTO agg_mwh
    FROM telemetry.plant_5min WHERE value IS NOT NULL;

    SELECT sum(value * samples) / 60 / 1000 INTO hier_mwh
    FROM telemetry.plant_hourly WHERE value IS NOT NULL;

    IF raw_mwh IS NULL OR agg_mwh IS NULL THEN
        RAISE EXCEPTION 'no data - load telemetry and refresh the aggregates first';
    END IF;

    -- An aggregate that disagrees with its source is worse than no aggregate:
    -- it is fast and wrong, and nothing downstream would notice.
    IF abs(raw_mwh - agg_mwh) > tolerance THEN
        RAISE EXCEPTION 'raw % vs 5-minute aggregate % (diff %)',
            raw_mwh, agg_mwh, abs(raw_mwh - agg_mwh);
    END IF;

    -- A hierarchical aggregate reads another aggregate, not raw data. If the
    -- chain drifts, every rollup above it is wrong too.
    IF abs(agg_mwh - hier_mwh) > tolerance THEN
        RAISE EXCEPTION '5-minute % vs hourly % (diff %)',
            agg_mwh, hier_mwh, abs(agg_mwh - hier_mwh);
    END IF;

    RAISE NOTICE 'reconciliation: raw % = 5min % = hourly % MWh',
        round(raw_mwh, 6), round(agg_mwh, 6), round(hier_mwh, 6);
END $$;
