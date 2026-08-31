# 34 - Grafana Dashboards Record (Phase 12c)

Three generated dashboards, plus a correction to the TimescaleDB schema that the dashboards exposed.

**Status: NOT VERIFIED.** No dashboard here has been rendered against a live datasource. They are structurally valid Grafana JSON whose panel queries parse as PostgreSQL - the same caveat the DDL carries, and it is stated up front rather than buried.

---

## 1. The Schema Was Broken, and Parsing Hid It

Writing dashboards meant asking what tables they query. That question exposed a real defect in Phase 11's output.

`generate_ddl` emitted **zero `CREATE TABLE` statements**. It produced `create_hypertable('telemetry.inverter_telemetry', ...)` against tables that were never created, and would have failed on its first statement against a live server.

It passed validation because `SELECT create_hypertable(...)` is syntactically just a function call, and `sqlglot` has no way to know the table does not exist.

**Parsing validates syntax, not sense.** That is the honest limit of the "parsed, not executed" status, and this is what it was concealing.

### The fix

`generate_table_ddl` now derives table definitions **from the actual exported frames** - column names and types read from the data rather than declared by hand, so the schema cannot drift from what the simulator writes.

```
CREATE TABLE IF NOT EXISTS telemetry.block_telemetry (
    "time" TIMESTAMPTZ,
    "asset_id" TEXT,
    "dc_power_kw" DOUBLE PRECISION,
    ...
);
CREATE INDEX IF NOT EXISTS block_telemetry_asset_time_idx
    ON telemetry.block_telemetry (asset_id, time DESC);
```

Two details that matter:

**Timestamps are `TIMESTAMPTZ`.** A naive column drops the offset and shifts every series by the server's timezone, silently.

**Uniqueness is an index, not a primary key.** Hypertables require any unique index to include the partitioning column, and a plain index keeps the duplicate-detection exercise in `13 §11` possible in staging.

Ordering is now explicit in the emitted file and in the command: tables first, then hypertables. `northstar-sim ddl` without `--dataset` prints a warning saying so rather than emitting a schema that cannot apply.

---

## 2. Dashboards Are Generated, Not Hand-Edited

Hand-maintained dashboard JSON drifts from the schema silently. A renamed column leaves a panel showing an **empty graph rather than an error**, and nobody notices until someone needs the number.

Generating them from the same constants the rest of the package uses means a schema change breaks the generator, which is loud.

| Dashboard | Answers |
|---|---|
| `northstar-overview` | Export, energy, the loss chain, curtailment |
| `northstar-inverters` | Peer comparison, states, thermal behaviour |
| `northstar-data-quality` | Completeness, missingness, station disagreement |

Every panel carries a **description**. A panel without one is unreadable to anyone who did not build it, and the validator rejects it.

The descriptions carry the findings from earlier phases rather than restating the query:

- Grid export: *"Goes negative overnight: forty inverters at standby plus ten transformers at no-load. That is station service, not a fault."*
- Curtailed power: *"Curtailment at high irradiance with no fault code is economic, not equipment. Join the price series before dispatching anyone."*
- Normalised output: *"Normalising by plant-average irradiance instead manufactures underperformers out of the cloud field."*
- Station disagreement: *"Spatial disagreement moves with the wind; calibration disagreement persists."*

---

## 3. What Is Checked

| Check | Verified |
|---|---|
| JSON is structurally valid | Yes |
| Panel SQL parses as PostgreSQL | Yes |
| Panels do not overlap in the grid | Yes |
| Panels fit the 24-column grid | Yes |
| Dashboard UIDs unique and stable | Yes |
| Time series panels alias a `time` column | Yes |
| Every panel has a description | Yes |
| **A panel renders** | **No** |
| **A query returns the expected rows** | **No** |
| **The datasource uid resolves** | **No** |

Grafana macros such as `$__timeFilter` are substituted server-side and are not valid SQL alone, so they are stripped before parsing. That is a real gap in the check: a malformed macro would pass.

---

## 4. Remaining, and Now Coupled

Three items remain and they close together in one session:

1. Apply `db/init/02_tables.sql` then `03_hypertables.sql` against a live TimescaleDB
2. Confirm the continuous aggregates create and refresh - the third leg of the reconciliation in `15 §11`
3. Provision the dashboards and confirm each panel returns data

Until then the storage and dashboard layers share one status: **written, parsed, unrun**.

---

## 5. Downstream Document Updates Required

- `13 §9`: table definitions are generated from exported data; ordering is tables then hypertables
- `15 §11`: parsing is not execution - record what the DDL check does and does not establish
- `02 §13`: dashboards are generated from package constants, not authored
