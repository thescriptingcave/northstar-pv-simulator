# 10 - Scenario Catalog

**Version 2.0** - supersedes v1.0. Adds tracker, market, and attribution-ambiguity scenarios. Change log in §11.

## 1. Scenario Design Standard

Each scenario requires:

| Field | Notes |
|---|---|
| Scenario ID | Stable, referenced by `scenario_instances` |
| Name | |
| Objective | The analytical question it exists to enable |
| Prerequisites | Required plant or environmental state |
| Start rule | Scheduled, probabilistic, or condition-triggered |
| Duration | Fixed, distribution, or condition-terminated |
| Affected assets | Explicit or selection rule |
| Environmental conditions | Required or induced |
| State changes | Per `08` |
| Telemetry effects | Which signals move, by how much, in what order |
| Events and alarms | Per `12` |
| Expected production impact | Magnitude and duration |
| **Loss cause code** | **New in v2.0** - per `18 §5.2` |
| **Expected revenue impact** | **New in v2.0** - monetized per `18 §5.3` |
| **Cost impact** | **New in v2.0** - per `18 §6.2` |
| **Availability treatment** | **New in v2.0** - counted or excluded per `18 §7.1` |
| Recovery | Sequence and duration |
| Validation assertions | Automated, per `15 §5` |
| Expected detection method | How an analyst should find it |

The four new fields exist because a scenario without an economic consequence and a contractual treatment is only half specified. Two scenarios with identical energy impact can have wildly different revenue impact depending on when they occur, and different contractual treatment depending on cause.

## 2. Baseline Scenarios

Emergent from real weather rather than scheduled - see `06 §4.3`. These are **selected** from the real record, not injected.

| ID | Name | Selection criterion |
|---|---|---|
| SCN-001 | Clear summer day | High daily `kt*`, June-August |
| SCN-002 | Clear winter day | High daily `kt*`, December-February |
| SCN-003 | Overcast day | Low daily `kt*`, low variance |
| SCN-004 | Passing clouds | Mid `kt*`, high 1-min variance |
| SCN-005 | Rapid cloud ramps | High count of >50%/min POA excursions |
| SCN-006 | Hot clear day | Ambient > 40 C, high `kt*` |
| SCN-007 | High-wind cooling day | Wind > 12 m/s, high `kt*` |
| SCN-008 | **Dust event** (new) | Sharp soiling step, low precipitation |
| SCN-009 | **Cold clear morning** (new) | Ambient < -5 C at sunrise - Voc margin condition |

**Why selection rather than injection matters.** An injected clear day carries the simulator's assumptions about what a clear day looks like. A selected clear day carries reality's. The catalog stores the selection query, not the data.

## 3. Performance Scenarios

| ID | Name | Loss code | Monetized |
|---|---|---|---|
| SCN-020 | Midday inverter clipping | `LOSS_CLIPPING` | No - design |
| SCN-021 | Plant export curtailment | `LOSS_CURTAIL_*` | Per reason |
| SCN-022 | Progressive soiling | `LOSS_SOILING` | Yes |
| SCN-023 | Rain cleaning event | Recovery | - |
| SCN-024 | Long-term module degradation | `LOSS_DEGRADATION` | No - expected |
| SCN-025 | Localized shading / mismatch | `LOSS_MISMATCH` | No |

## 4. Tracker Scenarios - NEW in v2.0

Enabled by DR-004. Stuck-tracker detection is among the highest-value real-world PV analytics and did not exist in the v1.0 fixed-tilt design.

| ID | Name | Description |
|---|---|---|
| SCN-026 | Stuck tracker | Row-block freezes at a fixed angle. Output diverges from peers in a time-of-day-dependent pattern - matching peers at the frozen angle's optimal hour, diverging increasingly away from it |
| SCN-027 | High-wind stow | Trackers move to stow at wind > 20 m/s. Sharp plant-wide output drop with high irradiance, no fault, full recovery. Superficially resembles curtailment |
| SCN-028 | Tracker misalignment | Slow angular drift. Gradual, small, persistent underperformance against peers - a change-point detection problem |
| SCN-029 | Spurious stow | Faulty anemometer triggers stow at normal wind. Peer trackers unaffected. Sensor fault causing physical response - crosses the `09 §7` boundary |

SCN-026 is the signature case worth emphasizing. A stuck tracker produces a **symmetric U-shaped deviation** from peers across the day, which is entirely distinguishable from a soiling or degradation deficit (flat proportional) and from an inverter derate (irradiance-dependent). Distinguishing these three from daily normalized output curves is a complete analytics exercise.

SCN-029 is deliberately nasty: a sensor fault causes a real physical output change. It violates the naive rule "sensor faults don't change production," which is a rule the dataset should teach and then complicate.

## 5. Fault Scenarios

| ID | Name | Loss code | Availability |
|---|---|---|---|
| SCN-040 | Single inverter trip | `LOSS_INV_OUTAGE` | Counted |
| SCN-041 | Repeated inverter restart | `LOSS_INV_OUTAGE` | Counted if beyond transient threshold |
| SCN-042 | Inverter thermal derating | `LOSS_INV_THERMAL` | Counted |
| SCN-043 | String group outage | `LOSS_DC_OUTAGE` | Counted |
| SCN-044 | Combiner imbalance | `LOSS_DC_OUTAGE` | Counted |
| SCN-045 | Transformer overheating | `LOSS_TRANSFORMER` | Counted |
| SCN-046 | Transformer trip / block outage | `LOSS_BLOCK_OUTAGE` | Counted |
| SCN-047 | Grid outage | `LOSS_GRID` | Excluded |
| SCN-048 | Breaker trip | `LOSS_BLOCK_OUTAGE` | Counted |
| SCN-049 | **DC overvoltage on cold morning** (new) | `LOSS_INV_OUTAGE` | Counted |

SCN-049 uses the thin cold-temperature Voc margin from `03 §5`. It occurs only on cold clear winter mornings, is seasonally clustered, and is invisible for most of the year - a good test of whether an analyst investigates rare conditional faults or only frequent ones.

**Transient rule (per `20 §10`):** SCN-041 restarts within 15 minutes are transients, not failures, and do not count toward MTBF. The threshold is configurable and recorded. Whether a given restart sequence crosses it is a real judgement call the data should force.

## 6. Market Scenarios - NEW in v2.0

Enabled by `18`. These have no physical cause and cannot be detected without the price join.

| ID | Name | Description |
|---|---|---|
| SCN-029E | Economic curtailment | Controller curtails at `P_rt < -$27.50`. High irradiance, no fault, sharp output drop. **Requires price join to attribute** |
| SCN-030 | Congestion curtailment | ERCOT dispatch instruction under local transmission constraint. Similar signature, different compensability |
| SCN-031 | Scarcity event | Extreme positive prices. No physical effect, but multiplies the cost of any coincident outage by 10-50x |
| SCN-032 | Sustained negative price period | Multi-hour, spring, high wind. Extended curtailment; revenue impact of an outage during this window is **positive** |

SCN-032 produces the most counterintuitive result in the dataset: an equipment failure that made money. Any analyst pipeline that reports lost revenue as unconditionally positive will be wrong here, and finding out why is the point.

## 7. Data Quality Scenarios

| ID | Name | Alters physical truth? |
|---|---|---|
| SCN-060 | Missing inverter telemetry | No |
| SCN-061 | Stuck irradiance sensor | No |
| SCN-062 | Temperature sensor drift | No |
| SCN-063 | Telemetry spikes | No |
| SCN-064 | Communications outage | No |
| SCN-065 | Duplicate records | No |
| SCN-066 | Timestamp skew | No |
| SCN-067 | **Pyranometer soiling** (new) | No - but biases PR calculation |
| SCN-068 | **Weather station disagreement** (new) | No |

SCN-067 is subtle and important: a soiled pyranometer under-reads irradiance, which makes PR appear artificially **high**. An analyst chasing a suspiciously good PR is doing the right thing, and the answer is a dirty sensor rather than a good plant.

SCN-068 pushes station spread above the 8% threshold in `20 §11`, which should cause the affected intervals to be filtered from PR calculations per `20 §4.4`. Whether the analyst applies that filter is itself observable.

## 8. Maintenance Scenarios

| ID | Name | Availability treatment |
|---|---|---|
| SCN-080 | Planned inverter maintenance | Excluded, within 0.5% annual allowance |
| SCN-081 | Block maintenance | Excluded, within allowance |
| SCN-082 | Module / string cleaning | Excluded; resets soiling per `06 §7.2` |
| SCN-083 | Sensor replacement / calibration | Excluded; resets sensor drift |
| SCN-084 | **Maintenance exceeding allowance** (new) | Counted once allowance is exhausted |

SCN-084 makes the allowance in `18 §7.1` a live constraint rather than a footnote. Scheduling maintenance is a real optimization: excluded hours are free until the allowance runs out, at which point they count against the guarantee.

## 9. Attribution-Ambiguity Scenarios - NEW in v2.0

The most valuable and most deliberately difficult category. Each has a defensible answer and a naive answer that differ.

| ID | Name | Ambiguity |
|---|---|---|
| SCN-090 | Disputed availability event | Inverter trips during a grid voltage excursion. Grid-caused (excluded) or equipment-caused (counted)? Telemetry supports both readings |
| SCN-091 | Soiling versus degradation | Slow output decline with no rain. Is it recoverable soiling or permanent degradation? Only the next rain event resolves it |
| SCN-092 | Derating versus curtailment | Inverter output below available power. Thermal derate or commanded setpoint? Requires `commanded_power_kw` to distinguish |
| SCN-093 | Sensor drift versus real degradation | Irradiance sensor drifts high; PR appears to decline. Is the plant degrading or is the reference wrong? Cross-station comparison resolves it |

These exist because real operational analytics is mostly disambiguation, not detection. A dataset where every anomaly has one obvious cause teaches detection and nothing else.

## 10. Compound Scenarios

V1 datasets must include overlapping causes. Compound scenarios prevent analytics from becoming unrealistically easy.

| Combination | Why it is hard |
|---|---|
| Hot day + clipping + inverter thermal derating | Three simultaneous plateau-like signatures |
| Cloudy day + inverter outage | Outage hidden by low production |
| Soiling + pyranometer soiling | Errors partially cancel; PR looks normal while output declines |
| Curtailment during high irradiance | Looks like a fault |
| Transformer thermal issue during high loading | Cause and consequence are collinear |
| **Stuck tracker + soiling** (new) | Two persistent underperformance signals with different time-of-day shapes |
| **Economic curtailment + concurrent inverter fault** (new) | Fault is masked by curtailment; loss attribution must separate them |
| **Scarcity event + block outage** (new) | Maximum financial impact; tests whether cost ranking differs from energy ranking |

## 11. Scenario Scheduling

| Mode | Use |
|---|---|
| Selected from real record | Baseline scenarios (§2) |
| Explicitly scheduled | Maintenance, controlled test cases |
| Probability-based | Random equipment faults, per-asset failure rates |
| Condition-triggered | Thermal derating, stow, economic curtailment, cold Voc |
| Recurring | Cleaning campaigns, planned outages |
| Duration-based | Fixed-repair-time faults |
| Recovery-condition based | Faults clearing when conditions normalize |

Condition-triggered scenarios are preferred wherever physically defensible. They produce realistic clustering and correlation with weather and market state that scheduled scenarios cannot, and they leave no scheduling artifact for an analyst to detect instead of the phenomenon.

Historical generation must reproduce identical scenario schedules from the `fault_schedule` and `maintenance_schedule` seed substreams per DR-013.

## 12. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Scenario standard | 14 fields | 18 fields - adds loss code, revenue impact, cost impact, availability treatment |
| Baseline scenarios | Implied synthetic | Selected from real weather record |
| New baselines | - | SCN-008 dust, SCN-009 cold morning |
| Tracker scenarios | None | SCN-026 to SCN-029 |
| Market scenarios | None | SCN-029E, SCN-030 to SCN-032 |
| Attribution ambiguity | None | SCN-090 to SCN-093 |
| Fault additions | - | SCN-049 cold DC overvoltage |
| Data quality additions | - | SCN-067 pyranometer soiling, SCN-068 station disagreement |
| Maintenance additions | - | SCN-084 allowance exhaustion |
| Compound scenarios | 5 examples | 8, including financial and tracker combinations |
| Transient rule | Absent | 15-minute threshold, excluded from MTBF |


## 13. Scheduling Parameters - as implemented

| Scenario class | Rate | Duration |
|---|---|---|
| Inverter trip | 0.6 per plant-day | Log-normal, 10-600 min |
| String outage | 0.2 per plant-day | Log-normal, 20-900 min |
| Stuck tracker | 0.25 per plant-day | Log-normal, 60-1400 min |
| Transformer trip | 0.05 per plant-day | Log-normal, 30-1400 min |
| Data-quality defect | 0.16 **per asset** per day | Log-normal, 3-2880 min |

**Faults are scheduled during daylight only.** Uniform scheduling across 24
hours put most trips in darkness: five faults over a week cost 0.01% of export
and left no telemetry signature. See `09 §9`.

**Defect rate scales with fleet size.** A flat plant-wide rate corrupted 0.017%
of samples - an order of magnitude below a real SCADA system. Per-asset rating
gives 0.183%, inside the `20 §11` target.

## 14. Open Scenario Decision: POI Limit

The point-of-interconnection limit currently equals AC nameplate, and with
losses downstream it **never binds** - so interconnection-limited curtailment is
exercised only in tests.

Many real plants are built with POI capacity *below* inverter nameplate, making
meter-side clipping a routine daily event rather than a rarity. If the dataset
should exhibit it as a normal-operations phenomenon, `poi_export_limit_kw` is
the single parameter to lower. Recorded as a scenario decision, not a defect.
