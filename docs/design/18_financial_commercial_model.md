# 18 - Financial and Commercial Model

## 1. Purpose

Documents 01-16 model the plant physically and operationally but assign no economic value to anything. This document defines the commercial layer.

**Scope is deliberately operational, not corporate finance.** Per DR-009, this document covers revenue, settlement, cause-attributed lost revenue, O&M cost, and contractual performance guarantees. Debt schedules, depreciation, tax equity, IRR, and LCOE are explicitly out of V1 scope.

**Governing principle, extending `01 §4`:**

> Every kWh the plant fails to produce must carry a cause code and a dollar value at the price prevailing at the moment it was lost.

This is the analytical product that distinguishes an operating-plant dataset from a physics dataset.

---

## 2. Commercial Structure

### 2.1 Revision to DR-008

DR-008 originally specified a 70/30 physical PPA / merchant split. **This is superseded.** The V1 structure is a fully merchant physical position overlaid with a financial hedge, which is both the dominant real ERCOT structure and analytically richer.

| Component | Structure |
|---|---|
| Physical energy | 100% settled at ERCOT West RT 15-minute Settlement Point Price |
| Financial hedge | Fixed-volume, fixed-price swap (contract for differences) |
| Hedge volume | 70 MW, flat, all hours (24 x 7) |
| Hedge strike | $34.00 / MWh |
| Hedge term | 10 years from COD |
| Production tax credit | $27.50 / MWh on all generated MWh, 10 years |
| Basis | Plant resource node vs. ERCOT West Hub, settled separately |

**Why this is better than a physical PPA for teaching purposes.** A physical as-generated PPA makes revenue a trivial function of energy. A fixed-volume hedge introduces three risks that only exist in time-series form:

- **Shape risk** - the plant generates on a solar curve; the hedge is flat. Overnight hours are short the hedge; midday hours are long it.
- **Volume risk** - generation shortfall during a high-price hour means buying back hedge volume at the spot price. An outage during a scarcity event is far more expensive than the same outage at noon in April.
- **Basis risk** - resource-node price diverges from hub price under local congestion, which in West Texas is chronic.

None of these can be analyzed without joining telemetry to market data at settlement grain. That join is the point.

### 2.2 Settlement Grain

All settlement occurs at the **15-minute** interval, matching ERCOT RT settlement. Telemetry at 1 minute is integrated to 15-minute energy before monetization.

---

## 3. Settlement Calculation

### 3.1 Notation

| Symbol | Meaning | Unit |
|---|---|---|
| `i` | 15-minute settlement interval | - |
| `E_i` | Metered export energy in interval `i` | MWh |
| `P_rt,i` | RT settlement point price at plant node | $/MWh |
| `P_hub,i` | RT settlement point price at ERCOT West Hub | $/MWh |
| `P_da,i` | Day-ahead settlement point price | $/MWh |
| `V_h` | Hedge volume per interval (70 MW x 0.25 h = 17.5 MWh) | MWh |
| `S` | Hedge strike price | $/MWh |
| `C_ptc` | Production tax credit rate | $/MWh |

### 3.2 Components

```
Energy revenue        R_energy,i  = E_i * P_rt,i
Hedge settlement      R_hedge,i   = V_h * (S - P_hub,i)
PTC value             R_ptc,i     = E_i * C_ptc
Basis exposure        R_basis,i   = E_i * (P_rt,i - P_hub,i)     [derived, reporting only]

Gross margin          M_i         = R_energy,i + R_hedge,i + R_ptc,i
```

`R_hedge,i` is independent of `E_i` by construction. It is positive when the index settles below strike and negative when above. `R_basis,i` is a reporting decomposition, not an additional cash flow.

### 3.3 Day-Ahead Position

V1 assumes the plant is **not** day-ahead scheduled - all energy settles real-time. Day-ahead prices are still acquired because DA-to-RT spread is a required analytical input for forecasting exercises (`02 §10`) and because a future V2 can add DA offer behavior without redesign.

---

## 4. Economic Curtailment

### 4.1 Controller Rule

The plant controller curtails export when marginal revenue is negative:

```
curtail  if  P_rt,i + C_ptc < 0
```

With `C_ptc = 27.50`, the threshold is `P_rt,i < -$27.50/MWh`. The hedge does not enter the decision because hedge settlement is volumetrically fixed and therefore not marginal.

Curtailment is applied with a configured hysteresis band (default $5.00/MWh) and a minimum dwell time (default 15 minutes) to prevent unrealistic chattering at the threshold.

### 4.2 Why This Is the Most Valuable Scenario in the Package

An economic curtailment event has these properties simultaneously:

- Plant output drops sharply and materially.
- Irradiance is unchanged and high.
- No equipment fault exists.
- No fault code, no alarm beyond an informational curtailment event.
- Inverter states move to CURTAILED, which looks superficially like DERATED.
- It recurs, seasonally clustered, on high-wind high-solar spring days.

An analyst looking only at telemetry will misclassify it as an equipment problem. The only way to attribute it correctly is to join telemetry to the price series. This forces the discrimination `07 §8` demands and cannot be short-cut.

### 4.3 Curtailment Reason Taxonomy

`curtailment_reason` is a required enum on plant telemetry:

| Value | Trigger | Compensable |
|---|---|---|
| `NONE` | Not curtailed | - |
| `ECONOMIC` | Negative price net of PTC | No |
| `CONGESTION` | Transmission constraint / ERCOT dispatch instruction | Sometimes |
| `GRID_DIRECTIVE` | Reliability instruction from ERCOT | Yes |
| `VOLTAGE_FREQ` | Local grid condition outside limits | No |
| `MAINTENANCE` | Planned outage of export path | No |

The compensability column matters: uncompensated curtailment is lost revenue, compensated curtailment is not. Conflating them is a common real-world reporting error and a good exercise.

---

## 5. Loss Attribution Framework

### 5.1 Principle

Loss attribution is a **waterfall from theoretical maximum to metered export**. Each step subtracts a quantity with exactly one cause code. The waterfall must close exactly - residual is itself a tracked category and a large residual is a validation failure.

### 5.2 Waterfall

| Step | Quantity | Cause code | Monetized |
|---|---|---|---|
| 0 | Theoretical DC at STC-rated capacity and measured POA | `THEORETICAL` | - |
| 1 | Less thermal derating | `LOSS_THERMAL` | No |
| 2 | Less soiling | `LOSS_SOILING` | Yes - recoverable |
| 3 | Less degradation | `LOSS_DEGRADATION` | No - expected |
| 4 | Less mismatch and shading | `LOSS_MISMATCH` | No |
| 5 | Less DC wiring | `LOSS_DC_WIRING` | No |
| 6 | Less string / combiner unavailability | `LOSS_DC_OUTAGE` | Yes |
| 7 | Less inverter conversion | `LOSS_INVERTER_EFF` | No |
| 8 | Less inverter clipping | `LOSS_CLIPPING` | No - design choice |
| 9 | Less inverter unavailability | `LOSS_INV_OUTAGE` | Yes |
| 10 | Less inverter thermal derating | `LOSS_INV_THERMAL` | Yes |
| 11 | Less AC collection | `LOSS_AC_COLLECTION` | No |
| 12 | Less transformer | `LOSS_TRANSFORMER` | No |
| 13 | Less transformer / block unavailability | `LOSS_BLOCK_OUTAGE` | Yes |
| 14 | Less tracker fault or stow | `LOSS_TRACKER` | Yes |
| 15 | Less curtailment | `LOSS_CURTAIL_<reason>` | Per §4.3 |
| 16 | Less grid unavailability | `LOSS_GRID` | Depends |
| 17 | = Metered export | `EXPORTED` | Revenue |
| 18 | Unexplained | `LOSS_RESIDUAL` | Flagged |

**"Monetized" means the loss is converted to lost revenue and reported as avoidable.** Clipping and degradation are not monetized as losses because they are design and physics, not failures - reporting them as avoidable is a classic analytical error the dataset should let an analyst make and then correct.

### 5.3 Monetization

For each 15-minute interval and each cause code `c`:

```
LostEnergy_c,i    = energy attributed to cause c            [MWh]
LostRevenue_c,i   = LostEnergy_c,i * (P_rt,i + C_ptc)       [$]
```

Lost revenue uses the marginal rate `P_rt,i + C_ptc`, not the blended average, because the hedge is volumetrically fixed - a lost MWh is monetized at the price it would have earned at the margin.

**Lost revenue can be negative.** During negative-price intervals, an outage *saved* money. This is real, counterintuitive, and worth surfacing - it is the single best sanity check that an analyst understands the settlement model rather than pattern-matching "outage = bad."

### 5.4 Ground Truth Separation

The attributed waterfall lives in `solar_loss_attribution_truth` and is **validation-mode only** per DR-014. An analyst working blind must reconstruct attribution from telemetry, then score their reconstruction against truth. That scoring exercise is the core reliability-analytics curriculum item.

---

## 6. Operating Cost Model

### 6.1 Fixed Costs

| Item | Basis | Rate |
|---|---|---|
| O&M contract | $/kW-AC-yr | 12.00 |
| Asset management | $/kW-AC-yr | 2.00 |
| Insurance | $/kW-AC-yr | 4.50 |
| Land lease | $/acre-yr | 900 (approx. 700 acres) |
| Property tax | $/kW-AC-yr | 3.00 |

Fixed costs accrue on a daily straight-line basis so they appear in daily financial aggregates.

### 6.2 Event-Driven Costs

Triggered by maintenance and fault events, joined by `event_id`:

| Event | Cost | Notes |
|---|---|---|
| Truck roll | $850 | Any on-site dispatch |
| Inverter reset / site visit | $850 | Truck roll only |
| Inverter power module replacement | $18,000 | Plus truck roll |
| Inverter full replacement | $220,000 | Long lead time |
| Combiner / fuse repair | $1,400 | |
| String repair | $600 | |
| Transformer repair | $45,000 | |
| Transformer replacement | $310,000 | 12-20 week lead time |
| Tracker motor / controller | $2,400 | |
| Module cleaning, full plant | $3.50 / kW-DC | ~$440k per full wash |
| Module cleaning, per block | $3.50 / kW-DC of block | |
| Sensor replacement / calibration | $2,200 | |

Lead times matter: they determine outage duration, which determines lost energy, which determines lost revenue. A transformer failure in July is an order of magnitude more expensive than the repair cost alone.

### 6.3 Cleaning Economics

The cleaning decision is a genuine optimization the dataset should support:

```
CleanNow  if  E[soiling recovery over horizon] * E[price] > cleaning cost
```

Because soiling accumulates deterministically from the real precipitation series (DR-002 Layer 4) and rain resets it stochastically, the optimal cleaning date is a real decision problem with a real answer. This is one of the best applied forecasting exercises in the package.

---

## 7. Contractual Performance Guarantees

### 7.1 Availability Guarantee

| Parameter | Value |
|---|---|
| Guaranteed availability | 98.0% |
| Measurement basis | Energy-weighted, per `20_kpi_definitions` |
| Measurement period | Calendar year |
| Excluded events | Force majeure, grid outage, curtailment, planned maintenance within allowance |
| Planned maintenance allowance | 0.5% of annual daylight hours |
| Liquidated damages | Shortfall % x annual expected energy x $34.00/MWh x 1.0 |
| LD cap | 10% of annual O&M contract value |

### 7.2 Performance Ratio Guarantee

| Parameter | Value |
|---|---|
| Guaranteed PR, year 1 | 80.5% |
| Basis | Temperature-corrected PR, per `20_kpi_definitions` |
| Annual degradation allowance | 0.40% relative |
| Measurement period | Calendar year |
| Exclusions | Same as §7.1, plus curtailment |
| Liquidated damages | (PR_guaranteed - PR_actual) / PR_guaranteed x annual expected energy x $34.00/MWh |

### 7.3 Why Exclusions Are the Interesting Part

The exclusion rules are where operational analytics becomes contentious in reality. Whether a given hour counts against availability depends on cause attribution - which depends on the analysis in §5. The dataset should contain events where cause attribution is genuinely ambiguous from telemetry alone, so the exclusion calculation has a defensible answer and a naive answer that differ.

Required scenario: **SCN-090 disputed availability event** - an inverter trip during a grid voltage excursion, where the trip is arguably grid-caused (excluded) or arguably equipment-caused (counted).

---

## 8. Data Model Additions

### 8.1 New Time-Series Tables

**`solar_market_prices`** - hypertable, 15-minute grain

| Column | Type | Notes |
|---|---|---|
| `time` | timestamptz | Interval ending, UTC |
| `settlement_point` | text | Node or hub identifier |
| `price_type` | text | `RT_SPP`, `DA_SPP`, `RT_LMP` |
| `price_usd_mwh` | numeric | Signed; negatives are real |
| `source` | text | Provenance |
| `is_corrected` | boolean | ERCOT price corrections |

**`solar_settlement`** - hypertable, 15-minute grain

| Column | Type |
|---|---|
| `time` | timestamptz |
| `run_id` | uuid |
| `export_energy_mwh` | numeric |
| `rt_price_usd_mwh` | numeric |
| `hub_price_usd_mwh` | numeric |
| `energy_revenue_usd` | numeric |
| `hedge_settlement_usd` | numeric |
| `ptc_value_usd` | numeric |
| `basis_usd` | numeric |
| `gross_margin_usd` | numeric |
| `curtailed_energy_mwh` | numeric |
| `curtailment_reason` | text |

**`solar_loss_attribution_truth`** - hypertable, 15-minute grain, restricted role

| Column | Type |
|---|---|
| `time` | timestamptz |
| `run_id` | uuid |
| `asset_id` | text |
| `cause_code` | text |
| `lost_energy_mwh` | numeric |
| `lost_revenue_usd` | numeric |
| `is_monetized` | boolean |
| `scenario_id` | text |
| `event_id` | uuid |

### 8.2 New Operational Tables

**`solar_om_costs`** - event-joined cost records
**`solar_kpi_daily`** - daily rollup of PR, availability, capacity factor, revenue, losses
**`solar_guarantee_ledger`** - running annual position against §7 guarantees

---

## 9. Analytical Questions Enabled

The financial layer unlocks a class of question the physical model alone cannot express:

- What did each outage cost, and how does cost rank differently from duration?
- Which inverter is the worst performer by lost revenue rather than by lost kWh?
- What fraction of annual lost revenue is avoidable versus structural?
- What is the marginal value of one point of availability?
- Should the plant have been cleaned earlier, and by how much did the delay cost?
- Which faults occur disproportionately during high-price intervals?
- What is the revenue-optimal maintenance window, and does it match the scheduled one?
- How much did economic curtailment cost, and would a different PTC assumption change the curtailment decision?
- How does hedge shape mismatch vary seasonally, and what does that cost?
- What is the correlation between plant output and price - and what does negative correlation imply about merchant revenue per MWh?

The last one is the deepest: in a high-solar-penetration market, solar generates most when solar is worth least. Capture rate below 100% of the average price is the central economic fact of merchant solar, and it emerges naturally from real ERCOT prices joined to real production shape.

---

## 10. Validation Rules

Extending `15`:

1. Loss waterfall closes: `THEORETICAL - sum(all losses) = EXPORTED`, residual within 0.5% of theoretical.
2. `sum(15-min export energy)` reconciles with revenue-meter cumulative energy within tolerance.
3. Every monetized loss record references a valid event or scenario instance.
4. Hedge settlement is invariant to generation - verified by perturbing generation and confirming `R_hedge` is unchanged.
5. Curtailment occurs if and only if the §4.1 rule fires, accounting for hysteresis and dwell.
6. Negative-price intervals exist in the dataset. If a generated year contains zero negative prices, the price acquisition is wrong.
7. Annual capture rate is below 100% of the time-weighted average price. If it is above, production shape or price join is wrong.
8. Guarantee ledger arithmetic is reproducible from `solar_kpi_daily`.
9. No lost-revenue record exists for an interval with no lost energy.
10. Costs sum to the same annual total whether aggregated by event, by asset, or by month.

---

## 11. Deferred to V2

- Debt sizing, DSCR, amortization
- MACRS depreciation and tax equity flip
- LCOE and IRR
- Day-ahead offer strategy and DA/RT arbitrage
- Ancillary services participation
- Battery co-location and dispatch optimization
- REC / environmental attribute revenue
- Insurance claim modeling

The settlement structure in §3 and the cost structure in §6 leave room for all of these without schema redesign.


## 12. Amendments from Implementation

### 12.1 Curtailment Is Decided at Settlement Grain

`§4.1` gives the rule as `price + PTC < 0`. **The evaluation happens on
15-minute settlement prices, not on 1-minute telemetry.**

Curtailment is a commercial decision and settlement is 15-minute, so evaluating
against minute-level price noise is wrong twice over: no operator does it, and
it over-curtails badly. Deciding per minute produced 1,538 curtailed minutes of
which only 294 had a genuinely negative marginal price - the plant sat idle
through **$25,844 of positive-margin generation** because each brief noise
excursion triggered its own dwell.

**Note:** the configured 15-minute dwell equals exactly one settlement interval,
so it is the minimum possible. The hysteresis band does the anti-chatter work.

### 12.2 Cascading Attribution

`§5.2` losses are attributed **cascading, not independently**. A stage is
attributed what it removed *from what reached it*. Attributing each stage
against the theoretical maximum over-counts as soon as two stages act.

The formulation closes by construction: each stage is `upstream x (1 - factor)`
and the next starts from `upstream x factor`. Measured closure: **7.35e-19** of
theoretical energy against a 0.5% tolerance.

**Derating and clipping are not additive.** A derating inverter clips less.
Measured: clipping falls from 66.0 to 26.7 MWh when derating engages.

### 12.3 Hedge Independence Is Tested

`§3.2` asserts hedge settlement is independent of generation. This is now
**verified by perturbation**, not asserted: halving output leaves hedge
settlement bit-identical while energy revenue moves.

### 12.4 Curtailment Saved Money

The result the layer exists to produce: `LOSS_CURTAILMENT` of 3,425 MWh with a
revenue impact of **-$12,358**. Curtailing made money. Capture rate 57.8%.
