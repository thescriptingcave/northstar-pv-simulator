# 09 - Failure and Degradation Model

**Version 2.0** - amended after Phases 7 and 8. Derating thresholds, fault scheduling and signature contracts are now implemented.

## 1. Objective

Failures must create identifiable time-series signatures rather than only event records.

## 2. Failure Classes

### PV/DC Side

- string outage
- partial string loss
- connector/fuse failure
- mismatch
- shading
- heavy soiling
- accelerated degradation
- hotspot-like derating abstraction

### Inverter

- trip
- overtemperature
- fan/cooling degradation
- MPPT degradation/failure abstraction
- DC over/undervoltage
- ground-fault abstraction
- intermittent restart cycle

### Transformer/AC

- overheating
- derating
- trip
- breaker opening
- block outage

### Grid

- grid outage
- abnormal voltage
- abnormal frequency
- export limitation
- curtailment command

### Sensors/Data

- stuck value
- bias
- drift
- spike
- noisy measurement
- missing samples
- communications outage

## 3. Gradual Degradation

Long-term degradation must be modeled separately from sudden faults.

Examples:

- module aging
- slowly increasing soiling
- inverter cooling degradation
- sensor drift

These are particularly valuable for trend and change-point analysis.

## 4. Fault Signature Contract

Every fault definition should specify:

- initiating condition
- affected assets
- start time
- duration
- severity
- telemetry precursors
- telemetry during fault
- event/alarm output
- production impact
- recovery behavior
- expected analytical detection method

## 5. Example - Inverter Overtemperature

Possible sequence:

1. high ambient temperature / high loading
2. inverter internal temperature rises
3. high-temperature warning
4. thermal derating begins
5. AC output falls below peer inverters despite similar irradiance
6. trip if critical threshold is reached
7. cooldown
8. restart
9. normal output resumes

This is a complete time-series story, not merely a random outage.

## 6. Example - Soiling

- soiling factor worsens slowly
- normalized DC output declines relative to clean peers
- weather remains otherwise comparable
- rain/cleaning event occurs
- output recovers abruptly or gradually

## 7. Data Fault vs Equipment Fault

The simulator must maintain this distinction.

A stuck AC-power sensor can report constant output while the actual inverter continues changing. An inverter failure changes actual physical output.

This distinction is essential for realistic data-quality analysis.


## 8. Inverter Thermal Derating - as implemented

**The configured onset is an ambient threshold**, as inverter datasheets state
it. Comparing it directly to internal temperature made derating fire at 42 C
ambient, hit its cap, and hold AC output at 1,750 kW instead of 2,500.

The internal onset is the ambient onset plus the full-load temperature rise:
45 + 22 = **67 C**.

| Parameter | Value |
|---|---|
| Ambient onset | 45 C |
| Full-load internal rise | 22 C |
| Derate slope | 1.5% per C above internal onset |
| Maximum derate | 20%, beyond which the unit trips |
| Thermal time constant | 12 minutes |

Resulting frequency:

| Ambient | Internal peak | Derating |
|---|---|---|
| 30 C | 52.0 C | none |
| 40 C | 62.0 C | none |
| 46 C | 68.0 C | 1.5% |
| 52 C | 74.0 C | 10.5% |

No derating below 46 C ambient. West Texas reaches it on the hottest afternoons
only - the correct frequency for the phenomenon: rare, real, and worth
detecting.

**Derating suppresses clipping.** A derating inverter clips *less*, because
derating pulls output below the cap before the cap can bind. Measured: clipping
falls from 66.0 to 26.7 MWh. The two losses are **not additive**.

## 9. Fault Scheduling - as implemented

**Faults begin during operating hours only.** Scheduling uniformly across 24
hours put most trips in darkness, where output is already zero: five injected
faults over a week cost 0.01% of export and moved **no telemetry at all**. The
events table was populated and the plant looked untouched.

This is also more realistic - trips are driven by electrical and thermal stress,
which is a daytime phenomenon.

Durations are drawn **log-normally**, spanning 10 to 310 minutes in a test week.
A fixed duration makes outage length uninformative and MTTR a constant.

**Transient rule.** A restart completing within 15 minutes is a transient, not a
failure, and is excluded from MTBF and MTTR. Operating hours means **daylight**
hours; counting nighttime standby inflates MTBF by roughly a factor of two.

## 10. Fault Signature Contract - as implemented

| Scenario | Severity | Signature |
|---|---|---|
| SCN-040 inverter trip | 1.0 | Output to zero, state `FAULT`, fault code set, peers unaffected |
| SCN-043 string outage | 1/12 | Output drops **by a step** and keeps running |
| SCN-026 stuck tracker | frozen angle | Symmetric U-shaped deviation from peers across the day |
| SCN-046 transformer trip | 1.0 | Every inverter behind the transformer stops, regardless of its own health |

The stuck tracker matches peers near the frozen angle's optimal hour and
diverges increasingly away from it - a shape distinguishable from soiling (flat,
proportional) and thermal derating (irradiance-dependent).

**Approximation, recorded rather than hidden:** the stuck tracker applies a
cosine of the angular error rather than re-running transposition. It captures
the shape, which is the analytically important part, without a second pvlib pass
per fault. If quantitative tracker-loss accuracy later matters, this is the
place to revisit.
