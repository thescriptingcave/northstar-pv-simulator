# 23 - Spatial Cloud Field Record (Phase 3)

## Result: PASSED

```
  [PASS] correlated_not_identical     GHI correlation 0.9521 .. 0.9998
  [PASS] advection_lag_matches        worst lag error 0.5 min against geometry
  [PASS] aggregate_smoothing          ramp std ratio 0.2705 (aggregate / individual)
  [PASS] lagged_correlation_recovers  zero-lag 0.175 -> 0.980 at +6 min
```

Thirteen positioned assets - three weather stations and ten power blocks - spanning a 3,260 x 845 m footprint, on a broken-cloud day (kt* ~ 0.62) with an 8 m/s wind from the west.

---

## 1. Why This Layer Is Load-Bearing

Without spatial structure every asset sees identical irradiance. Under that assumption:

- inverter peer comparison degrades to comparing identical inputs
- plant aggregate is exactly proportional to any single asset
- weather-station comparison has nothing to compare
- cloud-passage ramp analysis has no spatial signature

Roughly half the analyses in `02 §5` become trivial or meaningless. The requirement comes from the analytical contract, not from realism for its own sake.

---

## 2. Three Errors Found

### 2.1 A single correlation length cannot serve both scales

The first implementation used one 1,500 m Gaussian correlation length. Result: inverters 155 m apart correlated at **r > 0.9989**. Technically "not identical", and technically passing a naively worded acceptance criterion - but with no usable structure for peer comparison, which is the entire purpose of the layer.

Real cloud fields are multi-scale. The field is now a weighted sum of three scales:

| Correlation length | Variance share | Role |
|---|---|---|
| 150 m | 0.30 | Decorrelates neighbouring inverters |
| 700 m | 0.35 | Intra-block and block-to-block structure |
| 2,500 m | 0.35 | Coherent cloud edges that produce measurable lag |

Grid resolution moved from 100 m to 50 m; at 100 m the shortest scale was undersampled and degenerated toward white noise.

### 2.2 Grid extents derived from the wrong dimension

`along_origin_m` was set from `across_extent_m` - a copy-paste error. Along-wind coordinates therefore clipped to the grid edge, and distinct assets silently returned identical field samples. **No error was raised.** The field extents are now explicit ranges (`along_min_m`, `along_max_m`, `across_half_width_m`) covering exactly the coordinates any asset will request, and a test asserts the grid spans what it is asked for.

A related shape bug: `numpy.convolve` in `same` mode returns `max(len(a), len(kernel))`, not `len(a)`. A smoothing kernel wider than the grid silently changed the array shape. Kernel radius is now capped at half the array dimension.

### 2.3 The field must *be* the variability, not modulate it

The important one.

The first design generated a stochastic plant-average series in Layer 2, then applied the spatial field as a multiplicative modulation on top. But the plant-average series is **identical at every asset**, so it dominated every correlation. Measured advection lag was **0 minutes where geometry predicted 6.8**, and no amount of tuning the field could change that - the common term simply outweighed the spatial one.

The temporal variability an asset sees must come *from* the advected field. Layer 2 now also exposes `kt_envelope`, the deterministic interpolation of the source with no stochastic perturbation. Layer 3 uses that envelope and supplies all sub-interval variability itself.

**Before:** `kt_asset = kt_plant_perturbed x (1 + a x sigma x field)`
**After:** `kt_asset = kt_envelope x (1 + a x sigma x field)`

Measured lag immediately matched geometry.

---

## 3. Mechanism

A stationary multi-scale field is generated once in a cloud-fixed frame and advected past the site. An asset at position `r` samples at:

```
along coordinate  = travel(t) - (r . w_hat)
across coordinate = r . w_perp
```

where `travel(t)` is the cumulative integral of wind speed. Using cumulative displacement rather than a fixed lag means a change in wind speed correctly changes how fast structure arrives.

Two coordinates are needed. Along-wind carries the advection and therefore the lag; cross-wind carries decorrelation without lag. Pure time-shifting of one series would make every asset see an *identical* series at a different time - correlation exactly 1.0 at the right lag, which is not what a real plant looks like.

Cross-wind correlation length is 1.5x the along-wind value, because cloud streets elongate with the flow.

---

## 4. Measured Behaviour

**Advection lag versus geometry**, stations spanning 3,260 m at 8 m/s from the west:

| Pair | Separation | Predicted | Measured |
|---|---|---|---|
| WS1 -> WS2 | 1,630 m | +3.4 min | **+3 min** |
| WS1 -> WS3 | 3,260 m | +6.8 min | **+7 min** |

**Correlation, zero-lag versus optimal-lag:**

| Pair | Zero-lag | At optimal lag |
|---|---|---|
| WS1 -> WS2 | 0.355 | 0.735 at +3 min |
| WS1 -> WS3 | 0.155 | **0.967 at +7 min** |

This is the signature that matters. Distant assets look nearly uncorrelated instantaneously and strongly correlated once cloud travel time is removed. **Wind direction is therefore recoverable from irradiance telemetry alone**, independently of the anemometer - a genuine analytical exercise that exists only because this layer does.

**Aggregate smoothing:** plant-mean ramp standard deviation is 0.27 of the individual-asset mean, approaching the 1/sqrt(10) = 0.316 independent-asset limit.

**Within-block correlation:** 0.993 to 0.998 across four inverters 155 m apart. High, as it should be for neighbours - but with enough residual difference to give underperformance detection a realistic noise floor.

**Energy conservation:** daily plant-average insolation drifts 0.039% from source. The field redistributes energy across the site; it does not create or destroy it.

---

## 5. Scale Matters for Acceptance Criteria

Doc `16 §6` states the acceptance criteria at block level. That is the wrong scale for two of them.

Within one 620 m block at 8 m/s, the inverter-to-inverter cloud transit time is roughly 19 seconds - **below the 1-minute telemetry cadence**, so lag is unresolvable there by construction. Similarly, four assets correlated at 0.995 cannot show meaningful aggregate smoothing.

Both properties are real and measurable at **plant scale**, across the 3.26 km footprint. The gate is therefore run over weather stations and power blocks rather than over one block's inverters. This is a correction to the acceptance criteria, not a weakening of them.

---

## 6. Parameters

| Parameter | Value |
|---|---|
| Correlation scales | 150 m (0.30), 700 m (0.35), 2,500 m (0.35) |
| Cross-wind scale factor | 1.5x |
| Grid resolution | 50 m |
| Spatial amplitude | 0.55 of the temporal variability envelope |
| Minimum advection wind | 1.5 m/s |
| Footprint smoothing | station 0 min, combiner 1, inverter 2, block 4 |

---

## 7. Downstream Document Updates Required

- `06 §5`: multi-scale field, the envelope-versus-modulation distinction, the cross-wind coordinate
- `16 §6`: acceptance criteria stated at plant scale, with the sub-cadence reasoning
- `02 §4`: add wind-direction inference from lag structure as an explicit exercise
