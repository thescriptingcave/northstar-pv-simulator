# 38 - Database Verification

The "written but never executed" items were blocked on the belief that no database was available. **That was only half true.** PostgreSQL 16 is in the whitelisted Ubuntu archive; only the TimescaleDB and Grafana repositories are unreachable.

Installing a real server converted most of that category from unverified to verified, and **found a defect that would have stopped a load on the first attempt**.

---

## 1. What Was Actually Blocked

| Repository | Reachable | Consequence |
|---|---|---|
| `archive.ubuntu.com` (PostgreSQL 16) | **Yes** | Schemas, roles, grants, table DDL, data loading, SQL reconciliation all testable |
| `packagecloud.io/timescale` | No, HTTP 403 | `create_hypertable`, `time_bucket`, continuous aggregates, compression |
| `apt.grafana.com` | No, HTTP 403 | Dashboard rendering |

The blocker was narrower than assumed. Assuming it was total left real verification on the table for several phases.

---

## 2. The Defect: `simulator` Could Not Write

`db/init/01_schemas.sql` granted the `simulator` role `USAGE` on every schema and **no table privileges whatsoever**. No `ALTER DEFAULT PRIVILEGES` line existed for it.

The role that writes every table in the system could not write to any table:

```
SET ROLE simulator; SELECT ... FROM truth.inverter_truth;
ERROR:  permission denied for table inverter_truth
```

The first real load attempt would have failed. This went unnoticed because the file had only ever been **parsed**, and a missing `GRANT` is not a syntax error.

Two fixes, both from the same root cause - grants that were written but never exercised:

- `ALTER DEFAULT PRIVILEGES` for `simulator`, covering `SELECT, INSERT, UPDATE, DELETE` on all six schemas
- **Explicit grants on already-existing tables.** Default privileges apply only to tables created *afterward*, and only by the role that set them. Without this the file's correctness depended on execution order relative to the table DDL.

The analyst exclusion is then **re-asserted last**, so the blanket grants cannot be read as reopening `truth`. DR-014 is the one boundary that must not drift.

---

## 3. Role Isolation - Executed for the First Time

Doc `13 §2` specified this. It had never run.

| Case | Expected | Result |
|---|---|---|
| analyst reads telemetry | rows | 403,240 |
| **analyst reads truth** | **denied** | `permission denied for schema truth` |
| analyst writes telemetry | denied | `permission denied for table plant_telemetry` |
| validator reads truth | rows | ok |
| validator writes truth | denied | `permission denied for table inverter_truth` |
| simulator writes truth | allowed | `INSERT 0 1` (**failed before the fix**) |
| simulator writes telemetry | allowed | `INSERT 0 1` (**failed before the fix**) |

`db/tests/test_role_isolation.sql` now encodes all six as a runnable file that aborts on the first violation. It was verified to **fail** when a grant is deliberately revoked - a test that cannot fail proves nothing.

---

## 4. Schema and Load - Executed

| Step | Result |
|---|---|
| `01_schemas.sql` (minus the extension line) | 6 schemas, 3 roles, all grants apply |
| Generated table DDL | **5 tables, 5 indexes, no errors** |
| `COPY` from the Parquet export | **645,184 rows** across five streams |

The generated DDL is the file that did not exist until doc 34 - the schema previously called `create_hypertable` against tables that were never created. It now demonstrably executes.

---

## 5. Reconciliation Leg Three - Mostly Closed

Doc `15 §11` requires agreement between raw SQL, a continuous aggregate, and DuckDB over Parquet.

Daily exported energy over the same dataset:

| Engine | Total |
|---|---|
| PostgreSQL 16 over loaded tables | **6501.149361 MWh** |
| DuckDB over Parquet | **6501.149361 MWh** |

Exact agreement. **A real SQL engine reading loaded data reproduces the Parquet result**, which is the substance of the third leg. What remains is narrower than "the third leg": only that a TimescaleDB *continuous aggregate* matches the same figure.

---

## 6. Still Genuinely Blocked

| Item | Blocker |
|---|---|
| `create_hypertable` | `function create_hypertable(unknown, unknown) does not exist` |
| `time_bucket` and continuous aggregates | `function time_bucket(interval, timestamptz) does not exist` |
| Compression ratio | Needs the extension |
| Dashboard rendering | Needs Grafana |

All four fail with the same cause: the extension is not installed and its repository returns 403. Nothing about the SQL is wrong - it is the correct TimescaleDB API - but correctness there remains **unverified**.

---

## 7. What This Says About "Parsed"

Twice now, parsing has passed something that execution rejected: a schema with no `CREATE TABLE`, and grants that denied the writing role.

**Parsing establishes that a statement is well-formed. It establishes nothing about whether it does the intended thing.** Where execution is possible at all, it is worth the effort of arranging - and it was possible here for longer than assumed.

---

## 8. Downstream Document Updates Required

- `13 §2`: role isolation is implemented as a runnable test; note the simulator grant requirement
- `15 §11`: leg three is verified against PostgreSQL; only the continuous-aggregate variant is outstanding
