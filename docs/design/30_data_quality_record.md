# 30 - Data Quality Injection Record (Phase 10)

## Result: PASSED

```
  [PASS] defects_injected                    54 defects across 7 of 7 types
  [PASS] physical_truth_unaltered            plant export and every inverter truth
                                             frame bit-identical
  [PASS] measured_telemetry_corrupted        26 of 43 measured frames differ
  [PASS] corruption_within_target            0.183% of samples flagged
  [PASS] quality_flags_are_fallible          50% of defects carry no flag
  [PASS] stuck_sensor_freezes_only_reporting measured 1 distinct value, truth 33
  [PASS] gaps_are_nan_not_zero               0 zero-filled samples inside gaps
```

---

## 1. The Property the Phase Exists For

    A stuck AC-power sensor reports constant output while the actual inverter
    continues changing. An inverter failure changes actual physical output.

Measured: during a stuck-sensor defect on `NORTHSTA-BLK06-INV4`, the reported series held **1 distinct value** while truth carried **33**. Plant export and every inverter truth frame were bit-identical between the clean and corrupted runs.

This is guaranteed structurally rather than by discipline. `apply_defects` copies before touching anything; `run_plant` keeps truth, measured and corrupted as separate objects; and tests assert frame equality and non-mutation rather than numeric tolerances.

**The pipeline order is the phase.** Faults (Phase 8) act on truth. Sensors (Phase 6) determine how accurately truth was measured. Defects (here) determine what was reported. Collapsing any two makes an equipment fault indistinguishable from a data fault, and every exercise in `02 §9` becomes unanswerable.

---

## 2. Quality Flags Are Deliberately Fallible

A quality column that flags every defect is a complete oracle, and every data-quality exercise collapses into `WHERE quality = 'GOOD'`.

| Defect | Flag rate | Why |
|---|---|---|
| Gap, comms outage | 100% | An absent sample is unambiguous |
| Spike | 65% | Range checks catch the obvious ones |
| Duplicate | 50% | |
| Timestamp skew | 40% | |
| Stuck | 30% | Stale detection needs a long enough run |
| **Drift** | **5%** | Reports entirely plausible values throughout |

Observed: **50% of injected defects carried no flag at all.** A frozen instrument does not know it has frozen. Trusting the flag rather than testing the data is a mistake the dataset now permits, as `11 §12` requires.

Drift at 5% is the hardest case by design: it is in-band, monotonic and slow. Detecting it requires a second reference - which is exactly why three weather stations exist.

---

## 3. Defect Rate Must Scale With Fleet Size

The first schedule used a flat plant-wide rate of 1.2 defects per day. Spread across the fleet, that corrupted **0.017% of samples** - an order of magnitude below what a real SCADA system produces, and far too sparse for any exercise to have material to work with.

The rate is now **per asset per day** (default 0.02), so it scales with instrumentation. Communications outages are also weighted up, because they take every field on an asset at once and single-signal defects barely move the fleet-wide availability metric an analyst actually computes.

Result over a simulated week: 54 defects across 29 assets, **0.183% of samples** carrying a non-`GOOD` flag - inside the `20 §11` targets of availability above 99% and flagged fraction below 1%.

---

## 4. Missing Means NaN, Never Zero

Zero-filled irradiance is indistinguishable from night. It corrupts every daylight filter downstream and cannot be recovered once written.

The gate counts zero-filled samples inside every gap and comms outage. Result: **0**.

This is the same rule the acquisition layer enforces in `19 §5.3`, applied at the other end of the pipeline. It is worth stating twice because the failure is silent in both places.

---

## 5. Duplicates Go to a Staging Frame

Duplicate rows cannot exist in a table whose primary key forbids them. Doc `13 §11` resolves this by injecting them into a **staging** frame without the constraint.

Finding and resolving duplicates before load is the exercise, and it is how real ingestion pipelines work: the source is permissive, the destination is not, and the reconciliation is somebody's job.

---

## 6. Seven Defect Types

| Scenario | Behaviour |
|---|---|
| SCN-060 gap | Samples become NaN |
| SCN-061 stuck | Value frozen; truth continues to vary |
| SCN-062 drift | Slow, monotonic, in-band |
| SCN-063 spike | Isolated implausible readings, roughly 5% of the window |
| SCN-064 comms outage | Every field on the asset goes missing at once |
| SCN-065 duplicate | Rows repeated in a staging frame |
| SCN-066 timestamp skew | Values shifted against the true time base |

Timestamp skew is the subtlest: each asset's own series still looks entirely reasonable, and only cross-asset correlation collapses. That makes it findable exactly the way the spatial layer's advection lag is findable, and by the same technique.

---

## 7. A Bug Worth Recording

`corrupted.inverters.get(id) or corrupted.weather.get(id)` raises. Pandas refuses to evaluate the truthiness of a DataFrame, so `or` on a possibly-`None` frame is never safe. Replaced with an explicit `is None` check.

---

## 8. Downstream Document Updates Required

- `02 §9`: the flag-detection rates, and that unflagged defects are the majority case
- `11 §12`: quality flag values and their deliberate fallibility, now implemented
- `13 §5`: add `defect_schedule` to the truth schema alongside `scenario_instances`
- `20 §11`: record the observed 0.183% flagged share against the 1% target
