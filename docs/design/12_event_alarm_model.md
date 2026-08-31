# 12 - Event and Alarm Model

**Version 2.0** - amended after Phases 5, 8 and 10. State transition, fault and data-quality events are now populated.

## 1. Principle

Telemetry describes continuous/periodic state. Events describe discrete occurrences. Alarms describe conditions requiring attention.

They should not be collapsed into one concept.

## 2. Event Fields

Conceptual fields:

- event_id
- event_time
- end_time when applicable
- asset_id
- asset_type
- event_type
- severity
- source
- scenario_id
- fault_code
- message/category
- acknowledged/resolved state where modeled
- simulation run ID

## 3. Event Categories

- state transition
- fault
- maintenance
- grid
- curtailment
- weather
- data quality
- recovery

## 4. Alarm Lifecycle

Potential lifecycle:

NORMAL
-> WARNING
-> ALARM
-> ACKNOWLEDGED
-> CLEARED

V1 does not need human acknowledgement behavior unless analytically useful, but alarm start and clear times are important.

## 5. Severity

Suggested levels:

- INFO
- WARNING
- MAJOR
- CRITICAL

Severity should reflect impact, not random assignment.

## 6. Event/Telemetry Alignment

Event timestamps must align with observable telemetry changes.

For example, if an inverter trips at 13:07:

- event begins at 13:07
- inverter state changes
- AC output falls
- plant output decreases
- availability changes
- fault code appears where appropriate

## 7. Event Windows

The design should support easy analysis of:

- N minutes before event
- event duration
- N minutes after recovery

This is important for root-cause and precursor analysis.


## 8. Populated Event Categories

| Category | Source | Phase |
|---|---|---|
| State transition | Inverter state machine | 5 |
| Fault | Scenario engine | 8 |
| Recovery | Scenario engine | 8 |
| Data quality | Defect injection | 10 |

**State transitions were the first populated category.** Each carries a
timestamp, asset, from-state, to-state and a **reason** - required, because a
transition without one cannot be joined to an event record.

**Every scenario produces two events**, onset and clearance. A test asserts the
count is exactly twice the instance count: a scenario that begins and never
clears is an unclosed event, and an analyst computing outage duration from the
events table would get an unbounded answer.

## 9. Events Reconstruct from Telemetry, and Should Agree

Outage duration is recoverable from the state series by gaps-and-islands
(exercise EX-401) **and** from the events table. The two should agree, and where
they disagree one of them is wrong.

That redundancy is deliberate. It is why `§1` insists telemetry and events not
be collapsed into a single concept.

## 10. Quality Flags Are Events About Measurement, and They Are Fallible

The `quality` field is itself a measured artefact. **Roughly half of injected
defects carry no flag at all**: drift is flagged 5% of the time, stuck sensors
30%, gaps and communications outages 100%.

A frozen instrument does not know it has frozen. A quality column that flagged
everything would be a complete oracle, and every data-quality exercise would
collapse into `WHERE quality = 'GOOD'`.
