# 07 - Solar Production Model

**Version 2.1** - supersedes v2.0. Physics delegated to pvlib per DR-003; amended after implementation. Change log in §14.

## 1. Purpose

Define the causal chain from solar resource to grid export, and the loss accounting that makes every gap between them attributable.

**Governing change from v1.0:** this document no longer describes generic behavior to be implemented from scratch. It names specific validated models. Hand-rolled PV physics is where simulators quietly become wrong, and a wrong physics core silently invalidates every downstream analytic without ever failing a test.

## 2. Production Chain

```
Solar position (SPA)
  -> clear-sky irradiance (Ineichen-Perez)
  -> real GHI/DNI/DHI (cached NSRDB, downscaled per 06)
  -> tracker rotation (single-axis, backtracking)
  -> front POA (Perez transposition)
  -> rear POA (infinite sheds, bifaciality 0.70)
  -> effective irradiance (IAM, spectral)
  -> soiling adjustment
  -> cell temperature (Faiman)
  -> module DC output (CEC single-diode)
  -> degradation adjustment
  -> mismatch adjustment
  -> string availability
  -> DC wiring loss
  -> inverter DC input
  -> MPPT
  -> inverter conversion (Sandia)
  -> clipping at AC limit
  -> inverter thermal derating
  -> inverter setpoint / curtailment command
  -> AC collection loss
  -> transformer loss
  -> plant export limit
  -> revenue meter
  -> grid
```

## 3. Model Chain - LOCKED

| Stage | pvlib model | Function |
|---|---|---|
| Solar position | NREL SPA | `solarposition.spa_python` |
| Clear sky | Ineichen-Perez | `clearsky.ineichen` |
| DNI/DHI decomposition | DIRINT or Erbs | `irradiance.dirint` |
| Tracking | Single-axis, backtracking | `tracking.singleaxis` |
| Front transposition | Perez | `irradiance.get_total_irradiance` |
| Rear irradiance | Infinite sheds | `bifacial.infinite_sheds.get_irradiance` |
| Incidence angle modifier | Physical IAM | `iam.physical` |
| Spectral correction | First Solar / SAPM | `spectrum.spectral_factor_firstsolar` |
| Cell temperature | Faiman | `temperature.faiman` |
| DC model | CEC single-diode | `pvsystem.calcparams_cec` + `pvsystem.singlediode` |
| Inverter model | Sandia | `inverter.sandia` |
| Losses | Explicit per stage | Not `pvsystem.pvwatts_losses` |

**The lumped `pvwatts_losses` derate is explicitly rejected.** It collapses every loss into one number, which destroys the loss attribution in `18 §5.2` and makes `02 §7` unanswerable. Every loss stage must be individually computable.

**Version pinning:** pvlib 0.15.2, per DR-012 as revised in `19 §2.4`. Model function signatures have changed across pvlib versions; the pin is load-bearing.

**Perez must be pinned explicitly on any reference chain.** `pvlib.modelchain.ModelChain` defaults to **Hay-Davies**, not Perez. The first physics gate run failed at 15% relative error for exactly that reason - it was comparing two different physical models and reporting the difference as an implementation fault.

**Solar position takes ambient temperature and pressure.** They enter the atmospheric refraction correction. Omitting them leaves pvlib's 12 C default in place, shifting apparent zenith by up to 0.04 degrees, which propagates through tracking and transposition to roughly 1% per-sample power error. `ModelChain` passes them; the simulator must too. That error is the dangerous magnitude: too small to look wrong on a chart, large enough to corrupt annual energy.

## 4. Expected Power - Three Baselines

`02 §12` and `20 §7.2` require three distinct notions of "expected." They are not interchangeable, and conflating them is a substantive analytical error.

| Baseline | Definition | Computation | Analyst access |
|---|---|---|---|
| **Unconstrained truth** | What the plant would produce with no faults, no curtailment, clean arrays, all equipment available | Parallel pvlib run with fault and control layers disabled | Validation only |
| **Weather-adjusted expected** | What a healthy plant should produce given measured conditions | ASTM E2848 regression fitted on a clean period | Yes |
| **P50 budget** | Annual expectation from typical meteorology | pvlib run on TMY data | Yes |

The unconstrained truth run is also the **physics validation oracle**: it is computed by an independent `ModelChain` invocation, so agreement between the constrained simulator and the unconstrained reference under fault-free conditions is a direct check that the production chain is implemented correctly. This is the Phase 1 acceptance gate in `17 §4`.

## 5. DC Production

DC production responds primarily to POA irradiance, modified in a fixed order:

Applied in this order, as multiplicative factors so each is exactly attributable
in the loss waterfall:

| Step | Effect | Source |
|---|---|---|
| Effective irradiance | IAM and spectral corrections | pvlib |
| Soiling | `losses.soiling_ratio` | `06 §7` |
| Cell temperature | Faiman | `06 §6.3` |
| Single-diode solve | CEC parameters, looked up | DR-016 |
| **Degradation** | **Time-varying**, see below | `losses.plant_age_years` |
| Mismatch | `losses.mismatch` | Configuration |
| DC wiring | `losses.dc_wiring` | Configuration |
| String availability | 0 or 1 per string group | Fault engine |

**Degradation must vary within the record.** It was originally a scalar property
applied uniformly across a run, which makes it **invisible to every longitudinal
method**: a year-on-year estimate over a multi-year dataset recovers a rate of
exactly zero, and `01 §8`'s fourth success criterion cannot be run at all.

`physics.degradation_series(config, index)` advances plant age across the
window. `plant_age_years` is the age at the window **start**. The scalar
`config.degradation_factor` survives for single-window use only.

**The module non-linearity term is signed.** The gap between the linear STC
extrapolation and the single-diode solution is `LOSS_LOWLIGHT`, and it must not
be clipped at zero: at high irradiance the single-diode solution *exceeds* the
linear extrapolation, making the term a gain. Clipping it reported inverter
conversion loss as 0.04% instead of 1.32%.

**Hard constraints:**

- No meaningful generation at night. POA below the inverter startup threshold produces zero AC.
- DC power cannot exceed configured physical bounds without explicit modeled tolerance.
- Failed strings reduce the affected inverter's available DC, not the plant average.
- Bifacial gain is additive to front POA and varies with albedo, tracker angle, and row geometry - it is not a flat percentage adder.

## 6. Temperature Derating

Higher cell temperature reduces conversion efficiency per the module temperature coefficient (-0.29 %/C).

**Required emergent property:** two timestamps with identical POA irradiance produce measurably different DC output because cell temperature differs. On a West Texas summer day the spread between a 10:00 and 15:00 reading at matched irradiance should be several percent.

This is the entire physical basis of the temperature-correction exercise in `20 §4.2` and the reason raw seasonal PR comparison is invalid. If the data does not exhibit this spread, the temperature model is not connected.

## 7. Inverter Conversion

AC output depends on:

- DC input power and voltage
- Sandia efficiency curve (efficiency varies with both load and DC voltage)
- Operating state per `08 §2`
- AC nameplate limit
- Internal temperature and thermal derating
- Commanded setpoint from the plant controller

**AC power is never simply DC power times a constant.** Efficiency at 10% load differs materially from efficiency at 90% load, which is why the efficiency-metric load filter in `20 §8` exists.

Night standby draw (250 W per inverter) produces small negative AC power overnight. This is real, it appears in the data, and it is a good check on whether an analyst's daylight filter is correct.

## 8. Clipping

When available DC would produce AC above the inverter limit:

- AC power plateaus near the AC nameplate
- DC and available power continue to rise
- clipped energy is recorded as `LOSS_CLIPPING` in the attribution waterfall
- inverter operating state remains `RUNNING`, not `DERATED`

**Expected magnitude:** 1.5-3.5% of potential annual AC energy at DC/AC 1.258. Zero clipping or 10% clipping both indicate a modeling error.

**Clipping is not a fault.** It is the intended consequence of the DC/AC ratio and is not monetized as an avoidable loss per `18 §5.2`. An analyst who reports clipping as recoverable has made a classic error the dataset should permit and then contradict.

**Signature:** a flat-topped AC curve with a simultaneously rising DC curve. Visually distinct from curtailment (which plateaus below nameplate) and from derating (which declines rather than plateaus).

**Measuring clipping requires the uncapped output.** `pvlib.inverter.sandia`
applies `min(Paco, ...)` internally, so clipped and unclipped output are
indistinguishable in its result. `physics.sandia_preclip` implements the
published Sandia formulation with the cap omitted, using the same CEC
coefficients - verified to match `pvlib.inverter.sandia` exactly below the cap.

**Clipping and thermal derating are not additive.** A derating inverter clips
*less*, because derating pulls output below the cap before the cap can bind.
Measured: clipping falls from 66.0 to 26.7 MWh when derating engages. An analyst
who sums an estimated clipping loss and an estimated derating loss over-counts.

## 9. Curtailment

Curtailment is commanded output reduction despite available resource. It is fully specified in `18 §4`.

### 9.1 Distinguishing the Four Causes of Low Output

The signals must let an analyst separate:

| Cause | POA | DC available | AC output | Inverter state | Event | Price |
|---|---|---|---|---|---|---|
| Insufficient sun | Low | Low | Low | RUNNING | None | Any |
| Clipping | High | High | At AC limit | RUNNING | None | Any |
| Curtailment | High | High | Below limit, at setpoint | CURTAILED | Curtailment event | Often negative |
| Equipment fault | High | High or reduced | Low or zero | FAULT / DERATED | Fault event + code | Any |

The required discriminating signals are `available_power_kw`, `commanded_power_kw`, `operating_state`, `curtailment_reason`, and the joined market price series.

### 9.2 Economic Curtailment

Per `18 §4.1`, the controller curtails when `P_rt + C_ptc < 0`. This produces an output reduction that is:

- sharp and material
- coincident with high irradiance
- unaccompanied by any fault code
- superficially indistinguishable from derating
- recurring and seasonally clustered

**Correct attribution is impossible from telemetry alone.** It requires the price join. This is deliberate and is the most valuable single discrimination exercise in the package.

## 10. Collection and Transformer Loss

| Stage | Model | Behavior |
|---|---|---|
| AC collection | I²R on MV cable | Load-dependent, quadratic |
| Block transformer | No-load + load loss | No-load constant, load loss quadratic |
| Transformer temperature | First-order thermal lag on loading | Rises after loading rises |

Losses must be load-dependent, not fixed percentages. Fixed-percentage losses produce a constant efficiency, which eliminates the efficiency-variation analysis in `02 §5` and makes transformer health monitoring impossible.

Formulation: no-load loss is constant whenever energised; load loss scales with
the square of loading. AC collection loss is 0.8% at rated output, also scaling
with the square of loading. Measured on a clear day: transformer 0.56%,
collection 0.64% of inverter AC energy.

**Transformer output is not clipped at zero.** No-load loss is present whenever
the transformer is energised, and utility PV plants keep theirs energised
overnight, so output is genuinely **negative in darkness** - the plant imports
station service. Clipping discarded that energy and broke the loss chain by
0.14% of daily export: small enough to read as rounding, large enough to consume
a quarter of the 0.5% waterfall budget in `18 §5.2` before any real loss is
modelled.

**Thermal lag cannot be measured on a real day.** Under clipping the load
plateaus for hours, so winding temperature approaches steady state and peaks at
the *end* of the plateau rather than one time constant after the load peak. Use
a step response: verified at 63.2% of rise reached in 44 minutes against a
configured 45-minute constant.

## 11. Energy

Energy is **integrated from power**, never generated independently:

```
E_i = P_i * tau_i
E_cumulative = sum(E_i)
```

Requirements:

- Cumulative and daily energy are mathematically consistent with power samples and sampling interval
- Daily energy resets at local midnight, with the timezone policy from `03 §2`
- Revenue-meter cumulative energy is monotonically non-decreasing
- 15-minute settlement energy per `18 §2.2` is the integral of 1-minute power

Independent energy generation is the single most common way simulated datasets fail reconciliation, and it makes the `15 §7` aggregate validation meaningless.

## 12. Loss Attribution

The production chain in §2 maps one-to-one onto the loss waterfall in `18 §5.2`. Every stage that reduces power must emit a quantity attributed to exactly one cause code.

**Closure requirement:**

```
THEORETICAL - sum(all attributed losses) = EXPORTED
```

with residual under 0.5% of theoretical. A growing residual means a loss path exists that is not being attributed - which is a correctness bug, not a rounding issue.

The waterfall is computed at 15-minute grain to align with settlement, and stored in `solar_loss_attribution_truth` under the restricted role per DR-014.

## 13. Noise

Noise is applied at Layer 5 of the environmental model (`06 §8`) and at sensor level on electrical measurements. It does **not** perturb the physical chain.

**Randomness perturbs a causal model; it never replaces one.** A measurement noise term on `ac_power_kw` must not change the energy that was actually produced, must not propagate to plant totals computed from truth, and must not break the correlation requirements in `15 §4`.

## 14. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Physics source | Implied hand-rolled | pvlib, models named and locked |
| Tracker | Absent | Single-axis with backtracking in the chain |
| Bifacial | Absent | Rear POA via infinite sheds |
| IAM and spectral | Absent | Included |
| Inverter efficiency | "efficiency curve" | Sandia model, load and voltage dependent |
| Lumped derate | Not addressed | Explicitly rejected |
| Expected power | Single concept | Three distinct baselines with access rules |
| Validation oracle | Absent | Independent ModelChain run as Phase 1 gate |
| Clipping magnitude | Unquantified | 1.5-3.5% expected, out-of-range flagged |
| Curtailment | Described qualitatively | Four-cause discrimination table; economic rule from `18 §4` |
| Night standby | Absent | 250 W per inverter, appears as negative AC |
| Loss attribution | "some may be derived" | One-to-one map to `18 §5.2` with closure requirement |
| Transformer losses | "slightly" | Load-dependent no-load plus load loss, with thermal lag |


## 15. Changes from v2.0

| Item | v2.0 | v2.1 |
|---|---|---|
| Perez | Named in the chain | **Must be pinned explicitly**; ModelChain defaults to Hay-Davies |
| Solar position | Not specified | Takes ambient temperature and pressure, or 1% error follows |
| Degradation | Static factor | **Time-varying**; the scalar form is single-window only |
| Module non-linearity | Not identified | Signed `LOSS_LOWLIGHT`; clipping it hides conversion loss |
| Clipping measurement | Implied available | Requires `sandia_preclip`; pvlib caps internally |
| Clipping and derating | Treated separately | **Not additive** - derating suppresses clipping |
| Transformer at night | Clipped at zero | **Negative output**; clipping breaks chain closure |
| Thermal lag | Stated | Measurable only by step response |
| Balance-of-plant figures | Unquantified | Transformer 0.56%, collection 0.64% |
