# 33 - Analysis Notebooks Record (Phase 12b)

Three executed notebooks and a tested analysis package, completing the Python half of Phase 12.

**Every notebook is executed in the build, not authored and shelved.** A notebook that has never run is a document; one that has run is a result.

---

## 1. The Methods Package

`northstar_analytics` holds the reference implementations an analyst's own work is scored against:

| Method | Standard | Tested against |
|---|---|---|
| ASTM E2848 expected power | ASTM E2848 | Data generated from the model - must recover the coefficients |
| Year-on-year degradation | RdTools convention | A known injected rate |
| Change-point detection | - | Known injected cleaning events |
| Forecast features and skill | - | Deliberate leakage, and a known-useless model |

Each is tested against data with a **known injected answer**. That is the only way to tell a working estimator from one producing plausible numbers, and it is the same principle the truth schema exists to serve.

The estimators consume the **analyst-facing** tree - measured telemetry, sensor error and data-quality defects included. An estimator that only works on physical truth is not an estimator.

---

## 2. Degradation: Unbiased, and Doc 20's Tolerance Is Tight

Across 30 realisations at three injected rates:

| Injected | Mean error | Std | Worst |
|---|---|---|---|
| -0.40 %/yr | +0.014 pp | 0.117 pp | 0.268 pp |
| -1.00 %/yr | +0.014 pp | 0.117 pp | 0.267 pp |
| -2.50 %/yr | +0.015 pp | 0.115 pp | 0.264 pp |

The estimator is **unbiased**. But a single three-year daily estimate at 2% noise carries roughly **0.12 percentage points of standard error**, so design doc `20 §14.5`'s plus-or-minus 0.15 pp tolerance is about a **1.3-sigma bound** - it will be missed by chance in a meaningful fraction of realisations.

`DegradationEstimate` now reports `standard_error` alongside the rate. Either the tolerance should widen to roughly 0.25 pp, or the acceptance dataset needs more years.

Monthly pre-aggregation was tried and made it **worse** (std 0.150 pp): fewer pairs outweighs less noise.

---

## 3. A Real Bug Found by a Notebook

Notebook 01 measures the module temperature coefficient. Rated: **-0.433 %/C**.

The first run returned **+0.50 %/C** from a two-variable regression controlling for irradiance - the wrong sign, from a correctly written regression.

**Cause: multicollinearity, introduced by the development resource.** `clearsky_resource` held ambient temperature constant. With constant ambient, cell temperature is a near-deterministic function of irradiance via the Faiman model, the two regressors are collinear, and the fit cannot separate them.

**Fix:** `clearsky_resource` now supports a diurnal ambient cycle whose peak **trails solar noon**, plus day-to-day drift. The lag decouples temperature from irradiance within a day - mid-afternoon is hotter than the equally-lit mid-morning - and the drift decouples it across days. That is real physics the constant-ambient stand-in was suppressing.

With varying ambient:

| View | Result |
|---|---|
| Naive correlation, 900-1000 W/m2 | -0.355 (right sign, badly attenuated) |
| Narrow band, 985-995 W/m2 | -0.893 |
| **Regression controlling for irradiance** | **-0.481 %/C** |
| Module rated | -0.433 %/C |

Only the controlled estimate recovers the rated coefficient. This is now the notebook's central lesson, and it is a lesson the dataset can teach precisely because the answer is known.

**Note for real data:** NSRDB supplies genuinely varying ambient, so this collinearity is an artefact of the development stand-in only. It is recorded because it would have silently produced a wrong-signed published coefficient.

---

## 4. Notebook 01 - Resource and Production

**Front POA to DC: r = 0.996. Front POA to AC: r = 0.607.**

The gap is the story. DC is unconstrained - more light, more current. AC is capped by the inverter and can be commanded to zero by the plant controller, so above the clipping threshold it stops responding to irradiance entirely.

An analyst seeing weak POA-to-AC correlation should not conclude the array is faulty. They should ask what is holding AC down.

**Overnight export: -120 kW**, mean -117.5. Forty inverters at standby plus ten transformers at no-load. Station service is real and it appears at the meter.

**Fleet POA spread: 6.9%** at any instant. Inverters do not see the same weather.

---

## 5. Notebook 02 - Expected Power

ASTM E2848 fitted on the first half of a filtered clean period, evaluated on the second.

- 49.2% of intervals survive filtering (curtailment and low irradiance excluded)
- R-squared 0.9935 on 2,478 training samples
- Hold-out mean absolute error 1,506 kW, **1.99% of mean output**
- Mean residual +126 kW - small and signed, so no material change between periods

The hard part is not the fit. It is **choosing the training period**: coefficients are only meaningful on a stretch free of faults, curtailment and heavy soiling, and in blind mode you cannot consult the truth schema to find one.

---

## 6. Notebook 03 - Forecasting

Two failure modes are demonstrated rather than described.

**Target leakage.** Every feature is a lag, a rolling mean of lags, or a calendar term known in advance. The leakage check passes on the real feature set and **fires immediately** when the concurrent target is added. That is what a too-good backtest looks like from the inside.

**No baseline.** Skill is measured against persistence, on a chronological split.

| Model | MAE | RMSE | Skill |
|---|---|---|---|
| Persistence | 2,835 kW | 10,167 kW | 0.0% |
| Gradient boosting | 2,764 kW | 9,627 kW | **2.5%** |
| Gradient boosting, steepest 10% of ramps | 8,349 kW | - | **58.1%** |

Overall skill of 2.5% looks unimpressive and is not. At a 15-minute horizon persistence is a genuinely strong baseline, because irradiance is highly autocorrelated over short intervals.

**Where the model earns its place is on ramps: 58.1% skill on the steepest decile.** Persistence is worst exactly when a cloud arrives, which is when the forecast matters most. Reporting only the aggregate number hides the entire value of the model.

---

## 7. What Remains

- **Grafana dashboards** - next, and unverifiable without a live datasource
- **TimescaleDB reconciliation leg** - still outstanding from Phase 11, needs `make db-up`
- **A canonical three-year dataset** - required by the V1 release gate and by any real degradation work; roughly 20 minutes to generate at 15-minute cadence

---

## 8. Downstream Document Updates Required

- `06 §3`: the development resource now supports lagged diurnal ambient, and why constant ambient breaks temperature analysis
- `20 §14.5`: widen the degradation tolerance to about 0.25 pp, or require more years
- `02 §12`: forecasting skill must be reported by ramp regime, not only in aggregate


---

## 8. Amendment: Notebooks Must Not Assume a Working Directory

`make notebooks` failed from a clean checkout with
`Table with name plant_telemetry does not exist`. The dataset was present and
correct; `Path("datasets/curriculum")` resolved to `notebooks/datasets/curriculum`.

`jupyter execute` runs with the notebook's own directory as the working
directory. JupyterLab uses wherever it was launched. A hardcoded relative path
is correct in exactly one of those, and the original verification used
`nbclient` with `path="."` set explicitly - a fourth working directory, and the
only one where the bug was invisible.

`northstar_analytics.find_dataset` walks up from the working directory until it
finds `datasets/<name>/analyst`. `open_dataset` now also fails at the path with
a message naming the fix, rather than three frames later inside DuckDB.

**Jupyter is an optional dependency group**, not a default install:

```bash
make lab                                # or
uv run --group notebooks jupyter lab
```

`uv run jupyter lab` without the group fails with `Failed to spawn: jupyter`.
JupyterLab is a large dependency to impose on someone who only wants a dataset.
