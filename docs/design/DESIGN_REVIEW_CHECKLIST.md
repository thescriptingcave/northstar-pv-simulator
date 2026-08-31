# Design Review Checklist

**Version 2.0.** Use before implementation begins. Items marked ✅ were resolved by documents 17-20 and the v2.0 revisions.

## Analytical Contract

- ✅ Every major telemetry signal supports at least one analytical question (`11`)
- ✅ Required SQL/time-series techniques are supported (`02 §11`)
- ✅ Forecasting and anomaly detection have sufficient temporal structure (`06`)
- ✅ Ground truth is separable from analyst-facing data (`13 §2`, role-enforced)
- ✅ **Financial questions are answerable** (`02 §8`, `18`)
- ✅ **Attribution and disambiguation exercises exist** (`02 §10`, SCN-090 to 093)

## Plant Model

- ✅ Reference plant scale is locked (`03 §3`)
- ✅ Asset hierarchy is locked (`03 §6`)
- ✅ Telemetry-bearing asset levels are locked (`03 §7`)
- ✅ Capacity totals reconcile (`03 §5` - derived, not asserted)
- ✅ **Equipment selection is locked** (DR-005, DR-006)
- ✅ **Array topology is locked** (DR-004 - single-axis tracking, bifacial)
- ✅ **Asset positions exist for spatial modeling** (`13 §3`)

## Data Sources

- ✅ **Resource data source identified and current** (`19 §4.1`)
- ✅ **Market data source identified** (`19 §4.4`)
- ✅ **Precipitation source identified** (`19 §4.3` - NSRDB lacks it)
- ✅ **Cache layout, manifest, and versioning defined** (`19 §5`)
- ✅ **Runtime network policy defined** (zero calls during simulation)
- ✅ **Licensing and redistribution policy defined** (`19 §10`)
- ⬜ **API keys registered and endpoints tested** (`19 §11` - Phase 0.5)
- ⬜ **Site confirmed within NSRDB coverage** (`19 §11`)
- ⬜ **ERCOT settlement point selected** (`19 §11`)

## Physics and Behavior

- ✅ Environmental drivers are temporally coherent (`06 §4`)
- ✅ **Environmental drivers are spatially coherent** (`06 §5`)
- ✅ DC production is causally linked to irradiance (`07 §5`)
- ✅ Temperature effects are defined (`07 §6` - Faiman, named)
- ✅ Inverter conversion and clipping are defined (`07 §7`, `07 §8` - Sandia)
- ✅ Curtailment is distinct from clipping and resource limitation (`07 §9.1`)
- ✅ Energy derives from power (`07 §11`)
- ✅ **Physics models are named, not described** (DR-003)
- ✅ **A physics validation oracle exists** (`07 §4`, `15 §4`)
- ✅ **Tracking and bifacial behavior are specified** (`06 §6.2`)

## Operations

- ✅ Major state machines are defined (`08`)
- ✅ Faults produce telemetry signatures (`09 §4`)
- ✅ Maintenance affects availability (`18 §7.1`)
- ✅ Recovery behavior is defined (`08 §5`)
- ✅ **Tracker states and faults are defined** (`11 §7`, SCN-026 to 029)
- ✅ **Transient versus failure threshold is defined** (`20 §10` - 15 minutes)

## Commercial

- ✅ **Offtake structure is defined** (`18 §2`)
- ✅ **Settlement grain and formulas are defined** (`18 §3`)
- ✅ **Curtailment economics are defined** (`18 §4`)
- ✅ **Loss waterfall with cause codes is defined** (`18 §5.2`)
- ✅ **Monetized versus structural losses are distinguished** (`18 §5.2`)
- ✅ **O&M cost model is defined** (`18 §6`)
- ✅ **Contractual guarantees and exclusions are defined** (`18 §7`)

## KPIs

- ✅ **Every KPI has a normative formula** (`20`)
- ✅ **Standards anchor is chosen** (IEC 61724-1, ASTM E2848)
- ✅ **PR filtering rules are normative** (`20 §4.4`)
- ✅ **All four availability definitions are specified** (`20 §6`)
- ✅ **Aggregation rules are specified** (`20 §12` - never average ratios)
- ✅ **Three expected-energy baselines are distinguished** (`20 §7.2`)

## Data

- ✅ Units are defined (`11`)
- ✅ Cadence is defined (`11 §3`)
- ✅ **Field classification is mandatory, not advisory** (`11 §11`)
- ✅ UTC and timezone policy is defined (`13 §12`)
- ✅ **Interval-beginning convention is stated** (`11 §2`)
- ✅ **DST 23/25-hour day handling is required** (`13 §12`)
- ✅ Raw and aggregate grains are defined (`13 §10`)
- ✅ Parquet portability is included (`13 §13`)
- ✅ **Truth is separated by role, not convention** (`13 §2`)
- ✅ **Duplicate-record scenario is reconciled with key uniqueness** (`13 §11`)
- ✅ **Chunk intervals and compression policy are specified** (`13 §9`)
- ✅ **A deliberate decision not to aggregate combiners is recorded** (`13 §9.2`)

## Reproducibility

- ✅ **Reproducibility key includes cache version** (`14 §3`)
- ✅ **Named seed substreams are specified** (DR-013)
- ✅ **Differential reproducibility is required and testable** (`14 §3`)
- ✅ **Runtime versions are pinned with rationale** (`19 §2.4`)

## Validation

- ✅ Physical invariants are documented (`15 §6`)
- ✅ Temporal invariants are documented (`15 §5`)
- ✅ Scenario assertions are documented (`15 §8`)
- ✅ Aggregate reconciliation is required (`15 §11` - three-way)
- ✅ Dataset acceptance report is defined (`15 §12`)
- ✅ **Acquisition validation is defined** (`15 §3`)
- ✅ **Physics oracle gate is defined** (`15 §4`)
- ✅ **Financial reconciliation is defined** (`15 §9`)
- ✅ **KPI recovery validation is defined** (`15 §10`)
- ✅ **Role isolation is verified by test** (`15 §11`)

## Release Gate

- ✅ No unresolved design decision materially affects schema or simulator behavior
- ✅ V1 scope and deferred features are explicit (`01 §5`, `01 §6`)
- ✅ Implementation can proceed incrementally without redesigning the analytical contract (`16`)
- ✅ **Build sequence has hard gates, not just phases** (`16 §5`)
- ✅ **Release criteria are tied to stated success criteria** (`16 §16`)

## Remaining Before Code

Three open items, all in `19 §11`:

- ⬜ API registrations complete and endpoints tested
- ⬜ One test year fetched end to end, passing all T0 checks
- ⬜ Cross-source temperature correlation above 0.9 confirmed

**Design review is otherwise closed.** Phase 0.5 may begin.
