# 06 - Environmental Model

**Version 2.1** - supersedes v2.0. Five-layer hybrid strategy per DR-002; amended after implementation. Change log in §12.

## 1. Objective

Generate environmental drivers with realistic temporal and spatial structure.

v1.0 described *what* the environmental model must produce. v2.0 specifies *how*, because the how determines whether the requirements in `02 §2` are actually met. Independently-generated environmental signals cannot produce the cross-signal correlation, autocorrelation, and spatial coherence the analytical contract requires, regardless of how carefully each signal is individually tuned.

## 2. Architecture

Five layers. Each has a distinct provenance and a distinct role in the reproducibility model.

| Layer | Content | Source | Seeded |
|---|---|---|---|
| 1 | Real resource envelope | External API, cached | No - cached artifact |
| 2 | Temporal downscaling to 1 min | Stochastic | Yes - `weather_downscale` |
| 3 | Spatial cloud field | Stochastic + advection | Yes - `cloud_field` |
| 4 | Derived environmental state | Deterministic (pvlib) | No |
| 5 | Sensor measurement | Stochastic | Yes - `sensor_noise`, `sensor_drift` |

**Truth is layers 1-4. Measurement is layer 5.** Everything before layer 5 is simulator physical truth; only layer 5 output reaches analyst-facing telemetry. This is the `11 §8` separation, enforced architecturally rather than by naming convention.

## 3. Layer 1 - Real Resource Envelope

Specified in full in `19_external_data_acquisition`. Summary:

| Field | Source | Native cadence |
|---|---|---|
| GHI, DNI, DHI | NSRDB GOES CONUS v4 (PSM4) | 5 min |
| Ambient temperature | NSRDB GOES CONUS v4 | 5 min |
| Wind speed, direction | NSRDB GOES CONUS v4 | 5 min |
| Relative humidity, pressure | NSRDB GOES CONUS v4 | 5 min |
| Surface albedo | NSRDB GOES CONUS v4 | 5 min |
| Precipitation | Open-Meteo ERA5 archive | 60 min |
| TMY baseline | NSRDB GOES TMY v4 | 60 min |

Precipitation comes from a second source because NSRDB does not carry it, and the soiling model in §7 requires real rainfall to be meaningful.

**The simulator makes no network calls.** The cache is a versioned input artifact; the run records `cache_version` and validates checksums at startup.

**Why real data rather than synthetic.** Synthetic weather cannot produce: genuine seasonality, inter-annual variability, real correlation between wind and price, real drought and rain-event timing, or a defensible connection to a physical site. It also makes forecasting exercises circular - forecasting a generator you wrote is not forecasting.

## 4. Layer 2 - Temporal Downscaling

### 4.1 Why Downscaling Is Required

Five-minute data does not contain the sub-5-minute ramp structure that PV analytics lives in. Cloud-edge transitions, clipping onset flutter, and 1-minute ramp rates all occur below the source grain. Simple interpolation produces a smooth spline, which makes ramp analysis impossible and forecasting artificially easy.

The synthetic span here is 5 minutes to 1 minute. In v1.0 this document implied hourly source data; the improvement to 5-minute native source (`19 §2.2`) reduces the interval being synthesized by a factor of twelve and preserves real cloud dynamics that hourly data destroys.

### 4.2 Method

Downscaling operates in **clear-sky index space**, never on raw irradiance.

1. Compute 1-minute clear-sky GHI, DNI, DHI using `pvlib.clearsky.ineichen` with Linke turbidity for the site.
2. Derive the source-grain clear-sky index `kt* = GHI_observed / GHI_clearsky`.
3. Interpolate `kt*` to 1-minute resolution.
4. Superimpose a mean-reverting bounded stochastic process - Ornstein-Uhlenbeck, clipped to `[0, 1.15]` - whose **variance is conditioned on `kt*`**.
5. Permit brief excursions above `kt* = 1.0`.
6. **Renormalize** so the 1-minute mean over each source interval exactly equals the source value.
7. Reconstruct GHI, and decompose to DNI/DHI using the Erbs or DIRINT model, preserving the closure relationship.

### 4.3 Variance Conditioning

| `kt*` band | Sky condition | Relative variability |
|---|---|---|
| `> 0.90` | Clear | Near zero |
| `0.80 - 0.90` | Thin cloud | Low |
| `0.40 - 0.80` | Broken cloud | **Maximum** |
| `0.25 - 0.40` | Heavy cloud | Moderate |
| `< 0.25` | Solid overcast | Near zero |

Variability is not uniform. Clear days and solidly overcast days are both smooth; broken cloud is where ramps live. Conditioning on `kt*` is what makes SCN-004 (passing clouds) and SCN-005 (rapid cloud ramps) **emerge from real weather** rather than being scheduled.

This matters beyond realism: a scheduled scenario leaves a scheduling artifact, and an analyst can eventually learn to detect the simulator instead of the phenomenon. An emergent scenario has no artifact to find.

### 4.4 Cloud-Edge Enhancement

Step 5 is deliberate. Real irradiance exceeds clear-sky values for short periods when direct beam is unobstructed while nearby cloud edges scatter additional light onto the plane of array. Enhancement events of 10-15% above clear-sky lasting seconds to minutes are common and physically real.

They are also a frequent source of **false-positive anomaly detections** - an analyst who flags all above-clear-sky irradiance as sensor error will be wrong. Including enhancement makes the data-quality exercises honest.

### 4.5 Renormalization Invariant

Step 6 is a hard invariant, not a refinement. **It applies to irradiance, not to
the clear-sky index**:

```
mean(GHI_1min over source interval j) == GHI_source,j    for all j
```

Stating it on `kt*` is insufficient. The mean of a product is not the product of
the means, and clear-sky irradiance varies within a source interval, so
renormalizing `kt*` left GHI interval means drifting by up to 7.5 W/m2.
Enforcing it on GHI gives a maximum error of **2.3e-13 W/m2**.

**Renormalization and the physical ceiling conflict at twilight.** Scaling an
interval to match its source mean diverges where the clear-sky reference
approaches zero: unbounded, the clear-sky index reached **2.97** against a 1.15
ceiling - physically impossible irradiance that would have propagated into every
downstream analysis.

Resolution is a short fixed-point alternation between renormalizing and
clipping. Where the two are compatible both hold exactly; where they are not,
**the ceiling wins**, and the residual is reported rather than hidden. On a June
test day, 282 of 289 intervals are exact and 7 twilight intervals retain up to
1.05 W/m2.

Asserted in unit test. This guarantees that monthly and annual energy remain tied to real meteorology, that the TMY-based P50 baseline in `20 §7.2` remains comparable, and that the downscaling cannot silently drift the resource.

## 5. Layer 3 - Spatial Cloud Field

### 5.1 Why This Layer Is Load-Bearing

Without spatial structure every asset sees identical irradiance. Under that assumption:

- inverter peer comparison degrades to comparing identical inputs
- plant aggregate is exactly proportional to any single asset
- weather-station comparison has nothing to compare
- cloud-passage ramp analysis has no spatial signature

Roughly half the analyses in `02 §5` become trivial or meaningless. This is not a realism refinement; it is a requirement of the analytical contract.

### 5.2 Multi-Scale Structure

**A single correlation length cannot serve both scales.** One 1,500 m Gaussian
length left inverters 155 m apart correlated at r > 0.9989 - technically "not
identical" and with no usable structure for peer comparison whatsoever.

The field is a weighted sum of three scales:

| Correlation length | Variance share | Role |
|---|---|---|
| 150 m | 0.30 | Decorrelates neighbouring inverters |
| 700 m | 0.35 | Intra-block and block-to-block structure |
| 2,500 m | 0.35 | Coherent edges producing measurable lag |

Cross-wind correlation length is 1.5x the along-wind value: cloud streets
elongate with the flow. Grid resolution is 50 m; at 100 m the shortest scale is
undersampled and degenerates toward white noise.

### 5.3 The Field Is the Variability, Not a Modulation

The most important correction in this document.

An earlier design generated a stochastic plant-average series in Layer 2, then
applied the spatial field as a modulation on top. But that series is **identical
at every asset**, so it dominated every correlation: measured advection lag was
**0 minutes where geometry predicted 6.8**, and no field tuning could change it.

Layer 2 therefore also exposes `kt_envelope` - the deterministic interpolation
of the source, with no stochastic perturbation. **Layer 3 uses that envelope and
supplies all sub-interval variability itself.**

```
before:  kt_asset = kt_plant_perturbed x (1 + a x sigma x field)
after:   kt_asset = kt_envelope       x (1 + a x sigma x field)
```

### 5.4 The Field Wraps

The field is advected past the plant, so sizing the grid to the total distance
travelled means 126,000 km over a year at 4 m/s - 2.5 million columns and 5.3
GiB. Beyond a 10,000 km cap the along-wind coordinate **wraps**.

Wrapping is not merely a memory fix. The alternative, clipping at the grid edge,
would return an **identical value for every sample past it** - one frozen cloud
pattern for most of a multi-year record. The field is stationary, so a repeat at
roughly monthly period is statistically indistinguishable from fresh structure.

Cross-wind still clips, correctly: assets never leave the site.

### 5.5 Method

1. Generate a 2-D cloud transmissivity field over the plant footprint, with spatial correlation length drawn from the `kt*` regime.
2. Advect the field across the site at the prevailing wind vector from Layer 1.
3. Each asset with position `r` samples the field at effective time `t - (r · ŵ) / |w|`.
4. Apply low-pass smoothing proportional to asset footprint - a power block smooths more than a pyranometer.

**Cheap equivalent implementation:** a single 1-D advected series plus a per-asset time offset computed from position projected onto the wind direction, with footprint-proportional smoothing. This reproduces the essential behavior at a fraction of the cost and is the recommended V1 approach.

### 5.3 Emergent Behavior

One mechanism produces, with no additional code:

| Behavior | Analytical use |
|---|---|
| Correlated but lagged block ramps | Cloud-motion analysis, cross-correlation lag estimation |
| Plant aggregate smoother than any asset | Portfolio smoothing quantification |
| Weather-station disagreement | Sensor-vs-spatial-variability discrimination |
| Non-trivial inverter peer baseline | Underperformance detection with realistic noise floor |
| Direction-dependent lag structure | Wind-direction inference from telemetry alone |

The last one is a genuinely interesting exercise: wind direction is recoverable from the lag structure of irradiance across the plant, independent of the anemometer.

### 5.4 Plant Geometry

Asset positions are required configuration. The 10 power blocks are laid out on the 700-acre footprint with recorded (x, y) coordinates in a local site coordinate system. Footprint extent is roughly 2 km, so at a typical 8 m/s wind a cloud edge takes approximately 4 minutes to traverse the plant - clearly resolvable at 1-minute cadence and a realistic lag magnitude.

## 6. Layer 4 - Derived Environmental State

Deterministic. No randomness enters here.

### 6.1 Solar Geometry

| Quantity | Model |
|---|---|
| Solar position | `pvlib.solarposition.spa_python` (NREL SPA) |
| Sunrise, sunset, transit | `pvlib.solarposition.sun_rise_set_transit_spa` |
| Airmass | `pvlib.atmosphere.get_relative_airmass` |
| Extraterrestrial irradiance | `pvlib.irradiance.get_extra_radiation` |

Day length varies from 10.1 h to 14.2 h at this latitude - a real seasonal signal, not an imposed one.

### 6.2 Plane of Array

| Stage | Model |
|---|---|
| Tracker rotation | `pvlib.tracking.singleaxis`, backtracking enabled, +/- 60 deg |
| Front transposition | `pvlib.irradiance.get_total_irradiance`, Perez |
| Rear irradiance | `pvlib.bifacial.infinite_sheds` |

POA irradiance is the principal resource signal for production. Rear-side irradiance is simulator truth with **no corresponding sensor** - a useful concrete instance of the `11 §8` truth-versus-measurement distinction.

### 6.3 Temperature

Ambient temperature comes from Layer 1 and carries real daily cycle, seasonal
cycle, weather variation, and noise.

**The development stand-in must vary ambient too.** `clearsky_resource`
originally held ambient constant, which makes cell temperature a
near-deterministic function of irradiance via Faiman. The two regressors are
then collinear, and a regression separating them returns a temperature
coefficient of **the wrong sign** - measured at +0.50 %/C against a rated
-0.433.

It now supports a diurnal cycle whose peak **trails solar noon**, plus
day-to-day drift. The lag decouples temperature from irradiance within a day -
mid-afternoon is hotter than the equally-lit mid-morning - and the drift
decouples it across days. With varying ambient a controlled regression recovers
**-0.481 %/C**.

Real NSRDB data varies naturally, so this is an artefact of the stand-in only.
It is recorded because it would have produced a wrong-signed published
coefficient.

**Cell temperature is derived, never generated:**

```
T_cell = faiman(poa_global, temp_air, wind_speed)
```

using `pvlib.temperature.faiman`. Consequences that must be visible in the data:

- Cell temperature rises **after** irradiance rises, with thermal lag
- Wind measurably cools modules at constant irradiance
- Morning and afternoon at equal irradiance have different cell temperatures
- Two timestamps with identical POA produce different DC output because temperature differs

The last point is the entire basis of the temperature-correction exercise in `20 §4.2`.

### 6.4 Wind Height Correction

Source wind is at 10 m; module cooling models expect approximately 3 m. Apply a log wind profile with documented roughness length (0.03 m, recorded in the cache manifest per `19 §5.2`). Skipping this correction overestimates cooling and biases every temperature-related analysis.

## 7. Soiling and Precipitation

### 7.1 Model

```
soiling_ratio = pvlib.soiling.hsu(rainfall, cleaning_threshold, tilt, pm2_5, pm10, depo_veloc)
```

Driven by the **real precipitation series** from Open-Meteo. Consequences:

- Soiling accumulates during real dry spells
- Rain-cleaning events (SCN-023) occur on real rain days
- West Texas dry-season soiling ramps are long and pronounced
- Dust events produce correlated step changes across blocks

### 7.2 Manual Cleaning

Cleaning maintenance (SCN-082) resets soiling ratio to 1.0 for the cleaned assets. Because accumulation is deterministic from real rainfall and reset is stochastic, the optimal cleaning date is a genuine optimization problem with a real answer - see `18 §6.3`.

### 7.3 Per-Asset Soiling

Soiling ratio is tracked per string group, not plant-wide. This permits:

- partial cleaning campaigns (one block at a time)
- differential soiling from local dust sources
- a cleaned reference array against which soiling ratio can be estimated, per `20 §9.2`

## 8. Layer 5 - Sensor Model

Truth becomes measurement. Each sensor instance carries independent parameters.

Effects are applied in **physical order**: contamination and calibration act on
the quantity before the instrument responds to it, the instrument then lags and
adds noise, and quantization happens last in the analogue-to-digital conversion.
Quantizing before adding noise produces a visibly different artefact.

**Temperature bias is an offset, not a gain.** A 0.35 K RTD error does not scale
with the reading; modelling it multiplicatively is wrong physics that looks
entirely plausible in a chart.

| Effect | Parameter | Notes |
|---|---|---|
| Calibration bias | Per-instance, fixed | Constant offset or gain error |
| Drift | Per-instance rate | Slow, monotonic - the SCN-062 mechanism |
| Noise | Per-instance sigma | Gaussian, small |
| Quantization | Per-instance resolution | ADC granularity |
| Response time | Per-instance time constant | Pyranometers lag; thermopile slower than photodiode |
| Soiling | Pyranometer-specific | Sensors soil too, independently of modules |
| Failure | Stuck, spike, dropout | SCN-060, 061, 063 |

**The three weather stations carry independent bias and drift and therefore legitimately disagree.** Station spread is a first-class analytical signal: `20 §11` sets a typical spread threshold of 8%, above which resource measurement is treated as unreliable and PR intervals are filtered.

Critical property: **a sensor fault must not alter physical truth.** A stuck irradiance sensor reports a constant value while actual irradiance, and therefore actual production, continues to vary. This is the `09 §7` distinction and is validated explicitly in `15 §6`.

## 9. Snow

Deferred. Low value at this site - snowfall is rare in Pecos County and would exercise a mechanism that fires a handful of times per decade. Deferred to V2 per `17 §5`.

## 10. Determinism

| Layer | Seed stream | Reproducibility mechanism |
|---|---|---|
| 1 | None | Cache version + checksum |
| 2 | `weather_downscale` | `SeedSequence` child |
| 3 | `cloud_field` | `SeedSequence` child |
| 4 | None | Deterministic |
| 5 | `sensor_noise`, `sensor_drift` | `SeedSequence` children |

Named substreams per DR-013 mean the weather realization can be held fixed while the sensor realization varies, or vice versa. This is what makes controlled A/B comparison and supervised training-set construction possible.

Full reproducibility key: `(cache_version, config_version, seed, simulator_version)`.

## 11. Validation

Extending `15`:

1. Renormalization invariant holds for every source interval (§4.5).
2. Downscaled `kt*` variance is materially higher in the 0.4-0.8 band than outside it.
3. Nighttime GHI, DNI, DHI, and POA are zero at solar zenith above 95 degrees.
4. `GHI ≈ DHI + DNI * cos(zenith)` within tolerance at all daylight timesteps.
5. Cross-asset irradiance correlation decreases monotonically with separation distance.
6. Cross-asset irradiance lag correlates with the wind direction from Layer 1.
7. Plant-aggregate 1-minute ramp rate distribution has lower variance than any single asset.
8. Cell temperature peaks **after** irradiance peaks on clear days.
9. At matched irradiance and ambient temperature, higher wind speed produces lower cell temperature.
10. Soiling ratio decreases monotonically between rain events and increases only on rain or cleaning.
11. Weather-station measurements differ from truth and from each other by the modeled amounts and no more.
12. A stuck sensor's truth series continues to vary while its measured series does not.
13. Downscaled annual insolation matches source annual insolation to within 0.1%.

## 12. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Resource origin | Synthetic | Real NSRDB PSM4, 5-minute, cached |
| Architecture | Prose description | Five explicit layers with provenance |
| Cloud model | "temporally coherent changes" | `kt*`-space OU process with variance conditioning |
| Cloud-edge enhancement | Absent | Included; false-positive source for anomaly work |
| Spatial coherence | "potentially with small offsets" | Advected field, load-bearing, with emergent behavior table |
| Tracker | Not modeled | Single-axis with backtracking |
| Bifacial | Not modeled | Rear irradiance via infinite_sheds |
| Cell temperature | "should depend on" | Faiman model, named |
| Wind height | Unaddressed | Log-profile correction to 3 m |
| Soiling driver | Generic accumulation | pvlib HSU driven by real precipitation |
| Soiling granularity | Plant-level implied | Per string group |
| Snow | "Optional V1.1/V2" | Deferred, with rationale |
| Determinism | Not addressed here | Named seed substreams per layer |
| Validation | Absent | 13 explicit checks |


## 13. Changes from v2.0

| Item | v2.0 | v2.1 |
|---|---|---|
| Renormalization invariant | Stated on `kt*` | **Stated on irradiance**; kt* form drifts 7.5 W/m2 |
| Ceiling interaction | Not addressed | Fixed-point alternation; ceiling wins at twilight |
| Correlation length | Single scale | **Three scales**, 150 / 700 / 2500 m |
| Field versus modulation | Modulation on a shared series | **Field is the sole variability source** |
| Field extent | Sized to total advection | **Wraps** at a 10,000 km cap |
| Development ambient | Constant | Lagged diurnal cycle plus drift, or temperature analysis inverts |
| Sensor effect order | Listed | Physical order specified; temperature bias is an offset |


## Observed Resource Path

`clearsky_resource` is the **development fallback**, not the only source.
`northstar_sim.observed.real_resource` reads fetched NSRDB partitions and
returns the same frame contract, so it substitutes directly into
`downscale_to_minute`.

Two rules the observed path adds:

- **Gaps are interpolated only across short outages, never zero-filled.** Zero
  irradiance is a physically meaningful value - it means night - so zero-filling
  a daytime gap fabricates an outage that did not happen.
- **Components are checked against `GHI ~= DHI + DNI * cos(z)`.** A violation
  drives transposition to NaN, and a single NaN poisons the sensor layer's
  cumulative state for the entire series. Repair is bounded to sun above 84
  degrees zenith and clamped to the solar constant: dividing GHI by a near-zero
  cosine implies thousands of W/m2 and drove measured AC to 405% of nameplate.

The repair runs at **load** time, so the cache stays a faithful record of what
the provider sent. See `43`.
