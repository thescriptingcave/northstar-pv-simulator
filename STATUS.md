# Status

Twelve phases complete. 400 tests, 12 gates, 39 design documents.
Everything in `QUICKSTART.md` runs offline and is verified.

## Complete and verified

| Area | Evidence |
|---|---|
| Plant model, physics, spatial field | Gates 1–3; physics matches pvlib `ModelChain` at floating-point precision |
| Full plant, states, sensors, losses | Gates 4–7; loss waterfall closes at 7e-19 |
| Faults, financials, data quality | Gates 8–10 |
| Parquet + DuckDB storage | Gate 11; 2.48 GB per simulated year |
| SQL curriculum | 14 exercises, all executed against real data |
| Notebooks | 3, executed in the build |
| Acceptance report | 25 checks, ACCEPTED on the dev dataset |
| Blind degradation recovery | **-0.345 %/yr recovered against -0.400 injected**, 0.055 pp error |
| Design docs vs code | Reconciled and grep-verified (doc 36) |

## Written but never executed

These are **not** verified. Treat them as a starting point.

| Item | Why |
|---|---|
| ~~Hypertables and continuous aggregates~~ | **VERIFIED** on TimescaleDB 2.17.2 built from source — 5 hypertables, 6 aggregates, 5 compression policies. Three defects found and fixed. See doc 39 |
| ~~Grafana rendering~~ | **VERIFIED.** All three dashboards render against a live datasource. A timezone bug in the default window made every panel read "No data" while every component was individually correct — see doc 40 §6 |
| ~~Role isolation~~ | **VERIFIED** on PostgreSQL 16 — 6/6 checks, and a real defect fixed. See doc 38 |
| ~~Compression ratio~~ | **MEASURED: 5.48x** (151 MB → 27 MB) |
| ~~Third reconciliation leg~~ | **CLOSED**: raw SQL, continuous aggregate and DuckDB all agree to 9.9e-8 MWh |

## Blocked on you — one Docker session

```bash
make db-up
psql -f db/init/01_schemas.sql
psql -f db/init/02_tables.sql
psql -f db/init/03_hypertables.sql
```

Then confirm: hypertables create, continuous aggregates refresh, the
`analyst` role is denied on the `truth` schema, and the dashboards render.

That closes the third leg of the reconciliation in `15 §11` — two of three
legs are verified today.

## ~~Blocked on credentials~~ — COMPLETE

**79 of 79 partitions fetched, 0 failures, 197 requests** (2026-08-31).

| Source | Fetched |
|---|---|
| NSRDB GOES CONUS v4 | 2023, 2024, 2025 at 5-minute |
| NSRDB TMY | 1 |
| Open-Meteo ERA5 | 2023, 2024, 2025 hourly |
| ERCOT RT SPP | 2025, 3 settlement points, 15-minute |
| ERCOT DAM SPP | 2025, 3 settlement points, hourly |

`check_negative_prices_present` executed against real data for the first time
and **passed** for `HB_WEST` 2025.

**DR-015 is resolved.** `HRNT_SLR_RN` returned all twelve months at both
cadences, so the plant's pricing node is confirmed rather than provisional.

Getting there took eleven defects in the fetch layer, recorded in `42`. The
originals:

| Registration | For |
|---|---|
| `developer.nlr.gov` | NSRDB weather |
| `developer.ercot.com` | Real prices |
| `eia.gov/opendata` | Generation reference data |

Put them in `.env`, then:

```bash
make plan                                       # 445 partitions, credentials checked
uv run northstar-fetch fetch --limit 1          # one partition per provider first
make fetch                                      # the full run
```

`plan` confirms the values are *set*. Only a request confirms the providers
*accept* them, and `--limit 1` establishes that in five requests rather than
445.

**Also outstanding:** confirm the pricing node `HRNT_SLR_RN` has 2019–2024
price coverage. DR-015 is provisional until then — see `21 §5`.

## Remaining work I could do

| Item | Note |
|---|---|
| Canonical 3-year dataset at 1-minute cadence | ~40 minutes to generate; the V1 gate needs it |
| `Recovery` section of the acceptance report | Needs the multi-year dataset above |
| Doc 01 §8 criteria 2 and 3 scored blind | Attribution and valuation are demonstrated but not scored |
| Availability and PR recovery (T7) | Only degradation has been run |

## Phase 13 — COMPLETE

`generate --real` runs the plant on fetched NSRDB irradiance and ERCOT prices.
The loaders, harmonization, physics and export all work; a full simulated year
produces 56 million rows and 287,835.8 MWh.

**A full simulated year passes acceptance on real data: 24 checks, 0
failures.** 56 million rows, both DST transitions, 365 date partitions, and the
full ERCOT price year at the plant's own node.

Getting there took six defects, recorded in `43`. The one that mattered:
`_first_order_lag` propagated NaN forever, so a single gap in truth POA killed
measured irradiance and cell temperature for the rest of the record. It looked
like a scale problem for two rounds of evidence and was actually seasonal —
every passing run was June, every failing run included January.

The historical failure state, kept because the diagnosis is the useful part:

| | |
|---|---|
| Truth POA | 20,929,040 non-null, correct |
| **Measured POA** | **33,360 non-null, every value exactly zero** |
| Measured AC | correct: 830.57 kW mean against 834.67 truth |
| Measured cell temperature | 1.6 to 11.7 C — impossible for West Texas |

Irradiance and temperature channels are destroyed; power channels are intact.
Every remaining failure follows from that: NaN correlations, empty fleet
spread, PR of 0.0000, and all 525,596 intervals filtered because the KPI
filter needs POA.

**Eliminated:**

- injected defects — a `--no-defects` run is identical
- the closure repair — truth is clean, peak DC 106.9% of nameplate
- sensor soiling — 3%/year gives a factor of 0.97, not zero
- record duration — measured POA survives a 365-day index when only a day is
  simulated

**Not eliminated:** simulation volume. Every working run was 3–7 days; the
failing one is 21 million inverter rows. Something in the sensor or export path
behaves differently at that scale.

**Resolved:** `capture_rate` was 123-128% because the report always
regenerated a **synthetic** price series from the dataset's own irradiance,
never using the real prices the dataset was settled against. It now prefers
cached real prices and falls back to synthetic only when nothing covers the
range, saying which it used. On a week-long `--real` run: 123.2% -> 101.9%,
and the ceiling moved to 1.05 because prices uncorrelated with generation sit
at 1.0 by construction.

**Also open:**
- `82 illegal inverter transitions, first: STARTING -> CURTAILED` — a state
  machine gap that only real price data reaches

Use `--start`/`--end` with `--real` to iterate in minutes rather than an hour.

## A note on scripts/update.sh

It **destroyed a 56-million-row dataset**. The restore path was:

```bash
rm -rf "$repo/$item"           # delete the original
cp -R "$stash/$item" "$repo"/  # copy 2 GB back from a temp directory
```

The delete happens first, so a failed or interrupted copy loses the data
outright - and the trap removes the stash on exit.

**The stashing was never necessary.** The archive contains no `.env`, no
`datasets/` and no `resource_cache/`, so extracting over the top cannot touch
them. Verify before trusting it:

```bash
unzip -l <archive> | grep -E "\.env$|datasets/|resource_cache"
```

It now touches nothing local, and re-execs from a copy of itself first -
bash reads a script incrementally, so overwriting it mid-run made bash resume
at the old byte offset in the new file.

**Use `git pull` instead.** An updater living inside what it updates has two
failure modes that version control simply does not have. This script should be
deleted now that the repository is on GitHub.

## Phase 14 — blind scoring, on branch

`northstar_sim/scoring.py` scores blind analysis against injected truth. The
detector sees the analyst tree only; truth is opened afterwards to score.

**Criterion 1 measured for the first time**: 39.2% recall, 81.7% precision on a
full year (42.9% / 75.0% on a week, so stable). That is a statement about a
naive peer-ratio detector, not about the data — it misses stuck trackers and
soiling by construction. A better detector is the right response, not a lower
bar.

**Criterion 3 blocked.** Loss is computed as `available_power_kw -
ac_power_kw`, which only sees faults that open a gap between capability and
outcome. A stuck tracker reduces available power itself, so two of four
scenario classes contribute nothing and the ranking proves nothing.

The fix is a **fault-free counterfactual**: the same weather and seed with
`--no-faults`, attributing each scenario's loss as the difference. Needs a
second full-year run plus attribution logic. See `44 §3`.

## Known noise

**513 `DeprecationWarning`s on pandas 3.x / numpy 2.4+**, reading "The
'generic' unit for NumPy timedelta is deprecated". They originate inside
pandas' own `Timedelta` construction and inside pvlib's `solarposition`, not in
this project's call sites - `pd.Timedelta(minutes=<int>)` is idiomatic and the
arguments are already Python integers.

Deliberately **not muted**. A `filterwarnings` entry would clean the output and
hide a category the message says "will raise an error in the future", including
any instance that turns out to be ours. It resolves on a pandas update.

One test had to be narrowed because of this: the physics chain asserts no
`RuntimeWarning` - the scipy divide-by-zero class it was written to guard -
rather than no warnings at all. Asserting zero warnings of any kind made a
dependency's deprecation timetable able to fail a test about our arithmetic.

## Known limitations, recorded not hidden

- **Equipment is roughly 2019 vintage.** pvlib's bundled CEC databases are a
  stale snapshot topping out near 510 W modules. No analytical lesson depends
  on model year. See `05 §12`.
- **The stuck-tracker fault uses a geometric approximation** rather than
  re-running transposition. It captures the signature shape, not quantitative
  loss. See `09 §10`.
- **The POI limit never binds**, so interconnection-limited curtailment is
  exercised only in tests. Lowering `poi_export_limit_kw` is the one parameter
  if you want it. See `10 §14`.
- **Prices are synthetic** until ERCOT credentials exist. Calibration targets
  documented in `19 §12`.
