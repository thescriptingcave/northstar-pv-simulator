# 11 - Telemetry Specification

**Version 2.0** - supersedes v1.0. Adds tracker telemetry, curtailment semantics, bifacial fields, market prices, and explicit cadence and classification. Change log in §14.

## 1. Telemetry Philosophy

Every signal must have:

- an analytical purpose
- a source asset
- a unit
- a cadence
- a valid range
- a documented relationship to other signals
- defined missing-data behavior
- quality semantics
- **a classification: truth, measured, derived, or ground truth** (new in v2.0, mandatory)

A signal without a classification is a leakage risk. See §9.

## 2. Common Fields

Every telemetry row includes:

| Field | Type | Notes |
|---|---|---|
| `time` | timestamptz | UTC, tz-aware, interval-beginning |
| `asset_id` | text | Stable across the run per `05 §11` |
| `run_id` | uuid | Simulation run / dataset identifier |
| `quality` | text | `GOOD`, `SUSPECT`, `STALE`, `MISSING`, `ESTIMATED` |

`time` convention is interval-beginning. NSRDB and ERCOT do not agree on this, and the harmonization rule in `19 §5.3` resolves it at cache-write time. Getting it wrong shifts everything by one interval and is invisible until reconciliation fails.

## 3. Cadence and Cardinality

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
| Market prices | 3 points | 15 min | 288 |

Approximately 89 million telemetry rows per simulated year. Cadence is locked per DR-007.

## 4. Weather Telemetry

| Field | Unit | Class | Range |
|---|---|---|---|
| `ghi_wm2` | W/m2 | Measured | 0 - 1400 |
| `dni_wm2` | W/m2 | Measured | 0 - 1100 |
| `dhi_wm2` | W/m2 | Measured | 0 - 800 |
| `poa_global_wm2` | W/m2 | Measured | 0 - 1500 |
| `poa_direct_wm2` | W/m2 | Derived | 0 - 1400 |
| `poa_diffuse_wm2` | W/m2 | Derived | 0 - 500 |
| `poa_rear_wm2` | W/m2 | **Truth** | 0 - 300 |
| `ambient_temp_c` | C | Measured | -25 - 55 |
| `module_temp_c` | C | Measured | -25 - 90 |
| `wind_speed_ms` | m/s | Measured | 0 - 35 |
| `wind_direction_deg` | deg | Measured | 0 - 360 |
| `relative_humidity_pct` | % | Measured | 0 - 100 |
| `surface_pressure_hpa` | hPa | Measured | 850 - 1050 |
| `precipitation_mm` | mm | Measured | 0 - 100 |
| `albedo` | - | Measured | 0 - 1 |
| `clearsky_ghi_wm2` | W/m2 | Derived | 0 - 1200 |
| `clearsky_index` | - | Derived | 0 - 1.15 |
| `solar_zenith_deg` | deg | Derived | 0 - 180 |
| `solar_azimuth_deg` | deg | Derived | 0 - 360 |

**`poa_rear_wm2` is truth with no sensor.** Rear irradiance is not measured in real plants. It is included as simulator truth because bifacial gain must be attributable, and it is the clearest concrete example of the truth-versus-measurement distinction.

Three stations, independently biased and drifting per `06 §8`. Station disagreement is a first-class signal, not an error.

## 5. Inverter Telemetry

| Field | Unit | Class |
|---|---|---|
| `dc_voltage_v` | V | Measured |
| `dc_current_a` | A | Measured |
| `dc_power_kw` | kW | Measured |
| `ac_voltage_v` | V | Measured |
| `ac_current_a` | A | Measured |
| `ac_power_kw` | kW | Measured |
| `ac_frequency_hz` | Hz | Measured |
| `power_factor` | - | Measured |
| `efficiency_pct` | % | Derived |
| `internal_temp_c` | C | Measured |
| `heatsink_temp_c` | C | Measured |
| `available_power_kw` | kW | **Truth** |
| `commanded_power_kw` | kW | Measured (setpoint) |
| `operating_state` | enum | Measured |
| `fault_code` | text | Measured |
| `derate_reason` | enum | Measured |
| `daily_energy_kwh` | kWh | Derived |

### 5.1 The Discriminating Triple

`available_power_kw`, `commanded_power_kw`, and `ac_power_kw` together are what make the four-cause discrimination in `07 §9.1` possible:

| Condition | available | commanded | actual |
|---|---|---|---|
| Normal | = actual | = nameplate | = available |
| Clipping | > actual | = nameplate | = AC limit |
| Curtailment | > actual | < nameplate | = commanded |
| Thermal derate | > actual | = nameplate | < available, declining |
| Fault | any | any | ~0 |

`available_power_kw` is classified **truth**, not measured, because real inverters estimate it imperfectly. Its presence in analyst-facing telemetry is a deliberate simplification; making it truth-only would render curtailment undetectable, which is too hard. Record it as a known simplification.

`derate_reason` enum: `NONE`, `THERMAL`, `GRID_VOLTAGE`, `GRID_FREQUENCY`, `DC_LIMIT`, `COMMANDED`.

## 6. Combiner Telemetry

| Field | Unit | Class |
|---|---|---|
| `dc_current_a` | A | Measured |
| `dc_voltage_v` | V | Measured |
| `dc_power_kw` | kW | Derived |
| `channels_active` | int | Measured |
| `channels_total` | int | Configuration |
| `imbalance_pct` | % | Derived |
| `fuse_status` | enum | Measured |
| `soiling_ratio` | - | **Truth** |
| `state` | enum | Measured |

Cadence 5 minutes. 480 combiners is the largest stream by volume and exists to give string-imbalance detection (SCN-044) a real peer population.

`imbalance_pct` is defined as `(max_channel_current - mean_channel_current) / mean_channel_current`. Definition matters - alternatives using standard deviation or min-max give different thresholds.

## 7. Tracker Telemetry - NEW in v2.0

| Field | Unit | Class |
|---|---|---|
| `tracker_angle_deg` | deg | Measured |
| `commanded_angle_deg` | deg | Measured |
| `angle_error_deg` | deg | Derived |
| `tracker_state` | enum | Measured |
| `backtracking_active` | bool | Measured |
| `motor_current_a` | A | Measured |
| `stow_reason` | enum | Measured |

`tracker_state`: `TRACKING`, `BACKTRACKING`, `STOWED_WIND`, `STOWED_NIGHT`, `STOWED_MAINT`, `STUCK`, `FAULT`.

`stow_reason`: `NONE`, `WIND`, `NIGHT`, `MAINTENANCE`, `MANUAL`, `SENSOR_FAULT`.

`angle_error_deg` is the SCN-026 and SCN-028 detection signal. In real plants it is often unavailable, so the intended exercise is to detect stuck and misaligned trackers from **production data alone** and then validate against the angle telemetry.

## 8. Transformer Telemetry

| Field | Unit | Class |
|---|---|---|
| `input_power_kw` | kW | Measured |
| `output_power_kw` | kW | Measured |
| `loading_pct` | % | Derived |
| `primary_voltage_v` | V | Measured |
| `secondary_voltage_v` | V | Measured |
| `current_a` | A | Measured |
| `oil_temp_c` | C | Measured |
| `winding_temp_c` | C | Measured |
| `efficiency_pct` | % | Derived |
| `state` | enum | Measured |

Winding temperature responds to loading with first-order thermal lag. The lag is the SCN-045 mechanism and is required by §11.

## 9. Plant Telemetry

| Field | Unit | Class |
|---|---|---|
| `total_dc_power_kw` | kW | Derived |
| `total_ac_power_kw` | kW | Derived |
| `grid_export_power_kw` | kW | Measured |
| `available_power_kw` | kW | **Truth** |
| `export_limit_kw` | kW | Measured |
| `curtailment_setpoint_kw` | kW | Measured |
| `curtailment_reason` | enum | Measured |
| `daily_energy_kwh` | kWh | Derived |
| `cumulative_energy_mwh` | MWh | Measured |
| `active_inverter_count` | int | Derived |
| `plant_availability_pct` | % | Derived |
| `performance_ratio` | - | Derived |
| `plant_state` | enum | Measured |
| `grid_voltage_v` | V | Measured |
| `grid_frequency_hz` | Hz | Measured |

`curtailment_reason` enum per `18 §4.3`: `NONE`, `ECONOMIC`, `CONGESTION`, `GRID_DIRECTIVE`, `VOLTAGE_FREQ`, `MAINTENANCE`.

This enum is the only telemetry-side hint that an economic curtailment occurred. It is deliberately **not** sufficient on its own to establish that curtailment was economically rational - that requires the price join per `18 §4.2`.

## 10. Market Price Stream - NEW in v2.0

Not telemetry in the SCADA sense, but a required time-series input joined to telemetry for all financial analysis.

| Field | Unit | Class |
|---|---|---|
| `time` | timestamptz | - |
| `settlement_point` | text | - |
| `price_type` | enum | `RT_SPP`, `DA_SPP`, `RT_LMP` |
| `price_usd_mwh` | $/MWh | External |
| `is_corrected` | bool | External |

15-minute cadence for SPP. **Prices are signed and never clipped at zero.** A price series with no negative values in a West Texas year is wrong, per `19 §8`.

Three settlement points: `HB_WEST` (hedge index), `LZ_WEST` (regional), plant resource node (basis).

## 11. Field Classification - MANDATORY

Every field carries exactly one class:

| Class | Definition | Analyst access | Example |
|---|---|---|---|
| **Truth** | Simulator physical state, uncorrupted | Restricted role | `poa_rear_wm2`, `soiling_ratio` |
| **Measured** | Sensor output, noisy, biased, gappable | Default | `ac_power_kw`, `ghi_wm2` |
| **Derived** | Computed from measured fields | Default | `efficiency_pct`, `loading_pct` |
| **Ground truth** | Injected scenario labels | Restricted role | `scenario_id`, `cause_code` |
| **External** | Third-party data | Default | `price_usd_mwh` |
| **Configuration** | Static asset property | Default | `channels_total` |

Enforced by database role and schema separation per DR-014, not by naming convention. A naming convention leaks - an analyst will use a truth column accidentally and the blind-analysis criterion in `01 §8` becomes unverifiable.

**Known exception:** `available_power_kw` is truth-classified but exposed to analysts, per §5.1. Documented deliberately rather than hidden.

## 12. Sampling and Quality Behavior

Stable nominal cadence, with configurable deviations:

| Behavior | Mechanism | Scenario |
|---|---|---|
| Gaps | Sample dropped entirely | SCN-060 |
| Communication outage | Contiguous block of gaps | SCN-064 |
| Stuck value | Value frozen; truth continues | SCN-061 |
| Drift | Slow monotonic bias | SCN-062 |
| Spikes | Isolated implausible values | SCN-063 |
| Duplicates | Repeated asset/time key | SCN-065 |
| Timestamp skew | Per-asset clock offset | SCN-066 |
| Jitter | Small timestamp perturbation | Configurable |

`quality` field values:

| Value | Meaning |
|---|---|
| `GOOD` | Nominal |
| `SUSPECT` | Failed a plausibility check |
| `STALE` | Repeated value beyond threshold |
| `MISSING` | Expected sample absent |
| `ESTIMATED` | Filled by the simulator's own gap handling |

**The quality flag is itself a measured artifact and can be wrong.** A stuck sensor may report `GOOD` throughout. Trusting the flag rather than testing the data is a mistake the dataset should permit.

## 13. Required Correlations

Verified in `15 §4`:

| Relationship | Expected |
|---|---|
| POA irradiance to inverter DC power | Strong positive, near-linear |
| DC power to AC power | Strong positive, efficiency-modified, plateaus at clipping |
| Ambient + POA to module temperature | Positive, with thermal lag |
| Wind speed to module temperature | Negative at matched irradiance |
| Transformer loading to winding temperature | Positive, with lag |
| Grid export to aggregate AC power | Near-unity unless curtailed or disconnected |
| Daily energy to integrated power | Exact within tolerance |
| Tracker angle to time of day | Deterministic, backtracking-modified |
| Cross-asset POA | Correlated, lagged by wind advection |
| Rear POA to albedo and tracker angle | Positive |
| Soiling ratio to days since rain | Negative, monotonic between resets |
| Plant output to price | **Negative** on average - the merchant solar problem |

The final row is the only correlation in the table that is economic rather than physical, and it is the most consequential: solar generates most when solar is worth least. It emerges only from joining real prices to real production shape.

## 14. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Field classification | "the specification must label" | Mandatory column with six classes and role enforcement |
| Cadence table | Prose, "1-5 minutes depending" | Locked table with row volumes |
| Tracker telemetry | Absent | Full stream, seven fields |
| Bifacial | Absent | `poa_rear_wm2` as truth signal |
| Curtailment reason | Absent | Six-value enum |
| Derate reason | Absent | Six-value enum |
| Discriminating triple | Implied | Explicit table mapping condition to signal pattern |
| Market prices | Absent | New stream with settlement points |
| Precipitation | "optional" | Required - drives soiling |
| Clear-sky reference | Absent | `clearsky_ghi_wm2`, `clearsky_index` |
| Quality field | "optional quality/status" | Required, five values, explicitly fallible |
| Transformer temperature | Single field | Oil and winding, separately |
| Combiner soiling | Absent | Per-combiner truth field |
| Correlations | 6 examples | 12, including the economic one |


## 15. Fields Added in Implementation

### 15.1 Inverter (§5)

| Field | Class | Purpose |
|---|---|---|
| `available_power_kw` | Truth | What the inverter could have produced |
| `commanded_power_kw` | Measured | The controller setpoint |
| `curtailed_power_kw` | Derived | Available less delivered, where a setpoint bound |
| `state_reason` | Measured | Why the state is what it is; required for the event join |
| `ac_preclip_kw` | Truth | Sandia output before the AC cap - clipping is unmeasurable without it |
| `internal_temp_c` | Measured | Drives thermal derating |
| `thermal_derate_factor` | Truth | The reduction imposed |
| `dc_ideal_kw` | Truth | DC before plant losses |

**Available and commanded power together are what make curtailment
distinguishable from failure.** Without both, an analyst cannot tell a plant
that was told to stop from one that broke.

### 15.2 Classification Is Load-Bearing (§11)

Measured fields now **genuinely differ from truth**. Verified after a full
round trip through Parquet: 1137.05 kW measured against 1130.79 kW truth for the
same inverter over the same window.

A dataset where the two agree has lost its sensor layer, and nothing else would
report an error.

### 15.3 Quality Flags Are Fallible (§12)

Implemented with deliberate detection rates below 1.0. Roughly **half of
injected defects carry no flag**. Trusting the flag rather than testing the data
is a mistake the dataset permits by design - see `12 §10`.
