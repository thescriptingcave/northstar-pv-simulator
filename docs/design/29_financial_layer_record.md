# 29 - Financial Layer Record (Phase 9)

## Result: PASSED

```
  [PASS] capture_rate_below_unity       57.8% (generation-weighted over time-weighted)
  [PASS] negative_prices_present        11.4% negative, 3.89% of settlement
                                        intervals below the negative-PTC threshold
  [PASS] economic_curtailment_fires     35.0 h (4.86% of the record)
  [PASS] curtailment_saved_money        lost revenue $-12,358
  [PASS] hedge_independent_of_output    $467,898 settled on fixed volume
  [PASS] energy_revenue_positive        $403,874
  [PASS] losses_split_by_recoverability 5 avoidable, 10 structural cause codes
  [PASS] cost_ranking_is_informative    energy and revenue rankings differ
```

---

## 1. The Result That Justifies the Phase

**`LOSS_CURTAILMENT`: 3,425 MWh lost, revenue impact `-$12,358`.**

Curtailing *made money*. The plant stopped generating during intervals where price plus the production tax credit was negative, and not generating was worth more than generating.

Any analysis pipeline that reports lost revenue as unconditionally positive breaks here, and finding out why is the single best check that an analyst understands the settlement model rather than pattern-matching "outage equals bad".

**Capture rate 57.8%** - generation-weighted price $12.31 against time-weighted $26.07. Below 100% is the central economic fact of merchant solar: the plant produces most when its output is worth least. It is invisible in every physical metric and emerges only from joining prices to production shape.

---

## 2. Three Calibration Failures, All Silent

The synthetic price series is a development stand-in - production prices come from the cached ERCOT data in `northstar-fetch`. But its *structure* determines every financial conclusion, and getting it wrong produces confident nonsense rather than an error.

### 2.1 Clearness is not penetration

Penetration was proxied by the **clear-sky index**, which sits near 1.0 from sunrise to sunset. Prices were therefore suppressed across the entire day rather than in a midday bell.

Result: 27% of intervals negative, and a **generation-weighted price of -$3.77/MWh** - the plant was paying to run, all month. Regional solar *output* follows a diurnal bell; clearness does not.

### 2.2 Suppression depth

A suppression coefficient of 46 against a base of 32 drove **54%** of intervals negative and made total energy revenue negative. That is not a market anyone operates in. ERCOT West runs roughly 10-15% negative annually, concentrated in high-wind spring afternoons.

Retuned to base $38 with $30 suppression: **11.4% negative**, mean $24.43/MWh.

### 2.3 Curtailment decided against noise

The curtailment decision was evaluated per minute. Each brief noise excursion below the threshold triggered its own 15-minute dwell.

| | Per-minute decision |
|---|---|
| Curtailed minutes | 1,538 |
| Of which marginal price was genuinely negative | 294 (19%) |
| Value destroyed sitting idle at positive margin | **$25,844** |

**Curtailment is a commercial decision and settlement is 15-minute.** The decision now uses settlement-grain prices. Deciding against minute-level noise is wrong twice over: no operator does it, and it over-curtails badly.

Moving to settlement grain then exposed the next layer of the problem: with the noise smoothed away, the 15-minute average never fell below the threshold and curtailment stopped firing entirely. The sub-threshold prices had been **noise artefacts, not structure**. Real deeply-negative prices come from structural oversupply, so the windy-episode term was deepened until the settlement-grain series reaches the threshold on its own.

Final: 3.89% of settlement intervals below the negative-PTC threshold, 35 hours of curtailment per month, 64% of curtailed minutes at genuinely negative marginal price.

---

## 3. Settlement Structure

| Component | Basis | Month total |
|---|---|---|
| Energy revenue | Metered export at node price | $403,874 |
| Hedge settlement | Fixed 70 MW at $34.00 strike, contract for differences | $467,898 |
| Production tax credit | $27.50/MWh on generated energy | $873,601 |
| Basis | Node minus hub, reporting only | small |

**The hedge settles independently of generation.** A test asserts that halving output leaves hedge settlement unchanged while energy revenue moves. That independence is what creates volume risk: an outage during a scarcity hour costs the hedge buy-back *on top of* the lost energy, so the cost of an outage depends on when it happens, not only on how much energy it cost.

Lost revenue uses the **marginal** rate `price + PTC`, not a blended average, because the hedge is volumetrically fixed. A lost MWh is monetized at what it would have earned at the margin.

---

## 4. Cost Ranking Differs From Energy Ranking

The gate asserts this explicitly. If the two always agreed, the financial layer would add nothing beyond a unit conversion.

They differ because the marginal rate varies by an order of magnitude across the day. A loss concentrated in evening scarcity hours outranks a larger loss spread across suppressed midday hours.

---

## 5. KPIs

Measured over 30 days at 38 C ambient:

| Metric | Value |
|---|---|
| Performance ratio | 0.8297 |
| Temperature-corrected PR | 0.9445 |
| Capacity factor (AC) | 0.3928 |
| Availability, time-based | 0.9289 |
| Availability, daylight-weighted | 0.8704 |
| Availability, energy-weighted | 0.9977 |

### Two findings that correct design doc 20

**Daylight-weighted availability is *lower* than time-based**, not higher. Doc `20 §6` implies the ordering `time <= daylight <= energy`. That is wrong here, and correctly so: faults are scheduled during operating hours, so they concentrate exactly where daylight availability is measured. The same holds in real plants - stress-driven faults cluster in daylight. **The ordering claim in doc 20 should be removed.**

**Corrected PR exceeds raw PR by 11 points.** With the STC convention (`T_ref = 25 C`) and a -0.433 %/C module in a 38 C month, this is arithmetically correct: corrected PR answers "what would PR be if cells were at 25 C". Doc `20 §14.1` gives an expected corrected-PR band of 0.79-0.86, which is inconsistent with the 25 C convention in a hot climate. **Either the band or the reference convention needs restating.**

---

## 6. A Note on the Dwell Timer

The configured 15-minute dwell equals exactly **one settlement interval**, so it is the minimum possible and adds nothing beyond the interval itself. The hysteresis band does the anti-chatter work.

Raising `curtailment_dwell_minutes` is the lever if a longer minimum commitment is wanted - which would be realistic, since real controllers do not toggle export every fifteen minutes.

---

## 7. Downstream Document Updates Required

- `18 §4.1`: curtailment is decided at settlement grain, not per minute
- `18 §3`: confirm hedge independence is tested, not merely asserted
- `20 §6`: **remove** the `time <= daylight <= energy` ordering claim
- `20 §14.1`: restate the corrected-PR band against the 25 C reference convention
- `19 §4.4`: the synthetic price model is a development stand-in only; its calibration targets are recorded here for comparison against the real series
