# 13 - Time-Series Data Model

**Version 2.0** - supersedes v1.0. Adds market and financial tables, role-based truth separation, concrete partitioning and retention. Change log in §14.

## 1. Purpose

Define the conceptual storage model. Exact DDL is still deferred to implementation, but table sets, grains, keys, and access boundaries are now locked.

## 2. Schemas and Roles - LOCKED (DR-014)

Truth separation is enforced by PostgreSQL schema and role, not by naming convention. A convention leaks; an analyst uses a truth column by accident and the blind-analysis criterion in `01 §8` becomes unverifiable.

| Schema | Contents | Roles with SELECT |
|---|---|---|
| `plant` | Configuration and dimension tables | `analyst`, `validator`, `simulator` |
| `telemetry` | Measured and derived time series | `analyst`, `validator`, `simulator` |
| `ops` | Events, alarms, maintenance | `analyst`, `validator`, `simulator` |
| `market` | Prices, settlement | `analyst`, `validator`, `simulator` |
| `truth` | Physical truth and injected ground truth | `validator`, `simulator` only |
| `meta` | Run metadata, cache manifests, validation reports | all |

The `analyst` role is the default connection for exercises and Grafana. `validator` is used only for acceptance testing and answer-checking.

## 3. Configuration and Dimension Tables (`plant`)

| Table | Notes |
|---|---|
| `sites` | Location, timezone, capacity, albedo |
| `power_blocks` | Block ID, site, position (x, y), capacity |
| `pv_arrays` | Orientation, GCR, module type |
| `tracker_row_blocks` | **New.** Row-block ID, parent array, axis azimuth, rotation limits |
| `string_groups` | Module count, rated DC, parent combiner |
| `combiners` | Channel count, rated current, parent inverter |
| `inverters` | Ratings, efficiency curve params, thresholds, block |
| `transformers` | Ratings, thermal params, block |
| `weather_stations` | Position (x, y), sensor complement |
| `revenue_meters` | |
| `breakers` | |
| `plant_controller` | Export limit, curtailment config |
| `module_types` | CEC database key, electrical params, degradation |
| `inverter_types` | Sandia model params |
| `asset_positions` | **New.** (x, y) in site coordinates for every telemetry-bearing asset |

`asset_positions` is required by the spatial cloud field in `06 §5.4`. Without it, cloud advection has no geometry to advect across.

Every asset carries a stable unique ID, asset type, parent ID, commissioning timestamp, rated capacity, enabled flag, and configuration version, per `05 §11`.

## 4. Time-Series Fact Tables (`telemetry`)

| Table | Grain | Assets | Hypertable |
|---|---|---|---|
| `weather_telemetry` | 1 min | 3 | Yes |
| `inverter_telemetry` | 1 min | 40 | Yes |
| `combiner_telemetry` | 5 min | 480 | Yes |
| `tracker_telemetry` | 5 min | 40 | Yes |
| `transformer_telemetry` | 1 min | 10 | Yes |
| `block_telemetry` | 1 min | 10 | Yes |
| `plant_telemetry` | 1 min | 1 | Yes |
| `meter_telemetry` | 1 min | 1 | Yes |

`tracker_telemetry` is new in v2.0 per DR-004.

## 5. Truth Tables (`truth`)

| Table | Grain | Contents |
|---|---|---|
| `environmental_truth` | 1 min | Undistorted irradiance, temperature, wind, soiling ratio, cloud field state |
| `production_truth` | 1 min | Unconstrained available power per asset, per-stage loss quantities |
| `loss_attribution` | 15 min | Cause-coded lost energy and lost revenue per `18 §5` |
| `scenario_instances` | Event | Which scenario, which assets, when, intended severity |
| `sensor_state` | 1 min | Per-sensor bias, drift, fault status |

Mirroring the telemetry tables in a separate schema doubles some storage. That is the intended trade: it is the only way to make the truth/measurement boundary enforceable rather than aspirational.

## 6. Operational Tables (`ops`)

| Table | Notes |
|---|---|
| `events` | Per `12 §2` |
| `alarms` | Lifecycle per `12 §4` |
| `maintenance` | Planned and unplanned windows |
| `om_costs` | **New.** Event-joined cost records per `18 §6.2` |

## 7. Market and Financial Tables (`market`) - NEW in v2.0

| Table | Grain | Notes |
|---|---|---|
| `prices` | 15 min | RT SPP, DA SPP, RT LMP by settlement point. Signed |
| `settlement` | 15 min | Energy revenue, hedge settlement, PTC, basis, gross margin |
| `kpi_daily` | Daily | PR, PR_corr, availability (4 variants), capacity factor, yields, losses, capture rate, data quality |
| `guarantee_ledger` | Daily / annual | Running position against availability and PR guarantees |

`kpi_daily` is the single most-queried table in the analytics curriculum and should be materialized rather than computed on demand.

## 8. Metadata Tables (`meta`)

| Table | Notes |
|---|---|
| `simulation_runs` | Run ID, config version, cache version, seed, simulator version, start/end, record counts, status |
| `cache_manifests` | Copy of the `19 §5.2` manifest for each run |
| `validation_reports` | Per `15 §11` |
| `config_versions` | Full configuration snapshot per run |

The reproducibility key `(cache_version, config_version, seed, simulator_version)` is recorded here and is what makes a dataset regenerable.

## 9. TimescaleDB Design

### 9.1 Hypertables

All telemetry, truth time-series, price, and settlement tables are hypertables partitioned on `time`.

A **hypertable** presents a normal PostgreSQL table interface while partitioning time-series data into underlying chunks.

| Table class | Chunk interval | Rationale |
|---|---|---|
| 1-minute telemetry | 7 days | ~400k rows per chunk for inverters |
| 5-minute telemetry | 14 days | Combiners are the largest stream |
| 15-minute market | 90 days | Low volume |
| Truth tables | Match their telemetry counterpart | Consistent join performance |

Chunk interval targets roughly 25% of available memory per chunk under the Timescale guidance; tune after the Phase 8 volume test rather than guessing precisely now.

### 9.2 Continuous Aggregates

A **continuous aggregate** is a Timescale-managed materialized aggregate maintained incrementally. It should be used where it improves repeated analytical queries, not because the feature exists.

| Aggregate | Source | Grain | Justification |
|---|---|---|---|
| `inverter_5min` | `inverter_telemetry` | 5 min | Peer comparison; efficiency rolling windows |
| `inverter_hourly` | `inverter_5min` | 1 h | Hierarchical rollup - aggregate on aggregate |
| `plant_5min` | `plant_telemetry` | 5 min | Dashboard default |
| `plant_hourly` | `plant_5min` | 1 h | Energy reporting |
| `plant_daily` | `plant_hourly` | 1 day | Feeds `kpi_daily` |
| `weather_hourly` | `weather_telemetry` | 1 h | Resource analysis; TMY comparison |
| `settlement_hourly` | `settlement` | 1 h | Revenue reporting |

Hierarchical continuous aggregates (aggregate built on aggregate) are used deliberately for `inverter_hourly` and `plant_hourly` because they are both a real performance win and a specific Timescale feature worth learning.

**Not** aggregated: combiner telemetry. It is the largest stream but is queried episodically for imbalance investigation, not continuously. Aggregating it would cost storage and refresh cycles for little benefit - and deciding *not* to aggregate something is itself part of the lesson.

### 9.3 Compression

Enable columnar compression on chunks older than 30 days, segmented by `asset_id` and ordered by `time DESC`. Expected compression ratio for this data shape is substantial; measure it and record the result, since the before/after comparison is a curriculum item.

### 9.4 Retention

| Data | Raw retention | Aggregate retention |
|---|---|---|
| Telemetry | Full dataset duration | Full |
| Truth | Full | n/a |
| Market | Full | Full |

Retention policies are demonstrated on a scratch copy rather than applied to canonical datasets - deleting the raw data of a 3-year dataset to prove a policy works is a poor trade. The policy mechanics are the learning objective, not the deletion.

## 10. Time Grain

| Layer | Grain |
|---|---|
| Source resource data | 5 min |
| Simulation step | 1 min |
| Raw telemetry, major assets | 1 min |
| Raw telemetry, combiner and tracker | 5 min |
| Settlement and prices | 15 min |
| Aggregates | 5 min, 15 min, hourly, daily |

## 11. Keys and Uniqueness

Telemetry uniqueness: `(run_id, asset_id, time)`.

`run_id` is mandatory in the key, not optional, so multiple simulation runs can coexist in one database. This is required for the A/B comparison enabled by the named seed substreams in DR-013 - two runs differing only in fault schedule must be queryable side by side.

**Exception:** the duplicate-record data-quality scenario (SCN-065) deliberately violates uniqueness. It is therefore injected into a staging table without the constraint, and the exercise is to find and resolve duplicates before loading. Enforcing uniqueness at the destination while allowing it to be violated at the source is exactly how real ingestion pipelines work.

## 12. UTC and Local Time

Storage is `timestamptz`, UTC, interval-beginning. Plant timezone (`America/Chicago`) is configuration metadata in `plant.sites`.

Local solar-day analysis converts at query time. Because West Texas observes DST, local day boundaries shift twice a year - producing one 23-hour and one 25-hour local day. Daily energy aggregation must handle both correctly, which is a deliberate and non-trivial exercise.

## 13. Parquet Portability

Export layout, hive-partitioned:

```
parquet/
  run_id=<uuid>/
    stream=inverter_telemetry/
      date=2023-06-14/part-0.parquet
    stream=plant_telemetry/
      date=2023-06-14/part-0.parquet
    stream=weather_telemetry/
      ...
    dimension=inverters/part-0.parquet
    dimension=asset_positions/part-0.parquet
```

Requirements:

- timestamps preserved as `timestamp[us, tz=UTC]`
- numeric types preserved; no float-to-string coercion
- asset IDs preserved as strings
- one file per stream per day for 1-minute streams; per week for 5-minute streams
- dimension tables exported unpartitioned
- truth tables exported to a **separate directory** that can be withheld, preserving blind analysis outside the database

DuckDB reads this layout directly. Every SQL exercise should be runnable both ways per DR-011 - once with `time_bucket` and continuous aggregates, once with portable window functions.

## 14. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Truth separation | "stored separately" | Six schemas with explicit role grants |
| Table list | "potential entities" | Locked table set with grains |
| Tracker tables | Absent | `tracker_row_blocks`, `tracker_telemetry` |
| Asset positions | Absent | Required for spatial cloud field |
| Market and financial | Absent | `prices`, `settlement`, `kpi_daily`, `guarantee_ledger`, `om_costs` |
| Chunk intervals | Unspecified | Per table class |
| Continuous aggregates | "potential" list | Locked set, with a documented decision *not* to aggregate combiners |
| Hierarchical CAs | Not mentioned | Used deliberately |
| Compression | "where supported" | 30-day policy, segment and order specified |
| Retention | "should demonstrate" | Demonstrated on scratch copies, with rationale |
| Keys | "plus run ID if multiple runs" | `run_id` mandatory |
| Duplicate scenario | Not reconciled with uniqueness | Staging-table approach |
| DST | Not addressed | Explicit 23/25-hour local day requirement |
| Parquet layout | "may include" | Concrete layout with truth directory separated |


## 15. Schema Generation - as implemented

### 15.1 Table Definitions Are Generated

`northstar_sim.storage.generate_table_ddl` derives table definitions **from the
actual exported frames** - column names and types read from the data - so the
schema cannot drift from what the simulator writes.

This exists because the first schema generator emitted **zero `CREATE TABLE`
statements**. It produced `create_hypertable('telemetry.inverter_telemetry', ...)`
against tables that were never created, and would have failed on its first
statement against a live server. It passed validation because
`SELECT create_hypertable(...)` is syntactically just a function call.

**Ordering is normative: tables first, then hypertables.** `create_hypertable`
requires the table to exist.

Two details:

- **Timestamps are `TIMESTAMPTZ`.** A naive column drops the offset and shifts
  every series by the server timezone, silently.
- **Uniqueness is an index, not a primary key.** Hypertables require any unique
  index to include the partitioning column, and a plain index keeps the
  duplicate-detection exercise in §11 possible in staging.

Chunk intervals and compression policies are likewise generated from the
constants the rest of the package uses, not hand-maintained.

### 15.2 Truth Schema Additions

| Table | Contents |
|---|---|
| `sensor_fleet` | Per-instrument bias, drift, response and soiling - ground truth about instrument error |
| `defect_schedule` | Injected data-quality defects, with a `flagged` column |
| `scenario_instances` | Injected faults, with cause codes and durations |

The sensor fleet and defect schedule are what make an analyst's calibration and
defect detection **scoreable** rather than merely plausible.

### 15.3 Two-Tree Parquet Export (§13)

Inside PostgreSQL, schemas and roles enforce the truth boundary. **Outside it,
only directory separation is available**, so the export writes two independent
trees:

```
datasets/dev/
  analyst/run_id=.../stream=inverter_telemetry/date=.../part-0.parquet
  truth/  run_id=.../stream=inverter_truth/     date=.../part-0.parquet
```

Handing over the analyst tree and keeping the truth tree is what makes blind
analysis possible with no server running.

**The analyst tree must carry measured telemetry**, not truth. Exporting truth
frames there would silently defeat the sensor and defect layers - the numbers
would simply be too clean, with no error anywhere.

Measured: 462,374 rows for 3 days, 44 bytes per row, **2.48 GB per simulated
year** before columnar compression.
