# 14 - Functional and Non-Functional Requirements

**Version 2.0** - supersedes v1.0. Adds data acquisition, financial, and concrete performance requirements. Change log in §10.

## 1. Functional Requirements

The system comprises two programs: a **fetch client** and a **simulator**. They are separate by design (`19 §7`).

### 1.1 Fetch Client

The fetch client shall:

- acquire real resource, precipitation, market price, and grid context data per `19 §3`
- write a versioned, checksummed, hive-partitioned local cache
- produce a manifest recording source, endpoint, client version, fields, row counts, transformations, and checksums
- be idempotent - an unchanged manifest triggers no network I/O
- be resumable at partition granularity
- respect provider rate limits with backoff and jitter
- refresh expiring auth tokens transparently
- re-fetch a trailing window to capture market price corrections
- validate acquired data per `19 §8` and refuse to write on failure
- operate in a `--verify-only` mode requiring no network access

### 1.2 Simulator

The simulator shall:

- load and validate a complete plant configuration before simulating
- load and checksum-verify the resource cache; **abort on mismatch**
- **make zero network calls during a run**
- downscale and spatially distribute the environmental resource per `06`
- calculate production through the `pvlib` model chain per `07 §3`
- compute an independent unconstrained-truth baseline per `07 §4`
- update asset operating states per `08`
- execute scheduled, probabilistic, and condition-triggered scenarios
- generate telemetry at the cadences in `11 §3`
- generate events and alarms per `12`
- apply the sensor model, producing measured signals distinct from truth
- inject data-quality defects
- calculate plant aggregates
- **compute the loss attribution waterfall and verify closure**
- **join market prices and compute settlement per `18 §3`**
- **execute the economic curtailment rule per `18 §4.1`**
- **accrue O&M costs and guarantee positions**
- **compute KPIs per `20`**
- preserve deterministic behavior for a fixed configuration, cache version, and seed
- support historical batch generation
- support incremental / streaming-style generation
- load to PostgreSQL/TimescaleDB and export to Parquet
- preserve stable asset IDs across a run
- write run metadata and a validation report

## 2. Configuration Requirements

Configuration controls:

| Group | Parameters |
|---|---|
| Site | Coordinates, elevation, timezone, albedo, footprint geometry |
| Plant | Block count, inverters per block, combiners, strings, module and inverter types |
| Array | Tracker type, rotation limits, GCR, backtracking, bifaciality, height |
| Simulation | Start, end, timestep, master seed |
| Resource | Cache version, source years, downscaling parameters, cloud field parameters |
| Scenarios | Schedule, probabilities, trigger conditions |
| Sensors | Per-instance bias, drift, noise, failure rates |
| Data quality | Defect types, rates, targets |
| Commercial | Hedge volume and strike, PTC rate, curtailment threshold and hysteresis, cost rates, guarantee terms |
| Output | Database connection, Parquet path, streams enabled |

Configuration is versioned and snapshotted into `meta.config_versions` for every run.

## 3. Reproducibility

Given identical:

- configuration version
- **resource cache version**
- master seed
- simulator version
- start and end times

the simulator shall reproduce equivalent results.

**Cache version is part of the key.** A simulator that fetches at runtime is not reproducible regardless of how carefully it seeds its RNG, which is why `19 §1` forbids runtime network access.

Seeding uses `numpy.random.SeedSequence` with named child streams per DR-013: `weather_downscale`, `cloud_field`, `sensor_noise`, `sensor_drift`, `fault_schedule`, `maintenance_schedule`, `dataquality_injection`, `market_noise`.

**Differential reproducibility requirement:** changing one substream's seed shall not perturb the others. Two runs differing only in fault schedule must have byte-identical weather realizations. This is what makes controlled A/B comparison and supervised training-set construction possible, and it is testable.

## 4. Performance

### 4.1 Targets

| Metric | Target |
|---|---|
| Telemetry rows per simulated year | ~89 million |
| Generation throughput | 1 simulated year in under 60 minutes on an Apple Silicon workstation |
| Database load | 89M rows in under 45 minutes with COPY and deferred indexing |
| Compressed storage | Under 25 GB per simulated year, post-compression |
| Dashboard query (plant overview, 30 days, from continuous aggregate) | Under 2 seconds |
| Raw-table equivalent query | Measured and compared - the contrast is a curriculum item |
| Parquet export | Under 15 minutes per simulated year |

These are targets, not guarantees. They exist so that a Phase 8 volume test has something to pass or fail against; revise them once measured rather than treating a miss as acceptable by default.

### 4.2 Scaling Behavior

Generation must be chunkable by time so a 3-year dataset is produced as a sequence of bounded-memory runs, not one process holding three years in memory.

## 5. Testability

Every major model exposes deterministic invariants. Scenarios have expected outcomes. The design permits:

- unit tests per model
- **physics oracle tests** - simulator output versus independent `pvlib.ModelChain` under fault-free conditions
- integration tests across the production chain
- end-to-end dataset validation per `15`
- statistical validation of correlations and distributions
- **financial reconciliation tests** per `18 §10`
- **KPI recovery tests** - injected degradation, availability, and PR recovered by independent estimation per `20 §14`
- regression tests against a golden small dataset

The physics oracle test is the strongest single test in the suite and gates Phase 1.

## 6. Extensibility

Possible without core redesign:

- battery storage (settlement and curtailment structures accommodate it)
- multiple sites
- more detailed string modeling
- day-ahead offer strategy and DA/RT arbitrage
- ancillary services
- additional SCADA signals
- alternative markets (CAISO, MISO) via the source registry in `19 §3`
- full pro-forma financial layer

The source registry and cache manifest are what make a market change tractable - the fetch client changes, the simulator does not.

## 7. Data Quality

Generated data must satisfy:

- explicit units on every field
- documented ranges
- timestamp consistency and declared interval convention
- referential integrity across schemas
- causal consistency
- no unexplained impossible values
- controlled, configurable missingness and anomalies
- **mandatory field classification** per `11 §11`
- **loss waterfall closure** within 0.5%

## 8. Observability

Every run produces a summary containing:

- run ID, start and end wall-clock time
- configuration version
- **resource cache version and checksum status**
- master seed and derived substream seeds
- simulator version and pvlib version
- records generated per stream
- scenarios executed, with instance counts
- faults injected
- **loss waterfall closure residual**
- **financial reconciliation status**
- validation status per `15`
- warnings and errors

Written to `meta.simulation_runs` and to a human-readable report file.

## 9. Security and Secrets

- API keys from environment variables or a gitignored secrets file, never from configuration or manifest
- Database credentials likewise
- The `analyst` role has no SELECT on the `truth` schema; this is verified by an automated test, not assumed
- No acquired data payloads committed to the repository (`19 §10`)

## 10. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Program structure | Single simulator | Fetch client plus simulator, separated |
| Data acquisition | Absent | Full functional requirement set |
| Network policy | Unaddressed | Zero network access during simulation, enforced |
| Reproducibility key | Config, seed, version, time | Adds cache version |
| Differential reproducibility | Absent | Required and testable |
| Performance targets | "should support... without unrealistic resources" | Seven concrete targets |
| Financial requirements | Absent | Settlement, attribution, cost, guarantees |
| Testability | Five test types | Adds physics oracle, financial reconciliation, KPI recovery |
| Configuration | 13 items | Grouped table including commercial and resource groups |
| Observability | 9 fields | Adds cache version, closure residual, financial status, pvlib version |
| Security | Absent | Secrets policy and verified role isolation |


## 11. Measured Performance

`§4.1` set targets before anything ran. These are the measured figures; where
they differ, the measurement governs.

| Metric | Target | Measured |
|---|---|---|
| Generation, one simulated year | under 60 min | **13.6 min** |
| Per inverter-day | - | 0.022 s (0.373 s before shared geometry) |
| Three-year hourly dataset | - | 103 s |
| Parquet storage per simulated year | under 25 GB | **2.48 GB** |
| Bytes per row | - | 44 |
| Export, 3 days / 462k rows | - | 2.2 s |

The 17-fold throughput gain came from two sources of pure duplicated work:
`retrieve_sam` re-parsing the CEC databases on every call, and solar geometry
recomputed per inverter despite depending only on location, time and tracker
geometry. See `24_plant_scaleout_record`.

**Database load, compression ratio and continuous-aggregate refresh remain
unmeasured** - they need a running server.
