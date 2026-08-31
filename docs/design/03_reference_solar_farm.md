# 03 - Reference Solar Farm

**Version 2.3** - supersedes v2.1. Site, equipment, and topology are LOCKED per `17_locked_design_decisions` as amended by DR-016 (`22_equipment_and_physics_gate_record`).

> **v2.3 correction.** v2.1's equipment was an invented 585 W bifacial TOPCon with a 1500 V inverter, giving 26 modules per string. No such module exists in pvlib's bundled CEC database, and `fit_desoto` could not derive single-diode parameters from datasheet values - it fails even on a known-good CEC entry used as a control. Inventing coefficients is what `05 §2` forbids, so DR-016 moved to **real CEC entries** and let capacity re-derive. The selected inverter's actual DC ceiling is **1200 V, not 1500**, which is the binding constraint. Every count below changed.

> **v2.1 correction.** v2.0 specified 28 modules per string and claimed approximately 1,494 V at the design minimum temperature. That arithmetic was wrong: the true figure is 1,586 V, which exceeds the 1,500 V inverter ceiling. The maximum is **26**. The error was found when Phase 1 made the calculation executable (`check_string_voltage`), and it cascades through string, combiner, module and capacity counts below. See §5 and §12.

## 1. Purpose

A canonical plant gives the simulator a stable physical context. Unlike v1.0, the values below are **not** a baseline for review - they are locked inputs to Phase 1. Reopening any of them requires revisiting the analytical contract in `02`.

## 2. Site - LOCKED (DR-001)

Working name: **NorthStar PV Solar Farm**

| Parameter | Value |
|---|---|
| Location | Pecos County, West Texas |
| Latitude | 31.35 N |
| Longitude | -103.30 W |
| Elevation | 850 m |
| Timezone (configuration metadata) | `America/Chicago` |
| Storage timezone | UTC, tz-aware |
| Ground albedo | 0.25 (desert scrub) |
| Site area | approximately 700 acres |
| Market | ERCOT, West Load Zone |
| Hedge index point | `HB_WEST` |
| Settlement point | West Texas solar resource node (selected per `19 §4.4`) |

Timezone is metadata, not storage. All telemetry stores UTC; local solar-day analysis converts at query time per `13 §6`. West Texas observes DST, which makes local-time day boundaries a non-trivial and deliberately included exercise.

## 3. Capacity

| Parameter | Value |
|---|---|
| AC nameplate | 100.0 MW |
| DC nameplate | 124.66 MWp |
| DC/AC ratio | 1.2466 |
| Point of interconnection export limit | 100 MW |
| Architecture | Utility-scale, block-based |
| Grid connection | Utility substation / POI |

## 4. Equipment - LOCKED

### 4.1 PV Module - DR-016

**Heliene 96M475**, a real entry in the pvlib CEC module database with validated
single-diode parameters. Database key `Heliene_96M475`.

| Parameter | Value |
|---|---|
| Rated power, STC | 477.39 W |
| Voc | 62.39 V |
| Vmp | 52.06 V |
| Isc | 9.84 A |
| Imp | 9.17 A |
| Cells in series | 96 |
| Temp. coefficient of Pmax | -0.433 %/C |
| Temp. coefficient of Voc | -0.31 %/C (`beta_oc` -0.193409 V/C) |
| Year-1 degradation | 1.0% |
| Annual degradation, year 2+ | 0.40% |
| Bifaciality factor | 0.70 |

Single-diode parameters are **looked up, never derived**. `load_equipment`
refuses a configuration whose CEC key is absent or unset rather than falling
back to plausible-looking coefficients.

**Bifaciality note.** The selected CEC entry is monofacial. Rear irradiance is
computed by `infinite_sheds` and enters as
`effective = poa_front x iam + poa_diffuse + bifaciality x poa_rear`, which is
pvlib's own documented approach. The CEC entry supplies the cell electrical
model; bifaciality applies to the irradiance reaching it.

**Standing risk.** The bundled CEC databases are a stale snapshot topping out
near 510 W. Modelling genuinely current equipment needs the live NREL SAM
libraries or manufacturer PAN files. The plant is physically correct but its
equipment is roughly a 2019 vintage. No analytical lesson depends on model year.

### 4.2 Inverter - DR-016

**Sungrow SG2500U-550V**, a real CEC inverter entry with validated Sandia
coefficients. Database key
`Sungrow_Power_Supply_Co___Ltd___SG2500U__550V_`.

| Parameter | Value |
|---|---|
| Rated AC (Paco) | 2,500,000 W exactly |
| Rated DC (Pdco) | 2,542,502 W |
| **Maximum DC voltage** | **1,200 V** |
| Nominal DC voltage (Vdco) | 900 V |
| Peak efficiency | 98.77% |
| Thermal derate onset | 45 C **ambient** |
| Trip threshold | 75 C internal |
| Startup irradiance threshold | ~20 W/m2 POA |
| Night standby draw | 750 W |

Selected because Paco is exactly 2,500,000 W, so 40 units give exactly 100.0 MW
AC and the reference plant keeps its nameplate without changing block structure.

**The derate onset is an ambient threshold**, as datasheets state it. Comparing
it directly to internal temperature makes derating fire constantly - see
`09 §5`.

### 4.3 Tracker (DR-004)

| Parameter | Value |
|---|---|
| Type | Horizontal single-axis, N-S axis |
| Rotation limit | +/- 60 degrees |
| Backtracking | Enabled |
| Ground coverage ratio | 0.33 |
| Module height above ground | 1.8 m |
| Stow wind speed | 20 m/s |

Tracking is not cosmetic. It produces the broad-shouldered production plateau,
the backtracking transitions visible at dawn and dusk, and an entire fault class
(stuck tracker, misalignment, spurious stow) that fixed tilt cannot express.
Stuck-tracker detection via peer comparison is among the highest-value
real-world PV analytics.

## 5. Capacity Reconciliation

Module and string counts are **derived** from electrical characteristics, not
asserted. The binding constraint is the inverter's 1,200 V DC ceiling against
open-circuit voltage at the design minimum temperature.

| Step | Calculation | Result |
|---|---|---|
| Module Voc at -10 C | 62.39 + (-0.193409)(-35) | 69.159 V |
| Max modules per string | floor(1200 / 69.159) | **17** |
| String DC rating | 17 x 477.39 W | 8.116 kWp |
| Strings per combiner | Design choice | 32 |
| Combiners per inverter | Design choice | 12 |
| Strings per inverter | 32 x 12 | 384 |
| DC per inverter | 384 x 8.116 kWp | 3,116.4 kWp |
| DC/AC per inverter | 3,116.4 / 2,500 | **1.2466** |
| Inverters per block | Design choice | 4 |
| Blocks | Design choice | 10 |
| Total inverters | 4 x 10 | 40 |
| Total combiners | 12 x 40 | 480 |
| Total strings | 384 x 40 | 15,360 |
| Total modules | 17 x 15,360 | 261,120 |
| **Plant DC** | 261,120 x 477.39 W | **124.66 MWp** |
| **Plant AC** | 40 x 2,500 kW | **100.00 MW** |

**Cold-temperature Voc check.** Open-circuit voltage rises as cell temperature
falls, so the binding case is a cold clear sunrise: the array is illuminated and
at ambient while the inverter has not yet begun drawing current. A 17-module
string reaches **1,175.7 V** against the 1,200 V ceiling - a margin of 24.3 V,
or 2.0%.

That margin is deliberately thin and realistic, and provides the physical basis
for a DC overvoltage fault (SCN-049) on cold clear winter mornings.

**This check is enforced in code**, not asserted in prose:
`northstar_sim.validation.check_string_voltage` rejects any configuration whose
cold string voltage exceeds the inverter ceiling.

## 6. Asset Hierarchy

```
Plant (NorthStar PV)
├── Weather Stations (3)
├── Power Blocks (10)
│   ├── PV Arrays
│   │   └── Tracker Row-Blocks (4 per power block)
│   │       └── String Groups
│   │           └── Strings (384 per inverter)
│   │               └── Modules (configuration level only)
│   ├── Combiner Boxes (48 per block)
│   ├── Inverters (4 per block)
│   ├── Block Transformer (1 per block)
│   └── Switchgear
├── AC Collection System
├── Substation
├── Plant Controller / SCADA
├── Revenue Meter
└── Point of Interconnection
```

Tracker row-blocks are new in v2.0. They are the granularity at which tracker faults occur and at which tracker telemetry is produced.

## 7. Telemetry-Bearing Levels and Cardinality (DR-007)

Modules do not generate telemetry. They remain in the configuration model so that temperature, degradation, and electrical characteristics influence production.

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
| **Total telemetry** | **585** | | **~243,000** |
| Market prices | 3 points | 15 min | 288 |

Approximately **89 million telemetry rows per simulated year**. Large enough to make compression, continuous aggregates, and retention policies necessary rather than decorative; small enough to run on an Apple Silicon workstation.

Combiner telemetry is 57% of volume. If reduction becomes necessary, drop cadence to 15 minutes rather than sampling a subset - losing peer population destroys imbalance detection (SCN-044), losing resolution does not.

## 8. Geographic and Seasonal Behavior

The locked site produces:

| Characteristic | Consequence |
|---|---|
| Latitude 31.35 N | Day length 10.1 h (winter solstice) to 14.2 h (summer solstice) |
| High DNI | Frequent clear days, sustained clipping March-September |
| Summer ambient > 40 C | Genuine module and inverter thermal derating |
| Winter minimum near -10 C | Cold Voc margin scenario |
| Low episodic rainfall | Long soiling ramps with sharp rain resets |
| Dust events | Correlated multi-block soiling step changes |
| High spring wind | Tracker stow events; module cooling; also drives negative prices |

## 9. Time Resolution

| Layer | Resolution |
|---|---|
| Source weather data | 5 minutes (NSRDB GOES CONUS v4) |
| Internal simulation step | 1 minute |
| Raw telemetry, major assets | 1 minute |
| Raw telemetry, combiner and tracker | 5 minutes |
| Market settlement | 15 minutes |
| Aggregates | 5 min, 15 min, hourly, daily |

The 5-minute source resolution is a change from v1.0 and a significant improvement - see `19 §2.2`.

## 10. Dataset Targets

| Dataset | Duration | Purpose |
|---|---|---|
| Smoke | 2 days | Build verification |
| Development | 30 days | Feature work |
| Seasonal | 365 days | Full seasonal analysis, PR, capacity factor |
| Degradation | 3 years | **Required** - year-on-year degradation estimation per `20 §9.3` needs at least two full years |
| Forecasting | 3+ years | Train/validation/test splits with seasonal coverage |

Three years is now a hard requirement, not an aspiration. The degradation validation check in `20 §14.5` cannot be performed on less.

Source data availability supports this: NSRDB GOES CONUS covers 2018 onward, and `19 §4.1` specifies fetching 2019-2024.

## 11. Why This Scale

A 100 MW-class plant with 40 inverters and 480 combiners produces:

- meaningful peer populations at inverter, combiner, block, and tracker level
- localized failures that are visible against peers but not against plant total
- aggregate smoothing measurably different from individual asset variability
- clipping and curtailment behavior at realistic magnitude
- data volumes that exercise time-series storage without requiring a cluster
- financial magnitudes large enough that lost-revenue differences are material

while remaining conceptually tractable.

## 12. Changes from v1.0

| Item | v1.0 | v2.0 |
|---|---|---|
| Site | "Selected later" | Pecos County, TX, locked with coordinates |
| Module | Unspecified | 585 W bifacial TOPCon, full parameters |
| Inverter | "approximately 2.5 MW" | 2,500 kW with full parameters |
| Array type | "fixed-tilt or single-axis" | Single-axis tracking, locked |
| Bifacial | Not considered | Included, 0.70 bifaciality |
| DC nameplate | "approximately 125 MWp" | 125.80 MWp, derived |
| String counts | "derived later" | Derived and reconciled in §5 |
| Combiners | "multiple per inverter" | 12 per inverter, 480 total |
| Tracker row-blocks | Absent | New asset level |
| Telemetry cardinality | Levels listed, no counts | Full table with row volumes |
| Source data resolution | Unstated | 5 minutes, NSRDB GOES CONUS v4 |
| Multi-year dataset | "for degradation experiments" | 3 years required |

## 13. Changes from v2.0 (historical)

> The table below records the v2.0 to v2.1 step and is **superseded** by §14.
> Its figures are history, not current values. Read §5 and §14 for what the
> plant is now.

| Item | v2.0 | v2.1 |
|---|---|---|
| Modules per string | 28 (arithmetically wrong) | **26**, verified in code |
| Cold string Voc | "approximately 1,494 V" | **1,473.1 V** computed |
| Combiners per inverter | 12 | **13**, to recover DC/AC ratio |
| Strings per inverter | 192 | **208** |
| Total combiners | 480 | **520** |
| Total strings | 7,680 | **8,320** |
| Total modules | 215,040 | **216,320** |
| Plant DC | 125.80 MWp | **126.55 MWp** |
| DC/AC ratio | 1.258 | **1.2655** |
| Telemetry assets | 585 | **625** |
| Rows per year | ~89M | **~93M** |
| Voltage check | Prose assertion | Enforced by `check_string_voltage` with a regression test |


## 14. Changes from v2.1

| Item | v2.1 | v2.3 |
|---|---|---|
| Module | Invented 585 W bifacial TOPCon | **Heliene 96M475**, real CEC entry, 477.39 W |
| Inverter | Generic 2500 kW, assumed 1500 V | **Sungrow SG2500U-550V**, real CEC entry, **1200 V** |
| Single-diode parameters | To be derived from datasheet | Looked up; derivation refused in code |
| Modules per string | 26 | **17** |
| Strings per combiner | 16 | **32** |
| Combiners per inverter | 13 | **12** |
| Strings per inverter | 208 | **384** |
| Total combiners | 520 | **480** |
| Total modules | 216,320 | **261,120** |
| Plant DC | 126.55 MWp | **124.66 MWp** |
| DC/AC ratio | 1.2655 | **1.2466** |
| Cold string Voc | 1,473.1 V against 1500 | **1,175.7 V against 1200** |
| Telemetry assets | 625 | **585** |
| Rows per simulated year | ~93M | **~89M** |

Rationale and the failed `fit_desoto` diagnostic are in
`22_equipment_and_physics_gate_record`.
