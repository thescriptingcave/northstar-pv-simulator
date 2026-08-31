# 20 - KPI Definitions and Standards Mapping

**Version 1.2** - amended again after the acceptance report was run against winter as well as summer datasets. Four acceptance bands were calibrated on one season and rejected valid data from another.

## 1. Purpose

`02` enumerates many KPIs but defines none normatively. That is a real gap: "availability" alone has at least four defensible definitions, and two analysts computing it from the same dataset will disagree by percentage points without a written rule.

This document makes every KPI computable and unambiguous. Per DR-010, definitions follow published standards rather than local convention.

**Normative references:**

| Standard | Scope |
|---|---|
| IEC 61724-1 | PV system performance monitoring - measurement, data exchange, analysis |
| IEC 61724-2 | Capacity evaluation method |
| IEC 61724-3 | Energy evaluation method |
| ASTM E2848 | Reporting performance of PV systems - regression method |
| IEC 62446-1 | Commissioning tests and documentation |
| NREL/RdTools | Degradation rate estimation, year-on-year method |

**Rule:** where this document and a standard conflict, the standard governs and this document is a bug.

---

## 2. Notation

| Symbol | Meaning | Unit |
|---|---|---|
| `P_0` | Installed DC capacity at STC | kW-DC |
| `P_ac,nom` | Installed AC capacity | kW-AC |
| `G_i` | POA irradiance in interval `i` | W/m2 |
| `G_STC` | Reference irradiance, 1000 | W/m2 |
| `T_cell,i` | Cell temperature | degrees C |
| `T_ref` | Reference cell temperature, 25 | degrees C |
| `gamma` | Temperature coefficient of Pmax, -0.0029 | 1/degrees C |
| `E_dc,i` | DC energy in interval | kWh |
| `E_ac,i` | AC energy at inverter output | kWh |
| `E_grid,i` | Metered export energy | kWh |
| `tau_i` | Interval duration | hours |
| `H_i` | POA insolation, `G_i * tau_i / 1000` | kWh/m2 |

All KPIs are computed at a stated grain. **Reporting a KPI without its grain is invalid** - hourly PR and annual PR are different numbers and are not interchangeable.

---

## 3. Yields (IEC 61724-1)

| KPI | Definition | Unit |
|---|---|---|
| Reference yield `Y_r` | `sum(H_i) / (G_STC / 1000)` = `sum(H_i)` | hours |
| Array yield `Y_a` | `sum(E_dc,i) / P_0` | hours |
| Final yield `Y_f` | `sum(E_grid,i) / P_0` | hours |
| Capture losses `L_c` | `Y_r - Y_a` | hours |
| System losses `L_s` | `Y_a - Y_f` | hours |

Yields have units of hours and are read as "equivalent hours at nameplate." A `Y_f` of 5.8 h/day means the plant produced as much as it would have running at full DC nameplate for 5.8 hours.

**Note on the `P_0` denominator.** `Y_f` conventionally normalizes by DC capacity, not AC. Using `P_ac,nom` produces a different and non-comparable number. With a DC/AC ratio of 1.258 this is a 26% difference - large enough to invalidate any benchmark comparison. Fix the convention and record it.

---

## 4. Performance Ratio

### 4.1 Basic PR (IEC 61724-1)

```
PR = Y_f / Y_r = (sum(E_grid,i) / P_0) / sum(H_i)
```

Dimensionless, conventionally expressed as a percentage.

### 4.2 Temperature-Corrected PR

Basic PR varies seasonally purely because cell temperature varies. A West Texas plant will show a summer PR several points below its winter PR with no change in health whatsoever. Comparing raw monthly PR across seasons is therefore meaningless, and temperature correction is mandatory for the guarantee in `18 §7.2`.

```
PR_corr = sum(E_grid,i) / sum( P_0 * (H_i) * [1 + gamma * (T_cell,i - T_ref)] )
```

`T_ref` is 25 C for the STC convention. An alternative convention uses the annual average cell temperature as `T_ref`, which makes annual `PR_corr` approximately equal annual `PR`. **Use the 25 C convention and state it.** Both are defensible; mixing them is not.

### 4.3 Availability-Adjusted PR

For contractual purposes, excluded periods per `18 §7.1` are removed from both numerator and denominator before computing PR. This isolates equipment performance from equipment availability - the two guarantees must not double-count the same shortfall.

### 4.4 Filtering Rules

Before any PR calculation, exclude intervals where:

| Condition | Threshold | Reason |
|---|---|---|
| Low irradiance | `G_i < 50` W/m2 | Ratio is numerically unstable near zero |
| Solar zenith | `> 85` degrees | Extreme incidence angle |
| Data quality flag | Any non-`GOOD` flag on POA or export | Corrupted input |
| Curtailment | `curtailment_reason != NONE` | Not a performance shortfall |
| Sensor disagreement | Station spread `> 15%` | Unreliable resource measurement |

**These filters are the single most common source of disagreement between two analysts computing PR from the same data.** They are normative here, not advisory. Every PR figure must record the filter set applied.

---

## 5. Capacity Factor

```
CF_ac = sum(E_grid,i) / (P_ac,nom * sum(tau_i))
CF_dc = sum(E_grid,i) / (P_0 * sum(tau_i))
```

`CF_ac` is the conventional reported figure. Both are defined here because the DC/AC ratio makes them differ substantially and the ambiguity causes real confusion.

Expected range for this plant: `CF_ac` roughly 0.30-0.34 annual.

---

## 6. Availability

Four definitions, all valid, all different. Each is required.

### 6.1 Time-Based Availability

```
A_time = (T_period - T_unavailable) / T_period
```

Simple, and largely meaningless for solar - an inverter that fails at 2 a.m. and is restored at 5 a.m. loses three hours of availability and zero energy.

### 6.2 Daylight-Weighted Availability

```
A_daylight = (T_daylight - T_unavailable_daylight) / T_daylight
```

Where daylight is defined as solar zenith below 85 degrees. Better, still crude.

### 6.3 Energy-Weighted Availability

```
A_energy = E_actual / (E_actual + E_lost_unavailability)
```

Where `E_lost_unavailability` is the sum of loss-attribution categories `LOSS_DC_OUTAGE`, `LOSS_INV_OUTAGE`, `LOSS_BLOCK_OUTAGE`, `LOSS_TRACKER`, and `LOSS_GRID` from `18 §5.2`.

**This is the contractual definition per `18 §7.1`** and the only one that reflects economic reality. It requires the loss attribution model to exist - which is why `18` and `20` are interdependent.

### 6.4 Contractual Availability

`A_energy` with excluded events removed from the numerator's loss set:

```
A_contract = E_actual / (E_actual + E_lost_unavailability_non_excluded)
```

Exclusions per `18 §7.1`. The gap between `A_energy` and `A_contract` is exactly the value of the exclusion clauses, and quantifying it is a genuine asset-management exercise.

### 6.5 Ordering: There Is None

An earlier version of this document implied `A_time <= A_daylight <= A_energy`.
**That is wrong**, and measurement showed it: over 30 simulated days the figures
came out

| Definition | Value |
|---|---|
| Time-based | 0.9289 |
| **Daylight-weighted** | **0.8704** |
| Energy-weighted | 0.9977 |

Daylight-weighted is **lower** than time-based, and correctly so. Faults are
driven by electrical and thermal stress, so they cluster in operating hours -
exactly the hours daylight availability measures. The same holds in real plants.

Report which definition is in use. Do not assume an ordering.

### 6.6 Asset-Level Availability

Computed per inverter, per block, and plant-wide. Plant availability is **not** the mean of inverter availabilities - it is energy-weighted, so a large inverter's outage counts more than a small one's. Reporting the unweighted mean is a common error the dataset should permit and then contradict.

---

## 7. Expected Energy and Weather-Adjusted Variance

### 7.1 ASTM E2848 Regression

Expected power as a function of measured conditions:

```
P_expected = G * (a + b*G + c*T_amb + d*v_wind)
```

Coefficients `a` through `d` are fitted from a healthy reference period. This is the industry-standard capacity test formulation and is the basis for weather-adjusted performance reporting.

**Training period rule:** fit on a period free of faults, curtailment, and heavy soiling, using the ground-truth scenario table to select it during model development. In blind analyst mode, selecting a clean training period is itself part of the exercise.

### 7.2 Three Expected-Energy Baselines

The dataset supports three distinct notions of "expected," and conflating them is a substantive error:

| Baseline | Source | Use |
|---|---|---|
| **P50 budget** | TMY run (`SRC-WX-02`) | Annual budget vs. actual; separates weather variance from performance variance |
| **Weather-adjusted expected** | ASTM E2848 regression on actual weather | Isolates performance from resource; the availability and PR guarantees use this |
| **Simulator truth expected** | `07 §3` unconstrained power | Validation only; not available to a blind analyst |

Budget-versus-actual variance decomposes as:

```
Total variance = Resource variance + Performance variance + Availability variance + Curtailment variance
```

Producing that decomposition correctly is the single best test of whether an analyst understands the dataset.

---

## 8. Efficiency Metrics

| KPI | Definition | Expected range |
|---|---|---|
| Inverter conversion efficiency | `E_ac / E_dc` per inverter | 0.96 - 0.988 |
| Weighted inverter efficiency (CEC) | Load-weighted per CEC test protocol | ~0.985 |
| Transformer efficiency | `E_out / E_in` | 0.985 - 0.995 |
| AC collection efficiency | `E_meter / sum(E_ac,inverters)` | 0.985 - 0.995 |
| DC-to-grid efficiency | `E_grid / E_dc` | 0.94 - 0.97 |
| Realized DC/AC ratio | `max(P_dc) / max(P_ac)` over period | approaching 1.258 |

Efficiency computed at low load is numerically unstable and physically different. Apply a load filter (default: above 10% of rating) and state it.

---

## 9. Loss Metrics

### 9.1 Clipping

```
E_clipped = sum( max(0, P_dc,available,i * eta_i - P_ac,limit) * tau_i )
Clipping ratio = E_clipped / (E_ac + E_clipped)
```

Hours-at-clip is a useful companion metric: count of intervals where `P_ac >= 0.99 * P_ac,limit`.

**Clipping is not a fault.** It is the intended consequence of a 1.258 DC/AC ratio. Expected annual clipping loss for this design: roughly 1.5-3.5% of potential AC energy. A dataset showing 0% clipping or 10% clipping indicates a modeling error.

### 9.2 Soiling Ratio

```
SR = P_measured / P_expected_clean
```

Estimated from the relationship between normalized output and time since last rain, or by comparison against a cleaned reference array. Values below roughly 0.90 in West Texas indicate a cleaning is overdue.

### 9.3 Degradation Rate

Year-on-year method (RdTools convention):

1. Fit an ASTM E2848 expected-power model on a clean period.
2. **Form a daily performance index as the ratio of daily measured energy to
   daily expected energy.** Not a daily median of hourly ratios - see below.
3. For each day, compute the ratio to the same date one year prior.
4. Take the median of that distribution and annualize.

**Step 2 dominates the result.** Measured on a real three-year record against a
known injected rate of -0.400 %/yr:

| Aggregation | Estimate | Error |
|---|---|---|
| Daily median of hourly ratios | -0.16 %/yr | +0.24 pp |
| **Ratio of daily energy sums** | **-0.35 %/yr** | **+0.05 pp** |
| Ratio of monthly energy sums | +0.56 %/yr | +0.96 pp |

A median of hourly ratios weights every interval equally, so unstable
low-irradiance hours - small denominator, noisy ratio - drive a statistic that
should be driven by the hours carrying the energy. Only the daily energy ratio
lands inside tolerance. Monthly aggregation leaves too few year-on-year pairs.

Filtering is **not** the lever here. Excluding clipped intervals, or restricting
to high irradiance, made the estimate worse in every variant tried.

Reference implementation: `northstar_analytics.daily_performance_index`.

**Leap days.** Shifting 29 February forward one year clamps onto 28 February and
collides with the existing entry. Deduplicate after shifting or the join fails.

**Requires at least two full years** - a hard requirement on dataset length for degradation exercises, and the reason `03 §8` specifies multi-year targets.

Expected result for this design: approximately -0.40%/year after year 1. **This is the cleanest closed-loop validation in the entire package** - the simulator injects a known degradation rate, and the analyst's independently-estimated rate should recover it. If the analysis cannot recover the injected value, either the analysis or the simulator is wrong, and the ground-truth table resolves which.

### 9.4 Capture Rate (economic)

```
Capture rate = (sum(E_i * P_rt,i) / sum(E_i)) / (mean(P_rt,i))
```

Generation-weighted average price divided by time-weighted average price. For solar in a high-penetration market this is **below 100%** and declines over time as solar penetration grows. It is the central economic fact of merchant solar, it is invisible in any physical metric, and it emerges only from joining real prices to real production shape.

---

## 10. Reliability Metrics

| KPI | Definition |
|---|---|
| MTBF | `sum(operating hours) / count(failures)`, per asset class |
| MTTR | `sum(repair duration) / count(repairs)` |
| Failure rate `lambda` | `count(failures) / (asset count * operating hours)` |
| Forced outage rate | Forced outage hours / (forced outage hours + operating hours) |
| Recurrence rate | Repeat failures on the same asset within 90 days / total failures |

**Boundary rules, which matter more than the formulas:**

- "Operating hours" means daylight hours. Counting nighttime standby as operating inflates MTBF by roughly a factor of two. **Implemented and enforced** in `northstar_sim.scenarios.reliability_metrics`.
- A trip followed by successful auto-restart within 15 minutes is a **transient**, not a failure. Counting transients as failures makes MTBF unusable. The 15-minute threshold is configurable and must be recorded.
- Repair duration is time to restored production, not time to technician arrival.

---

## 11. Data Quality Metrics

Per IEC 61724-1 monitoring requirements:

| KPI | Definition | Target |
|---|---|---|
| Data availability | Received samples / expected samples | > 99% |
| Daylight data availability | Same, daylight only | > 99.5% |
| Flagged fraction | Samples with any quality flag / total | < 1% |
| Stuck fraction | Samples in a run of >= 30 identical values | < 0.1% |
| Impossible-value rate | Samples outside physical bounds / total | 0% |
| Sensor spread | Max pairwise difference across the three met stations, daylight | < 8% typical |
| Timestamp integrity | Duplicate or non-monotonic keys | 0 |

**Observed on generated datasets:** 0.183% of samples carry a non-`GOOD` flag,
inside the 1% target. Weather-station spread averages 3.08% - 1.62% from spatial
separation, the rest from instrument calibration - against the 8% usable
threshold.

**Roughly half of injected defects carry no flag at all.** Drift is flagged 5%
of the time, stuck sensors 30%. A frozen instrument does not know it has frozen.
Filtering on the quality column is not a substitute for testing the data.

Data quality is reported **alongside** every performance KPI, never separately. A PR of 84% computed from 91% data availability is not the same claim as a PR of 84% from 99.8% availability, and reporting the first without the second is misleading.

---

## 12. Aggregation Rules

| Rule | Specification |
|---|---|
| Energy | Sums across time and assets |
| Power | Averages across time; sums across assets |
| Ratios | **Never average ratios.** Recompute from summed numerator and denominator |
| Temperature | Irradiance-weighted mean when used in performance context; simple mean for reporting |
| Availability | Energy-weighted, never simple mean |
| Rounding | Round only at presentation. Store full precision |
| Partial periods | Flagged as partial; never annualized without an explicit annualization flag |

The "never average ratios" rule is worth stating loudly. `mean(monthly PR)` is not `annual PR`, and the difference grows with seasonal spread. In West Texas the two differ by roughly half a percentage point - small enough to slip through review, large enough to matter against an 80.5% guarantee.

---

## 13. Implementation Mapping

| KPI | Source table | Grain | Materialization |
|---|---|---|---|
| `Y_r`, `Y_a`, `Y_f` | `solar_weather_telemetry`, `solar_plant_telemetry` | daily | `solar_kpi_daily` |
| `PR`, `PR_corr` | as above | daily, monthly, annual | `solar_kpi_daily` + rollup |
| `CF_ac` | `solar_plant_telemetry` | daily, monthly, annual | `solar_kpi_daily` |
| `A_time`, `A_daylight` | `solar_events`, `solar_inverter_telemetry` | daily | `solar_kpi_daily` |
| `A_energy`, `A_contract` | `solar_loss_attribution_truth` (dev) / analyst reconstruction (blind) | daily, annual | `solar_kpi_daily`, `solar_guarantee_ledger` |
| Expected energy (E2848) | `solar_weather_telemetry` + fitted model | 15 min | derived view |
| P50 budget | TMY simulation run | monthly, annual | static reference table |
| Inverter efficiency | `solar_inverter_telemetry` | 5 min continuous aggregate | `solar_inverter_5min` |
| Clipping | `solar_inverter_telemetry` | daily | `solar_kpi_daily` |
| Soiling ratio | `solar_string_telemetry`, weather | daily | `solar_kpi_daily` |
| Degradation rate | `solar_plant_telemetry` | annual | offline analysis |
| Capture rate | `solar_settlement` | monthly, annual | `solar_kpi_daily` rollup |
| MTBF, MTTR | `solar_events` | annual | offline analysis |
| Data quality | all telemetry | daily | `solar_kpi_daily` |

---

## 14. Validation Additions

Extending `15`:

1. Raw `PR` for a fault-free, curtailment-free, clean-array period falls within 0.79-0.86.

   **`PR_corr` is not bounded by the same range.** Under the 25 C reference
   convention in §4.2, corrected PR answers "what would PR be if cells were at
   25 C", so in a hot climate it necessarily exceeds raw PR - measured at 0.945
   against a raw 0.830 over a 38 C month. Expect `PR_corr` between 0.88 and 0.98
   for this site under that convention. A corrected figure inside the raw band
   would indicate the correction is not being applied.
2. Annual `CF_ac` falls within 0.28-0.36.
3. `Y_r` for a clear summer day at this latitude falls within 7.5-9.5 hours.
4. Basic `PR` is measurably lower in summer than winter; `PR_corr` is materially flatter across seasons. **If temperature correction does not flatten the seasonal profile, the correction is implemented wrong.**
5. Estimated degradation rate recovers the injected DR-005 rate within +/- 0.15 %/year over a three-year dataset.
6. `A_time`, `A_daylight`, and `A_energy` are all different numbers, ordered `A_time <= A_daylight <= A_energy` for typical fault patterns.
7. Annual capture rate is below 100%.
8. Loss waterfall (`18 §5.2`) sum reconciles with `L_c + L_s` from §3.
9. KPIs computed from continuous aggregates match KPIs computed from raw telemetry within tolerance.
10. Monthly PR values recomputed from summed components do not equal the mean of daily PR values - and the difference is explainable. If they are identical, aggregation is being done wrong somewhere.

Check 10 is deliberately inverted: it fails when the implementation is naive, and passes when the implementation is correct. That is more useful than a check that passes by default.


## 15. Seasonal Validity of Acceptance Bands

A December dataset - 70% clear-sky index, 8 C mean ambient, five-year-old plant -
failed **four** acceptance checks that a June dataset passed. All four were the
checks, not the data.

### 15.1 Performance ratio can exceed 1.0

Measured **1.0087** on the winter record against a 0.95 ceiling.

This is not an error. Cells below the 25 C reference outperform their STC
rating, and on a 1.25 DC/AC plant a cold clear week genuinely exceeds unity.
Real cold-climate plants report winter PR above 1.0 routinely.

Acceptance band widened to **0.55 - 1.15**.

### 15.2 Corrected PR does not always exceed raw PR

`§14.1` as amended in v1.1 stated that corrected PR must exceed raw PR under
the 25 C convention. **That is true only where cells are above 25 C.**

The direction follows the cells:

| Mean cell temperature | Correction factor | Corrected PR |
|---|---|---|
| Above 25 C | Below 1 | **Higher** than raw |
| Below 25 C | Above 1 | **Lower** than raw |

Measured: summer mean cell 57.4 C, corrected PR above raw. Winter mean cell
18.9 C, corrected PR 0.9856 against a raw 1.0087 - below, and correct.

Any check on this relationship must **derive the expected direction from the
data**, not assume a climate.

### 15.3 Capacity factor spans a wide seasonal range

Winter measured **0.1426** against a 0.15 floor. Band widened to **0.03 - 0.60**:
a short cloudy winter record legitimately reaches the low end, and nothing on a
1.25 DC/AC plant exceeds the high end.

### 15.4 Relative fleet spread inflates at low irradiance

Fleet plane-of-array spread is reported as a fraction of the mean, so it grows
as the mean shrinks. Winter measured **30.9%** against a 0.30 ceiling, with no
change in the underlying spatial model. Band widened to 0.60.

### 15.5 Capture rate has no useful floor

A cloudless week drives regional solar penetration to maximum every midday, so
the generation-weighted price collapses. Measured **6.2%** on a clear summer
week against a 0.2 floor.

The meaningful bound is the **upper** one: a capture rate above 1.0 means the
price join is wrong or the production shape is not solar. The floor was reduced
to zero.

### 15.6 The general point

A band calibrated on one season is testing the fixture, not the dataset. Any
acceptance criterion in this document should state the range of conditions it
is valid across, and be checked against a winter dataset as well as a summer
one before it is trusted.

---

## 16. Changes from v1.0

| Item | v1.0 | v1.1 |
|---|---|---|
| Availability ordering | Implied `time <= daylight <= energy` | **Claim removed** - daylight is lower, correctly |
| Degradation aggregation | Unspecified normalisation | **Ratio of daily energy sums**, with the comparison table |
| Leap days | Not addressed | Deduplicate after the one-year shift |
| `PR_corr` acceptance band | Same 0.79-0.86 as raw PR | **0.88-0.98** under the 25 C convention |
| Data quality figures | Targets only | Observed 0.183% flagged, 3.08% station spread |
| Flag reliability | Not stated | ~50% of defects unflagged; drift 5%, stuck 30% |
| MTBF basis | Stated | Confirmed implemented |

Sources: `29_financial_layer_record`, `30_data_quality_record`,
`33_notebooks_record`, `35_degradation_recovery_record`.
