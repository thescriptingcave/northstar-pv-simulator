# northstar-fetch

External resource and market data acquisition for the NorthStar PV solar farm simulator.

This is **Phase 0.5** of the implementation roadmap. It produces the versioned, checksummed cache that every later phase consumes. The simulator itself performs no network access — which is what makes a simulation run reproducible.

Implements design document `19_external_data_acquisition`.

## Why a separate program

A simulator that fetches at runtime is not reproducible, no matter how carefully it seeds its random number generator. Separating acquisition from simulation makes the cache a declared input artifact on equal footing with the configuration file and the seed:

```
reproducibility key = (cache_version, config_version, seed, simulator_version)
```

## Install

From the repository root:

```bash
uv venv --python 3.13
uv sync
cp .env.example .env   # then fill in credentials
```

Site configuration lives at `config/northstar.toml` in the repository root, not
in this package: the simulator needs the same site identity, and two copies
would drift silently.

Credentials are read from the environment only. They never appear in the configuration file or the manifest, so both are safe to commit.

| Variable | Source | Register at |
|---|---|---|
| `NREL_API_KEY`, `NREL_EMAIL` | NSRDB | https://developer.nlr.gov/signup/ |
| `ERCOT_USERNAME`, `ERCOT_PASSWORD`, `ERCOT_SUBSCRIPTION_KEY` | ERCOT | https://developer.ercot.com/ |
| `EIA_API_KEY` | EIA | https://www.eia.gov/opendata/ |

## Use

```bash
northstar-fetch --config config/northstar.toml plan       # what would be fetched — no network access
northstar-fetch --config config/northstar.toml fetch   # acquire missing partitions
northstar-fetch --config config/northstar.toml fetch --force
northstar-fetch --config config/northstar.toml verify  # offline integrity check
northstar-fetch --config config/northstar.toml summary # cache contents by source
northstar-fetch select-node <extract-dir>              # rank pricing-node candidates
```

`verify` is what the simulator runs at startup and what a reviewer runs to confirm a dataset is regenerable. It needs no credentials and no network.

## Sources

| ID | Source | Content | Cadence | Coverage |
|---|---|---|---|---|
| `SRC-WX-01` | NSRDB GOES CONUS v4 (PSM4) | GHI, DNI, DHI, temp, wind, humidity, pressure, albedo | 5 min | 2018–, CONUS, 2 km |
| `SRC-WX-02` | NSRDB GOES TMY v4 | Typical year, P50 baseline | 60 min | 4 km |
| `SRC-WX-03` | Open-Meteo Archive (ERA5) | Precipitation, plus temp/wind cross-check | 60 min | 1940– |
| `SRC-PX-01` | ERCOT RT settlement point prices | Signed prices, 3 settlement points | 15 min | Archive |
| `SRC-PX-02` | ERCOT DAM settlement point prices | Day-ahead, for DA/RT spread | 60 min | Archive |

Open-Meteo is **not optional**. NSRDB carries no precipitation field, and the soiling model is driven by real rainfall. Its temperature series doubles as the independent cross-check that catches coordinate and timezone errors.

## Provider notes

Three facts invalidate most published tutorials, verified August 2026:

- The NREL developer domain was retired on 29 May 2026. Requests go to `developer.nlr.gov`.
- PSM v3.2.2 is deprecated. PSM v4 GOES CONUS supplies **5-minute, 2 km** data from 2018 — which is why this design downscales only 5 min → 1 min rather than an hour → 1 min.
- pvlib removed `get_psm3` and added `get_nsrdb_psm4_*` in 0.13. This package uses the PSM4 client when available and falls back to direct HTTP otherwise.

Re-verify before a fetch campaign. These move.

## Design notes

**Idempotent and resumable.** A partition is skipped only when the manifest records it *and* its checksum still matches. A partial or corrupted write is treated as absent, so an interrupted run self-heals rather than carrying bad data forward.

**Manifest written per partition**, not at the end. A run killed halfway leaves a valid manifest describing exactly what completed.

**Validation blocks the write.** A failing tier-0 check aborts caching. Refusing the write is far cheaper than discovering at Phase 3 that a dataset was built on a coordinate error.

**Two checks are load-bearing:**

- *Night irradiance must be zero* above 95° zenith. Non-zero night sun is almost always a timezone error, and it is silently undetectable once cached.
- *A West Texas price year must contain negative intervals.* Zero negatives does not mean an unusual market; it means the price field was clipped, coerced or mis-parsed. Asserted at year level, since a single month can legitimately contain none.

**Missing values stay `NaN`.** Zero-filled irradiance is indistinguishable from night, corrupts every daylight filter downstream, and cannot be recovered from.

**Interval convention is normalized to interval-beginning.** NSRDB labels at the beginning, ERCOT at the end. Getting this wrong shifts every series by one interval and does not surface until financial reconciliation fails.

## Cache layout

```
resource_cache/
  manifest.json
  weather/source=nsrdb_goes_conus_v4/site=northstar/year=2023/data.parquet
  weather/source=open_meteo_era5/site=northstar/year=2023/data.parquet
  prices/source=ercot_rt_spp/point=HB_WEST/year=2023/month=06/data.parquet
```

Hive-partitioned so DuckDB and pandas push predicates down without loading the tree.

**Do not commit the payloads.** Commit the fetch client, the configuration and the manifest. Anyone can regenerate the cache with their own keys — which keeps the repository small, sidesteps redistribution questions, and demonstrates reproducible-pipeline practice.

## Extending

Adding a provider or a market means one `Source` subclass and one registry entry in `orchestrator.SOURCE_REGISTRY`. Partitioning, skipping, validation, checksumming and manifest updates are all handled by the orchestrator.

## Tests

```bash
pytest -q
ruff check src/ tests/
```

29 tests, no network access. Provider behaviour is supplied through stub adapters, so the whole pipeline is testable offline and deterministically. Notable cases:

- a second `run()` refetches nothing (idempotency)
- a corrupted parquet file is treated as absent, not as valid data
- a 12-hour irradiance roll against the zenith angle is caught as the night-sun timezone signature
- a failed validation leaves the partition uncached and the manifest accurate
- a cache version bump invalidates existing records rather than merging them

## Status

Code complete and tested offline. Three items from `19 §11` remain and require live credentials:

- [ ] API registrations complete, endpoints reachable
- [ ] One test year fetched end to end, passing all tier-0 checks
- [ ] Cross-source temperature correlation above 0.9 confirmed

The plant pricing node is `HRNT_SLR_RN`, selected per DR-015 and locked provisionally. Price-history coverage must be confirmed before the full multi-year fetch; `docs/design/21_node_selection_record.md` §6 gives the fallback order.
