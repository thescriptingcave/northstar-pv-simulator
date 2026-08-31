# 22 - Equipment Reselection and Physics Gate Record

Covers DR-016 (equipment reselection) and the Phase 2 physics oracle gate result.

---

## DR-016 - Equipment Reselection - LOCKED

**Decision:** the reference plant is built from **real entries in pvlib's bundled CEC databases**, not from an invented datasheet.

| | Was (DR-005 / DR-006) | Now |
|---|---|---|
| Module | "Generic 585 W bifacial TOPCon" | **Heliene 96M475** (`Heliene_96M475`) |
| Module rating | 585 W | 477.39 W |
| Inverter | "Generic central 2500 kW" | **Sungrow SG2500U-550V** |
| Inverter Paco | 2,500 kW (assumed) | 2,500,000 W (actual) |
| Inverter Vdcmax | 1500 V (assumed) | **1200 V** (actual) |

### Why

Design document `05` section 2 already required that single-diode parameters be populated from the CEC database "rather than inventing coefficients." Phase 2 tried to honour that and could not:

1. **No 585 W module exists in the bundled CEC database.** It tops out near 510 W. The bundled snapshot predates the current module generation.
2. **`pvlib.ivtools.sdm.fit_desoto` could not derive parameters from the datasheet.** It returned a negative series resistance — physically impossible — and then failed to converge across several internally consistent datasheet variants.
3. **The routine was verified against a control.** Feeding it a known-good CEC entry's own datasheet values, it failed to reproduce that entry's published parameters. The problem is the fitting routine, not the inputs, so tuning the datasheet was not a path forward.

That left two options: invent single-diode coefficients (forbidden, and the exact failure mode design doc 05 exists to prevent), or move to real equipment and let capacity re-derive. The second is the only defensible one.

### Consequences

Capacity re-derived, with the real inverter's 1200 V DC ceiling now binding:

| Quantity | Calculation | Result |
|---|---|---|
| Module Voc at -10 C | 62.39 + (-0.193409)(-35) | 69.159 V |
| Max modules per string | floor(1200 / 69.159) | **17** |
| String DC | 17 x 477.39 W | 8.116 kWp |
| Strings per inverter | 12 combiners x 32 strings | 384 |
| DC per inverter | 384 x 8.116 kWp | 3,116.4 kWp |
| DC/AC per inverter | 3,116.4 / 2,500 | **1.2466** |
| Plant AC | 40 x 2,500 kW | **100.00 MW exactly** |
| Plant DC | | **124.66 MWp** |
| Combiners / strings / modules | | 480 / 15,360 / 261,120 |
| Telemetry assets | | **585** |

The Sungrow unit was selected specifically because its Paco is exactly 2,500,000 W, so 40 units preserve the 100 MW nameplate without adjusting block structure. Telemetry cardinality returns to 585 assets, matching the original design.

**Bifaciality is retained at 0.70** even though the selected CEC entry is monofacial. Rear irradiance is computed by `infinite_sheds` and enters as `effective_irradiance = poa_front * iam + poa_diffuse + bifaciality * poa_rear`, which is pvlib's own documented approach for bifacial systems. The CEC entry supplies the cell electrical model; bifaciality is a plant design parameter applied to the irradiance reaching it.

### Standing risk

The bundled CEC databases are a stale snapshot. Modelling genuinely current equipment requires the live NREL SAM libraries or manufacturer PAN files, neither of which is available offline. This is recorded so it is not rediscovered later: **the plant is realistic and physically correct, but its equipment is roughly a 2019 vintage.** No analytical lesson depends on the model year.

---

## Phase 2 Physics Oracle Gate - PASSED

**Result: exact agreement, zero error, across 767 daylight samples.**

```
  samples compared        767
  max relative error DC   0.000e+00
  max relative error AC   0.000e+00
  max absolute error DC   0.000000 kW
  max absolute error AC   0.000000 kW
  AC energy difference    0.000e+00
  tolerance               1.0e-06
```

Two independent code paths: `northstar_sim.physics` composes explicit pvlib calls; `northstar_sim.oracle` delegates to `pvlib.modelchain.ModelChain`, which resolves and orders the same models through entirely separate code.

### What the gate caught

It failed twice before passing. Both causes are the kind that never surface downstream — correlations, ramps and energy integration all look correct while the numbers are systematically wrong.

**1. Transposition model mismatch — 15% relative error.**
`ModelChain` defaults to **Hay-Davies** transposition. DR-003 locks **Perez**. The reference must be pinned explicitly; leaving the default in place compares two different physical models and blames the difference on the implementation.

**2. Solar position refraction — 1% relative error.**
`ModelChain` feeds `temp_air` (and `pressure`, when present) into the atmospheric refraction correction. The simulator chain was using pvlib's 12 °C default while the weather was at 35 °C, shifting apparent zenith by up to 0.04°. That propagates through tracking and transposition to roughly 1% per-sample power error.

A 1% systematic error is exactly the magnitude that survives review. It is too small to look wrong on a chart and large enough to corrupt annual energy, performance ratio and every capacity-test regression built on the dataset.

---

## Layer 2 Downscaling - Two Corrections

**1. The invariant belongs on irradiance, not on the clear-sky index.**

Design doc `06` section 4.5 states the renormalization invariant in `kt*` space. That is insufficient: the mean of a product is not the product of the means, and clear-sky irradiance varies within a source interval. Renormalizing `kt*` left GHI interval means drifting by up to 7.5 W/m². Enforcing the invariant on GHI directly gives **2.3e-13 W/m² maximum error**.

**2. Renormalization and the physical ceiling pull against each other at twilight.**

Scaling an interval to match its source mean diverges where the clear-sky reference approaches zero. Unbounded, the clear-sky index reached **2.97** against a 1.15 ceiling — physically impossible irradiance that would have propagated into every downstream analysis.

Resolution is a short fixed-point alternation between renormalizing and clipping. Where the two are compatible both hold exactly; where they are not, **the ceiling wins**, because impossible irradiance is worse than a small interval-mean error. The residual is measured and reported rather than hidden: on a June test day, 282 of 289 intervals are exact and 7 twilight intervals retain up to 1.05 W/m².

---

## UTC and Local Solar Days

The first test window ran UTC midnight to UTC midnight. At -103.3° longitude that spans **19:00 local on 20 June to 19:00 local on 21 June** — two partial solar days.

Consequences observed immediately:

- "First daylight sample" was early evening, inverting the tracker east/west assertion.
- Front-POA-to-DC correlation measured across two partial days rather than one.

Design doc `13` section 12 flags UTC-versus-local as a deliberate exercise. It is not theoretical: **any per-day assertion on a UTC-aligned window is wrong at this longitude.** Test windows are now aligned to the local solar day (05:00 UTC in CDT).

---

## Physics Findings Worth Keeping

**Front POA is not a sufficient predictor of DC power.** Correlation of front-side `poa_global` to DC is 0.956; correlation of `effective_irradiance` to DC is 0.9999. Two effects decouple them, and both are wanted:

- rear-side gain varies with tracker angle and albedo independently of front POA
- the incidence angle modifier cuts the direct component at oblique angles

An analyst regressing DC on front POA alone will see residual structure. That structure is the point of modelling bifacial gain and IAM, and a regression that ignores it will systematically misattribute the residual.

---

## Locked Model Chain

| Stage | Model |
|---|---|
| Solar position | NREL SPA, with ambient temperature and pressure |
| Clear sky | Ineichen-Perez |
| Tracking | `tracking.singleaxis`, backtracking, ±60° |
| Transposition | **Perez** (explicitly pinned on both chains) |
| Rear irradiance | `bifacial.infinite_sheds` |
| Incidence angle | `iam.physical` |
| Cell temperature | `temperature.faiman` (u0=25.0, u1=6.84) |
| DC | CEC single-diode (`calcparams_cec` + `singlediode`) |
| AC | Sandia (`inverter.sandia`) |

---

## Downstream Document Updates Required

Not yet applied; recorded so they are not lost:

- `03` → v2.3: module, inverter, string sizing, capacity table, cardinality
- `05`: equipment catalog entries and the CEC-key requirement
- `06` §4.5: restate the invariant on irradiance; add the ceiling interaction
- `07` §3: note that Perez must be pinned explicitly on any reference chain
- `17`: supersede DR-005 and DR-006 with DR-016
