# 41 - Usability and Packaging

Eight defects, every one surfaced by someone running the project from a clean
checkout rather than from the working copy it was built in.

None were found by the test suite. All eight were things that had been worked
around by hand in the development environment without noticing the workaround
had happened.

---

## 1. The Pattern

| Defect | How it presented |
|---|---|
| No Parquet loader existed | `make db-up` created the schema; every table empty; Grafana blank |
| No Grafana datasource provisioned | Panel uid resolved to nothing; blank with no error logged |
| Dashboard window fixed at `now-7d` | Dataset is historical; blank even when loaded |
| `provider.yaml` contained JSON | Accepted only because YAML is a JSON superset |
| Datasource credentials used `${VAR:-default}` | Grafana has no bash default expansion; authenticated with the literal string |
| `psycopg2` never declared | `make db-load` failed on import after five minutes of dataset generation |
| Jupyter never declared | `uv run jupyter notebook` - "Failed to spawn: jupyter" |
| Notebooks assumed the working directory | `jupyter execute` runs from `notebooks/`; dataset resolved one level too deep |

The credential one is the worst of the set: the datasource **appears** in the
Grafana UI, so it looks provisioned. Typing the credentials in by hand fixes it
until the next restart, when provisioning overwrites the edit.

---

## 2. Fixes

**`northstar-sim load`** moves an exported dataset into TimescaleDB with `COPY`,
then refreshes every continuous aggregate parents-first. A hierarchical
aggregate reads another aggregate, so refreshing in the wrong order leaves the
rollups empty even with the raw data present.

**`write_datasource`** generates the provisioning YAML from the same constant
the panels reference, using Grafana's `$VAR` interpolation. Compose supplies
the values with its own defaults, where `:-` does work.

**Dashboards take a time window**, defaulted from the dataset's actual span.

**`find_dataset`** walks up from the working directory. `jupyter execute` uses
the notebook's directory, JupyterLab uses wherever it was launched, and a
relative path is correct in exactly one of those.

**`db/diagnose.sh`** checks all five causes of a blank panel and prints the
**actual** error. Its first version redirected stderr to `/dev/null` and
reported "permission denied" with no indication of what was denied, to whom, or
by which server - a diagnostic that hid the diagnosis.

---

## 3. `northstar-sim generate`

Datasets were only producible as a side effect of the acceptance gates, which
fix their conditions to whatever the check requires - `curriculum-gate` runs at
50 C with faults and defects forced on. That is a fixture, not a generator, and
it meant every dataset ever examined was a June dataset.

`generate` exposes window, seed, ambient mean and diurnal amplitude, wind speed
and direction, clear-sky index, plant age, and injection toggles.

| | Energy | Cell temperature |
|---|---|---|
| Summer, clear, 38 C | 6,527.8 MWh | 24.5 to 68.1 C |
| Winter, 70% clear, 8 C | 2,396.5 MWh | -11.5 to 30.7 C |

Same seed and arguments give byte-identical output; a different seed gives a
genuinely different plant-year rather than the same one with noise added.

**Generating the first non-summer dataset immediately broke four acceptance
checks.** That is recorded in `20 §15` and `37 §7`, and it is the strongest
argument for the command existing: a generator that can only produce one season
guarantees the checks are only valid for that season.

---

## 4. Exercises Now Run on PostgreSQL

The generated exercise files carried the line "the TimescaleDB form is identical
apart from the schema prefix" instead of the query. Accurate for eleven of
fourteen, wrong for three, and useless for all of them - a reader pasting the
DuckDB form into a database client gets
`relation "inverter_telemetry" does not exist`.

Checking the claim found three genuine dialect incompatibilities:

| Exercise | DuckDB | PostgreSQL |
|---|---|---|
| EX-402 | `INTERVAL 60 MINUTE` | `INTERVAL '60 minutes'` |
| EX-601 | `UNPIVOT` | `CROSS JOIN LATERAL (VALUES ...)` |
| EX-602 | `::DOUBLE` | `::double precision` |

Every exercise now ships both dialects as runnable SQL, and
`db/tests/test_exercises_postgres.py` executes all fourteen against a live
database.

---

## 5. Time-Series Query Set

`sql/timeseries/` holds queries that **require** TimescaleDB - `time_bucket`
with timezones, `gapfill`/`locf`, continuous aggregates, advection lag. The
existing curriculum is portable SQL by design; this set is not.

Four findings from writing them, each caught by execution:

- **The UTC-day trap.** Bucketing a West Texas plant by UTC day splits the solar
  day; the symptom is a generating span of 23:59.
- **`locf` does not fill NULLs.** It fills buckets `gapfill` created. A comms
  outage writing NULL rows leaves the buckets present, so there is nothing to
  fill.
- **The steepest ramps are commercial.** Curtailed buckets ramp at 3.53 MW
  against 0.85 MW for weather.
- **Detrend before correlating.** Raw irradiance correlates at 0.998 at *every*
  offset; the diurnal cycle swamps the cloud signal. The residual peaks cleanly
  at -9 minutes.

`timescaledb_toolkit` hyperfunctions are deliberately absent - unavailable in
the environment where these were verified, and nothing here ships unexecuted.

---

## 6. Packaging

`README.md` rewritten as a GitHub landing page, `LICENSE` added (it was declared
in `pyproject.toml` and absent from disk), and a CI workflow that runs lint,
tests and all twelve gates.

CI **excludes** TimescaleDB and Grafana deliberately: those are verified
manually and their status belongs in `STATUS.md`, not behind a green badge that
implies more than it checks.

Jupyter is an optional dependency group. `psycopg2-binary` is a hard dependency
of the simulator package, because the loader imports it.

---

## 7. What Would Have Caught These Earlier

Every one of the eight was invisible from the development working copy. Two
cheap habits would have caught all of them:

- **Run from a clean extraction**, not the working directory. Four of the eight
  were missing declarations or paths that happened to resolve locally.
- **Follow the documented path exactly as written**, including the prerequisite
  steps. The loader gap was visible the moment `make db-up` was followed by
  opening Grafana rather than by a manual `COPY`.

---

## 8. Downstream Document Updates Required

None outstanding. `20 §15` and `37 §7` carry the seasonal calibration findings;
`32` and `33` carry the exercise and notebook amendments.
