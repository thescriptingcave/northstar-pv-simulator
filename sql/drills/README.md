# SQL Drills

Interview-shaped SQL practice on real plant data. Every query runs unchanged in
**DuckDB** and **PostgreSQL** — verified, 17/17 in both.

These are about **technique**, not the domain. `sql/learning/` is the PV track;
this is the SQL one. Doing both on the same tables means you are never
context-switching between a toy schema and the real thing.

```bash
# Run a whole file and see every result
make dev-dataset
make drill FILE=sql/drills/01_window_functions.sql

# Or interactively
duckdb

# TablePlus / psql
SET search_path TO telemetry, public;
```

---

## The four drills

**`01_window_functions.sql`** — the most-tested area, and the one most people
half-know.

- `ROW_NUMBER` / `RANK` / `DENSE_RANK` — know the tie behaviour cold
- Top-N per group — window in a subquery, filter outside, and **why** you
  cannot filter a window function in `WHERE`
- Running totals and moving averages — `ROWS` against `RANGE`
- `LAG` / `LEAD` and period-over-period change
- **The `LAST_VALUE` trap** — the default frame ends at the current row, so it
  returns the current value. Asked often.

**`02_gaps_and_islands.sql`** — consecutive runs. "Longest login streak",
"sessions from an event log", "consecutive days above target".

- The difference-of-row-numbers idiom
- The running-sum-of-a-flag idiom, which generalises better
- Finding the gaps rather than the islands
- Islands with a tolerance, so one noisy sample does not split a real event

**`03_joins_and_dedup.sql`** — where interviews are actually lost.

- Self-join for pairwise comparison, and why `a.id < b.id` beats `!=`
- Deduplication with `ROW_NUMBER`, and why `DISTINCT` is not enough
- **NULL semantics** — `NULL != 100` is `NULL`, not true
- Anti-joins, and why `NOT IN` with a NULL returns nothing at all
- **The fan-out trap** — join-then-aggregate inflates sums, and still returns a
  plausible number

**`04_aggregation_and_pivot.sql`** — reshaping without a spreadsheet.

- Conditional aggregation with `FILTER` — portable, beats dialect `PIVOT`
- `WHERE` against `HAVING`, in one query where swapping them changes the answer
- Percentiles and why the mean hides skew
- `UNPIVOT` portably, via `LATERAL` over `VALUES`
- `GROUPING SETS` — several aggregation levels in one pass

---

## How to practise

Read the question, **write the query yourself**, then compare. Reading a
solution builds recognition, not recall, and interviews test recall under
pressure.

For each one, be able to say out loud:

- why that window frame and not the default
- what happens to the NULLs
- what the row count should be before you run it

That last one catches fan-out before it embarrasses you. If a join changes your
row count and you did not expect it, stop.

---

## Where the harder material is

| | |
|---|---|
| `sql/exercises/` | 14 graded exercises across seven tiers, both dialects |
| `sql/timeseries/` | TimescaleDB: `time_bucket`, gapfill, advection lag |
| `sql/learning/` | Six-week PV analytics track |

`sql/timeseries/02_gaps.sql` is worth doing straight after drill 2: it shows
`locf` failing to fill a gap that `gapfill` never created, which is a subtler
version of the same problem.
