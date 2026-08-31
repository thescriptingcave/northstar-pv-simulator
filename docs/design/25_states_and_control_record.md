# 25 - State Machines and Control Record (Phase 5)

## Result: PASSED

```
  [PASS] only_legal_transitions       120 transitions, 0 illegal
  [PASS] startup_sequence_observed    STANDBY -> STARTING -> RUNNING present
  [PASS] standby_consumes_only        max standby output -0.750 kW
  [PASS] running_generates            min running output 29.72 kW
  [PASS] discriminating_signals       available, commanded and curtailed power present
  [PASS] backtracking_visible         sunrise |angle| 7.1 deg against peak 60.0 deg
  [PASS] plant_state_follows_fleet    observed [DAWN_STARTUP, NIGHT, NORMAL, PARTIAL_OUTAGE]
```

---

## 1. A State Model Must Constrain, Not Label

Two properties are enforced rather than assumed, because **neither is a hard error anywhere in the physics** and so nothing else in the pipeline would catch a violation:

**Only legal transitions occur.** An inverter reaches `RUNNING` through `STARTING`, never directly. Without enforcement the state column becomes decorative, and an analyst filtering on it gets wrong answers for reasons invisible in the data.

**State and telemetry agree.** A `STANDBY` inverter reporting generation is perfectly consistent with every equation in the model. It is only wrong against the state machine, which is why `08 §8` demands the check.

Every transition carries a timestamp **and a reason**. A transition without a reason cannot be joined to an event record, and `08 §5` requires that join.

---

## 2. Startup and Shutdown

| Parameter | Value | Why |
|---|---|---|
| Startup dwell | 3 minutes in `STARTING` | Real units perform grid synchronisation and insulation checks. The delay is what makes sunrise *observable* at 1-minute cadence rather than instantaneous |
| Starting output | 35% of available | Output ramps rather than stepping - realistic, and a distinguishable signature |
| Shutdown hysteresis | 60% of startup threshold | Without a gap an inverter chatters between states at dawn and dusk |

Observed on a clear June day:

```
  05:00Z  STANDBY   poa=   0.0   ac=  -0.8 kW  | insufficient irradiance
  12:21Z  STARTING  poa=  21.5   ac=  19.6 kW  | POA 21 W/m2 above startup threshold
  12:24Z  RUNNING   poa=  27.7   ac=  74.5 kW  | startup sequence complete
  01:36Z  STANDBY   poa=  10.6   ac=  -0.8 kW  | POA below shutdown threshold
```

State distribution across 40 inverters: RUNNING 54.9%, STANDBY 44.9%, STARTING 0.2%. 120 transitions, zero illegal.

**Standby output is negative**, exactly the configured parasitic draw. A sleeping inverter consumes; it does not sit at zero. This appears in telemetry as small negative AC power overnight and is a good check on whether an analyst's daylight filter is correct.

---

## 3. Available Versus Commanded Power

This is the distinction the whole control layer exists to create.

The plant controller enforces the point-of-interconnection limit by distributing per-inverter setpoints. The limit applies at the **meter**; the setpoints apply at the **inverters**; losses sit between them. Available inverter power is therefore converted to its expected export contribution before the limit is applied, and the required reduction shared **pro rata**.

Pro rata matters analytically. Real controllers use various strategies, but a uniform reduction keeps the per-asset signature uniform, so curtailment is not mistaken for one inverter underperforming.

An inverter held below what it could produce is relabelled **`CURTAILED`**, not left as `RUNNING` at lower output. Without the relabel, curtailment is indistinguishable from underperformance in the state column - the exact discrimination `07 §9` exists to enable.

Three signals now exist on every inverter: `available_power_kw` (simulator truth), `commanded_power_kw` (the setpoint), and `curtailed_power_kw` (the difference where a setpoint bound). Phase 9 adds the economic curtailment rule on top; the mechanism is already here.

### Note: the POI limit does not currently bind

With the POI limit set equal to AC nameplate and losses downstream of the inverters, plant export never reaches the limit - so curtailment is exercised only in tests, with a reduced limit.

This is worth recording rather than treating as a defect. Many real plants are built with **POI capacity below inverter nameplate**, which makes clipping-at-the-meter a routine daily event rather than a rarity. If the dataset should exhibit interconnection-limited curtailment as a normal-operations phenomenon, the POI limit is the parameter to lower. Deferred as a scenario decision, not a code change.

---

## 4. Backtracking Is Visible

At low sun a backtracking tracker rotates back toward horizontal to avoid row-to-row shading. The signature is that **the extreme angle occurs mid-morning, not at sunrise**.

Measured: sunrise |angle| 7.1 degrees against a peak of 60.0. An unbacktracked tracker would sit at its limit at first light.

The angle profile across the day is the characteristic shape: near 0 at sunrise, out to -60 mid-morning, through 0 at solar noon, +60 mid-afternoon, back toward 0 at sunset. This is a real analytical exercise - a stuck tracker produces a *symmetric U-shaped* deviation from peers, distinguishable from soiling (flat proportional) and thermal derate (irradiance-dependent).

---

## 5. Plant State

Derived from the fleet rather than asserted independently: `NIGHT` when nothing runs, `DAWN_STARTUP` while any inverter is starting, `NORMAL` when all run, `PARTIAL_OUTAGE` when some run and some do not, `CURTAILED` when any inverter is held below available power.

`PARTIAL_OUTAGE` already appears on a clean day, for two minutes at dawn - inverters cross the startup threshold at slightly different times because they see slightly different irradiance. That is the spatial layer showing up in the state machine, and it is correct.

---

## 6. States Defined but Not Yet Driven

`DERATED`, `FAULT` and `MAINTENANCE` are in the enum and the legal-transition map but nothing drives them yet. They arrive in Phases 7 and 8.

Defining them now means the transition map does not have to be reopened when faults land - and more importantly, the legality rules for fault recovery (`FAULT` cannot go directly to `RUNNING`; it passes through `OFF`, `STANDBY` or `MAINTENANCE`) are already tested.

---

## 7. Downstream Document Updates Required

- `08`: startup dwell, output fraction and hysteresis values; the reason field requirement
- `11 §5.1`: `available_power_kw`, `commanded_power_kw`, `curtailed_power_kw` and `state_reason` on inverter telemetry
- `12 §2`: state transitions are the first populated event category
- `10`: consider lowering the POI limit below nameplate if interconnection-limited curtailment should be a normal-operations phenomenon
