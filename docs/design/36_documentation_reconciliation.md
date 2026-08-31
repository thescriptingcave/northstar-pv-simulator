# 36 - Documentation Reconciliation

Fourteen implementation records (22-35) each ended with a "Downstream Document Updates Required" list. **None had been applied.** This record closes all 56 items.

---

## 1. Why This Mattered

The design package's central claim is that it is **normative**: the specification the code implements. That claim was false in specific, checkable ways.

Anyone reading `03` to understand the plant would have found 26 modules per string against a shipped 17, and a 126.55 MWp nameplate against a real 124.66. Anyone implementing degradation analysis from `20 §9.3` would have used a median of hourly ratios and **missed the tolerance**. Anyone reasoning about availability from `20 §6` would have assumed an ordering that measurement contradicts.

Stale prose beside working code is worse than no prose: it is confidently wrong, and there is no signal telling a reader which to trust.

---

## 2. Documents Corrected

| Doc | Version | Nature of change |
|---|---|---|
| `01` | 2.0 + | Success criteria status; criterion 4 demonstrated |
| `02` | 2.0 + | Contract coverage, wind-direction inference, ramp-regime forecasting |
| `03` | **2.3** | **Rewritten §4-5** - equipment, string sizing, capacity table |
| `04` | **2.0** | Balance of plant, controller, shared-geometry constraint |
| `05` | **2.0** | DR-016 selection; coefficient derivation refused in code |
| `06` | **2.1** | Invariant on irradiance, multi-scale field, wrapping, ambient variation |
| `07` | **2.1** | Perez pinning, solar position inputs, time-varying degradation, preclip |
| `08` | **2.0** | Dwell values, reason requirement, legality enforcement |
| `09` | **2.0** | Ambient-vs-internal onset, daylight scheduling, signature contract |
| `10` | 2.0 + | Scheduling parameters, per-asset defect rate, POI decision |
| `11` | 2.0 + | Eight added fields, classification, fallible flags |
| `12` | **2.0** | Populated event categories, event/telemetry redundancy |
| `13` | 2.0 + | Generated schema, truth tables, two-tree export |
| `14` | 2.0 + | Measured performance replacing projections |
| `15` | 2.0 + | Tier status, and parsing is not execution |
| `16` | 2.0 + | Oracle scoping, plant-scale criteria, release gate status |
| `17` | + | DR-005 and DR-006 superseded by DR-016; DR-015 provisional |
| `18` | + | Settlement-grain curtailment, cascading attribution, hedge test |
| `19` | + | Synthetic price model calibration targets |
| `20` | **1.1** | **Availability ordering removed**; degradation method specified |

Five documents (`04`, `05`, `08`, `09`, `12`) had never been revised at all.

---

## 3. The Three Corrections That Would Have Caused Wrong Work

**`03` capacity.** Every count was superseded by DR-016 and none had been updated. A reader would have built against a plant that does not exist.

**`20 §9.3` degradation method.** The document did not specify how daily values are formed. The obvious choice - a median of hourly ratios - **misses tolerance**, estimating -0.16 %/yr against -0.40 injected. Only the ratio of daily energy sums lands inside. The comparison table is now in the document.

**`20 §6` availability ordering.** The document implied `time <= daylight <= energy`. Measurement gives 0.9289, **0.8704**, 0.9977 - daylight is lower, because faults cluster in operating hours. The claim is removed rather than corrected, because there is no reliable ordering to state.

---

## 4. Change Logs Are History, Not Claims

`03` now carries two change tables: v2.0 to v2.1, and v2.1 to v2.3. The older one contains figures that are no longer true.

A note above it says so explicitly and points to the current section. A superseded table without that marker is a trap - it reads exactly like a current one.

---

## 5. Verified, Not Asserted

Each correction was checked by search rather than by recollection:

- stale values absent from live text: 26 modules/string, 126.55 MWp, 1.2655, 625 assets
- corrected values present: 17 modules/string, 124.66 MWp, `Heliene_96M475`, daily energy sums, time-varying degradation, Perez pinning, tables-before-hypertables, parsing-is-not-execution
- config agrees with `03 §5`: 17 modules/string, 124.66 MWp, 100.00 MW, ratio 1.2466, string Voc 1175.7 V against a 1200 V ceiling

The three apparent stale hits were traced to the historical change table and confirmed legitimate.

Full test suite passes unchanged; no code was touched.

---

## 6. Keeping It True

The failure mode was structural: implementation records accumulated update lists faster than anyone applied them, and nothing failed when they went unapplied.

Two cheap defences:

- **Apply updates in the same session that generates them.** A record's update list should be empty by the time the record is written.
- **A search-based check.** Superseded numeric values are greppable. The verification in §5 took seconds and would have caught this at any point in the preceding fourteen phases.
