# 15 - Validation and Acceptance Specification

**Version 2.0** - supersedes v1.0. Consolidates the check sets distributed across `06 §11`, `18 §10`, `19 §8`, and `20 §14` into one acceptance authority. Change log in §13.

## 1. Purpose

Define what must be true before generated data is trusted for analysis.

**This document is the acceptance authority.** Where individual documents state checks, they are collected here. A dataset is accepted or rejected against this specification and nothing else.

## 2. Validation Tiers

| Tier | When | Blocking |
|---|---|---|
| T0 - Acquisition | Cache write | Yes - bad cache is never written |
| T1 - Physics oracle | Phase 1, and on every physics change | Yes |
| T2 - Unit invariants | Every build | Yes |
| T3 - Dataset invariants | Every generated dataset | Yes |
| T4 - Statistical acceptance | Every canonical dataset | Yes |
| T5 - Scenario assertions | Every canonical dataset | Yes |
| T6 - Financial reconciliation | Every canonical dataset | Yes |
| T7 - KPI recovery | Multi-year datasets only | Yes for degradation datasets |

## 3. T0 - Acquisition Validation

Full specification in `19 §8`. Summary of blocking checks:

- expected row count for interval and year, leap day allowed
- no duplicate or non-monotonic timestamps
- GHI, DNI, DHI within physical bounds and closure-consistent
- nighttime irradiance zero above 95 degrees zenith
- prices present for every 15-minute interval
- **negative prices present in a full West Texas year** - zero negatives means the fetch is wrong
- NSRDB and Open-Meteo ambient temperature correlate above 0.9 hourly

The last check catches coordinate, timezone, and unit errors better than any other single test. Two independent sources for the same point must broadly agree.

## 4. T1 - Physics Oracle

Under fault-free, curtailment-free, clean-array conditions, simulator output must agree with an independent `pvlib.ModelChain` run to floating-point tolerance.

This is the **Phase 1 acceptance gate**. It is the only check that verifies the production chain itself rather than its consequences, and no later check substitutes for it. A physics error passes every correlation check while producing systematically wrong energy.

## 5. T3 - Temporal Invariants

- timestamps monotonic within an asset stream, except where a data-quality scenario explicitly violates it
- nominal cadence maintained outside injected gaps
- no accidental duplicate `(run_id, asset_id, time)` keys
- sunrise and sunset patterns match the configured geography and season
- daily energy resets at local midnight, correctly handling the 23-hour and 25-hour DST days
- **downscaling renormalization invariant holds for every source interval** (`06 §4.5`)
- **1-minute annual insolation matches source annual insolation within 0.1%**

## 6. T3 - Physical Invariants

- nighttime PV power approximately zero
- DC power non-negative under normal generation
- AC output respects inverter limits
- grid export respects plant and POI limits
- energy consistent with integrated power
- unavailable or tripped assets do not produce normal output
- transformer loading consistent with power flow
- values within documented plausible ranges unless a fault explicitly drives them outside
- **`GHI ≈ DHI + DNI·cos(zenith)`** within tolerance at all daylight timesteps
- **cell temperature peaks after irradiance peaks** on clear days
- **higher wind produces lower cell temperature** at matched irradiance and ambient
- **soiling ratio decreases monotonically between rain events**, increasing only on rain or cleaning
- **string Voc stays below 1500 V** except during the SCN-049 scenario
- **night standby draw produces small negative inverter AC power**

## 7. T4 - Correlation and Statistical Acceptance

| Relationship | Expected |
|---|---|
| POA irradiance to available DC power | Strong positive |
| Aggregate DC to AC power | Strong positive, clipping-modified |
| Module temperature to irradiance and ambient | Positive with lag |
| Transformer and inverter temperature to loading | Positive with lag |
| Outage or derate | Visible output reduction |
| Clipping | Visible AC plateau with rising DC |
| Curtailment | Visible separation of available from exported power |
| **Cross-asset irradiance correlation** | **Decreases monotonically with separation distance** |
| **Cross-asset irradiance lag** | **Consistent with wind direction from source data** |
| **Plant aggregate ramp variance** | **Lower than any individual asset** |
| **Downscaled `kt*` variance** | **Materially higher in the 0.4-0.8 band** |
| **Plant output to price** | **Negative on average** |

Exact statistical thresholds are set after prototype datasets are reviewed, then locked. Reviewing them repeatedly defeats the purpose.

Distribution checks:

- annual clipping loss within 1.5-3.5% of potential AC energy
- annual `CF_ac` within 0.28-0.36
- `PR_corr` for clean fault-free periods within 0.79-0.86
- clear summer day `Y_r` within 7.5-9.5 hours
- **`PR_corr` seasonal profile materially flatter than raw `PR`** - if temperature correction does not flatten it, the correction is wrong

## 8. T5 - Scenario Assertions

Every scenario has automated assertions. Each must specify effect on state, telemetry, peers, plant output, events, recovery, loss attribution, and revenue.

**Example - SCN-040 inverter trip:**

- target inverter enters FAULT then OFF
- target AC output drops materially within one timestep
- peer inverters unaffected
- plant output decreases by approximately the lost inverter's share
- event record exists with correct timestamp and fault code
- recovery follows the defined sequence
- `LOSS_INV_OUTAGE` energy is positive
- lost revenue computed at the prevailing marginal rate
- availability decreases, and by the energy-weighted amount

**Example - SCN-061 stuck irradiance sensor:**

- measured value becomes constant
- **truth continues to vary**
- only the affected sensor is corrupted
- ground truth records the injection
- **plant production is unaltered** by the measurement corruption
- other stations continue to disagree normally

**Example - SCN-029E economic curtailment:**

- price is below the `-C_ptc` threshold with hysteresis and dwell satisfied
- plant curtails; `curtailment_reason = ECONOMIC`
- irradiance is unchanged and high
- no fault code is emitted on any asset
- inverter states are CURTAILED, not DERATED or FAULT
- `LOSS_CURTAIL_ECONOMIC` energy is positive
- **lost revenue is negative** - curtailing was the correct decision

**Example - SCN-026 stuck tracker:**

- affected row-block angle is constant; `angle_error_deg` grows through the day
- peer row-blocks track normally
- deficit versus peers is U-shaped by time of day, minimal at the frozen angle's optimal hour
- `LOSS_TRACKER` energy is positive
- no inverter fault code

## 9. T6 - Financial Reconciliation

Full set in `18 §10`. Blocking checks:

1. **Loss waterfall closes:** `THEORETICAL - Σ(losses) = EXPORTED`, residual under 0.5% of theoretical
2. 15-minute settlement energy reconciles with revenue-meter cumulative energy
3. Every monetized loss references a valid event or scenario instance
4. Hedge settlement is invariant to generation - verified by perturbation
5. Curtailment occurs if and only if the `18 §4.1` rule fires
6. Annual capture rate is below 100% of time-weighted average price
7. Costs sum identically whether aggregated by event, asset, or month
8. Guarantee ledger arithmetic is reproducible from `kpi_daily`
9. No lost-revenue record exists for an interval with no lost energy

Check 1 is the most important in the document. A growing residual means an unattributed loss path exists, which is a correctness bug that silently invalidates every loss analysis.

## 10. T7 - KPI Recovery

The strongest validation available: inject a known value, estimate it blind, compare.

| Injected quantity | Independent estimation | Tolerance |
|---|---|---|
| Module degradation rate (0.40%/yr) | Year-on-year method per `20 §9.3` | ± 0.15 %/yr over 3 years |
| Energy-weighted availability | Reconstructed from telemetry and events | ± 0.2 pp |
| `PR_corr` | Computed from analyst-facing telemetry | ± 0.5 pp versus truth-computed |
| Soiling loss | Estimated from post-rain recovery steps | ± 15% relative |

If the analysis cannot recover the injected value, either the analysis or the simulator is wrong - and the ground-truth table resolves which. Both outcomes are useful.

## 11. Aggregate and Cross-Table Validation

**Aggregate reconciliation:** 5-minute, hourly, and daily aggregates reconcile with raw data within tolerance. Continuous aggregate results are compared against equivalent raw-table SQL, and against DuckDB-over-Parquet results for the same window.

Three-way agreement (raw SQL, continuous aggregate, DuckDB) is a stronger check than two-way and costs little.

**Cross-table integrity:**

- every telemetry asset exists in configuration
- parent-child relationships are valid
- events reference valid assets
- scenario instances reference valid scenario definitions
- maintenance windows align with intended asset states
- every truth row has a corresponding telemetry row and vice versa, except where a gap scenario applies
- **settlement intervals cover the full dataset with no gaps**
- **every price interval referenced by settlement exists in `market.prices`**

**Role isolation:** an automated test connects as `analyst` and confirms SELECT on the `truth` schema is denied. This is verified, not assumed.

## 12. Dataset Acceptance Report

Every canonical dataset produces a report containing:

| Section | Contents |
|---|---|
| Provenance | Run ID, config version, cache version and checksums, seeds, simulator and pvlib versions |
| Volume | Record counts by stream, time range, storage size raw and compressed |
| Completeness | Missingness by stream, duplicates, gap inventory |
| Distributions | Min, max, mean, percentiles by key signal |
| States | State distribution by asset class |
| Scenarios | Instance counts by scenario ID |
| Events | Counts by category and severity |
| Physics | T1 oracle result, T3 invariant pass/fail with failure inventory |
| Statistics | T4 correlation results against thresholds |
| Energy | Reconciliation raw versus aggregate versus Parquet |
| Financial | Waterfall closure residual, T6 results |
| KPIs | Annual PR, PR_corr, CF, availability (all four), capture rate |
| Recovery | T7 results where applicable |
| **Verdict** | **PASS / FAIL, with every failing check itemized** |

## 13. Final Acceptance Principle

A dataset is not accepted because it looks realistic on a graph. It is accepted because it satisfies documented physical, temporal, relational, statistical, financial, and scenario-specific checks.

A dataset that passes every check but looks wrong is worth investigating - the checks may be incomplete. A dataset that looks right but fails a check is not accepted, ever.

## 14. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Structure | Flat check list | Eight validation tiers with blocking status |
| Acquisition validation | Absent | T0, delegated to `19 §8` |
| Physics oracle | Absent | T1, Phase 1 gate |
| Environmental checks | Absent | Renormalization, spatial correlation, thermal lag |
| Financial validation | Absent | T6, nine checks, waterfall closure primary |
| KPI recovery | Absent | T7, four injected quantities with tolerances |
| Scenario examples | 2 | 4, including economic curtailment and stuck tracker |
| Distribution checks | "exact thresholds later" | Five concrete ranges |
| Aggregate reconciliation | Two-way | Three-way including DuckDB |
| Role isolation | Absent | Automated denial test |
| Acceptance report | 13 items | 13 sections with explicit verdict and failure itemization |


## 15. Verification Status by Tier

| Tier | Status |
|---|---|
| T0 Acquisition | **Unrun** - needs API credentials |
| T1 Physics oracle | **PASS** - zero relative error, two independent chains |
| T2 Unit invariants | PASS |
| T3 Dataset invariants | PASS |
| T4 Statistical acceptance | PASS |
| T5 Scenario assertions | PASS |
| T6 Financial reconciliation | PASS - waterfall closes at 7.35e-19 |
| T7 KPI recovery | **Degradation MET**; availability and PR unrun |

## 16. Parsing Is Not Execution

`§11` requires three-way reconciliation between raw SQL, a continuous aggregate
and DuckDB over Parquet. **Two legs are verified.**

| Leg | Status |
|---|---|
| pandas in-memory versus DuckDB over Parquet | **Verified**, 4.14e-16 max relative error |
| TimescaleDB raw versus continuous aggregate | **Outstanding** - needs a running server |

The TimescaleDB schema is validated by **parsing** with `sqlglot`, which
establishes that statements are syntactically valid PostgreSQL. It does **not**
establish that they run: a `create_hypertable` call against a non-existent table
parses cleanly and fails immediately on execution. That is exactly the defect
parsing concealed once already - see `34_dashboards_record`.

The same limit applies to the Grafana dashboards: structurally valid JSON with
parsing panel queries, never rendered against a live datasource.

**Treat "parsed" and "verified" as different words.**
