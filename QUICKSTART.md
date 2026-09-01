# Quickstart

Everything below runs **offline**. No API keys, no database, no network.
Verified end to end on the shipped archive.

## Requirements

| | |
|---|---|
| Python | 3.13 or newer |
| Package manager | [`uv`](https://docs.astral.sh/uv/) |
| Disk | ~1 GB for a development dataset |
| Docker | Optional — only for TimescaleDB and Grafana |
| `psql` client | Only for `db-load`, `db-test`, `db-diagnose` |

**macOS note.** The Postgres client is not bundled with Docker Desktop, and
`make db-load` needs it on the host:

```bash
brew install libpq
brew link --force libpq
```

Without it those three targets fail on a missing binary, or find an unrelated
`psql` from another Postgres install and connect to the wrong server.

No `.env` is needed to run the simulator. `.env.example` is for the *data
acquisition* client (Phase 0.5), which is a separate, optional path.

When you do use it, `.env` is read automatically — the loader searches upward
from the working directory, so any subdirectory works. Real environment
variables take precedence over the file.

## Updating an existing checkout

**`.env` is never in the archive** — only `.env.example` — so extracting over
your directory will not touch your credentials.

What *would* lose them is deleting the directory first. Use the update script
instead:

```bash
make update ARCHIVE=~/Downloads/northstar-pv-simulator.zip
uv sync
```

It preserves `.env` and `datasets/`, and if your `config/northstar.toml`
differs from the shipped one it saves yours as `config/northstar.toml.yours`
rather than silently choosing.

Once this lives in a Git repository, use `git pull` instead — Git handles all of
this natively and `.env` is already in `.gitignore`.

## 1. Install

```bash
uv venv
uv sync
```

## 2. Confirm it works

```bash
make test              # full suite, ~5 minutes
make validate-plant    # 12 structural checks on the derived plant
make summary           # derived capacity — nothing is asserted, all derived
```

`summary` should report **17 modules per string, 124.66 MWp DC, 100.00 MW AC**.
Those numbers are *derived* from the module and inverter electrical
characteristics, not typed into a config file.

## 3. Run the physics gates

Each gate is an independent falsifiable check. They take seconds to minutes.

```bash
make physics-gate      # production chain vs pvlib ModelChain — zero error
make spatial-gate      # cloud advection lag matches geometry
make plant-gate        # 40 inverters, energy chain closes
make state-gate        # no illegal state transitions
make sensor-gate       # measurement diverges from truth only as modelled
make loss-gate         # loss waterfall closes to 7e-19
make scenario-gate     # injected faults move telemetry
make financial-gate    # settlement, curtailment economics
make dataquality-gate  # defects corrupt reporting, never truth
make storage-gate      # Parquet export and aggregate reconciliation
```

Or all of them plus the tests: `make test`.

## 4. Generate a dataset

```bash
make dev-dataset       # ~2 minutes, writes datasets/curriculum/
make accept            # acceptance report — should print ACCEPTED
```

The export has **two independent trees**:

```
datasets/curriculum/
  analyst/   <- measured telemetry: sensor error and data defects included
  truth/     <- physical truth, injected faults, defect schedule, sensor fleet
```

**Hand someone the `analyst/` tree and keep `truth/` to run a blind analysis.**
That separation is the point of the whole project.

## 4b. Generate your own datasets

`make dev-dataset` always produces the same seven days — deterministic by
design, so results are reproducible. To get *different* data, use `generate`:

```bash
uv run northstar-sim generate --out datasets/winter \
    --start "2023-12-15 06:00" --end "2023-12-22 06:00" \
    --seed 42 --temp-air 8 --temp-amplitude 11 \
    --wind-speed 6 --clearsky-index 0.7 --plant-age 5
```

| Knob | Effect |
|---|---|
| `--seed` | Different weather, faults, defects and sensor calibration together |
| `--start` / `--end` | Any window; season changes everything downstream |
| `--temp-air` / `--temp-amplitude` | Ambient mean and diurnal swing |
| `--clearsky-index` | Below 1.0 for a cloudier record |
| `--wind-speed` / `--wind-direction` | Drives cloud advection across the site |
| `--plant-age` | Years at window start; degradation accrues from there |
| `--no-faults` / `--no-defects` / `--no-curtailment` | Clean baselines |

Same seed and arguments always give byte-identical output. A different seed
gives a genuinely different plant-year, not the same one with noise added.

Two examples, both accepted by the acceptance report:

| | Energy | Cell temperature |
|---|---|---|
| Summer, clear, 38 C | 6,527.8 MWh | 24.5 to 68.1 C |
| Winter, 70% clear, 8 C | 2,396.5 MWh | -11.5 to 30.7 C |

Always run `northstar-sim accept` on a new dataset before trusting it.

## 4c. Run on real data

Once `make fetch` has populated the cache:

```bash
make real-dataset
```

This replaces both development stand-ins at once — `clearsky_resource` with
fetched NSRDB irradiance, and `synthetic_prices` with real ERCOT settlement
prices at the plant's own node.

```bash
uv run northstar-sim generate --real --year 2025 \
    --out datasets/observed --run-id observed
```

`--year` defaults to the most recent cached year. Weather and price coverage
differ — ERCOT retains about a year, NSRDB reaches back to 2018 — so a weather
year without prices runs without curtailment and says so rather than failing.

## 5. Query it — no server required

```bash
duckdb
```

```sql
SELECT date_trunc('day', time) AS day,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
FROM read_parquet('datasets/curriculum/analyst/**/*.parquet',
                  hive_partitioning => true, union_by_name => true)
WHERE grid_export_power_kw IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

14 graded SQL exercises with worked answers and explanations are in
`sql/exercises/`. Every one is executed against a real dataset in CI.

## 6. Notebooks

Jupyter is an **optional dependency group**, kept out of the default install
because the simulator, the gates and the SQL curriculum need none of it.

```bash
make notebooks                          # execute all three headless
make lab                                # open JupyterLab
uv run --group notebooks jupyter lab    # the same, directly
```

`uv run jupyter lab` without `--group notebooks` fails with
`Failed to spawn: jupyter` — the group is not installed by default.

- `01_resource_and_production` — the causal chain, and a confounding trap
- `02_expected_power_and_losses` — ASTM E2848 regression
- `03_forecasting` — target leakage and skill against persistence

## What needs nothing from you

Everything above. The simulator has no external dependencies at runtime — the
resource model has a deterministic clear-sky stand-in, and prices have a
synthetic stand-in whose calibration targets are documented in `19 §12`.

## 7. TimescaleDB and Grafana

```bash
make db-up      # starts both — schema is created but EMPTY
make db-load    # loads the data, refreshes aggregates, retargets dashboards
```

**Both steps are required.** `db-up` only runs the schema scripts. Running it
alone leaves every table empty, and Grafana then correctly shows "No data" on
every panel.

`db-load` does three things that each independently cause blank panels if
skipped:

1. loads the Parquet export via `COPY` and refreshes all six continuous
   aggregates, parents before children
2. writes `db/grafana/datasources/northstar.yaml` — without it the panels'
   datasource uid resolves to nothing
3. points the dashboards' default time window at the dataset's actual span,
   rather than `now-7d`, which shows nothing for a historical dataset

### If the panels are blank

```bash
make db-diagnose
```

It prints the **actual** error at each step, plus who you connected as, which
server answered, and whether that server even has TimescaleDB installed. If
Grafana and a GUI client connect but `db-diagnose` cannot, they are not using
the same DSN — the usual causes are a `.env` overriding the credentials, or
another PostgreSQL already listening on 5432.

Four independent causes, each of which renders every panel empty with **no
error logged anywhere**:

1. database unreachable
2. tables empty — `db-up` creates the schema only
3. datasource not provisioned — the panels' uid resolves to nothing
4. datasource credentials written in shell `${VAR:-default}` form, which
   Grafana does not expand — it authenticates with the literal string
5. dashboards pointing at `now` rather than at the data

Cause 4 is the one that looks like success: the datasource appears in the UI,
and typing the credentials in by hand fixes it until the next restart, when
provisioning overwrites your edit.

The dataset spans **2023-06-21 05:00 to 2023-06-28 05:00 UTC**. If you are
setting the time picker by hand, use that range.

Grafana also caches dashboard JSON, and a time range saved on your session
overrides the dashboard default. Hard-refresh the browser after a reload.

Then Grafana is at `http://localhost:3000` (admin/admin). Verify with:

```bash
make db-test    # role isolation, reconciliation, and all 11 panel queries
```

## What needs something from you

| To do this | You need |
|---|---|
| TimescaleDB and Grafana | Docker |
| Real NSRDB weather and ERCOT prices | API keys — see `19` |

See `STATUS.md` for exactly what remains.
