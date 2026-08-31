# 26 - Sensor Layer Record (Phase 6)

## Result: PASSED

```
  [PASS] fleet_instantiated           215 sensor instances
  [PASS] truth_object_distinct        measured frames are distinct objects with distinct values
  [PASS] divergence_bounded           worst mean relative divergence 3.33%
  [PASS] error_is_systematic          median |residual-to-signal| correlation 0.584 across 40 instruments
  [PASS] station_spread_realistic     spatial 1.62% -> with instruments 3.08% (usable below 8%)
```

---

## 1. The Non-Negotiable Property

**A sensor fault must never alter physical truth.**

A stuck pyranometer reports a constant while actual irradiance, and therefore actual production, continues to vary. That distinction is the entire basis of data-quality analysis, and it only holds if measurement is a **pure function applied to truth**, never a modification of it.

The architecture enforces this rather than relying on discipline. `apply_sensor` copies before it touches anything and returns a new series; `measure_frame` returns a new frame; `run_plant` keeps `result.inverters` (truth) and `result.measured` (analyst-facing) as separate dictionaries. Tests check object identity and immutability, not just numeric values.

This is the code-side half of DR-014. The database-side half - schema and role separation, with an automated test that `analyst` is denied on `truth` - lands in Phase 11.

---

## 2. Six Effects, Applied in Physical Order

Contamination and calibration act on the quantity **before** the instrument responds to it; the instrument then lags and adds noise; quantization happens last, in the analogue-to-digital conversion. Order matters: quantizing before adding noise would produce a visibly different artefact.

| Effect | Behaviour | Why it is there |
|---|---|---|
| Calibration bias | Multiplicative gain, or additive offset for temperature | The dominant systematic. Does not average away |
| Drift | Slow, monotonic, accumulating over the record | Mechanism behind SCN-062 |
| Noise | Random, small | The only effect an averaging analyst removes |
| Response time | First-order lag, 20 s for irradiance, 60 s for temperature | Thermopiles settle slowly; a reason instruments disagree most during fast ramps |
| Quantization | Snap to reporting resolution | Invisible until someone looks for stuck values |
| Soiling | Multiplicative under-read, growing annually | Pyranometers get dirty too, independently of the modules |

Temperature bias is an **offset**, not a gain. A 0.35 K RTD error does not scale with the reading, and modelling it multiplicatively would be wrong physics that happens to look plausible.

Parameters sit inside the tolerances of real instrumentation: secondary-standard pyranometer roughly 2%, RTD better than 1 K, cup anemometer several percent. Observed fleet spread: calibration gain **0.970 to 1.035**, temperature offset **-0.96 to +0.76 C**.

---

## 3. The Soiled Pyranometer Inflates Performance Ratio

Worth stating separately because the direction is counterintuitive.

A dirty pyranometer **under-reads** irradiance. Performance ratio divides measured output by measured resource, so a smaller denominator makes PR look **better**. An analyst chasing a suspiciously good PR is doing exactly the right thing, and the answer is a dirty sensor rather than a healthy plant.

This is SCN-067, and it is now a physical consequence of the sensor model rather than an injected special case.

---

## 4. A Gate Bug: Systematic Error Is a Fleet Property

The `error_is_systematic` check first measured residual-to-signal correlation on **one** inverter and failed at 0.294 against a 0.3 threshold.

The check was wrong, not the code. That instrument happened to be drawn with a near-unity gain, so its error genuinely is mostly noise and the weak correlation is correct.

Across the fleet:

- |residual-signal correlation| ranges **0.043 to 0.919**, median 0.537
- correlation tracks |gain error| at **r = 0.952**
- of 40 instruments, 19 have gain error above 1%; their median correlation is 0.743

So the property is real and strong - it is simply a property of the *population*, not of any single instrument. The gate now checks the fleet median.

The lesson generalises: a gate whose verdict depends on which sample it happened to draw is not testing what it claims to test.

---

## 5. Station Disagreement Now Has Two Causes

| Source | Mean spread |
|---|---|
| Spatial only (Phase 3) | 1.62% |
| Spatial plus instruments | 3.08% |
| Usable threshold (`20 §11`) | 8% |

Instrument error roughly doubles the apparent disagreement between weather stations while staying comfortably inside the range that `20 §4.4` treats as usable for performance-ratio filtering.

This matters analytically: an analyst seeing stations disagree cannot immediately tell whether the cause is a cloud between them or a drifting instrument. Distinguishing the two requires looking at whether the disagreement persists (calibration) or moves with the wind (spatial) - which is exactly the discrimination SCN-068 and SCN-093 are built on.

---

## 6. Fleet Stability Under Change

Each instrument draws its parameters from an **independent substream keyed by asset and quantity**, not from a single sequential generator.

Without that, instrumenting one additional field would silently recalibrate every other sensor in the plant, and two datasets differing only in scope would not be comparable. A test asserts that a three-sensor fleet and a one-sensor fleet built from the same seed give sensor A identical calibration.

The sensor fleet table is itself **ground truth**: it records exactly how each instrument is wrong, which is what makes an analyst's calibration estimate scoreable rather than merely plausible.

---

## 7. Downstream Document Updates Required

- `06 §8`: the six effects, their order, and the temperature-offset-not-gain distinction
- `11 §11`: measured fields now genuinely differ from truth; classification is load-bearing
- `13 §5`: add `sensor_state` to the truth schema, populated from the fleet table
- `20 §11`: record the observed 3.08% station spread against the 8% threshold
