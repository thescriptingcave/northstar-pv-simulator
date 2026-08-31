-- Schema and role scaffolding for the NorthStar dataset.
--
-- Truth separation is enforced by schema and role, not by naming convention.
-- A convention leaks: an analyst uses a truth column by accident and the
-- blind-analysis success criterion becomes unverifiable.
--
-- Reference: docs/design/13_time_series_data_model.md section 2.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Analyst-facing.
CREATE SCHEMA IF NOT EXISTS plant;      -- configuration and dimensions
CREATE SCHEMA IF NOT EXISTS telemetry;  -- measured and derived time series
CREATE SCHEMA IF NOT EXISTS ops;        -- events, alarms, maintenance
CREATE SCHEMA IF NOT EXISTS market;     -- prices, settlement, KPIs

-- Restricted.
CREATE SCHEMA IF NOT EXISTS truth;      -- physical truth and injected labels

-- Open.
CREATE SCHEMA IF NOT EXISTS meta;       -- run metadata, manifests, reports

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analyst') THEN
        CREATE ROLE analyst NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'validator') THEN
        CREATE ROLE validator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'simulator') THEN
        CREATE ROLE simulator NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA plant, telemetry, ops, market, meta TO analyst;
GRANT USAGE ON SCHEMA plant, telemetry, ops, market, meta, truth TO validator;
GRANT USAGE ON SCHEMA plant, telemetry, ops, market, meta, truth TO simulator;

-- The analyst role is never granted USAGE on truth. This is the mechanism
-- behind blind analysis; removing it silently disables the entire premise.
REVOKE ALL ON SCHEMA truth FROM analyst;
REVOKE ALL ON SCHEMA truth FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA plant, telemetry, ops, market, meta
    GRANT SELECT ON TABLES TO analyst;
ALTER DEFAULT PRIVILEGES IN SCHEMA plant, telemetry, ops, market, meta, truth
    GRANT SELECT ON TABLES TO validator;

-- The simulator writes every table, including truth. Granting USAGE on the
-- schemas is not enough: without table privileges the loader is denied on the
-- first INSERT.
--
-- This was missing entirely and went unnoticed because the file had only ever
-- been parsed, never executed. `SET ROLE simulator; SELECT ... FROM truth.*`
-- returned "permission denied for table inverter_truth" the first time a real
-- server ran it.
ALTER DEFAULT PRIVILEGES IN SCHEMA plant, telemetry, ops, market, meta, truth
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO simulator;
ALTER DEFAULT PRIVILEGES IN SCHEMA plant, telemetry, ops, market, meta, truth
    GRANT USAGE, SELECT ON SEQUENCES TO simulator;

-- Default privileges apply only to tables created *after* they are set, and
-- only by the role that set them. Grant on anything already present so the
-- file is order-independent with respect to the table DDL.
GRANT SELECT ON ALL TABLES IN SCHEMA plant, telemetry, ops, market, meta TO analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA plant, telemetry, ops, market, meta, truth
    TO validator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES
    IN SCHEMA plant, telemetry, ops, market, meta, truth TO simulator;

-- Re-assert the analyst exclusion last: the blanket grants above must not be
-- read as reopening truth. DR-014 is the one boundary that cannot drift.
REVOKE ALL ON SCHEMA truth FROM analyst;
REVOKE ALL ON ALL TABLES IN SCHEMA truth FROM analyst;
