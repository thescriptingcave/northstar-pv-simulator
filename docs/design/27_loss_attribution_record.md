# 27 - Loss Attribution Record (Phase 7)

## Result: PASSED

```
  [PASS] waterfall_closes             residual 7.35e-19 of theoretical (tolerance 5.0e-03)
  [PASS] all_causes_attributed        9/9 cause codes carry energy
  [PASS] loss_magnitudes_plausible    inverter 1.32%, soiling 3.00%, degradation 1.16%
  [PASS] avoidable_classified         soiling avoidable; clipping and degradation structural
  [PASS] causes_are_separable         clipping 448 min, resource-limited 963 min, overlap 0
  [PASS] degradation_tracks_age       factor 0.9860 at 2.0 years
  [PASS] thermal_derating_engages     0.00 MWh at moderate ambient -> 60.25 MWh at extreme
  [PASS] derating_reduces_clipping    clipping 66.0 -> 26.7 MWh when derating
```

Closure is **7.35e-19** of theoretical energy against a 0.5% tolerance - a margin of sixteen orders of magnitude.

---

## 1. Attribution Is Cascading, Not Independent

Losses multiply. A stage is therefore attributed what it removed **from what reached it**, not what it would have removed from the theoretical maximum.

Attributing every stage against the theoretical over-counts as soon as two stages act: if thermal costs 13% and mismatch 2%, the mismatch stage removes 2% of the 87% that survived thermal, not 2% of the whole.

The formulation closes **by construction**. Each stage is `upstream x (1 - factor)` and the next starts from `upstream x factor`, so the telescoping sum is exactly the difference between the first and last terms. Closure is a property of the arithmetic rather than a tuned tolerance.

---

## 2. The Waterfall, Clear Day at 40 C Ambient

| Cause | Energy | Share | Avoidable |
|---|---|---|---|
| THEORETICAL | 1,432.8 MWh | 100% | |
| `LOSS_THERMAL` | 194.94 | 13.61% | no |
| `LOSS_CLIPPING` | 65.98 | 4.61% | no |
| `LOSS_SOILING` | 42.99 | 3.00% | **yes** |
| `LOSS_MISMATCH` | 23.56 | 1.64% | no |
| `LOSS_INVERTER_EFF` | 18.97 | 1.32% | no |
| `LOSS_DC_WIRING` | 17.32 | 1.21% | no |
| `LOSS_DEGRADATION` | 16.68 | 1.16% | no |
| `LOSS_AC_COLLECTION` | 7.95 | 0.55% | no |
| `LOSS_TRANSFORMER` | 6.14 | 0.43% | no |
| `LOSS_INV_STATE` | 0.68 | 0.05% | **yes** |
| `LOSS_LOWLIGHT` | **-29.08** | -2.03% | no |
| EXPORTED | 1,066.7 MWh | 74.4% | |

Thermal loss at 13.6% is high because the selected module's power temperature coefficient is -0.433 %/C and this is a 40 C day. It is also exactly why raw performance ratio is seasonal and why `20 §4.2` makes temperature correction mandatory.

---

## 3. The Module Non-Linearity Term Must Be Signed

`LOSS_LOWLIGHT` captures the gap between the linear STC extrapolation and the single-diode solution. **It is signed and must not be clipped at zero.**

Low-light efficiency falls away from the STC ratio, but at high irradiance the single-diode solution *exceeds* the linear extrapolation, making the term a gain. Clipping it at zero left the cascade below actual DC power and produced two symptoms:

- inverter conversion loss reported as **0.04%** instead of roughly 1.5%
- a spurious **-0.21%** pushed into the residual

Both looked minor. Neither would have failed the 0.5% closure test, and an analyst studying conversion efficiency would have concluded the inverters were essentially lossless.

---

## 4. Losses Are Not Additive: Derating Suppresses Clipping

The clearest emergent result of the phase.

| | 40 C ambient | 50 C ambient |
|---|---|---|
| `LOSS_THERMAL` | 194.9 MWh | 255.1 MWh |
| `LOSS_INV_THERMAL` | 0.0 | 60.3 MWh |
| `LOSS_CLIPPING` | **66.0 MWh** | **26.7 MWh** |

A derating inverter **clips less**, because derating pulls output below the cap before the cap can bind. The two losses are not independent, and an analyst who adds an estimated clipping loss to an estimated derating loss will over-count.

This falls out of the model rather than being asserted, and it is exactly the kind of interaction that makes compound scenarios (`10 §10`) hard in a useful way.

---

## 5. Two Semantic Bugs in Derating

**The onset is an ambient threshold, not an internal one.** Inverter datasheets state derating onset in ambient terms; the model compares internal temperature. Comparing the ambient figure directly to internal temperature made derating fire at 42 C ambient, hit its 30% cap, and hold AC output at 1,750 kW instead of 2,500. The internal onset is the ambient onset plus the full-load rise: 45 + 22 = 67 C.

**Slope and cap were too aggressive.** Retuned to 0.015/C and 20%. Resulting behaviour:

| Ambient | Internal peak | Derating |
|---|---|---|
| 30 C | 52.0 C | none |
| 40 C | 62.0 C | none |
| 46 C | 68.0 C | 1.5%, 446 min |
| 52 C | 74.0 C | 10.5%, 597 min |

No derating below 46 C ambient. West Texas reaches it on the hottest afternoons only, which is the correct frequency for the phenomenon - rare, real, and worth detecting.

---

## 6. Measuring Clipping Requires the Uncapped Output

`pvlib.inverter.sandia` applies `min(Paco, ...)` internally, so clipped and unclipped output are indistinguishable in its result. Clipping loss cannot be quantified without knowing what the inverter *would* have produced.

`sandia_preclip` implements the published Sandia formulation with the cap omitted, using the same CEC coefficients. A test asserts it matches `pvlib.inverter.sandia` exactly below the cap, so it is the same model rather than an approximation of the efficiency curve.

---

## 7. The Oracle Had to Be Rescoped

Adding DC-side losses to the production chain **broke the Phase 2 physics gate**, which had been passing at zero error.

The cause is legitimate: `ModelChain` models module and inverter physics. Degradation, mismatch, DC wiring and thermal derating are *plant* characteristics with no `ModelChain` equivalent. Leaving them enabled compared a plant against a module and reported the difference as an implementation error.

`run_inverter_chain` now takes `apply_plant_losses`, which the gate disables. The oracle continues to test what it was built to test.

Related: the equality assertion was relaxed from exact zero to below 1e-12. Adding a single multiply by unity moved the residual from 0 to 4e-16 without changing any physics, and a test that fragile is testing the floating-point unit rather than the model.

---

## 8. A Test Fixture Had Drifted

The physics test fixture still carried the **invented** module's -0.29 %/C temperature coefficient, while the shipped configuration uses Heliene's actual -0.433. That is a materially different plant: thermal loss changes by a third, and the sign of the non-linearity term flips.

A fixture that drifts from the shipped configuration tests a plant that does not exist. Now aligned.

---

## 9. Avoidable Versus Structural

`CAUSE_CODES` marks each loss as avoidable or not. **Clipping and degradation are deliberately structural**: they are consequences of the DC/AC ratio and of physics, not failures.

Reporting them as recoverable is the classic analytical error, and the dataset is built so an analyst can commit it and then be shown to be wrong. Soiling, inverter thermal derating, curtailment and state-driven losses are avoidable and will carry monetary value once Phase 9 attaches prices.

---

## 10. Downstream Document Updates Required

- `07 §5`: DC-side loss factors and their order; the signed non-linearity term
- `07 §7`: clipping requires the uncapped Sandia evaluation
- `09 §5`: ambient-versus-internal onset, retuned slope and cap
- `18 §5.2`: cascading attribution, and that derating and clipping are not additive
- `16 §5`: the oracle is scoped to physics; plant losses are excluded by construction
