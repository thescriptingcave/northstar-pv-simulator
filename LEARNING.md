# Learning PV Analytics

A six-week track through photovoltaic data science, using a plant whose
answers are known but not visible.

Every query runs unchanged in **DuckDB**, **TablePlus/psql**, **Jupyter** and
**Grafana**. All 15 are executed against real data in both engines before
shipping.

---

## Setup

**DuckDB, no server** — the fastest way to work:

```bash
make dev-dataset
duckdb
```

```sql
CREATE VIEW inverter_telemetry AS SELECT * FROM read_parquet(
  'datasets/curriculum/analyst/run_id=curriculum/stream=inverter_telemetry/**/*.parquet',
  hive_partitioning => true, union_by_name => true);
-- repeat for plant_telemetry, weather_telemetry, block_telemetry
```

**TablePlus or psql** — for window functions over a full year and for Grafana:

```bash
make db-up && make db-load
```

Tables live in the `telemetry` schema, so run this once per session:

```sql
SET search_path TO telemetry, public;
```

**Jupyter:**

```bash
make notebooks    # converts the .py sources to .ipynb and executes them
make lab
```

The notebooks are stored as `.py` in jupytext percent format — that keeps
diffs readable in git, since a committed `.ipynb` is mostly base64 output.
`make notebooks` generates the `.ipynb` files JupyterLab opens with cells.

Opening a `.py` directly gives you a plain script with no cells unless
jupytext's pairing is active; `jupytext.toml` configures that, but converting
first is more reliable.

```python
from northstar_analytics import find_dataset, open_dataset

db = open_dataset(find_dataset("curriculum"), "curriculum", "analyst")
db.execute(open("sql/learning/week1_resource.sql").read().split(";")[0]).df()
```

**Grafana** — `http://localhost:3000`, admin/admin. Paste any query into a new
panel; the datasource is already provisioned.

**Run a whole file at once**, useful for a first pass before working through
the queries individually:

```bash
make learn FILE=sql/learning/week1_resource.sql
make drill FILE=sql/drills/01_window_functions.sql
```

---

## The six weeks

Each file has three exercises. Each states the concept, the query, and **what
the answer should tell you** — including the trap.

### Week 1 — `week1_resource.sql`
**The resource, and why irradiance is not one number.**
Plane-of-array against horizontal, the clearness index, and why three weather
stations on one site should never agree.

*The trap:* mean GHI conflates day length with cloudiness. A short clear
winter day and a long cloudy summer day report the same mean.

### Week 2 — `week2_performance.sql`
**Performance ratio, and the temperature trap.**
The most-quoted PV metric and the easiest to compute wrongly.

*The trap:* PR is higher in winter. That is colder silicon, not better
maintenance. Correct each interval to 25 °C **before** aggregating — correcting
an aggregate uses a mean temperature no interval actually had.

### Week 3 — `week3_losses.sql`
**Where the energy went.**
Clipping, thermal derating, and curtailment.

*The trap:* derating and clipping are **not additive**. Heat reduces DC output,
which reduces clipping. Estimating both separately and summing over-counts.

### Week 4 — `week4_faults.sql`
**Finding faults without being told where they are.**
Peer normalisation, gaps-and-islands, and the limits of both.

*The trap:* normalising against the plant mean manufactures underperformers out
of the cloud field. And a stuck tracker is invisible to power/POA ratios
entirely — it reduces both. Detection needs more than one method.

*Score yourself:* `make score` runs a naive detector against injected truth.
It recovers **39.2% of faults at 81.7% precision**. Beating that is the exercise.

### Week 5 — `week5_market.sql`
**What the energy was worth.**
Capture rate, curtailment economics, settlement grain.

*The trap:* every metric in weeks 1–4 is blind to price. Measured on real 2025
ERCOT prices at this plant's node, capture rate is **82.2%** — and curtailment
carried a revenue impact of **−$337,369**, meaning curtailing *made* money.

### Week 6 — `week6_forecasting.sql`
**Predicting the next hour.**
Persistence baselines, ramp regimes, and leakage.

*The trap:* same-hour POA correlates near-perfectly with same-hour output
because it *is* the output, one step earlier in the physics. Using it as a
feature is leakage. If accuracy collapses when you lag every feature, the model
was reading the answer.

---

## Week 7 — machine learning on labelled gaps

The analyst tree has gaps from injected communications outages. The truth tree
has every value. That makes imputation a **supervised problem with free
labels**, scored in physical units, requiring no annotation.

```bash
make impute
```

Measured on the development dataset:

| column | best method | MAE | against forward fill |
|---|---|---|---|
| `ac_power_kw` | peer regression | 2.51 kW | **140x better** |
| `dc_power_kw` | peer regression | 3.74 kW | 101x better |
| `poa_global` | peer regression | 1.98 W/m² | 47x better |
| `cell_temperature` | peer median | 0.33 °C | 12x better |

Forward fill is what most production pipelines do. It is 140 times worse,
because carrying the last value through a daylight gap carries whatever the
inverter was doing before it went quiet.

**Why the peer methods win so heavily:** a PV plant's assets are highly
correlated at any instant. The information needed to reconstruct one inverter
is sitting in the other thirty-nine. That is a property of this domain, not a
general truth about imputation — and knowing *why* a method works is what an
interviewer is listening for.

**Two notebooks work through this properly:**

```bash
make lab
```

- **`04_imputation.py`** — the full method comparison, why peer methods win in
  this domain specifically, and how accuracy degrades with gap length. Forward
  fill goes from 0.05 kW MAE under 30 minutes to **391.93 kW** over 90–180,
  while peer regression stays at 2.43.
- **`05_fault_detection.py`** — the harder problem. Includes the finding that
  injected faults are **bimodal**: a third stop the inverter outright, the rest
  degrade it to about 0.935 of its peers. One threshold cannot catch both, and
  that — not tuning — is why the naive detector scores 39%.

**`06_fault_classification_sklearn.py`** — a `HistGradientBoostingClassifier`
over nine engineered features, scored against the naive baseline.

Two things it teaches that a successful model would not:

- **The test set had zero positives** on a seven-day record, because faults
  cluster in time. The fix is not to reshuffle at random — that reintroduces
  the leak — but to **split by asset**: train on some inverters, test on
  others. Events stay whole and the model must generalise across equipment.
- **The naive threshold beat the model**, 100%/95.2% against 53.8%/100%. With
  21 test positives that is insufficient evidence rather than a verdict, and
  saying so is the point.

**Things to try from here:**

- Beat peer regression with a gradient-boosted model over engineered features
- Does accuracy degrade with gap length? Score by duration bucket
- Impute `operating_state`, which is categorical and needs a classifier
- The same labelled-truth trick applies to fault classification —
  `scenario_instances` carries the class, and `make score` gives you a baseline
  at 39.2% recall

## What to do beyond the queries

The queries are a starting point, not the work. Three things worth building:

**A better fault detector.** The shipped one is a fixed peer-ratio threshold.
It misses stuck trackers and soiling by construction. `northstar-sim score`
gives you a scored comparison against ground truth — the only honest way to
know whether a method works.

**A degradation estimate.** `docs/design/35` records recovering an injected
−0.400 %/yr at −0.345 %/yr from the analyst tree alone. Try it independently
before reading how it was done.

**Dashboards for an operator, not an analyst.** `dashboards/` has three. Ask
what a control-room engineer needs at 6am that they do not have.

---

## SQL technique, separately

`sql/drills/` is an interview-shaped SQL track on the same tables — window
functions, gaps and islands, joins and NULL semantics, aggregation and pivots.
17 queries, verified in DuckDB and PostgreSQL.

Doing both tracks on one schema means never context-switching between a toy
database and the real thing. Start with `sql/drills/README.md`.

## Also available

| | |
|---|---|
| `sql/exercises/` | 14 graded exercises by SQL skill tier, both dialects |
| `sql/timeseries/` | TimescaleDB-specific: `time_bucket`, gapfill, advection lag |
| `notebooks/` | Resource and production, expected power, forecasting |
| `docs/design/` | 46 documents — the records are where the mistakes are |

The design records are worth reading once you have hit a trap yourself. Doc 27
explains why clipping a signed loss term reported inverter efficiency as 0.04%
instead of 1.32%; doc 43 records six defects found by running on real data.
