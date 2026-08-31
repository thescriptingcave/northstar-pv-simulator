# NorthStar PV Solar Farm Simulator

A utility-scale photovoltaic plant simulator that produces realistic,
explainable, analysis-ready time-series data — physics validated against
[pvlib](https://pvlib-python.readthedocs.io/), faults and sensor error injected
from known ground truth, and every claim below verified by a command you can run.

**The product is not the simulator. The product is the dataset**, and the
questions it makes answerable.

```bash
uv venv && uv sync
make dev-dataset     # ~2 min → 462k rows of telemetry
make accept          # → VERDICT: ACCEPTED (25 checks, 0 failures)
```

Then query it with no server running:

```sql
SELECT date_trunc('day', time) AS day,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
FROM read_parquet('datasets/curriculum/analyst/**/*.parquet',
                  hive_partitioning => true, union_by_name => true)
WHERE grid_export_power_kw IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

New here? **[QUICKSTART.md](QUICKSTART.md)**. What remains: **[STATUS.md](STATUS.md)**.

---

## Why this exists

Most synthetic time-series data is smooth, uncorrelated, and quietly wrong in
ways that make analysis pointless. A sine wave with noise teaches nothing about
finding a stuck tracker.

This generates data where **the answers are known but not visible**. Faults,
sensor drift, degradation and data defects are injected from a recorded ground
truth held in a separate directory tree. Hand someone the `analyst/` tree, keep
`truth/`, and their findings can be **scored**, not merely admired.

The strongest demonstration: a module degradation rate of **-0.400 %/yr** was
injected into three years of data, then recovered blind from the analyst tree at
**-0.345 %/yr** — a 0.055 pp error, inside the tolerance set in
`docs/design/20`. Reaching it required fixing four defects, all recorded in
`docs/design/35_degradation_recovery_record.md`.

---

## What it models

**NorthStar PV** — 100 MW AC / 124.66 MWp DC, single-axis tracking, bifacial,
Pecos County West Texas, interconnected to ERCOT West.

| | |
|---|---|
| Blocks / inverters | 10 / 40 at 2,500 kW |
| Combiners / strings / modules | 480 / 15,360 / 261,120 |
| Module / inverter | Heliene 96M475 / Sungrow SG2500U — real CEC database entries |
| Array | Horizontal single-axis, backtracking, GCR 0.33 |
| Telemetry | 585 assets, ~89M rows per simulated year |
| Cadence | 1 minute raw, 15 minutes settlement |
| Throughput | 13.6 min per simulated year |
| Storage | 2.48 GB/yr as Parquet, 5.48x compressed in TimescaleDB |

**Capacity is derived, not asserted.** String length falls out of module Voc at
the design minimum temperature against the inverter's DC ceiling.
`northstar-sim validate` rejects a configuration whose cold-morning string
voltage exceeds that ceiling — a check that caught a real error in the baseline
design (`docs/design/03` §5).

---

## The layers

```
Layer 1  Resource        NSRDB satellite irradiance, real precipitation
Layer 2  Temporal        5-min -> 1-min downscaling, variance-conditioned
Layer 3  Spatial         Multi-scale cloud field advected across the site
Layer 4  Physics         pvlib: SPA, tracking, Perez, Faiman, single-diode, Sandia
Layer 5  Sensors         Bias, drift, noise, response lag, quantisation, soiling
         |
Layer 6  Faults          Injected against truth, before measurement
Layer 7  Data quality    Gaps, stuck values, spikes, comms outages, duplicates
```

Order is load-bearing. Faults change **what the plant produced**; sensors change
**how accurately it was measured**; defects change **what was reported**.
Collapsing any two makes an equipment fault indistinguishable from a data fault.

---

## Twelve gates

Each is an independent falsifiable check, not a smoke test. `make test` runs all
of them plus 400 unit tests.

| Gate | What it proves |
|---|---|
| `validate` | 12 structural checks; capacity reconciles at every level |
| `physics-gate` | Production chain matches pvlib `ModelChain` to floating-point precision |
| `spatial-gate` | Cloud advection lag matches site geometry to 0.5 min |
| `plant-gate` | 40 inverters; energy chain closes; 13.6 min/simulated year |
| `state-gate` | Zero illegal state transitions; startup observable in telemetry |
| `sensor-gate` | Measurement diverges from truth only as modelled |
| `loss-gate` | Loss waterfall closes at 7e-19 of theoretical |
| `scenario-gate` | Injected faults move telemetry, not just the events table |
| `financial-gate` | Settlement, capture rate, curtailment economics |
| `dataquality-gate` | Defects corrupt reporting; physical truth bit-identical |
| `storage-gate` | Parquet export; aggregates reconcile |
| `curriculum-gate` | All 14 SQL exercises execute against real data |

---

## Results the dataset produces

Measured, not illustrative.

**Curtailment makes money.** `LOSS_CURTAILMENT` of 3,425 MWh carries a revenue
impact of **-$12,358**. Any pipeline reporting lost revenue as unconditionally
positive breaks here, and finding out why is the point.

**Capture rate 57.8%.** Generation-weighted price $12.31 against time-weighted
$26.07. Invisible in every physical metric; emerges only from joining prices to
production shape.

**Derating suppresses clipping.** At 40 C: clipping 66.0 MWh, no derating. At
50 C: derating 60.3 MWh, clipping falls to **26.7 MWh**. The two losses are not
additive, and summing estimates of both over-counts.

**Night export is -120 kW.** Forty inverters at standby plus ten transformers at
no-load. Station service is real and it appears at the revenue meter.

**The steepest ramps are commercial.** Curtailed 5-minute buckets ramp at
3.53 MW against 0.85 MW for weather — four times steeper. No cloud does that to
a 3.26 km site.

---

## Analysis material

**14 graded SQL exercises** (`sql/exercises/`) across seven tiers, each shipped
in both DuckDB and PostgreSQL form, each executed against real data in CI.

**Time-series queries** (`sql/timeseries/`) that genuinely need TimescaleDB —
`time_bucket` with timezones, `gapfill`/`locf` and their limits, continuous
aggregates, advection-lag recovery.

**Three notebooks** (`notebooks/`), executed in the build:

- resource and production, including a confounding trap that inverts the sign of
  the module temperature coefficient if you regress naively
- ASTM E2848 expected-power modelling
- forecasting, with target leakage demonstrated rather than described
  (2.5% skill over persistence overall, **58.1% on the steepest decile of ramps**)

---

## Repository layout

```
config/northstar.toml       Site, equipment, topology — all capacity derived from it
packages/
  northstar-fetch/          NSRDB, ERCOT and EIA acquisition with a checksummed cache
  northstar-sim/            Physics, states, faults, sensors, losses, storage
  northstar-analytics/      Expected power, degradation, change-point, forecasting
db/
  init/                     Schema, tables, hypertables — generated, not hand-written
  tests/                    Role isolation, reconciliation, panel and exercise queries
  diagnose.sh               Diagnoses a blank dashboard across five causes
sql/exercises/              14 graded exercises, both dialects
sql/timeseries/             TimescaleDB-specific query set
notebooks/                  Executed analysis notebooks
dashboards/                 Generated Grafana JSON
docs/design/                42 documents: 20 design, 22 implementation records
```

---

## Design documentation

`docs/design/` holds 20 normative design documents and 22 implementation
records. The records are the more interesting half — each documents what broke
and why:

- `22` — pvlib's `fit_desoto` returns negative series resistance; equipment reselected to real CEC entries
- `24` — rear-surface geometry error drove DC to 155% of nameplate
- `27` — clipping a signed term reported inverter efficiency as 0.04% instead of 1.32%
- `35` — degradation was a scalar and therefore invisible to every longitudinal method
- `39` — three schema defects that parsed cleanly and failed on execution

`36_documentation_reconciliation.md` records applying 56 pending updates across
all 20 design documents, and the verification that they landed.

---

## Honest status

Verified end to end: the simulator, storage, SQL curriculum, notebooks,
acceptance report, and — on a real TimescaleDB — schema, role isolation,
continuous aggregates, compression and three-way reconciliation.

**Not verified:** Grafana *rendering*. All 11 panel queries execute against a
live database and return data; the JSON-to-visual step has not been exercised in
CI. See `docs/design/40`.

**Requires credentials:** real NSRDB weather and ERCOT prices. Without them the
simulator uses a deterministic clear-sky resource and a synthetic price series
whose calibration targets are documented in `docs/design/19` §12.

Full detail in [STATUS.md](STATUS.md).

---

## Requirements

Python 3.13+, [`uv`](https://docs.astral.sh/uv/). Docker and a `psql` client
only for the TimescaleDB and Grafana path — see [QUICKSTART.md](QUICKSTART.md).

## License

MIT — see [LICENSE](LICENSE).

Solar resource data from [NREL NSRDB](https://nsrdb.nrel.gov/); market data from
[ERCOT](https://www.ercot.com/). Both carry their own terms, and neither is
redistributed in this repository.
