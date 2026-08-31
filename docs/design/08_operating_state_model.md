# 08 - Operating State Model

**Version 2.0** - amended after Phase 5. Dwell times, thresholds and the reason requirement are now implemented and normative.

## 1. Plant States

Suggested plant states:

- NIGHT
- DAWN_STARTUP
- NORMAL
- DERATED
- CURTAILED
- PARTIAL_OUTAGE
- GRID_DISCONNECTED
- MAINTENANCE
- EMERGENCY_SHUTDOWN

## 2. Inverter States

- OFF
- STANDBY
- STARTING
- RUNNING
- DERATED
- CURTAILED
- FAULT
- MAINTENANCE
- SHUTDOWN

## 3. Transformer States

- ENERGIZED
- DERATED
- HIGH_TEMPERATURE
- TRIPPED
- MAINTENANCE
- OFFLINE

## 4. Breaker States

- OPEN
- CLOSED
- TRIPPED

## 5. State Transition Requirements

State changes must:

- have timestamps
- have a reason
- affect telemetry
- optionally generate events/alarms
- obey legal transitions

Example:

STANDBY -> STARTING -> RUNNING

A fault may cause:

RUNNING -> FAULT -> OFF/MAINTENANCE -> STARTING -> RUNNING

## 6. Sunrise Behavior

At sunrise:

- irradiance rises from zero
- DC voltage may become available before substantial power
- inverter moves from standby to startup
- AC power begins after startup conditions are met

This transition should be visible in telemetry.

## 7. Sunset Behavior

At sunset:

- irradiance declines
- DC/AC power falls
- inverter crosses shutdown threshold
- inverter enters standby/off state
- daily energy stops increasing

## 8. State/Telemetry Consistency

Examples:

- FAULT inverter cannot simultaneously report full normal production
- GRID_DISCONNECTED plant cannot report normal grid export
- NIGHT should not produce significant solar power
- MAINTENANCE should create corresponding availability impact when equipment is unavailable


## 9. Startup and Shutdown - as implemented

| Parameter | Value | Why |
|---|---|---|
| Startup dwell | 3 minutes in `STARTING` | Grid synchronisation and insulation checks. The delay is what makes sunrise **observable** at 1-minute cadence rather than instantaneous |
| Starting output | 35% of available | Output ramps rather than stepping - realistic, and a distinguishable signature |
| Shutdown hysteresis | 60% of the startup threshold | Without a gap an inverter chatters between states at dawn and dusk |

Observed on a clear day:

```
  12:21Z  STARTING  poa= 21.5   ac= 19.6 kW  | POA above startup threshold
  12:24Z  RUNNING   poa= 27.7   ac= 74.5 kW  | startup sequence complete
  01:36Z  STANDBY   poa= 10.6   ac= -0.8 kW  | POA below shutdown threshold
```

**Standby output is negative** - exactly the configured parasitic draw. A
sleeping inverter consumes; it does not sit at zero.

## 10. Every Transition Carries a Reason

A state change with no reason **cannot be joined to an event**, and §5 requires
that join. `reason` is a required field on every transition record, not an
optional annotation.

Transitions are extracted from the state series at the moments it changes:
telemetry describes continuous state, events describe discrete occurrences, and
`12 §1` requires they not be collapsed.

## 11. Legality Is Enforced, Not Assumed

`RUNNING` is reachable only through `STARTING`; `FAULT` recovers only via `OFF`,
`STANDBY` or `MAINTENANCE`. Violations are detected by
`northstar_sim.states.validate_transitions`.

This matters because **neither legality nor state/telemetry consistency is a
hard error anywhere in the physics**. A `STANDBY` inverter reporting generation
satisfies every equation in the model. Nothing else in the pipeline would catch
it.

Measured over a full day across 40 inverters: 120 transitions, **zero illegal**.

`PARTIAL_OUTAGE` appears for two minutes at dawn on a clean day - inverters
cross the startup threshold at slightly different times because they see
slightly different irradiance. That is the spatial layer surfacing in the state
machine, and it is correct.
