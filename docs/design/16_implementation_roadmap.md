# 16 - Implementation Roadmap

**Version 2.0** - supersedes v1.0. Adds a data acquisition phase, a physics oracle gate, and a financial phase. Change log in §15.

## 1. Purpose

Defines the build sequence after documentation is locked. This is not code.

**Structural change from v1.0:** the roadmap now has a hard gate at Phase 2 (physics oracle) that must pass before any plant complexity is added, and a data acquisition phase before anything else. v1.0's sequence assumed a synthetic resource and could begin immediately; v2.0 cannot start until real data is in hand.

## 2. Phase 0 - Design Review

Lock: analytical requirements, reference plant, asset hierarchy, telemetry catalog, scenario list, validation rules, **data sources, KPI definitions, and the commercial model**.

**Exit criteria:**
- every row of the `01 §7` question-to-document map resolves
- documents 01-20 are internally consistent
- no unresolved decision materially affects schema or simulator behavior

**Status: complete.** Documents 17-20 added; 01, 02, 03, 06, 07, 10, 11, 13, 14, 15, 16 revised to v2.0.

## 3. Phase 0.5 - Data Acquisition - NEW

Deliver: the fetch client and a validated resource cache.

- NLR Developer Network and ERCOT API registration
- fetch client per `19 §7`
- NSRDB GOES CONUS v4, 2019-2024, 5-minute
- NSRDB GOES TMY v4
- Open-Meteo precipitation, matching years
- ERCOT RT SPP, DAM SPP for three settlement points
- EIA v2 grid context
- cache manifest with checksums

**Exit criteria:** the `19 §11` checklist passes in full, including T0 validation and the cross-source temperature correlation above 0.9.

**Why this comes first:** every downstream phase consumes this data. Discovering at Phase 3 that the site has no NSRDB coverage, or that the settlement point identifier is wrong, invalidates work already done. The checklist is cheap now and expensive later.

## 4. Phase 1 - Configuration and Static Plant Model

Deliver: plant configuration specification, asset identity model, parent-child relationships, **asset position geometry**, configuration validation.

**Acceptance:**
- the full reference plant instantiates from configuration
- capacity totals reconcile to 125.80 MWp / 100.0 MW per `03 §5`
- every telemetry-bearing asset has (x, y) coordinates
- configuration validation rejects malformed input rather than proceeding

## 5. Phase 2 - Physics Core and Oracle Gate - NEW STRUCTURE

Deliver: the `pvlib` model chain for **one inverter**, no faults, no sensors, no control.

Sequence:
1. Resource cache load and 5-minute-to-1-minute downscaling per `06 §4`
2. Assert the renormalization invariant
3. Solar position, tracking, transposition, bifacial rear irradiance
4. Cell temperature, single-diode DC, Sandia inverter conversion
5. Independent `ModelChain` reference run
6. Compare

**Acceptance - this is the gate:**
- simulator and independent `ModelChain` agree to floating-point tolerance (T1)
- renormalization invariant holds for every source interval
- clear-day production curve is correct in shape and magnitude
- night output is zero except standby draw
- energy reconciles with integrated power

**Nothing proceeds until this passes.** A physics error here passes every downstream correlation check while producing systematically wrong energy, and will not be caught again.

## 6. Phase 3 - Spatial and Environmental Completion

Deliver: cloud field advection, three weather stations, soiling from real precipitation, full environmental truth.

Expand from one inverter to **one block** (4 inverters).

**Acceptance:**
- inverters are correlated but not identical
- cross-asset lag is consistent with wind direction
- plant aggregate ramp variance is lower than any individual asset
- soiling accumulates between real rain events and resets on them
- weather stations disagree by modeled amounts

## 7. Phase 4 - Full Plant Scale-Out

Deliver: 10 blocks, 40 inverters, 480 combiners, 40 tracker row-blocks, transformers, collection, meter.

**Acceptance:**
- capacity reconciles at every hierarchy level
- block-to-block comparison shows realistic spatial variation
- aggregate smoothing scales correctly with asset count
- generation throughput is within the `14 §4.1` target

## 8. Phase 5 - Asset States and Control

Deliver: inverter, transformer, tracker, breaker, and plant state machines; plant controller; startup and shutdown sequences.

**Acceptance:**
- only legal transitions occur
- state and telemetry are consistent (`08 §8`)
- sunrise and sunset transitions are visible in telemetry
- tracker backtracking is visible in morning and evening

## 9. Phase 6 - Sensor Layer

Deliver: the truth-to-measurement transformation, per-instance bias, drift, noise, response time, quantization.

**Acceptance:**
- truth and measurement diverge only in modeled ways
- role isolation test passes - `analyst` cannot read `truth`
- station spread is within the `20 §11` typical range

**Ordering note:** the sensor layer comes before faults deliberately. Building faults first tempts you to define fault signatures against truth rather than against what an analyst can actually see.

## 10. Phase 7 - Losses and Attribution

Deliver: the full loss waterfall, per-stage attribution, clipping, thermal derating, degradation.

**Acceptance:**
- waterfall closes with residual under 0.5%
- each loss has a measurable, distinct analytical signature
- clipping magnitude is within 1.5-3.5% annual
- clipping, curtailment, derate, and outage are visually and numerically distinguishable

## 11. Phase 8 - Fault and Scenario Engine

Deliver: all scenario classes from `10`, event and alarm generation, recovery, maintenance.

**Acceptance:**
- every scenario has passing automated assertions (T5)
- compound scenarios produce correctly separated attribution
- attribution-ambiguity scenarios (SCN-090 to 093) produce genuinely ambiguous telemetry

## 12. Phase 9 - Financial Layer - NEW

Deliver: price join, settlement, economic curtailment, loss monetization, O&M costs, guarantee ledger, KPI computation per `20`.

**Acceptance:**
- T6 financial reconciliation passes in full
- capture rate is below 100%
- at least one interval exists where an outage increased margin
- economic curtailment fires only per the `18 §4.1` rule
- guarantee ledger reproduces from `kpi_daily`

## 13. Phase 10 - Data Quality Injection

Deliver: gaps, stuck values, drift, spikes, communication loss, duplicates, timestamp anomalies.

**Acceptance:**
- measurement defects do not alter physical truth
- the duplicate scenario is handled via the `13 §11` staging approach
- quality flags are sometimes wrong, deliberately

**Ordering note:** data quality comes late, after everything downstream is verified clean. Injecting defects into an unverified pipeline makes it impossible to tell a defect from a bug.

## 14. Phase 11 - Storage, Aggregates, and Portability

Deliver: TimescaleDB schema with six-schema role separation, hypertables, chunk intervals, continuous aggregates, compression, Parquet export, DuckDB verification.

**Acceptance:**
- three-way reconciliation: raw SQL, continuous aggregate, DuckDB over Parquet
- one-year volume and performance test against `14 §4.1` targets
- compression ratio measured and recorded
- truth Parquet directory is separable

## 15. Phase 12 - Analytics and Dashboard Layer

Deliver: SQL exercises beginner to advanced (each in both TimescaleDB and DuckDB form), Python notebooks, Grafana dashboards, anomaly and fault investigations, forecasting datasets.

## 16. V1 Release Gate

V1 is complete only when:

- a canonical 3-year dataset passes all validation tiers T0 through T7
- core scenarios from every class are represented
- raw and aggregate data are queryable and reconcile three ways
- the Parquet and DuckDB workflow functions without containers
- dashboards reveal expected behavior
- **an analyst working blind can identify injected faults from telemetry**
- **the analyst's independently-estimated degradation rate recovers the injected value within tolerance**
- **cost ranking of faults differs from energy ranking, and the difference is explainable**
- documentation matches implementation

The three bolded criteria correspond to success criteria 1, 4, and 3 in `01 §8`. They are the point of the exercise; everything else is infrastructure.

## 17. Recommended First Build Slice

Per `17 §4`, the smallest useful vertical slice:

1. Fetch 7 real days from NSRDB for the locked site - one clear, one overcast, one broken-cloud, one rainy, one high-wind, one hot, one cold
2. Downscale to 1 minute; assert the renormalization invariant
3. `pvlib` chain for one inverter, no faults, no sensors
4. **Compare against independent `ModelChain` - the physics gate**
5. Expand to one block with the spatial cloud field; assert correlated-but-not-identical
6. Add the sensor layer; assert truth and measurement diverge only as modeled
7. Load to TimescaleDB; assert continuous aggregate matches raw SQL
8. Add one fault (SCN-040) with full event, loss attribution, and revenue impact

Step 8 exercises the entire vertical stack - physics, state machine, event, telemetry signature, loss attribution, dollar impact - on the smallest possible model.

**Nothing expands until step 8 passes.** This preserves the governing principle: nothing gets expanded until the time-series behavior of the smaller model is correct.

## 18. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Phases | 10 | 13, plus Phase 0.5 |
| Data acquisition | Absent | Phase 0.5, gating everything |
| Physics validation | Implicit in Phase 3 acceptance | Phase 2 hard gate with independent oracle |
| Sensor layer position | Bundled with data quality (Phase 7) | Separate Phase 6, before faults |
| Data quality position | Phase 7 | Phase 10, after everything verified clean |
| Financial layer | Absent | Phase 9 |
| Spatial model | Absent | Phase 3 |
| Tracker | Absent | Phases 4 and 5 |
| Storage phase | Phase 8 | Phase 11, with three-way reconciliation |
| Release gate | 7 criteria | 9, three tied to `01 §8` success criteria |
| First slice | Weather to block to plant, 2 days | 8 steps with the physics gate at step 4 |
| Dataset length for V1 | 365 days | 3 years, required by degradation validation |


## 19. Amendments from Implementation

### Phase 2 - the oracle is scoped to physics

`ModelChain` models module and inverter physics. Degradation, mismatch, DC
wiring and thermal derating are **plant** characteristics with no `ModelChain`
equivalent, so `run_inverter_chain` takes `apply_plant_losses`, which the gate
disables. Leaving them on compares a plant against a module and reports the
difference as an implementation error.

### Phase 3 - acceptance criteria are stated at plant scale

`§6` states them at block level. That is the wrong scale for two of them.

Within one 620 m block at 8 m/s the inverter-to-inverter cloud transit is
roughly **19 seconds - below the 1-minute telemetry cadence**, so lag is
unresolvable there by construction. Four assets correlated at 0.995 cannot show
meaningful aggregate smoothing either.

Both properties are real and measurable across the 3.26 km footprint. The gate
runs over weather stations and power blocks. This is a correction to the
criteria, not a weakening of them.

### Phase 4 - shared geometry is an implementation constraint

Solar position, tracking, airmass and bifacial view factors must be computed
once per timestep, not once per asset. See `04 §9.4`.

### Phase 12 - the curriculum is executed, not authored

Every SQL exercise runs against a real exported dataset in the gate. A
curriculum that has never run is a list of plausible-looking queries, and the
subtly wrong ones are exactly the ones a learner cannot diagnose. EX-601 was
wrong four times, each time executing cleanly and returning something plausible.

Notebooks are executed in the build for the same reason.

## 20. Release Gate Status

| Criterion | Status |
|---|---|
| Canonical 3-year dataset passing T0-T7 | **Outstanding** - T0 needs credentials, dataset not generated at 1-minute cadence |
| Core scenarios from every class represented | Met |
| Raw and aggregate reconcile three ways | **Two of three legs** |
| Parquet and DuckDB workflow without containers | Met |
| Dashboards reveal expected behaviour | **Written, never rendered** |
| Blind analyst identifies injected faults | Met - EX-401, EX-601 |
| Degradation rate recovered within tolerance | **Met** - 0.055 pp error |
| Cost ranking differs from energy ranking | Met, asserted in the financial gate |
| Documentation matches implementation | Met as of this revision |
