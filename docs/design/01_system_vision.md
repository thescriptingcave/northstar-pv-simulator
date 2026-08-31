# 01 - System Vision

**Version 2.0** - supersedes v1.0. Change log in §10.

## 1. Mission

Build a configurable PV solar farm simulator that produces realistic historical and near-real-time time-series data suitable for SQL analysis, Python analysis, Grafana visualization, TimescaleDB features, anomaly detection, forecasting, operational analytics, **asset-management financial analytics**, and data-quality exercises.

The simulator is not a full electromagnetic or finite-element solar engineering model. Physical realism is required to the degree necessary to create believable causal and temporal behavior - and no further.

**Change in v2.0:** the simulator is no longer purely synthetic. It is driven by real satellite-derived solar resource data and real wholesale market prices for a real location. Physics is delegated to `pvlib` rather than hand-implemented. What the simulator generates is the *plant* - assets, states, faults, control behavior, sensors, and defects - layered onto a real environment and a real market.

## 2. Primary Product

The primary product is **analysis-ready time-series data**.

The simulator must generate plausible **relationships through time**:

- irradiance and PV production
- ambient temperature and module temperature
- module temperature and conversion efficiency
- DC production and AC production
- inverter loading and inverter temperature
- cloud passage and rapid power ramps across spatially separated assets
- tracker position and time of day
- equipment faults and production loss
- curtailment and clipped or export-limited power
- soiling and gradual performance deterioration, driven by real rainfall
- maintenance and recovery
- sensor faults and data-quality anomalies
- seasonal solar-resource and daylight changes
- **market price and curtailment decisions**
- **energy loss and revenue loss, at the price prevailing when it was lost**

The last two are new in v2.0 and are what make the dataset resemble an operating asset rather than a physics experiment.

## 3. Intended Users

- time-series SQL learner
- data engineer
- SQA / test engineer
- operations analyst
- reliability analyst
- **asset manager / commercial analyst**
- data scientist
- forecasting practitioner
- Grafana / TimescaleDB learner

## 4. Core Design Principles

1. Time-series analysis drives design.
2. Causality matters more than random-looking data.
3. Every important signal needs an analytical purpose.
4. Events must leave measurable telemetry signatures.
5. Normal behavior must be rich enough that abnormal behavior can be detected.
6. Historical data must contain trend, seasonality, cycles, noise, and structural changes.
7. Raw telemetry and derived metrics must remain distinguishable.
8. The simulator must be deterministic given the same configuration, cache version, and seed.
9. Asset hierarchy and IDs must remain stable across a run.
10. Physical constraints must prevent impossible value combinations.
11. Failures and scenarios must be testable.
12. The design must support incremental implementation.
13. **Borrow validated physics rather than implementing it.** (DR-003)
14. **Prefer emergent behavior over scheduled behavior.** A scheduled event leaves a scheduling artifact an analyst can learn to detect instead of the phenomenon. (`06 §4.3`, `10 §11`)
15. **Every lost kWh carries a cause code and a dollar value.** (`18 §1`)
16. **Truth and measurement are separated by database role, not by naming convention.** (DR-014)

## 5. V1 Scope

- one utility-scale PV plant at a locked real location (`03 §2`)
- ten power blocks, forty inverters, 480 combiners
- **single-axis tracking, bifacial modules**
- PV arrays and strings at practical analytical granularity
- transformers and AC collection
- three weather stations
- grid point of interconnection
- **real solar resource data, cached and versioned**
- **stochastic downscaling and spatial cloud field**
- DC and AC production via `pvlib`
- electrical and thermal losses, individually attributed
- clipping and curtailment, including **economically-driven curtailment**
- operational states
- faults, tracker faults, and maintenance
- alarms and events
- data-quality defects
- **real ERCOT market prices**
- **settlement, loss attribution, O&M cost, and contractual guarantee tracking**
- **IEC 61724-1 / ASTM E2848 KPI definitions**
- historical and streaming-style generation
- multiple telemetry cadences

## 6. Explicit Non-Goals for V1

- detailed semiconductor switching simulation
- electromagnetic transient simulation
- individual telemetry for every module
- full SCADA protocol emulation
- power-market bidding or day-ahead offer strategy
- battery energy storage
- multi-site fleet orchestration
- detailed transmission network modeling
- corporate finance: debt, tax, depreciation, IRR, LCOE
- snow modeling

All may become extensions. The settlement structure in `18 §3` and the cost structure in `18 §6` leave room for the financial ones without schema redesign.

## 7. Definition of Design Complete

Before implementation begins, the project must answer:

| Question | Answered in |
|---|---|
| What assets exist? | `03`, `05` |
| How are assets connected? | `03 §6`, `04` |
| Which assets generate telemetry? | `03 §7`, `11 §3` |
| What is the cadence of each signal? | `11 §3` |
| Which signals are measured, derived, truth, or ground truth? | `11 §11` |
| What physical and operational relationships constrain them? | `07`, `08`, `11 §13` |
| What normal temporal patterns exist? | `06`, `10 §2` |
| What failures exist? | `09`, `10` |
| What telemetry signature does each failure create? | `09 §4`, `10` |
| What events and alarms are emitted? | `12` |
| What analyses must the data support? | `02` |
| **Where does the real data come from?** | `19` |
| **What is each KPI, normatively?** | `20` |
| **What is each loss worth?** | `18 §5` |
| What database objects store raw and aggregate data? | `13` |
| What invariants prove the data is valid? | `15` |

**Phase 0 is complete when every row of this table resolves and the `19 §11` acquisition checklist passes.**

## 8. Success Criteria

v1.0 stated one criterion. v2.0 states four, in increasing difficulty:

1. **Detection** - an analyst who does not know the injected scenarios can discover meaningful plant behavior from telemetry.
2. **Attribution** - the analyst can correctly assign discovered anomalies to cause, including distinguishing equipment faults from data faults, clipping from curtailment, and soiling from degradation.
3. **Valuation** - the analyst can rank problems by financial impact and arrive at a ranking that differs from the energy-impact ranking.
4. **Reconstruction** - the analyst's independently-estimated degradation rate, availability, and PR recover the injected ground-truth values within tolerance.

Criterion 4 is the strongest form of validation available: the simulator injects known values, the analyst estimates them blind, and the ground-truth table resolves any disagreement. If the analysis cannot recover the injected value, either the analysis or the simulator is wrong, and it is always worth knowing which.

## 9. What This Is Not

A dataset is not accepted because it looks realistic on a graph. It is accepted because it satisfies documented physical, temporal, relational, financial, and scenario-specific checks (`15 §10`).

Equally, the simulator is not a goal in itself. It exists to produce a dataset that makes someone better at PV time-series analytics. Any design decision that adds simulator complexity without adding analytical capability should be rejected.

## 10. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Data origin | Fully synthetic | Real resource and market data, cached |
| Physics | Implied hand-rolled | pvlib, delegated |
| Array type | "fixed-tilt or single-axis" | Single-axis tracking, bifacial |
| Financial layer | Absent | Settlement, loss attribution, cost, guarantees |
| KPI definitions | Implied | IEC 61724-1 / ASTM E2848 |
| Design principles | 12 | 16 |
| Truth separation | Principle only | Enforced by database role |
| Success criteria | 1 | 4, culminating in ground-truth reconstruction |
| Definition of design complete | Question list | Question-to-document map with a completion gate |


## 11. Success Criteria - Status

`§8` states four criteria in increasing difficulty. Two are demonstrated end to
end.

| Criterion | Status |
|---|---|
| 1. Detection | Demonstrated - EX-401 and EX-601 recover injected faults and stuck sensors from telemetry alone |
| 2. Attribution | **Partial** - EX-501 separates the four low-output conditions; no blind scoring run |
| 3. Valuation | **Partial** - cost and energy rankings demonstrably differ; no blind scoring run |
| 4. **Reconstruction** | **MET** - injected -0.400 %/yr degradation recovered blind at -0.345 %/yr, error 0.055 pp |

Criterion 4 is the strongest available, and reaching it required four fixes:
degradation was a scalar and therefore invisible; the cloud field could not
reach multi-year scale; a leap day broke the estimator; and the aggregation
choice dominated the answer. Details in `35_degradation_recovery_record`.
