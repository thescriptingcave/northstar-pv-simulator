# 31 - Storage Record (Phase 11)

## Result: PASSED

```
  [PASS] both_trees_written           276,544 analyst rows, 185,830 truth rows
  [PASS] truth_tree_separable         5 analyst streams, no truth stream present
  [PASS] analyst_tree_is_measured     measured 1137.05 vs truth 1130.79 kW
  [PASS] ddl_parses                   every generated statement parses as PostgreSQL
  [PASS] aggregates_reconcile         172,840 raw rows -> 34,600 buckets,
                                      max relative error 4.14e-16
  [PASS] annual_size_within_budget    20.4 MB for 3.0 days -> 2.48 GB per year
  [PASS] hive_partitioning_by_date    partitioned by run, stream and date
```

---

## 1. What Was Verified and What Was Not

**No PostgreSQL server is available in the build environment.** Stating that plainly matters more than working around it.

| Path | Status |
|---|---|
| Parquet export | Verified end to end |
| DuckDB over Parquet | Verified end to end |
| Aggregate reconciliation | Verified, pandas against DuckDB |
| TimescaleDB DDL | **Parsed, not executed** |
| Continuous aggregate refresh | **Not verified** |
| Compression ratio | **Not measured** |

The DDL is validated with `sqlglot` against the PostgreSQL dialect, which distinguishes syntactically valid SQL from merely plausible-looking SQL - the usual failure mode of hand-written schema. It does not confirm that TimescaleDB accepts the hypertable and continuous-aggregate calls, which requires the extension loaded.

**First task when `make db-up` runs:** execute `db/init/02_hypertables.sql` and confirm the aggregates refresh. Doc `15 §11` wants a *three-way* reconciliation - raw SQL, continuous aggregate, DuckDB. Two legs are done. The third is the outstanding item of the phase.

---

## 2. Two Storage Paths, Deliberately

TimescaleDB gives hypertables, `time_bucket` and continuous aggregates. DuckDB over Parquet gives portable SQL with no containers running.

Every SQL exercise should run against both. Doing each twice teaches the difference between time-series-specific features and standard SQL - and it keeps the dataset usable with nothing installed.

---

## 3. Truth Separation Outside the Database

Inside PostgreSQL, schemas and roles enforce the truth boundary and an automated test confirms `analyst` is denied on `truth`. **Outside the database, only directory separation is available.**

The export therefore writes two independent trees:

```
datasets/dev/
  analyst/run_id=.../stream=inverter_telemetry/date=.../part-0.parquet
  truth/  run_id=.../stream=inverter_truth/     date=.../part-0.parquet
```

Handing over the analyst tree and keeping the truth tree is what makes blind analysis possible with no server running. The gate asserts no truth stream appears in the analyst tree.

**The subtler check:** the analyst tree must carry *measured* telemetry, not truth. Exporting the truth frames there would silently defeat the entire sensor and defect layer - the numbers would simply be too clean, with no error anywhere. Verified directly: 1137.05 kW measured against 1130.79 kW truth for the same inverter over the same window.

---

## 4. Reconciliation

172,840 raw 1-minute rows aggregate to 34,600 five-minute buckets. Two independent paths - pandas `resample` in memory, DuckDB `time_bucket` over Parquet - agree to **4.14e-16**, with zero energy error.

The row-count ratio is asserted to sit between 4.5 and 5.5, which catches a bucket-width or timezone error that a value comparison alone would miss.

---

## 5. Generated Rather Than Hand-Written Schema

`generate_ddl` emits hypertable creation, chunk intervals, compression policies and continuous aggregates from the same constants the rest of the package uses.

This matters because hand-maintained DDL drifts. The chunk interval for combiner telemetry is wider than for inverter telemetry - it is the largest stream, and a 7-day chunk would multiply chunk count for no benefit. That decision lives in one place and the schema follows it.

Hierarchical continuous aggregates are used deliberately: `inverter_hourly` is built on `inverter_5min`, and `plant_daily` on `plant_hourly`. Aggregate-on-aggregate is a genuine performance win and a specific Timescale feature worth exercising rather than stumbling into.

---

## 6. Size

| | |
|---|---|
| 3 simulated days | 20.4 MB, 462,374 rows |
| Bytes per row | 44 |
| Projected per year | **2.48 GB** |
| Budget (`14 §4.1`) | 25 GB |

Comfortably inside budget, and this is *before* TimescaleDB columnar compression. The projection excludes combiner telemetry, which `03 §7` puts at 57% of row volume; including it lands nearer 6 GB, still well inside.

---

## 7. Round-Trip Integrity

Two properties are asserted after the Parquet round trip, because both fail silently:

- **Timestamps stay timezone-aware UTC.** Coercion to naive or to local time shifts every series and is undetectable without a reference.
- **Asset identifiers stay strings.** Longitudinal analysis joins on them; numeric coercion of `NORTHSTA-BLK01-INV1` would not merely change the type, it would fail the join.

---

## 8. Downstream Document Updates Required

- `13 §9`: chunk intervals and compression policies are generated, not hand-written
- `13 §13`: the two-tree export layout, and that truth separation outside the database is directory-based
- `15 §11`: record that two of three reconciliation legs are verified; the TimescaleDB leg is outstanding
- `14 §4.1`: replace the projected storage figure with the measured 2.48 GB per year
