# 02 - Time-Series Analytics Requirements

**Version 2.0** - supersedes v1.0. Adds financial, tracker, and market analytics. Change log in §14.

## 1. Purpose

This document is the analytical contract. Plant design, telemetry design, and data acquisition must support these questions.

**Normative KPI definitions live in `20_kpi_definitions`.** This document says what must be answerable; `20` says exactly how each quantity is computed. Where the two appear to conflict, `20` governs.

## 2. Required Temporal Characteristics

Generated history must contain:

- intraday cycles and sunrise/sunset transitions
- daily and seasonal variation
- **real inter-annual variability** (from six years of real resource data)
- long-term trend and degradation
- autocorrelation and cross-signal correlation
- **spatial correlation with wind-driven lag structure**
- noise
- transient events and abrupt state changes
- gradual deterioration
- missingness and data-quality defects
- recoveries
- repeated and recurring conditions
- **price volatility, including negative prices and scarcity events**

## 3. Production Analysis

- What is instantaneous plant power?
- What is energy production by hour, day, week, month?
- When did production peak?
- What is plant capacity factor, AC and DC basis?
- What is DC-to-AC conversion efficiency, and how does it vary with load?
- What is the realized DC/AC ratio under actual operation?
- How much time does the plant spend at or near rated AC capacity?
- When does inverter clipping occur, and how much energy does it cost?
- How does actual production compare with each of the three expected-power baselines (`07 §4`)?
- What is the bifacial gain, and how does it vary with tracker angle and season?

## 4. Weather and Correlation Analysis

- GHI versus plant output
- POA irradiance versus DC power
- ambient versus module temperature, including lag
- module temperature versus conversion efficiency
- cloud cover versus power ramps
- wind speed versus module temperature at matched irradiance
- irradiance change versus power change
- weather-station comparison and sensor disagreement
- **cross-asset irradiance correlation as a function of separation distance**
- **cloud-motion lag structure, and wind direction inference from it**
- **clear-sky index distribution and its seasonal shape**
- **cloud-edge enhancement events, and how to avoid misclassifying them as sensor error**

## 5. Asset Performance Analysis

- inverter-to-inverter and block-to-block comparison
- normalized output per installed kW
- underperformer ranking - by energy **and** by revenue
- rolling inverter efficiency
- transformer loading and temperature trends
- string and combiner imbalance detection
- persistent versus transient underperformance
- **stuck-tracker detection from production data alone**
- **tracker misalignment as a change-point problem**
- **distinguishing the three persistent-deficit shapes:** stuck tracker (U-shaped by time of day), soiling (flat proportional), thermal derate (irradiance-dependent)

## 6. Reliability and Fault Analysis

- when a fault began, and what changed before it
- which assets were affected
- how much production was lost
- outage duration
- immediate versus gradual recovery
- fault recurrence rate
- MTBF and MTTR, with the daylight-hours and transient-threshold rules from `20 §10`
- availability by asset and plant, under all four definitions in `20 §6`
- common precursor signatures
- **which faults occur disproportionately during high-price intervals**

## 7. Curtailment and Loss Analysis

The data must distinguish, and monetize where appropriate:

| Loss | Cause code | Monetized |
|---|---|---|
| Resource limitation | - | - |
| Temperature | `LOSS_THERMAL` | No |
| Soiling | `LOSS_SOILING` | Yes |
| Degradation | `LOSS_DEGRADATION` | No |
| Mismatch and shading | `LOSS_MISMATCH` | No |
| DC wiring | `LOSS_DC_WIRING` | No |
| DC outage | `LOSS_DC_OUTAGE` | Yes |
| Inverter conversion | `LOSS_INVERTER_EFF` | No |
| Clipping | `LOSS_CLIPPING` | No |
| Inverter outage | `LOSS_INV_OUTAGE` | Yes |
| Inverter thermal derate | `LOSS_INV_THERMAL` | Yes |
| AC collection | `LOSS_AC_COLLECTION` | No |
| Transformer | `LOSS_TRANSFORMER` | No |
| Block outage | `LOSS_BLOCK_OUTAGE` | Yes |
| Tracker | `LOSS_TRACKER` | Yes |
| Curtailment | `LOSS_CURTAIL_*` | Per reason |
| Grid | `LOSS_GRID` | Depends |
| Residual | `LOSS_RESIDUAL` | Flagged |

Full waterfall in `18 §5.2`. Not every loss needs a physical sensor; several are simulator truth or derived analytical fields.

**The waterfall must close.** Residual above 0.5% of theoretical means an unattributed loss path exists.

## 8. Financial Analysis - NEW in v2.0

- What is revenue by interval, day, month, year?
- What did each outage cost, and does cost ranking differ from duration ranking?
- Which asset is worst by lost revenue rather than lost kWh?
- What fraction of annual lost revenue is avoidable versus structural?
- What is the marginal value of one point of availability?
- Should cleaning have happened earlier, and what did the delay cost?
- What did economic curtailment cost, and was curtailing correct?
- How much did the hedge pay or cost, and how does that vary seasonally?
- What is the **capture rate** - generation-weighted price over time-weighted price?
- How does hedge shape mismatch behave across a day and a year?
- What is basis exposure between plant node and hub?
- Where does the plant stand against its availability and PR guarantees, and what are the accrued liquidated damages?
- **Are there intervals where an outage increased margin?** (Yes - during sustained negative pricing.)

The capture-rate question is the deepest. In a high-solar market, solar generates most when solar is worth least, so capture rate is below 100% and declines as penetration grows. It is invisible in every physical metric and emerges only from joining real prices to real production shape.

## 9. Data Quality Analysis

- missing, delayed, and duplicate samples
- stuck sensors, drift, bias, spikes
- impossible values
- noisy signals and communication gaps
- inconsistent asset clocks
- **distinguishing sensor faults from equipment faults** (`09 §7`)
- **the case where a sensor fault causes a real physical response** (SCN-029 spurious stow)
- **the case where a sensor fault makes performance look better than it is** (SCN-067 pyranometer soiling inflating PR)
- **quality flags that are themselves wrong** (`11 §12`)

Defects must be configurable and distinguishable from equipment faults.

## 10. Attribution and Disambiguation - NEW in v2.0

Real operational analytics is mostly disambiguation. Each of these has a defensible answer and a naive answer that differ:

| Question | Discriminator |
|---|---|
| Clipping or curtailment? | `commanded_power_kw` versus AC limit |
| Curtailment or thermal derate? | `derate_reason`, `curtailment_reason` |
| Economic or congestion curtailment? | Price join |
| Soiling or degradation? | Behavior at the next rain event |
| Sensor drift or real decline? | Cross-station comparison |
| Grid-caused or equipment-caused trip? | Grid voltage and frequency in the pre-event window |
| Failure or transient? | 15-minute restart threshold |
| Stuck tracker, soiling, or derate? | Time-of-day shape of the deficit |

Scenarios SCN-090 to SCN-093 exist specifically to exercise these.

## 11. SQL Skills the Dataset Should Support

Core: time-range filtering, DATE/TIME extraction, GROUP BY, window functions, LAG/LEAD, rolling averages, cumulative energy, rate of change, conditional aggregation, FILTER, HAVING, telemetry-to-event joins, time bucketing, gap filling, interpolation, first/last values, percentiles, anomaly thresholds, event-window analysis, before/after comparison.

Added in v2.0: **telemetry-to-price joins across differing grains** (1-minute telemetry to 15-minute prices), **asof joins**, **peer-group normalization with window partitions**, **recursive or gaps-and-islands queries for outage duration**, **pivoting loss waterfalls**.

TimescaleDB specifics: hypertables, `time_bucket`, continuous aggregates, compression, retention policies, gap filling, time-weighted aggregates.

**Portability requirement:** every exercise must also be expressible in DuckDB over Parquet (DR-011). Doing each twice teaches the difference between time-series-specific features and portable SQL.

## 12. Python and Data Science Analysis

- pandas time indexing and resampling
- rolling statistics
- seasonal decomposition
- correlation analysis
- feature engineering and lag features
- anomaly detection
- forecasting
- change-point analysis
- supervised fault classification, using the ground-truth table as labels
- expected-power modeling via ASTM E2848 regression
- **degradation estimation via the year-on-year method** (`20 §9.3`)
- **price-aware forecasting**: forecast revenue, not just energy

The supervised fault-classification task is only possible because ground truth exists as labels. It is the clearest payoff of the truth separation in DR-014.

## 13. Grafana Requirements

Dashboards should emerge naturally for: plant overview, power and energy, weather and resource, inverter comparison, tracker status, transformer health, alarms and events, availability, loss waterfall, data quality, fault investigation, **revenue and settlement**, **guarantee position**.

## 14. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| KPI definitions | Implied | Delegated to `20`, normative |
| Loss categories | 10, unmonetized | 18, with cause codes and monetization flags |
| Financial analysis | Absent | Full section |
| Attribution section | Absent | Explicit discriminator table |
| Tracker analytics | Absent | Stuck, misaligned, and shape-discrimination |
| Spatial analytics | Absent | Distance correlation, lag structure, wind inference |
| Inter-annual variability | Absent | From six years of real data |
| SQL skills | Core set | Adds cross-grain joins, asof, gaps-and-islands |
| Portability | Not required | Every exercise expressible in DuckDB |
| Forecasting target | Energy | Energy and revenue |


## 15. Contract Coverage - as delivered

The curriculum in `sql/exercises` covers **37 skills** across seven graded
tiers, every exercise executed against a real dataset rather than authored and
shelved.

### Additions to §4 - weather and correlation

**Wind direction is recoverable from irradiance telemetry alone.** Distant
weather stations correlate at 0.155 instantaneously and **0.967 at a +7 minute
lag**, matching a geometric prediction of +6.8 minutes. Inferring wind direction
from the lag structure, independently of the anemometer, is an explicit
exercise.

### Additions to §9 - data quality

Flag-detection rates are deliberately below 1.0, and **unflagged defects are the
majority case**: roughly half carry no flag. Drift is flagged 5% of the time,
stuck sensors 30%, gaps 100%.

### Additions to §12 - forecasting

**Skill must be reported by ramp regime, not only in aggregate.** Measured
against persistence at a 15-minute horizon: 2.5% overall, **58.1% on the
steepest decile of ramps**. Persistence is a strong baseline for solar because
irradiance is highly autocorrelated over short intervals, so it is worst exactly
when a cloud arrives - which is when the forecast matters. Reporting only the
aggregate hides the entire value of the model.

### Additions to §13 - dashboards

Dashboards are **generated from package constants**, not authored. Hand-edited
JSON drifts from the schema silently: a renamed column leaves a panel showing an
empty graph rather than an error.
