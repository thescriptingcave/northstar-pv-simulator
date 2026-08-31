# 17 - Locked Design Decisions

## 1. Purpose

Documents 01-16 deliberately deferred several decisions to design review. This document closes them.

Each entry is a decision record: **Decision**, **Rationale**, **Consequences**. Decisions marked LOCKED are inputs to Phase 1 and should not be reopened without revisiting the analytical contract in `02`.

A decision is made here whenever leaving it open would block schema or simulator behavior. Where a decision is genuinely reversible without redesign, it is listed in section 5 as deferred.

---

## 2. Decision Records

### DR-001 - Site and Market - LOCKED

**Decision:** NorthStar PV is sited in **Pecos County, West Texas**, interconnected to **ERCOT West Load Zone**.

| Parameter | Value |
|---|---|
| Latitude | 31.35 N |
| Longitude | -103.30 W |
| Elevation | 850 m |
| Timezone (config metadata) | `America/Chicago` |
| Storage timezone | UTC |
| Ground albedo | 0.25 (desert scrub) |
| Market | ERCOT, West Load Zone / resource node settlement |

**Rationale:** West Texas maximizes the density of analytically interesting phenomena per unit of simulated time:

- High DNI and high clear-sky fraction produce frequent, sustained inverter clipping.
- Summer ambient regularly exceeds 40 C, driving genuine module temperature derating and inverter thermal derating rather than token effects.
- ERCOT has real, frequent **negative real-time prices** and **congestion curtailment** in West Texas. This makes curtailment a first-class economic phenomenon rather than a synthetic scenario, and it is what separates `07 §8` (curtailment) from `07 §7` (clipping) in a way an analyst must actually reason about.
- Low, episodic rainfall produces long soiling accumulation ramps punctuated by sharp rain-cleaning resets - ideal for change-point and trend analysis.
- Real dust events give a physically grounded reason for correlated multi-block performance drops.

CAISO was the runner-up (duck curve, negative midday pricing) but produces less thermal stress and less curtailment volume. Arizona was rejected as too clean - fewer anomalies to detect.

**Consequences:**
- Weather API: NREL NSRDB (PSM3) primary, Open-Meteo archive as fallback and cross-check.
- Price API: ERCOT real-time and day-ahead settlement point prices; EIA v2 for fuel mix and load context.
- Local-solar-day analysis must convert UTC to `America/Chicago`, exercising the timezone requirement in `13 §6`.

---

### DR-002 - Weather Resource Strategy - LOCKED

**Decision:** Five-layer hybrid. Real historical data sets the envelope; stochastic downscaling supplies sub-hourly structure; spatial advection distributes it across the plant.

**Layer 1 - Real resource acquisition (network, cached).**
Fetch hourly (or 30-minute where available) GHI, DNI, DHI, ambient temperature, wind speed, wind direction, relative humidity, surface pressure, and precipitation for the site.

Fetch two products:
- **Actual historical years** (target 2019-2024) - the "as-run" resource.
- **TMY** - the P50 expected-energy baseline.

Both are written once to a versioned Parquet resource cache. **The simulator never touches the network during a run.** The cache is a declared, versioned input artifact.

**Layer 2 - Stochastic downscaling to 1 minute (seeded).**
Downscaling operates in **clear-sky index** space, never in raw W/m2:

1. Compute 1-minute clear-sky GHI/DNI/DHI with `pvlib` (Ineichen-Perez with Linke turbidity).
2. Derive hourly `kt* = GHI_observed / GHI_clearsky`.
3. Interpolate `kt*` to 1-minute resolution.
4. Superimpose a mean-reverting bounded stochastic process (Ornstein-Uhlenbeck, clipped to `[0, 1.15]`) whose **variance is conditioned on `kt*`**: near zero at `kt* > 0.9` (clear) and `kt* < 0.25` (solid overcast), maximum in the `0.4 - 0.8` broken-cloud band.
5. Permit brief excursions above `kt* = 1.0` to reproduce **cloud-edge irradiance enhancement**, which is real and is a common source of false-positive anomaly detections.
6. **Renormalize** so the 1-minute mean over each source interval exactly equals the source value.

Step 6 is a hard invariant, not a nicety. It guarantees that monthly and annual energy remain tied to real meteorology and gives Phase 2 a machine-checkable acceptance test.

**Rationale for conditioning on `kt*`:** variability is not uniform. Clear days and solidly overcast days are both smooth; broken cloud is where ramps live. Conditioning is what makes SCN-004 (passing clouds) and SCN-005 (rapid ramps) *emerge* from real weather rather than being hand-scheduled - which in turn means the analyst cannot detect them by looking for the simulator's scheduling artifacts.

**Layer 3 - Spatial cloud field.**
A single cloud transmissivity field is generated per timestep and **advected across the plant footprint** at the prevailing wind speed and direction.

Implementation: each telemetry-bearing asset has an (x, y) position. Its sampled `kt*` is the field evaluated at time `t - (r · w_hat)/|w|`, where `r` is the asset position vector and `w` the wind vector, with low-pass smoothing proportional to the asset's footprint (a block smooths more than a pyranometer).

This one mechanism produces, without any additional code:
- correlated but time-lagged ramps between blocks,
- aggregate smoothing at plant level relative to any single inverter,
- genuine weather-station disagreement across the three stations,
- a non-trivial baseline for inverter peer comparison.

**Without spatial structure, all 40 inverters see identical irradiance and roughly half the analytics in `02 §5` collapse to noise.** This layer is load-bearing.

**Layer 4 - Deterministic derivation.**
Everything downstream is derived, never independently generated:
- POA via `pvlib` Perez transposition, with tracker geometry.
- Rear-side irradiance via `pvlib.bifacial.infinite_sheds`.
- Cell temperature via Faiman (POA, ambient, wind) - satisfies `06 §5`.
- Soiling via `pvlib.soiling.hsu`, driven by the **real precipitation series**, so rain-cleaning events (SCN-023) occur on real rain days.

**Layer 5 - Sensor model.**
Truth is transformed into measurement per sensor instance: calibration bias, slow drift, noise floor, pyranometer soiling, response time, quantization, and failure modes. The three weather stations carry independent bias and drift, so they legitimately disagree.

**Consequences:**
- The seed governs Layers 2, 3, and 5 only. Layer 1 is a cached artifact and Layer 4 is deterministic.
- Reproducibility key becomes `(resource_cache_version, config_version, seed, simulator_version)`. This satisfies `14 §3` without pretending network calls are reproducible.
- A `resource_cache` manifest (source, product, years, fetch timestamp, checksum) is a required deliverable.

---

### DR-003 - Physics Engine - LOCKED

**Decision:** `pvlib-python` is the production-chain engine. Custom code is written only for faults, state machines, control, sensor modeling, and data-quality injection.

**Rationale:** Document `07` reads as hand-rolled equations. Hand-rolled PV physics is where simulators quietly become wrong, and a wrong physics core silently invalidates every downstream analytic. `pvlib` is the validated reference implementation used by the industry, and it doubles as a **validation oracle**: the "expected/unconstrained power" required by `07 §3` can be computed by an independent `pvlib.ModelChain` run and compared against the simulator's constrained output.

**Model chain (locked):**

| Stage | Model |
|---|---|
| Solar position | NREL SPA |
| Clear sky | Ineichen-Perez |
| Tracking | `pvlib.tracking.singleaxis`, backtracking on |
| Transposition | Perez |
| Rear irradiance | `bifacial.infinite_sheds` |
| Cell temperature | Faiman |
| DC model | CEC single-diode (`pvsystem.calcparams_cec` + `singlediode`) |
| Inverter model | Sandia (`inverter.sandia`) |
| Losses | Explicit per-stage, not a lumped derate |

**Consequences:** `07_solar_production_model.md` should be revised to reference these models by name rather than describing generic behavior. Loss stages remain individually attributed as `02 §7` requires - the lumped `pvwatts_losses` shortcut is explicitly rejected.

---

### DR-004 - Array Topology - LOCKED

**Decision:** **Horizontal single-axis tracking, bifacial modules.**

| Parameter | Value |
|---|---|
| Tracker | Horizontal single-axis, N-S axis |
| Rotation limit | +/- 60 deg |
| Backtracking | Enabled |
| Ground coverage ratio | 0.33 |
| Bifaciality factor | 0.70 |
| Module height | 1.8 m |

**Rationale:** Fixed-tilt would be unrepresentative of any 2020s-era utility plant in West Texas and, more importantly, throws away analytical content. Tracking produces the characteristic broad-shouldered production plateau, morning/evening backtracking transitions, and a whole class of faults (stuck tracker, misaligned row, stow events) that fixed tilt cannot express. Bifacial adds albedo and row-geometry dependence and is now the default for utility procurement.

**Consequences:**
- New telemetry: tracker angle per row-block, tracker state, stow status.
- New scenarios required: **SCN-026 stuck tracker**, **SCN-027 high-wind stow**, **SCN-028 tracker misalignment**. Stuck-tracker detection is one of the highest-value real-world analytics and is a natural fit for peer comparison.
- Rear irradiance is a distinct, non-measured truth signal - a good example of `11 §8` "simulated physical truth" that has no sensor.

---

### DR-005 - Module Selection - LOCKED

**Decision:** 585 W bifacial n-type TOPCon, 144 half-cell, 1500 V system.

| Parameter | Value |
|---|---|
| Rated power (STC) | 585 W |
| Voc | 52.1 V |
| Vmp | 43.4 V |
| Isc | 14.25 A |
| Imp | 13.48 A |
| Efficiency | 22.6% |
| Temp. coefficient of Pmax | -0.29 %/C |
| Bifaciality | 0.70 |
| Year-1 degradation | 1.0% |
| Annual degradation thereafter | 0.40% |

**Rationale:** This is the mainstream 2024-2026 utility procurement class. TOPCon's low temperature coefficient matters in West Texas and produces a *subtler* thermal signature than legacy PERC - which is analytically better, because the effect must be found rather than being obvious.

**Implementation note:** Populate CEC single-diode parameters from the `pvlib` CEC module database, selecting the closest real entry, rather than inventing coefficients. Record the chosen database key in configuration for lineage.

---

### DR-006 - Inverter and DC/AC Ratio - LOCKED

**Decision:** Central inverters, 2.5 MW AC each, 40 total.

| Parameter | Value |
|---|---|
| Rated AC | 2,500 kW |
| Max DC input | 3,750 kW |
| MPPT channels | 6 |
| Max DC voltage | 1,500 V |
| Peak efficiency | 98.8% |
| Thermal derate onset | 45 C ambient |
| Trip threshold | 75 C internal |
| Night standby draw | 250 W |

**String and array build-out:**

| Quantity | Value |
|---|---|
| Modules per string | 28 (28 x 52.1 V = 1,459 V < 1,500 V at cold-temp Voc margin) |
| String DC rating | 16.38 kWp |
| Strings per combiner | 16 |
| Combiners per inverter | 12 |
| Strings per inverter | 192 |
| DC per inverter | 3,145 kWp |
| **DC/AC ratio per inverter** | **1.258** |
| Total strings | 7,680 |
| Total modules | 215,040 |
| **Plant DC nameplate** | **125.8 MWp** |
| **Plant AC nameplate** | **100 MW** |

This reconciles the capacity totals required by the `DESIGN_REVIEW_CHECKLIST` and derives module counts from real electrical characteristics as `03 §3` requires.

**Rationale for 1.258:** high enough to produce clipping on most clear days from roughly March through September - so clipping loss is a recurring, quantifiable, seasonally varying phenomenon rather than a rare event. Low enough that clipping is not permanently saturating midday, which would destroy the irradiance-to-AC-power correlation that `15 §4` requires.

**Cold-temperature Voc check:** at -10 C design minimum with a Voc temperature coefficient of -0.25 %/C, 28 modules reach approximately 1,494 V. Within the 1,500 V limit with minimal margin - this is deliberately realistic and creates a plausible basis for a DC overvoltage fault scenario on cold clear winter mornings.

---

### DR-007 - Telemetry Cardinality - LOCKED

| Stream | Assets | Cadence | Rows/day |
|---|---|---|---|
| Weather station | 3 | 1 min | 4,320 |
| Inverter | 40 | 1 min | 57,600 |
| Combiner | 480 | 5 min | 138,240 |
| Transformer | 10 | 1 min | 14,400 |
| Tracker row-block | 40 | 5 min | 11,520 |
| Power block | 10 | 1 min | 14,400 |
| Plant | 1 | 1 min | 1,440 |
| Revenue meter | 1 | 1 min | 1,440 |
| **Total** | **585** | | **~243,000** |

Approximately **89 million rows per simulated year**. Comfortable for TimescaleDB on Apple Silicon and large enough to make compression, continuous aggregates, and retention policies genuinely necessary rather than decorative.

**Decision:** All 480 combiners are instrumented. Combiner telemetry is the dominant stream (57% of volume) but is required for string-imbalance detection (SCN-044) to have a peer population. If volume becomes a problem it is reduced to 15-minute cadence, not to a sampled subset - losing peers is worse than losing resolution.

---

### DR-008 - Commercial Structure - LOCKED

**Decision:** Hybrid offtake. Both settlement mechanics are exercised.

| Component | Share | Terms |
|---|---|---|
| Fixed-price PPA | 70% of generation | $34.00/MWh, 20-year, as-generated |
| Merchant | 30% of generation | ERCOT West RT settlement point price, 15-minute settlement |
| Production tax credit | All generation | $27.50/MWh, 10 years |

**Economic curtailment rule:** the plant controller curtails merchant volume when the real-time price falls below the negative of the PTC value (approximately -$27.50/MWh), because generating at a lower price destroys value net of the credit.

**Rationale:** This single rule creates one of the best teaching artifacts in the whole design - a curtailment event that is **economically rational, price-driven, invisible in the weather data, and easily mistaken for an equipment fault** by an analyst who only looks at telemetry. It forces the analyst to join telemetry to market data to correctly attribute the loss. That is exactly the discrimination `07 §8` demands.

**Consequences:**
- New telemetry: `curtailment_reason` enum (`ECONOMIC`, `CONGESTION`, `GRID_DIRECTIVE`, `NONE`).
- New scenario: **SCN-029 negative-price economic curtailment**.
- Market price becomes a required time-series input stream, cached alongside weather.

---

### DR-009 - Financial Layer Scope - LOCKED

**Decision:** Operational financials, structured so a full pro-forma can be layered on later without redesign. Debt, tax, and IRR are **out of V1 scope**.

**In scope:**

1. **Revenue attribution** at 15-minute settlement grain: energy x applicable price, split PPA/merchant, plus PTC.
2. **Lost revenue by cause**, mapped one-to-one onto the loss buckets in `02 §7`. Every kWh of lost energy carries a cause code and a dollar value at the price prevailing when it was lost.
3. **O&M cost model:** fixed $12/kW-yr, plus event-driven costs (truck rolls, inverter repair, module cleaning campaigns, tracker repair).
4. **Contractual KPIs:**
   - Availability guarantee: 98.0%, energy-weighted, measured per IEC 61724-1.
   - Performance ratio guarantee: 80.5% year 1, declining per the degradation schedule.
   - Liquidated damages on shortfall against both.

**Rationale for excluding pro-forma:** debt schedules, MACRS, and IRR are spreadsheet finance, not time-series analytics. They add modeling surface without adding a single new temporal pattern. Cause-attributed lost revenue, by contrast, is *the* analytical product of a real asset-management team and is a pure time-series problem.

**Why this is the highest-value part of the whole simulator:** it closes the loop. A fault is no longer "inverter 12 tripped." It is "inverter 12 tripped for 4.2 hours during a $180/MWh scarcity event, costing $4,730, of which $3,310 was avoidable had the thermal warning been acted on." That is the analysis PV operators actually pay for, and it is only possible because the simulator retains ground truth per `02 §12`.

**Consequences:**
- Requires new document `18_financial_model.md`.
- Requires a `solar_settlement` fact table at 15-minute grain and a `solar_loss_attribution` table.
- The energy-loss estimates already required by `15 §5` become monetized.

---

### DR-010 - KPI Standards Anchor - LOCKED

**Decision:** Performance ratio, availability, expected energy, and reference yield follow **IEC 61724-1** definitions. Weather-adjusted expected-energy regression follows **ASTM E2848**.

**Rationale:** `02` specifies many KPIs but does not define them normatively. "Availability" alone has at least four defensible definitions (time-based, energy-weighted, contractual with exclusions, uptime). Picking a standard makes the numbers mean something and makes the portfolio work recognizable to industry reviewers.

**Consequences:** temperature-corrected PR and energy-weighted availability become required derived fields with documented formulas. The TMY run from DR-002 supplies the P50 baseline for budget-vs-actual variance.

---

### DR-011 - Storage and Analysis Stack - LOCKED

| Layer | Choice |
|---|---|
| Primary store | PostgreSQL 16 + TimescaleDB, Docker Compose, linux/arm64 |
| Portable store | Parquet, hive-partitioned by `run_id / stream / date` |
| Ad-hoc query | DuckDB over Parquet |
| Visualization | Grafana |
| Simulation engine | Python, `uv`-managed, package `northstar_sim` |

**Rationale:** DuckDB over Parquet is added deliberately. It means every SQL exercise can be done twice - once in TimescaleDB with `time_bucket` and continuous aggregates, once in DuckDB with standard window functions - which teaches the difference between time-series-specific features and portable SQL. It also means the dataset stays usable with no containers running.

---

### DR-012 - Python Runtime Pinning - LOCKED

**Decision:** Python **3.12**, pandas **2.2.x**, NumPy **1.26/2.x** per `pvlib` compatibility, `pvlib` **0.11.x**.

**Rationale:** Bleeding-edge pinning has already broken a project in this portfolio - the pandas 3.x move. The scientific PV stack (`pvlib`, `NREL-PySAM`, `solarfactors`) lags core-Python releases by a meaningful margin. A simulator whose entire value is *reproducible* output is the wrong place to chase versions.

Verify current `pvlib` support matrices before pinning; these move.

---

### DR-013 - Determinism Boundary - LOCKED

**Decision:** One master seed spawns independent named child streams via `numpy.random.SeedSequence`:

`weather_downscale`, `cloud_field`, `sensor_noise`, `sensor_drift`, `fault_schedule`, `maintenance_schedule`, `dataquality_injection`, `market_noise`.

**Rationale:** A single shared RNG means adding one fault scenario reshuffles every downstream random draw, and two runs that should differ in one respect differ in all of them. Named substreams make runs differentially comparable - you can change the fault schedule and hold the weather realization fixed, which is essential for A/B validation and for building supervised fault-classification training sets.

---

### DR-014 - Truth / Measurement Separation - LOCKED

**Decision:** Three physically separate tables per asset class:

| Table family | Contents | Analyst access |
|---|---|---|
| `*_telemetry` | Measured signals, sensor-corrupted, gapped | Yes - default |
| `*_truth` | Simulator physical state, uncorrupted | Validation mode only |
| `solar_ground_truth_events` | Injected scenario, fault, cause labels | Validation mode only |

Access is enforced by separate database roles/schemas, not by convention.

**Rationale:** `02 §12` and `13 §8` both require this, but a naming convention alone leaks. If truth columns sit in the same table, the analyst will use them accidentally and the blind-analysis success criterion in `01 §8` becomes unverifiable. Role separation makes blind analysis the default state rather than a discipline.

---

## 3. New Documents Required

| Doc | Title | Reason |
|---|---|---|
| 17 | Locked Design Decisions | This document |
| 18 | Financial and Commercial Model | DR-008, DR-009 - no coverage in 01-16 |
| 19 | External Data Acquisition Specification | DR-001, DR-002 - API sources, cache schema, refresh policy, licensing |
| 20 | KPI Definitions and Standards Mapping | DR-010 - normative formulas |

Documents requiring revision: `03` (site and equipment now locked), `06` (five-layer resource strategy), `07` (pvlib model chain), `10` (SCN-026 through SCN-029), `11` (tracker and curtailment-reason signals).

---

## 4. Revised First Build Slice

`16` proposes: weather -> one block -> inverters -> plant -> 1-minute -> 2 days -> validation.

**Revised, in dependency order:**

1. Resource cache fetch: 7 real days (one clear, one overcast, one broken-cloud, one rainy, one high-wind, one hot, one cold) from NSRDB for the locked site.
2. Layer 2 downscale to 1 minute, with the renormalization invariant asserted in test.
3. `pvlib` ModelChain for **one inverter**, no faults, no sensors - pure physics.
4. Compare against an independent `ModelChain` run. They must agree to floating-point tolerance. **This is the physics acceptance gate.**
5. Expand to one block (4 inverters) with the Layer 3 spatial field. Assert that inverters are correlated but not identical, and that plant aggregate is smoother than any individual.
6. Add sensor layer. Assert truth and measurement diverge only in the modeled ways.
7. Load to TimescaleDB. Assert continuous aggregate matches raw SQL.
8. One fault (SCN-040 inverter trip) with full event, loss attribution, and revenue impact.

Step 8 exercises the entire vertical stack - physics, state machine, event, telemetry signature, loss attribution, and dollar impact - on the smallest possible model. **Nothing expands until step 8 passes.**

---

## 5. Deliberately Deferred

These remain open because they are reversible without redesign:

- Exact continuous-aggregate refresh policies and compression settings - tune after the one-year volume test.
- Snow model (`06 §8`) - low value in West Texas.
- Battery storage - the settlement and curtailment structures in DR-008 leave room for it.
- Multi-year degradation datasets beyond 2 years.
- Reactive power and power factor modeling.
- Grafana dashboard specifications - defined in Phase 9.

---

## 6. Verification Note

API availability, product coverage, endpoint structure, and library version support all change. Before Phase 1, confirm current NSRDB PSM3 coverage and rate limits for the locked coordinates, current ERCOT price data access terms, and current `pvlib` Python/pandas support matrices. Record what is found in document 19.


---

## 8. Superseding Decisions

### DR-005 and DR-006 are SUPERSEDED by DR-016

The module and inverter selections in DR-005 and DR-006 specified equipment that
**does not exist in any available parameter database**, and pvlib's
`fit_desoto` could not derive single-diode parameters from their datasheet
values - it returned a negative series resistance and then failed on a
known-good CEC entry used as a control.

DR-016 replaces both with real CEC database entries and lets capacity re-derive.
The binding change is the inverter's actual **1200 V** DC ceiling, not the
assumed 1500 V.

Full rationale and the resulting capacity table:
`22_equipment_and_physics_gate_record` and `03 §5`.

### DR-015 remains PROVISIONAL

The plant pricing node `HRNT_SLR_RN` was selected from the ERCOT network model
by a reproducible filter, but **price-history coverage has not been confirmed**.
A node only appears in price history after its plant energizes. Validation
procedure and fallback order: `21_node_selection_record` §5 and §6.
