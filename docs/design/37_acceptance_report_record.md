# 37 - Dataset Acceptance Report

`15 §12` specified a fourteen-section acceptance report. It had never been built. This record covers building it and running it against a real dataset.

```
VERDICT: ACCEPTED  (25 checks, 0 failures)
```

---

## 1. What the Report Is For

`15 §13`:

> A dataset is not accepted because it looks realistic on a graph. It is accepted because it satisfies documented physical, temporal, relational, statistical, financial, and scenario-specific checks.

The report makes that decision **reviewable**. A verdict with no itemised evidence is an opinion; one listing every check with its measured value can be argued with.

Two design properties matter more than the section list:

**Every number is measured from the dataset**, not carried forward from the run that produced it. A report built from in-memory state validates the *simulator*; this validates the *artefact a recipient receives*, including anything the export or round trip damaged.

**Failures are itemised, never summarised.** "3 checks failed" is not actionable. Each failure names its section, check, measured value and cause.

---

## 2. It Rejected the First Dataset

Two checks failed on a dataset that was, in fact, fine. **Both checks were wrong.**

### 2.1 An invariant belongs to one tree

`ac_within_nameplate` reported **15,562 exceedances** reaching 2,531 kW against a 2,500 kW cap.

The same dataset had **zero exceedances in truth**.

The difference is the sensor layer doing its job. A power sensor with a calibration gain reading a clipped inverter legitimately reports above nameplate - that is what measurement error *is*. Physical truth must respect the cap exactly; measured telemetry must not be required to.

The check now runs against the truth tree, and a separate check bounds measured output by **what the sensor model permits** rather than by the cap:

```
  [PASS] ac_within_nameplate_truth        0 exceedances
  [PASS] measured_ac_within_sensor_error  peak 2,531.3 kW, +1.25% of cap
```

This generalises. Any invariant asserted in the design must be evaluated against the tree where it holds, and a report that checks truth invariants on analyst data will reject good datasets forever.

### 2.2 An ordering that was never an invariant

`effective_to_dc_correlation` failed because effective irradiance correlated with DC at 0.9883 against POA's 0.9908, and the check required it to be higher.

Measured **in truth**, where sensor noise cannot be blamed: POA 0.9927, effective 0.9921. The ordering fails on clean data.

Effective irradiance carries rear-side bifacial gain that varies with tracker geometry, adding variance that does not map linearly onto DC power. The assumption was simply wrong. The ordering assertion is removed; both correlations are still checked against an absolute floor.

---

## 3. Sections, as Built

| Section | Representative content |
|---|---|
| Provenance | Config version, site, pvlib and Python versions, nameplate, CEC keys |
| Volume | Rows by stream, time range, size on disk, projected annual |
| Completeness | Availability, duplicate keys, timestamp monotonicity |
| Distributions | POA, cell temperature, AC power ranges |
| States | Distribution by state, plus state/telemetry consistency |
| Scenarios | Instances by scenario ID |
| Events | Defects by kind with flagged share |
| Physics | Night generation, nameplate (both trees), peak DC fraction |
| Statistics | POA-to-DC correlation, fleet irradiance spread |
| Energy | Raw versus 5-minute aggregate reconciliation |
| Financial | Capture rate, energy revenue, gross margin |
| KPIs | PR, corrected PR, capacity factor, intervals filtered |
| Truth separation | No truth in the analyst tree; analyst data is measured |
| **Verdict** | **ACCEPTED / REJECTED with every failure itemised** |

Selected measured values from the canonical run:

```
  night_generation              max -0.700 kW
  state_telemetry_consistent    0 violations
  unflagged_share               50%
  raw_vs_aggregate              1.23e-06 relative
  performance_ratio             0.7890
  performance_ratio_corrected   0.9566
  capture_rate                  26.0%
  analyst_tree_is_measured      979.2515 vs truth 981.1542 kW
```

The `Recovery` section from `15 §12` is **not implemented**: T7 recovery needs a multi-year dataset, and the degradation result lives in `35_degradation_recovery_record` rather than here.

---

## 4. The Report Must Be Able to Fail

A verdict generator that always passes is worse than none - it converts an unchecked dataset into an apparently checked one.

Tests assert the rejection path directly: that a failing check flips the verdict, and that the rendered output names the section, check and cause rather than a count.

---

## 5. Usage

```
make dataset
uv run northstar-sim accept --dataset datasets/canonical --run-id curriculum \
    --report datasets/canonical/acceptance.csv
```

Exit code is zero when accepted, one when rejected, so it can gate a pipeline. The CSV is stored alongside the dataset it describes.

---

## 6. Downstream Document Updates Required

None outstanding. `15 §12` is implemented as specified except the `Recovery` section, which is noted above and recorded in `35`.


---

## 7. Amendment: Seasonal Calibration (post-v1)

Generating a **winter** dataset - December, 70% clear-sky, 8 C ambient, plant
age five years - rejected it on four checks. All four were bands calibrated
against the June dataset the report was developed on.

| Check | Winter value | Old band | New band |
|---|---|---|---|
| `capacity_factor_ac` | 0.1426 | 0.15 - 0.60 | **0.03 - 0.60** |
| `performance_ratio` | 1.0087 | 0.60 - 0.95 | **0.55 - 1.15** |
| `performance_ratio_corrected` | 0.9856 | must exceed raw PR | **direction derived from mean cell temperature** |
| `fleet_poa_spread` | 30.9% | 0.001 - 0.30 | **0.001 - 0.60** |

A fifth, `capture_rate`, failed on a *summer* dataset at 6.2% against a 0.2
floor - a cloudless week drives penetration to maximum every midday and
collapses the generation-weighted price. Floor reduced to zero; the meaningful
bound is the upper one.

The corrected-PR check is the most instructive. It asserted a relationship that
holds only where cells are above 25 C, and the fix was not a wider band but a
**check that reads the direction off the data**.

Both seasons now pass 25/25, as does the original development dataset.

**The lesson generalises beyond this report.** A check that only accepts the
conditions it was written against is testing its fixture. Every band here now
carries the range it is valid across, and `20 §15` records the same for the
normative definitions.

## 8. Amendment: The Report Is Reachable

The acceptance report was only ever run against datasets produced by the gates.
`northstar-sim generate` (see `41`) makes arbitrary datasets easy to produce,
which is how the seasonal calibration problem surfaced at all.
