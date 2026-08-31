# PV Solar Farm Simulator - Design Documentation v2.0

## Purpose

This package defines the target design for a utility-scale PV solar farm simulator whose primary purpose is to generate realistic, explainable, analysis-ready time-series data.

**Governing design principle:**

> Every significant asset, signal, state, event, fault, and scenario must justify its existence by enabling a useful engineering, operational, data-quality, forecasting, financial, or time-series analytical question.

This is a design package, not an implementation package. It contains no simulator code.

## What Changed in v2.0

v1.0 described a fully synthetic simulator with hand-implemented physics and no economic layer. v2.0 makes four structural changes:

1. **Real data.** Solar resource comes from NSRDB GOES CONUS PSM v4 (5-minute, 2 km); market prices come from the ERCOT Public API; precipitation from Open-Meteo. All cached, versioned, and checksummed. The simulator makes zero network calls at runtime.
2. **Borrowed physics.** The production chain is `pvlib`, with named models. An independent `ModelChain` run serves as a validation oracle and gates Phase 2.
3. **A commercial layer.** Settlement, cause-attributed lost revenue, O&M cost, and IEC 61724-1 contractual guarantees. Every lost kWh carries a cause code and a dollar value.
4. **Locked decisions.** Site, equipment, topology, tracking, and runtime versions are locked rather than deferred.

## Reference Plant

**NorthStar PV Solar Farm** - Pecos County, West Texas (31.35 N, -103.30 W), ERCOT West.

| | |
|---|---|
| AC nameplate | 100.0 MW |
| DC nameplate | 125.80 MWp (DC/AC 1.258) |
| Blocks / inverters | 10 / 40 at 2,500 kW |
| Combiners / strings / modules | 480 / 7,680 / 215,040 |
| Modules | 585 W bifacial n-type TOPCon |
| Array | Horizontal single-axis tracking, backtracking, GCR 0.33 |
| Telemetry | 585 assets, ~89M rows per simulated year |
| Raw cadence | 1 minute (5 min for combiners and trackers) |

## Design Flow

```
Time-Series and Financial Questions
  -> Analytical Behaviors
  -> Signals and Events
  -> Physical Asset Model
  -> Real Resource + Stochastic Downscaling + Spatial Cloud Field
  -> pvlib Production Chain
  -> Operating States, Control, and Scenarios
  -> Telemetry Generation and Sensor Model
  -> Loss Attribution and Settlement
  -> Time-Series Storage
  -> SQL / Python / Grafana Analysis
  -> Validation
```

## Documents

**Foundation**
1. `01_system_vision.md` **v2.0** - mission, scope, principles, success criteria
2. `02_time_series_analytics_requirements.md` **v2.0** - the analytical contract
3. `03_reference_solar_farm.md` **v2.0** - locked site, equipment, and capacity reconciliation

**Physical Model**
4. `04_physical_architecture.md` - plant hierarchy and energy/data flows
5. `05_equipment_catalog.md` - asset ontology and required properties
6. `06_environmental_model.md` **v2.0** - five-layer hybrid resource architecture
7. `07_solar_production_model.md` **v2.0** - pvlib production chain and loss stages

**Operations**
8. `08_operating_state_model.md` - plant and equipment state machines
9. `09_failure_degradation_model.md` - faults, degradation, analytical signatures
10. `10_scenario_catalog.md` **v2.0** - normal, abnormal, market, and ambiguous scenarios

**Data**
11. `11_telemetry_specification.md` **v2.0** - signals, cadence, units, classification
12. `12_event_alarm_model.md` - event and alarm semantics
13. `13_time_series_data_model.md` **v2.0** - schemas, roles, hypertables, aggregates, Parquet

**Requirements and Acceptance**
14. `14_functional_nonfunctional_requirements.md` **v2.0** - fetch client and simulator requirements
15. `15_validation_acceptance_specification.md` **v2.0** - eight validation tiers, acceptance authority
16. `16_implementation_roadmap.md` **v2.0** - build sequence with gates

**Locked Decisions and Extensions (new in v2.0)**
17. `17_locked_design_decisions.md` - fourteen decision records
18. `18_financial_commercial_model.md` - settlement, loss attribution, cost, guarantees
19. `19_external_data_acquisition.md` - sources, cache, versioning, validation
20. `20_kpi_definitions.md` - normative KPI formulas, IEC 61724-1 / ASTM E2848
21. `21_node_selection_record.md` - DR-015, plant pricing proxy selection and fallbacks
22. `22_equipment_and_physics_gate_record.md` - DR-016, equipment reselection and the Phase 2 gate result
23. `23_spatial_field_record.md` - Phase 3 spatial cloud field, findings and gate result
24. `24_plant_scaleout_record.md` - Phase 4 full plant, throughput and balance-of-plant
25. `25_states_and_control_record.md` - Phase 5 state machines, controller and startup sequences
26. `26_sensor_layer_record.md` - Phase 6 sensor model, truth/measurement separation
27. `27_loss_attribution_record.md` - Phase 7 loss waterfall, derating and cause codes
28. `28_fault_engine_record.md` - Phase 8 fault signatures, scheduling and reliability
29. `29_financial_layer_record.md` - Phase 9 settlement, curtailment economics and KPIs
30. `30_data_quality_record.md` - Phase 10 defect injection and quality flag fallibility
31. `31_storage_record.md` - Phase 11 Parquet export, TimescaleDB schema and reconciliation
32. `32_curriculum_record.md` - Phase 12 SQL curriculum, executed against real data
33. `33_notebooks_record.md` - Phase 12b analysis methods and executed notebooks
34. `34_dashboards_record.md` - Phase 12c Grafana dashboards and the schema correction
35. `35_degradation_recovery_record.md` - blind recovery of an injected degradation rate
36. `36_documentation_reconciliation.md` - all 56 pending doc updates applied and verified
37. `37_acceptance_report_record.md` - the dataset acceptance report, built and run
38. `38_database_verification_record.md` - schema, roles and reconciliation executed on PostgreSQL
39. `39_timescaledb_verification_record.md` - hypertables, aggregates and compression executed
40. `40_dashboard_verification_record.md` - panel queries executed; only rendering outstanding
41. `41_usability_record.md` - eight defects found by running from a clean checkout
42. `42_ercot_retention_record.md` - measured ERCOT retention and the year-overlap constraint

## Reading Order

**To understand the design:** 01, 02, 17, then follow interest.

**To start building:** 17 (decisions), 19 (acquisition), 16 (roadmap), then 03 and 06.

**To analyze a generated dataset:** 11 (what the signals are), 20 (what the KPIs mean), 02 (what to ask).

**Where documents conflict:** `17` governs decisions, `20` governs KPI definitions, `15` governs acceptance. Any other conflict is a bug.

## Design Status

**Design package reconciled with the implementation as of doc 36.** Phase 0 complete. Site, equipment, topology, data sources, KPI definitions, and commercial model are locked. Documents 01-20 are internally consistent.

**Next gate:** the `19 §11` acquisition checklist. No implementation should begin until it passes in full.

## Verification Note

External API endpoints, dataset products, rate limits, and library versions change. The provider details in `19 §2` were verified in August 2026 and include a domain migration and a dataset deprecation that invalidate most published tutorials. Re-verify before Phase 0.5.
