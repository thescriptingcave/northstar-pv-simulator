# 28 - Fault and Scenario Engine Record (Phase 8)

## Result: PASSED

```
  [PASS] scenarios_injected             7 instances across 3 scenario types
  [PASS] faults_move_telemetry          export shortfall 0.107% of clean production
  [PASS] faults_are_localised           7 of 40 inverters affected
  [PASS] faulted_assets_produce_nothing 0 samples with FAULT state and positive output
  [PASS] events_bracket_each_scenario   14 events for 7 instances
  [PASS] durations_are_varied           duration range 10-310 min, ratio 31.0x
  [PASS] reliability_computable         MTBF 15.4 h, MTTR 1.73 h, 1 transient excluded
  [PASS] faults_occur_in_daylight       7/7 began during operating hours
```

---

## 1. Ordering Is the Whole Point

Faults are applied to **physical truth, before the sensor layer**.

That ordering is what separates an equipment fault from a data fault. An inverter trip changes what the plant *produced*; a stuck sensor changes only what was *reported*. Reversing the order would make the two indistinguishable, collapsing the distinction `09 §7` exists to preserve and rendering every data-quality exercise in Phase 10 meaningless.

The pipeline is now: physics -> states and control -> **faults** -> sensors -> measured telemetry.

---

## 2. The Bug: Faults at Night Are Not Faults

The first schedule picked start times uniformly across 24 hours. Result over a simulated week:

- 5 injected faults
- **0.01%** of export lost
- **zero** inverters showing FAULT minutes

Most trips landed in darkness, where output is already zero. The events table was populated and the telemetry was untouched - precisely the failure mode the phase exists to avoid. A fault that only appears in an events table teaches an analyst to read the events table.

Faults now begin during operating hours only. This is also more realistic: trips are driven by electrical and thermal stress, which is a daytime phenomenon. After the fix, over the same week:

- 7 instances across 3 scenario types
- **0.107%** of export lost
- 7 of 40 inverters affected, peers untouched

---

## 3. Signatures, Not Just Outages

| Scenario | Severity | Signature |
|---|---|---|
| SCN-040 inverter trip | 1.0 | Output to zero, state FAULT, fault code set. Peers unaffected |
| SCN-043 string outage | 1/12 | Output drops **by a step** and keeps running. Distinguishing this from soiling requires noticing it arrived at once |
| SCN-026 stuck tracker | frozen angle | Symmetric U-shaped deviation from peers across the day |
| SCN-046 transformer trip | 1.0 | Every inverter behind the transformer stops, regardless of its own health |

The stuck tracker is the most analytically valuable. It matches peers near the frozen angle's optimal hour and diverges increasingly away from it - a shape distinguishable from soiling (flat, proportional) and from thermal derating (irradiance-dependent). Telling those three apart from daily normalised output curves is a complete exercise on its own.

**Implementation note, recorded rather than hidden:** the stuck tracker applies a cosine of the angular error rather than re-running transposition. This is a geometric approximation. It captures the *shape*, which is the analytically important part, without a second pvlib pass per fault instance. If quantitative tracker-loss accuracy later matters, this is the place to revisit.

---

## 4. Durations Must Be Long-Tailed

Durations are drawn log-normally, spanning 10 to 310 minutes in the test week - a 31x range.

A fixed duration would make outage length uninformative and MTTR a constant. The long tail reflects reality: most trips clear on an auto-restart, a few need a truck roll, and a handful wait on parts.

---

## 5. Transients Are Not Failures

A restart completing within 15 minutes is a **transient**, excluded from MTBF and MTTR. Counting a four-minute auto-restart as a failure makes both figures unusable.

Operating hours means **daylight** hours. Counting nighttime standby as operating time inflates MTBF by roughly a factor of two.

Measured over the test week: MTBF 15.4 h, MTTR 1.73 h, with 1 transient excluded from 7 instances.

---

## 6. Faults Must Be Local

A trip on one inverter that degraded its peers would destroy peer comparison, which is the primary detection method for everything in Phases 7 and 8.

The gate asserts that some but not all inverters are affected. The one deliberate exception is the transformer trip, which takes its whole block down - and that block-wide correlation is itself the signature distinguishing a transformer fault from four coincident inverter faults.

---

## 7. Ground Truth for Blind Scoring

The schedule is recorded as a table: scenario ID, asset, start, end, severity, cause code, trigger, duration, transient flag.

This is what makes an analyst's reconstruction **scoreable** rather than merely plausible. The blind-analysis success criterion in `01 §8` requires exactly this: inject known faults, have the analyst find them from telemetry alone, and score the result against what was actually injected.

Fault injection is **opt-in** (`inject_faults=False` by default), so the Phase 1 through 7 gates continue to test a fault-free plant.

---

## 8. Downstream Document Updates Required

- `09 §4`: fault signature contract per scenario, including the tracker approximation
- `10`: probabilistic scheduling parameters and the daylight-only constraint
- `12 §3`: fault and recovery event categories are now populated
- `20 §10`: transient threshold and daylight-hours basis confirmed in code
