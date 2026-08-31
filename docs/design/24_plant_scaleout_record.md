# 24 - Full Plant Scale-Out Record (Phase 4)

## Result: PASSED

```
  [PASS] hierarchy_reconciles         inverter sum vs block sum, max error 5.82e-11 kW
  [PASS] energy_chain_closes          residual 6.78e-18 of exported energy
  [PASS] dc_within_physical_bound     peak DC 91.7% of nameplate (bifacial gain permits >100%)
  [PASS] ac_respects_nameplate        peak inverter AC 100.0% of nameplate
  [PASS] losses_in_expected_range     transformer 0.56%, collection 0.64%
  [PASS] blocks_vary_realistically    block AC correlation 0.8939 .. 0.9816
  [PASS] fleet_aggregate_smoother     ramp std ratio 0.4220
  [PASS] throughput_within_budget     2.23 s/day -> 13.6 min/year (budget 60)
  [PASS] full_plant_simulated         40 inverters, 10 blocks, 585 telemetry assets
```

---

## 1. Throughput: 17x Faster

The first full-plant measurement projected **90.9 minutes per simulated year**, against a 60-minute budget. Profiling found two sources of pure duplicated work.

**The CEC databases were re-parsed on every call.** `pvlib.pvsystem.retrieve_sam` reads and parses a bundled CSV each time - about 135 ms, or 36% of a single inverter-day. At 40 inverters that is 5 seconds per simulated day spent re-reading two files whose contents cannot change within a run. Now memoised.

**Solar geometry was recomputed per inverter.** Solar position, tracker orientation, angle of incidence, airmass, extraterrestrial irradiance and the bifacial ground view factors depend only on location, time and tracker geometry - *not* on the irradiance an individual asset receives. Every inverter on the site shares them exactly.

They are now computed once into a `SiteGeometry` object and passed in.

| | Before | After |
|---|---|---|
| Per inverter-day | 0.373 s | **0.022 s** |
| One simulated year | 90.9 min | **13.6 min** |
| Three years | 273 min | **41 min** |

A regression test asserts the shared path and the standalone path produce **identical frames**, so the optimisation changed cost and nothing else.

---

## 2. Bifacial Rear Irradiance Used the Wrong Surface

Peak DC came out at **155% of nameplate** - physically impossible.

`pvlib.bifacial.infinite_sheds.get_irradiance_poa` returns irradiance for whatever surface orientation it is given. It was being handed the **front** tilt and azimuth, so it returned front-side irradiance, and 70% of that was then added back as bifacial gain.

The rear face points the opposite way:

```
rear tilt    = 180 - front_tilt
rear azimuth = (front_azimuth + 180) mod 360
```

Corrected, rear POA peaks at **173 W/m2** and averages 110 W/m2 in daylight - a bifacial gain of roughly 7-8% at 0.70 bifaciality, squarely in the expected range. Peak DC falls to **101.5%** of nameplate, which is correct: bifacial arrays do briefly exceed DC nameplate.

The test that pins this asserts the rear-to-front irradiance ratio stays between 0.02 and 0.30. A ratio near 1.0 means the geometry is wrong again.

---

## 3. Clipping the Loss Chain Broke Energy Closure

Transformer output was clipped at zero. That looks defensive and is wrong.

No-load loss is present whenever a transformer is energised, and utility PV plants keep their transformers energised overnight. Output is therefore **genuinely negative in darkness** - the plant imports station service. Clipping discards that energy, and the loss chain failed to close by **0.14% of daily export**: small enough to read as rounding, large enough to corrupt every loss attribution built on top of it.

Removing the clip at both the transformer and the substation takes closure to **6.78e-18** of exported energy - machine precision.

This matters beyond tidiness. Design doc `18 §5.2` requires the loss waterfall to close within 0.5%, with the residual itself a tracked category. A silent 0.14% leak would have consumed a quarter of that budget before any real loss was modelled.

---

## 4. Balance of Plant

**Transformer** - losses are load-dependent, not a fixed percentage. No-load loss is constant; load loss scales with the square of loading. A constant-efficiency transformer would produce a flat efficiency curve, eliminating the efficiency-variation analysis in `02 §5` and making transformer health monitoring impossible.

Winding temperature follows loading through a first-order lag. Verified by step response: **63.2% of the rise is reached at 44 minutes** against a configured 45-minute time constant.

The step test exists because measuring lag on a real day does not work. Under clipping the load plateaus for hours, so winding temperature approaches steady state and peaks at the *end* of the plateau rather than one time constant after the load peak. That is correct behaviour and a misleading measurement.

**AC collection** - resistive loss scaling with the square of loading, 0.8% at rated output.

**Measured on a clear June solstice day:** transformer 0.56%, collection 0.64% of inverter AC energy.

---

## 5. Fleet Behaviour

| Property | Clear day | Broken cloud |
|---|---|---|
| Block-to-block AC correlation | 0.9995 - 0.9999 | 0.894 - 0.982 |
| Block daily energy spread | 0.07% | 0.31% |
| Inverter daily energy spread | - | 0.42% |
| Fleet ramp smoothing ratio | - | 0.42 |

Daily-energy spread is small even under broken cloud, and that is realistic: over a full day, spatial differences average out. In a real plant, block-to-block *annual* energy differences are driven by soiling and equipment health rather than weather - which is exactly why those become detectable once Phases 5 through 8 add them.

The instantaneous correlation and the ramp smoothing are where the spatial layer shows, and both behave.

---

## 6. Acceptance Note on the DC Bound

The gate checks an **upper** bound on peak DC only. A lower bound would depend on the irradiance the gate happens to run at, making the check a statement about the test weather rather than about the model. The clear-sky case - peak DC between 100% and 112% of nameplate - is pinned separately in the unit tests where the irradiance is controlled.

---

## 7. Downstream Document Updates Required

- `04`: transformer and collection loss models, and the negative-output-at-night behaviour
- `07 §10`: load-dependent loss formulation, thermal lag measurement caveat
- `14 §4.1`: replace the projected throughput target with the measured 13.6 min/year
- `16 §7`: record the shared-geometry requirement as an implementation constraint
