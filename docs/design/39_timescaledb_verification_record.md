# 39 - TimescaleDB Verification

The TimescaleDB schema had been "written but never executed" for eight phases, on the stated grounds that the extension's apt repository returns 403.

**That was a real blocker for one installation route, and I treated it as a blocker full stop.** TimescaleDB is open source C and GitHub is reachable. Building it from source took twenty minutes and turned the entire storage layer from unverified into verified - finding **three defects that would each have stopped a real deployment**.

---

## 1. What Actually Ran

```
  01_schemas.sql       OK      6 schemas, 3 roles, all grants
  02_tables.sql        OK      5 tables, 5 indexes
  03_hypertables.sql   OK      5 hypertables, 6 continuous aggregates,
                               5 compression policies
```

Loaded **645,184 rows**, refreshed every aggregate, compressed chunks.

TimescaleDB 2.17.2 built against PostgreSQL 16.15. The first build used
`-DAPACHE_ONLY=1`, which excludes compression and continuous aggregates - they
are Community-licensed. Rebuilding without that flag was required.

---

## 2. Three Defects Parsing Could Not Catch

### 2.1 Compression segmented by a column that does not exist

```
ERROR:  column "asset_id" does not exist
HINT:   The timescaledb.compress_segmentby option must reference a valid column.
```

The generator emitted `compress_segmentby = 'asset_id'` for **every** stream.
`plant_telemetry` is plant-level - one row per timestamp - and has no such
column. The SQL is well-formed; it simply refers to a column that is not there.

The generator now takes the exported schemas and shapes each statement to the
columns the stream actually has.

### 2.2 A continuous aggregate that grouped by the wrong column

```
ERROR:  continuous aggregate view must include a valid time bucket function
```

In a hierarchical aggregate the output alias `bucket` collides with the source
column of the same name, and PostgreSQL resolves `GROUP BY bucket` to the
**source column** rather than the `time_bucket()` output. TimescaleDB then sees
no bucketing function and rejects the view.

Grouping by ordinal position is unambiguous.

### 2.3 A hierarchical aggregate assuming its parent's shape

```
ERROR:  column "asset_id" does not exist
```

Every hierarchical aggregate was assumed to carry `asset_id`. `plant_hourly`
reads `plant_5min`, which is plant-level and does not. Shape is now **inherited
from the parent aggregate** rather than assumed.

---

## 3. Three-Way Reconciliation - Closed

Doc `15 §11` requires agreement between raw SQL, a continuous aggregate, and
DuckDB over Parquet. Two legs were verified previously; the third was
outstanding.

| Leg | Exported energy |
|---|---|
| Raw SQL over the hypertable | 6501.149361 MWh |
| Continuous aggregate `plant_5min` | 6501.149361 MWh |
| DuckDB over Parquet | 6501.149361 MWh |

**Maximum pairwise difference: 9.9e-8 MWh.** The hierarchical `plant_hourly`
agrees with `plant_5min` to 0.000000.

An aggregate that disagrees with its source is worse than no aggregate: it is
fast and wrong, and nothing downstream notices. `db/tests/test_reconciliation.sql`
now enforces both comparisons.

---

## 4. Compression - Measured

| | |
|---|---|
| Before | 151 MB |
| After | 27 MB |
| **Ratio** | **5.48x** |

Measured on `inverter_telemetry` chunks, segmented by `asset_id` and ordered by
`time DESC`. This was previously listed as unmeasured.

Applied to the 2.48 GB per simulated year Parquet figure, compressed database
storage lands near 450 MB per year.

---

## 5. Role Isolation - Re-Verified

`db/tests/test_role_isolation.sql` passes 6/6 on the TimescaleDB instance,
including the `simulator` grant defect found in doc 38. The two database tests
now run together via `make db-test`.

---

## 6. What Remains Unverified

**Grafana dashboard rendering.** `apt.grafana.com` returns 403 and Grafana is
Go, not a twenty-minute build against an existing server. The panel SQL is
generated from the same constants as the schema and parses as PostgreSQL, but
no panel has been rendered.

That is now the **only** item in the "written but never executed" category.

---

## 7. The Lesson, Stated Plainly

Three times in this project, parsing passed something execution rejected: a
schema with no `CREATE TABLE`, grants that denied the writing role, and now
three statements referencing columns that do not exist.

The pattern is not that parsing is weak - it is that **"I cannot install X"
was accepted as "X cannot be verified"** without testing the second claim. One
apt repository was blocked. The source repository was not.

Before recording something as blocked, the blocker itself is worth verifying.

---

## 8. Downstream Document Updates Required

- `13 §9`: DDL generation is schema-aware; compression and aggregate shape follow the stream
- `14 §11`: record the measured 5.48x compression ratio
- `15 §11`: three-way reconciliation is closed, agreement to 1e-7 MWh
