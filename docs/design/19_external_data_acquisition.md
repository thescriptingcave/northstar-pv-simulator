# 19 - External Data Acquisition Specification

## 1. Purpose

DR-001 and DR-002 make the simulator dependent on real external data. This document specifies what is fetched, from where, under what terms, how it is cached, and how the cache is versioned.

**Governing constraint:**

> The simulator makes zero network calls during a simulation run. All external data is acquired ahead of time into a versioned local cache. The cache is a declared input artifact, on equal footing with the configuration file and the random seed.

This is what makes the reproducibility requirement in `14 §3` achievable. A simulator that fetches at runtime is not reproducible, regardless of how it seeds its RNG.

---

## 2. Provider Changes Verified as of August 2026

Three changes materially affect the design and supersede assumptions in earlier documents. **Re-verify before Phase 1 - these move.**

### 2.1 NREL Developer Network domain migration

The NREL developer domain has migrated. <cite index="27-1">The previous `developer.nrel.gov` domain was retired on May 29, 2026, and API references must be updated to `developer.nlr.gov`</cite>, operated under the National Laboratory of the Rockies. Any code or tutorial referencing the old host will fail.

### 2.2 PSM3 is deprecated; PSM v4 is current

<cite index="27-1">The Physical Solar Model v3.2.2 datasets have been replaced by GOES Aggregated v4.0.0</cite>. More importantly for this design, a natively high-resolution CONUS product now exists: <cite index="27-1">the NSRDB GOES CONUS dataset uses PSM v4.0.0, covers 2018 onwards over the continental United States, with 2 km spatial resolution and 5-minute temporal intervals</cite>.

**This materially improves DR-002.** The original design assumed hourly source data requiring hourly-to-1-minute stochastic downscaling. Five-minute native data reduces the synthetic span to 5-minute-to-1-minute, which is a far more defensible interpolation and preserves real cloud dynamics that hourly data destroys entirely.

### 2.3 pvlib PSM3 helpers removed, PSM4 client added

<cite index="48-1">pvlib added an NSRDB PSM v4 API client in version 0.13.0, providing `get_nsrdb_psm4_aggregated()`, `get_nsrdb_psm4_tmy()`, `get_nsrdb_psm4_conus()`, `get_nsrdb_psm4_full_disc()`, and `read_nsrdb_psm4()`</cite>. Conversely, <cite index="38-1">the older `get_psm3()`, `read_psm3()`, and `parse_psm3()` functions were removed following removal of the NSRDB PSM3 API</cite>.

**Consequence:** use `pvlib.iotools.get_nsrdb_psm4_conus()` directly rather than writing a custom NSRDB client. Any code sample found online using `get_psm3` is stale.

### 2.4 Revision to DR-012 (runtime pinning)

DR-012 recommended Python 3.12 / pandas 2.2 / pvlib 0.11 on the assumption that the PV stack lagged core Python. Verification shows this was too conservative: <cite index="42-1">pvlib 0.15.2 is the current release</cite>, <cite index="38-1">support for Python 3.9 has been dropped and gallery examples have been updated to work with pandas 3</cite>.

**Revised pinning:**

| Package | Pin | Rationale |
|---|---|---|
| Python | 3.13 | Broad wheel availability; 3.14 not yet universal across the scientific stack |
| pvlib | ==0.15.2 | Current stable; contains the PSM4 client |
| pandas | ~=2.3 | Deliberately one major behind. pvlib supports pandas 3, but pandas 3 broke an adjacent project in this portfolio and the simulator's entire value is reproducibility |
| numpy | >=1.26,<3 | |
| pyarrow | latest stable | Parquet |

Move to pandas 3.x as a deliberate, tested migration after the Phase 8 dataset validates - not as an initial condition.

---

## 3. Source Registry

| ID | Source | Content | Native cadence | Coverage | Auth |
|---|---|---|---|---|---|
| `SRC-WX-01` | NSRDB GOES CONUS v4 (PSM4) | GHI, DNI, DHI, temp, wind, humidity, pressure, albedo | 5 min | 2018- , CONUS, 2 km | API key |
| `SRC-WX-02` | NSRDB GOES TMY v4 | Same fields, typical year | 60 min | 4 km | API key |
| `SRC-WX-03` | Open-Meteo Archive (ERA5) | Temp, wind, humidity, precipitation, cloud cover | 60 min | 1940- , global | None |
| `SRC-PX-01` | ERCOT Public API - RT SPP | Real-time settlement point prices | 15 min | Rolling + archive | OAuth + subscription key |
| `SRC-PX-02` | ERCOT Public API - DAM SPP | Day-ahead settlement point prices | 60 min | Rolling + archive | OAuth + subscription key |
| `SRC-PX-03` | ERCOT Public API - LMP by settlement point | Locational marginal prices | 5 min | Rolling + archive | OAuth + subscription key |
| `SRC-GR-01` | EIA API v2 | ERCOT load, fuel mix, generation | 60 min | Historical | API key |

Precipitation is the reason `SRC-WX-03` is mandatory rather than optional: the NSRDB products do not carry a precipitation field, and the soiling model (DR-002 Layer 4) is driven by real rainfall. Open-Meteo supplies it.

---

## 4. Source Specifications

### 4.1 SRC-WX-01 - NSRDB GOES CONUS v4

| Attribute | Value |
|---|---|
| Endpoint | `/api/nsrdb/v2/solar/nsrdb-GOES-conus-v4-0-0-download` on `developer.nlr.gov` |
| Client | `pvlib.iotools.get_nsrdb_psm4_conus()` |
| Auth | NLR Developer Network API key (free registration) + email |
| Parameters | latitude, longitude, year, `time_step=5`, attribute list |
| Target years | 2019-2024 (six years, for inter-annual variability) |
| Fields | `ghi`, `dni`, `dhi`, `air_temperature`, `dew_point`, `wind_speed`, `wind_direction`, `relative_humidity`, `surface_pressure`, `surface_albedo`, `solar_zenith_angle` |

**Rate limiting.** <cite index="26-1">The API restricts the number of simultaneous requests, the number of requests per 24-hour period, and the maximum size of a single request</cite>. <cite index="25-1">Exceeding limits temporarily blocks the API key; the block lifts automatically after an hour</cite>. Consequence: fetch one site-year per request, serially, with backoff. Six years at 5-minute resolution is six requests, not a bulk pull.

**Large-request workflow.** For multi-year or polygon requests the API may return an acknowledgement rather than data, <cite index="49-1">continuing to build the archive server-side and emailing a download link when complete</cite>. The fetch client must handle both the direct-CSV and the deferred-email paths.

**Bulk alternative.** <cite index="21-1">The full datasets are available in bulk via the public S3 bucket at `data.openei.org` under `nrel-pds-nsrdb`</cite>. Use this only if API throughput becomes the bottleneck; the API path is simpler and sufficient for a single site.

**Note on PSM3-to-PSM4 differences.** PSM4 is not a cosmetic revision - community testing suggests annual AC energy estimates can differ by roughly 1-3% relative to PSM3, and PSM4 addresses a known irradiance artifact in the earlier model. Do not mix products across years within one dataset.

### 4.2 SRC-WX-02 - NSRDB GOES TMY v4

Fetched once. Supplies the P50 expected-energy baseline required by `20_kpi_definitions` for weather-adjusted budget-versus-actual variance. Hourly resolution is acceptable here because TMY is used for annual expectation, not for ramp analysis.

Client: `pvlib.iotools.get_nsrdb_psm4_tmy()`.

### 4.3 SRC-WX-03 - Open-Meteo Archive

| Attribute | Value |
|---|---|
| Endpoint | `https://archive-api.open-meteo.com/v1/archive` |
| Auth | None for non-commercial use |
| Purpose | Precipitation (primary), cross-validation of temp and wind (secondary) |
| Fields | `precipitation`, `rain`, `snowfall`, `temperature_2m`, `wind_speed_10m`, `cloud_cover`, `relative_humidity_2m` |
| Cadence | Hourly |

**Secondary use is deliberate.** Comparing Open-Meteo ERA5 reanalysis temperature against NSRDB satellite-derived temperature for the same site and hour produces a real, non-trivial disagreement between two credible sources. That disagreement is itself a teaching artifact - it is exactly the situation an analyst faces when a plant's met station disagrees with a satellite dataset, and it justifies the multi-station comparison in `02 §4`.

Wind speed requires height correction: Open-Meteo reports at 10 m, module-level cooling models expect roughly 2-3 m. Apply a log wind profile with a documented roughness length; record the correction in the cache manifest.

### 4.4 SRC-PX-01/02/03 - ERCOT Public API

| Attribute | Value |
|---|---|
| Host | `apiexplorer.ercot.com` / `developer.ercot.com` |
| Auth | Two-factor: OAuth2 ROPC token plus subscription key header |
| Token endpoint | `ercotb2c.b2clogin.com` B2C ROPC flow |
| Token lifetime | 3600 seconds - refresh required mid-fetch |
| Header | `Ocp-Apim-Subscription-Key` |
| RT SPP endpoint | `/np6-905-cd/spp_node_zone_hub` |
| DAM SPP endpoint | `/np4-190-cd/dam_stlmnt_pnt_prices` |
| RT LMP endpoint | `/np6-788-cd/lmp_node_zone_hub` |

<cite index="35-1">NP6-905 provides the Settlement Point Price for each Settlement Point, produced from SCED LMPs every 15 minutes</cite>. This is the settlement grain in `18 §2.2` and is the primary price stream.

<cite index="33-1">LMPs for each Settlement Point are normally produced by SCED every five minutes</cite> - `SRC-PX-03` is optional in V1 but worth acquiring, since 5-minute LMP against 15-minute SPP is a genuine time-series aggregation exercise.

**Recommended client.** `gridstatus` wraps the ERCOT API including token handling, pagination, and endpoint constants. Writing a raw client is possible but the B2C token flow plus pagination is meaningful incidental work with no learning value for this project. Use the library; keep the raw endpoint constants documented above so the dependency is replaceable.

**Settlement points to fetch:**

| Point | Purpose |
|---|---|
| `HB_WEST` | Hub - hedge index per `18 §3.2` |
| `LZ_WEST` | Load zone - regional context |
| Selected West Texas resource node | Plant node - basis calculation |

The resource node must be selected once and held stable. Pick an operating solar resource node in the Pecos/Fort Stockton area from the ERCOT settlement point list and record the identifier in the cache manifest.

**Price corrections.** ERCOT issues price corrections after initial publication. The fetch policy must therefore re-fetch a trailing window (default 30 days) and set `is_corrected` where values change. Silently serving stale prices is a correctness bug that will not surface until financial reconciliation fails.

### 4.5 SRC-GR-01 - EIA API v2

| Attribute | Value |
|---|---|
| Endpoint | `https://api.eia.gov/v2/electricity/rto/` |
| Auth | Free API key |
| Purpose | ERCOT system load, fuel mix, solar and wind penetration |

Provides the market context that explains price behavior: negative prices correlate with high wind plus high solar plus low load. Without this stream, price looks like exogenous noise; with it, price becomes a modelable function of system state - which is a forecasting exercise in its own right.

---

## 5. Cache Design

### 5.1 Layout

```
resource_cache/
  manifest.json
  weather/
    source=nsrdb_goes_conus_v4/
      site=northstar/
        year=2019/data.parquet
        year=2020/data.parquet
        ...
    source=nsrdb_goes_tmy_v4/
      site=northstar/data.parquet
    source=open_meteo_era5/
      site=northstar/
        year=2019/data.parquet
        ...
  prices/
    source=ercot_rt_spp/
      point=HB_WEST/
        year=2019/month=01/data.parquet
        ...
    source=ercot_dam_spp/
      ...
  grid/
    source=eia_v2/
      year=2019/data.parquet
```

Hive partitioning by year and month so DuckDB and pandas can predicate-pushdown without loading everything.

### 5.2 Manifest Schema

`manifest.json` is the versioning contract. It is the artifact that makes a simulation run reproducible.

```json
{
  "cache_version": "2026.08.1",
  "created_utc": "2026-08-21T00:00:00Z",
  "site": {
    "name": "northstar",
    "latitude": 31.35,
    "longitude": -103.30,
    "elevation_m": 850,
    "timezone": "America/Chicago",
    "ercot_settlement_point": "<resource node id>"
  },
  "sources": [
    {
      "source_id": "SRC-WX-01",
      "provider": "NSRDB GOES CONUS v4 (PSM4)",
      "host": "developer.nlr.gov",
      "endpoint": "/api/nsrdb/v2/solar/nsrdb-GOES-conus-v4-0-0-download",
      "client": "pvlib.iotools.get_nsrdb_psm4_conus",
      "client_version": "0.15.2",
      "years": [2019, 2020, 2021, 2022, 2023, 2024],
      "time_step_min": 5,
      "fields": ["ghi", "dni", "dhi", "air_temperature", "wind_speed",
                 "wind_direction", "relative_humidity", "surface_pressure",
                 "surface_albedo"],
      "row_count": 631008,
      "fetched_utc": "2026-08-21T00:14:22Z",
      "sha256": "<checksum of parquet payload>",
      "transformations": [],
      "license": "Public domain, US DOE. Attribution requested.",
      "notes": "PSM4 differs from PSM3; do not mix products across years."
    }
  ],
  "harmonization": {
    "timezone_policy": "All stored UTC, tz-aware",
    "wind_height_correction": {
      "from_m": 10, "to_m": 3, "method": "log_profile",
      "roughness_length_m": 0.03
    },
    "column_convention": "pvlib variable names"
  }
}
```

**Checksum discipline.** Every Parquet payload is hashed. A simulation run records the `cache_version` and validates checksums at startup. If a checksum mismatches, the run aborts rather than producing quietly-different output.

### 5.3 Harmonization Rules

Applied at cache-write time so the simulator consumes one consistent schema:

| Rule | Specification |
|---|---|
| Timestamps | UTC, tz-aware, interval-**beginning** labelled. Document this - NSRDB and ERCOT do not agree on convention and getting it wrong shifts everything by one interval |
| Column names | pvlib conventions (`ghi`, `dni`, `dhi`, `temp_air`, `wind_speed`) |
| Irradiance units | W/m2 |
| Temperature units | Degrees Celsius |
| Wind | m/s, corrected to 3 m |
| Precipitation | mm per interval, not cumulative |
| Prices | $/MWh, signed, never clipped at zero |
| Missing values | NaN, never zero-filled - zero-filled irradiance is indistinguishable from night |
| Leap days | Retained |
| DST | Not applicable in UTC; local conversion is a display concern only |

The missing-value rule is not a style preference. Zero-filling irradiance creates fake nighttime, which corrupts every daylight filter downstream and is silently undetectable.

---

## 6. Refresh and Versioning Policy

| Source | Refresh | Trigger |
|---|---|---|
| NSRDB CONUS | Once per year added | New year of interest |
| NSRDB TMY | Annually | Provider republication |
| Open-Meteo | Once per year added | Alignment with NSRDB years |
| ERCOT prices | Monthly, plus 30-day trailing re-fetch | Price corrections |
| EIA | Monthly | |

**Version bump rules.** Increment `cache_version` on any change to data content, harmonization rules, or source product. Never mutate a published cache version in place - a dataset generated against `2026.08.1` must remain regenerable forever.

Datasets record `cache_version` in `simulation_runs` per `14 §8`.

---

## 7. Fetch Client Requirements

The fetch client is a separate program from the simulator. Requirements:

1. Idempotent - re-running with an unchanged manifest performs no network I/O.
2. Resumable - a partial fetch resumes without refetching completed partitions.
3. Rate-limit aware - respects per-key daily and concurrent limits with exponential backoff and jitter; treats HTTP 429 as expected, not exceptional.
4. Token-refreshing - ERCOT tokens expire in 3600 s; refresh transparently mid-fetch.
5. Secret-hygienic - API keys from environment or a secrets file, never from the manifest, never committed.
6. Validating - runs §8 checks before writing; a failed check aborts the write.
7. Logging - one structured log line per request with source, range, status, row count, duration.
8. Offline-capable - a `--verify-only` mode that checks the cache without network access.

---

## 8. Acquired-Data Validation

Applied at cache-write time. These are distinct from the simulator validation in `15`.

**Structural**
- Expected row count for the interval and year, allowing for leap day
- No duplicate timestamps
- Monotonic timestamps
- Declared timezone present and UTC

**Physical**
- GHI within `[0, 1400]` W/m2
- DNI within `[0, 1100]` W/m2
- GHI approximately consistent with `DHI + DNI * cos(zenith)` within tolerance
- Nighttime GHI is zero or near-zero at solar zenith above 95 degrees
- Ambient temperature within `[-25, 55]` C for this site
- Clear-sky index `kt*` rarely above 1.2; sustained excursions indicate a bad clear-sky model or a coordinate error

**Market**
- Prices present for every 15-minute interval
- Negative prices present in the dataset - **if a full year of West Texas prices contains zero negative intervals, the fetch is wrong**
- Price magnitude within `[-$251, $5000]`/MWh, consistent with ERCOT floor and cap; verify current values, as these are periodically revised by the PUCT
- DAM and RT series cover identical date ranges

**Cross-source**
- NSRDB and Open-Meteo ambient temperature correlate above 0.9 at hourly grain. Lower correlation indicates a coordinate, timezone, or unit error, not a genuine disagreement.

The last check has caught more integration bugs in practice than any other single test. Two independent temperature sources for the same coordinates must broadly agree; when they do not, something structural is wrong.

---

## 9. Failure Modes and Fallbacks

| Failure | Detection | Response |
|---|---|---|
| API key blocked by rate limit | HTTP 429 or rejection | Backoff one hour; resume |
| NSRDB coordinate outside coverage | HTTP error from API | Verify against NSRDB Data Query endpoint before bulk fetch |
| ERCOT token expiry mid-fetch | 401 | Refresh and retry the failed page only |
| ERCOT price correction after cache write | Trailing re-fetch diff | Bump cache version, flag `is_corrected` |
| Provider domain migration | Connection failure | Documented in §2.1; re-verify host annually |
| pvlib API change across versions | Import or signature error | Pinned version per §2.4; upgrade deliberately |
| Precipitation unavailable for a year | Missing partition | Soiling model degrades to climatological reset rate; record in manifest |

**No silent fallbacks.** Every fallback is recorded in the manifest. A dataset generated with degraded inputs must be identifiable as such after the fact.

---

## 10. Licensing and Attribution

| Source | Terms |
|---|---|
| NSRDB | US Government work, public domain. Attribution to NSRDB and NLR requested in any published output |
| Open-Meteo | Free for non-commercial use; ERA5 attribution to Copernicus / ECMWF |
| ERCOT Public API | Public data; review current terms of use before redistribution |
| EIA | US Government work, public domain |

**Redistribution rule for the portfolio repository:** commit the fetch client, the manifest, and the validation report. Do **not** commit the acquired payloads. Anyone can regenerate the cache from the manifest with their own API keys. This keeps the repository small, avoids redistribution questions entirely, and demonstrates reproducible-pipeline practice - which is itself a portfolio signal.

---

## 11. Verification Checklist Before Phase 1

- [ ] NLR Developer Network API key registered and tested against `developer.nlr.gov`
- [ ] NSRDB GOES CONUS v4 confirmed to cover 31.35 N, -103.30 W via the NSRDB Data Query endpoint
- [ ] Current NSRDB rate limits read and encoded into the fetch client
- [ ] ERCOT Public API registration complete; ROPC token flow tested
- [ ] Plant resource node settlement point selected and recorded
- [ ] ERCOT price floor and cap values confirmed current
- [ ] EIA v2 API key registered
- [ ] `pvlib` version confirmed and PSM4 client signatures verified against installed version
- [ ] One test year fetched end to end and passing all §8 checks
- [ ] Cross-source temperature correlation above 0.9 confirmed

Nothing in Phase 1 should begin until this checklist is complete. Every item is cheap now and expensive after a dataset has been built on a wrong assumption.


## 12. The Synthetic Price Model Is a Development Stand-In

`northstar_sim.market.synthetic_prices` exists so the financial layer can be
built and tested without live ERCOT credentials, in the same way
`clearsky_resource` supports the physics gate. **Production prices come from the
cached ERCOT series specified in §4.4.**

Its calibration targets are recorded here for comparison against the real
series, because three failures in getting them right each produced confident
nonsense rather than an error:

| Property | Target | Note |
|---|---|---|
| Mean price | $24-30/MWh | |
| Negative intervals | 10-15% | 54% at first, making energy revenue negative overall |
| Settlement intervals below -PTC | 1-5% | Must be **structural**, not noise |
| Capture rate | 50-70% | -239% when penetration was proxied wrongly |

**Two calibration lessons that transfer to the real series:**

Penetration must be proxied by regional solar **output**, not by clearness. The
clear-sky index sits near 1.0 from sunrise to sunset, so using it suppressed
prices across the whole day and produced a generation-weighted price of
**-$3.77/MWh**.

Deeply negative prices must be **structural**, arising from oversupply. When
they came from minute-level noise, smoothing to settlement grain removed them
entirely and economic curtailment stopped firing.

Full detail: `29_financial_layer_record` §2.


---

## 13. Measured Provider Retention

`§4.4` assumed a single year list served every source. It does not.

| Source | Retention |
|---|---|
| NSRDB GOES CONUS v4 | 2018 onward |
| Open-Meteo ERA5 | decades |
| **ERCOT public API** | **~1 year** |

Measured 2026-08-30: ERCOT returns `HTTP 200` with `totalRecords` 0 for every
year before 2025. An out-of-window request is **not an error** - it succeeds
and returns nothing, which is why the failure was initially unreadable.

**Weather years must overlap price years**, or settlement, capture rate and
curtailment economics have no period on which both inputs exist. Full analysis
in `42`.

`make ercot-retention` measures the current window and prints the config line.
Re-run it before any long backfill; the window advances.


## 14. ERCOT Timestamp Handling

Three rules, each of which was violated in the first implementation:

1. **Delivery dates are Central Prevailing Time**, not UTC. Localize to
   `America/Chicago`, then convert.
2. **ERCOT labels intervals by their end**; the canonical convention is
   interval-beginning (`11 §3`). Subtract one interval at parse time.
3. **`DSTFlag` disambiguates the fall-back hour** - `True` is the daylight
   occurrence, `False` standard, matching pandas' `ambiguous` argument.

Rules 2 and 3 interact: with interval-ending labels the interval ending 02:00
is unambiguous, so the flag cannot resolve it. Both must be right together.

Detail in `42 §7`.


## 15. Settlement Point Type Selection

A load zone is published under both `LZ` and `LZEW`, and the two series are
**not identical over a full year**. Selection is by
`PREFERRED_POINT_TYPES = ("RN", "HU", "LZ", "LZEW")` and disagreements are
logged, never fatal.

Which is correct for settlement is unresolved; `make ercot-lz-compare`
quantifies the difference. See `42 §9`.


## 16. Column Matching Normalizes Separators

Provider column names vary in case **and** separator. NSRDB returns
`"Wind Speed"`; matching on `column.lower()` alone gives `"wind speed"` and
never reaches the underscored canonical keys.

Five columns went unmapped in every cached partition - wind speed, wind
direction, dew point, relative humidity, surface albedo - while `GHI`, `DNI`,
`DHI` and `Temperature` matched because they are single words. Values were
never affected; only names.

Matching now normalizes spaces and hyphens to underscores. See `43 §2`.
