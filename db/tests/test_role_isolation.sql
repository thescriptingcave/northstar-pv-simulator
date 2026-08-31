-- Role isolation test - DR-014 and doc 13 section 2.
--
-- Run against a database with 01_schemas.sql and the table DDL applied:
--     psql -d northstar -v ON_ERROR_STOP=1 -f db/tests/test_role_isolation.sql
--
-- Exits non-zero on the first violation. Every case below has been executed
-- against PostgreSQL 16; the simulator write cases failed the first time,
-- because the file granted USAGE on the schemas and no table privileges at all.

\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS truth.role_isolation_probe (
    time timestamptz, asset_id text, value double precision);

DO $$
DECLARE
    denied boolean;
BEGIN
    -- 1. The analyst must NOT reach truth. This is the boundary that makes
    --    blind analysis meaningful; everything else is a convenience.
    BEGIN
        SET LOCAL ROLE analyst;
        PERFORM count(*) FROM truth.role_isolation_probe;
        denied := false;
    EXCEPTION WHEN insufficient_privilege THEN
        denied := true;
    END;
    RESET ROLE;
    IF NOT denied THEN
        RAISE EXCEPTION 'DR-014 VIOLATED: analyst can read the truth schema';
    END IF;

    -- 2. The analyst must be able to read telemetry, or the dataset is unusable.
    BEGIN
        SET LOCAL ROLE analyst;
        PERFORM count(*) FROM telemetry.plant_telemetry;
        denied := false;
    EXCEPTION WHEN insufficient_privilege THEN
        denied := true;
    END;
    RESET ROLE;
    IF denied THEN
        RAISE EXCEPTION 'analyst cannot read telemetry';
    END IF;

    -- 3. The analyst is read-only. A mutable dataset is not reproducible.
    BEGIN
        SET LOCAL ROLE analyst;
        DELETE FROM telemetry.plant_telemetry WHERE false;
        denied := false;
    EXCEPTION WHEN insufficient_privilege THEN
        denied := true;
    END;
    RESET ROLE;
    IF NOT denied THEN
        RAISE EXCEPTION 'analyst can write to telemetry';
    END IF;

    -- 4. The validator reads truth - it scores analyst work against it.
    BEGIN
        SET LOCAL ROLE validator;
        PERFORM count(*) FROM truth.role_isolation_probe;
        denied := false;
    EXCEPTION WHEN insufficient_privilege THEN
        denied := true;
    END;
    RESET ROLE;
    IF denied THEN
        RAISE EXCEPTION 'validator cannot read truth';
    END IF;

    -- 5. The validator does not write. Only the simulator produces data.
    BEGIN
        SET LOCAL ROLE validator;
        INSERT INTO truth.role_isolation_probe VALUES (now(), 'x', 1.0);
        denied := false;
    EXCEPTION WHEN insufficient_privilege THEN
        denied := true;
    END;
    RESET ROLE;
    IF NOT denied THEN
        RAISE EXCEPTION 'validator can write to truth';
    END IF;

    -- 6. The simulator writes truth. This failed on first execution: the file
    --    granted schema USAGE but no table privileges.
    BEGIN
        SET LOCAL ROLE simulator;
        INSERT INTO truth.role_isolation_probe VALUES (now(), 'x', 1.0);
        denied := false;
    EXCEPTION WHEN insufficient_privilege THEN
        denied := true;
    END;
    RESET ROLE;
    IF denied THEN
        RAISE EXCEPTION 'simulator cannot write truth - grants are incomplete';
    END IF;

    RAISE NOTICE 'role isolation: 6/6 checks passed';
END $$;

DROP TABLE truth.role_isolation_probe;
