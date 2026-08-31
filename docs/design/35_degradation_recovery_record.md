# 35 - Blind Degradation Recovery (Doc 01 Section 8, Criterion 4)

## Result: MET

```
  3 years hourly, plant aged 2y -> 5y, expected-power model fitted on year one
  injected   -0.4000 %/yr
  estimated  -0.3455 %/yr   (716 year-on-year pairs)
  error      +0.0545 pp
  doc 20 tolerance +/-0.15 pp: MET
```

This is the strongest validation the package supports: inject a known rate, estimate it blind from analyst-facing data, and compare. **It had never been run.** Attempting it exposed four defects.

---

## 1. Degradation Was a Scalar, and Therefore Invisible

`config.degradation_factor` was a **property**, applied uniformly across an entire run:

```python
dc_power_w = dc_ideal_w * config.degradation_factor * ...
```

Within a three-year simulation, degradation never progressed. Every longitudinal method would have recovered a rate of exactly **zero**, and the criterion could not be run at all.

Replaced with `degradation_series(config, index)`, which advances plant age across the window. `plant_age_years` is now the age at the window **start**. The scalar property survives for single-window use with a docstring stating plainly that multi-year runs must not use it.

Verified: over three years the implied rate is **-0.394 %/yr** against a configured -0.400.

---

## 2. The Cloud Field Could Not Reach Multi-Year Scale

The field is advected past the plant, and the grid was sized to the **total distance travelled**: at 4 m/s that is 126,000 km over a year, 2.5 million columns, **5.3 GiB**. The run died allocating it.

Worse than the memory: the sampler **clipped** the along-wind index at the grid edge. Had the allocation succeeded on a smaller run, every sample past the edge would have returned an identical value - a single frozen cloud pattern for most of the record.

Now the along-wind coordinate **wraps** at a 10,000 km cap, roughly a month of advection. The field is stationary, so a repeat at that period is statistically indistinguishable from fresh structure, and it is undetectable in any analysis the dataset supports. Cross-wind still clips, correctly: assets never leave the site.

The spatial gate passes unchanged, to the same figures.

---

## 3. A Leap Day Broke the Estimator

The year-on-year method shifts the index forward by one year and joins. Shifting **29 February clamps to 28 February**, landing on an entry that already exists, and pandas refuses the join on duplicate labels.

The synthetic test never caught it: it spanned 2021 to 2023 and contains no 29 February. Only a real 2023-2025 record reaches it. A regression test now asserts the window spans a leap day.

---

## 4. The Aggregation Choice Dominated the Answer

The largest finding, and it is methodological rather than a bug.

The first recovery gave **-0.16 %/yr against -0.40 injected**, with a standard error of 0.90 pp - seven times worse than the synthetic benchmark. The instinct was to blame clipping and filter it out. That made it **worse**:

| Filter | Fit R2 | Estimate | Error |
|---|---|---|---|
| Irradiance > 50 (baseline) | 0.731 | -0.16 %/yr | +0.24 pp |
| Exclude clipped intervals | 0.696 | +0.09 %/yr | +0.49 pp |
| High irradiance only | 0.539 | +0.40 %/yr | +0.80 pp |
| Mid band 400-900 W/m2 | 0.572 | +0.38 %/yr | +0.78 pp |

Filtering was the wrong lever entirely. The problem was **how daily values were formed**:

| Method | Estimate | Error |
|---|---|---|
| Daily median of hourly ratios | -0.16 %/yr | +0.24 pp |
| **Ratio of daily energy sums** | **-0.35 %/yr** | **+0.05 pp** |
| Ratio of monthly energy sums | +0.56 %/yr | +0.96 pp |

A **median of hourly ratios weights every interval equally**, so unstable low-irradiance hours - small denominator, noisy ratio - drive a statistic that should be driven by the hours carrying the energy.

The **ratio of daily energy sums** weights by energy naturally and lands inside tolerance. Monthly aggregation is worse again: too few year-on-year pairs survive.

`daily_performance_index` is now the documented method, with the comparison table in its docstring so the choice is not silently reversed later.

---

## 5. What This Says About the Earlier Estimate

Phase 12b reported the degradation estimator as unbiased with roughly 0.12 pp standard error, measured on **synthetic** normalised series. That measurement was of the estimator alone.

On a real record the estimator is only as good as the normalisation feeding it, and the normalisation is where the error lives. The synthetic benchmark was not wrong, but it measured a narrower thing than it appeared to.

---

## 6. Cost

A three-year hourly dataset takes **103 seconds** for the full 40-inverter plant - roughly 34 seconds per simulated year. Well within reach for the canonical dataset the V1 gate requires, though that gate calls for 1-minute raw telemetry, which is about 24 times more work.

---

## 7. Downstream Document Updates Required

- `07 §5`: degradation is time-varying; the scalar form is single-window only
- `06 §5`: the cloud field wraps beyond a capped extent, and why wrapping beats clipping
- `20 §9.3`: specify the **ratio of daily energy sums**; a median of hourly ratios misses tolerance
- `20 §14.5`: the tolerance is met by the correct method, so it can stand as written
- `01 §8`: criterion 4 is now demonstrated end to end
