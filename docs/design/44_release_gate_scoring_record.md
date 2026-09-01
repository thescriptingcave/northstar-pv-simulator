# 44 - Blind Scoring for the Release Gate

`16 §16` names three criteria as "the point of the exercise; everything else is
infrastructure". One - degradation recovery - was demonstrated in `35` at
0.055 pp error. The other two had never been scored.

This phase builds the harness and produces the first real measurement of one
of them. The other is blocked on a method problem that is now understood.

---

## 1. What Makes It a Measurement

The detector receives a DuckDB connection to the **analyst tree only**. Truth
is opened afterwards, purely to score.

That separation is the whole design. A detector that can reach the answer is
not being measured against it, and every previous claim that this dataset is
"analysable" was an assertion rather than a result.

`northstar_sim/scoring.py`:

| | |
|---|---|
| `detect_underperformance` | Peer-ratio detection from the analyst tree |
| `score_detection` | Overlap match against `scenario_instances` |
| `compare_rankings` | Energy against revenue ordering |
| `run_blind_scoring` | Runs detection, then opens truth |

Detection normalises each inverter against its **block** mean, not the plant
mean. Against the plant, the spatial cloud field manufactures underperformers:
an inverter under a cloud is not a faulty inverter.

---

## 2. Criterion 1 - Measured

A full simulated year of 2025 on real NSRDB irradiance:

| | |
|---|---|
| injected | 421 |
| detected | 202 |
| matched | 165 |
| **recall** | **39.2%** |
| **precision** | **81.7%** |

A seven-day dataset gave 42.9% and 75.0%, so these are stable properties
rather than small-sample noise.

### Reading it honestly

**39.2% is a statement about the detector, not about the data.** It is a fixed
peer-ratio threshold with no awareness of fault class. It finds inverter trips
and partial DC loss, which open a sharp gap against block peers, and
systematically misses stuck trackers and soiling, which do not.

Precision of 81.7% says the signal is present and recoverable. Recall says one
naive method does not recover all of it.

**The correct response is a better detector, not a lower bar.** Tuning the
threshold until the number looks good would defeat the purpose of scoring
blind at all.

---

## 3. Criterion 3 - Blocked, and Why

```
scenario_id  energy_mwh  revenue_usd  energy_rank  cost_rank  moved
    SCN-040  383.579697  7055.479553            1          1  False
    SCN-043   12.271865   217.603091            2          2  False
```

Only **two of four** injected scenario classes appear. SCN-026 and SCN-046 are
absent, and that is a flaw in the attribution method.

### The cause

Loss is computed as `available_power_kw - ac_power_kw`: what the inverter could
have delivered against what it did.

That only captures faults which open a gap between capability and outcome -
trips, partial DC loss. **A stuck tracker reduces `available_power_kw`
itself**, so the gap stays zero and the fault contributes nothing. Any fault
that degrades the resource rather than the conversion is invisible to this
measurement.

So the identical rankings prove nothing. Two classes are missing, and the two
present happen to order the same way.

### The fix

**A fault-free counterfactual.** Run the same weather and seed with
`--no-faults`, and attribute each scenario's loss as the difference from that
baseline. That captures every fault class regardless of mechanism.

It needs a second full-year run and attribution logic to join the two datasets
by asset and interval. Real work, and the right way to do it.

---

## 4. Release Gate Status

| Criterion | State |
|---|---|
| Canonical 3-year dataset, T0-T7 | one year done; three needed |
| Scenarios from every class | four classes injected |
| Three-way reconciliation | closed (`39`, 1e-7 MWh) |
| Parquet/DuckDB without containers | closed |
| Dashboards reveal expected behaviour | panel queries verified; **rendering not** |
| **Blind fault identification** | **measured: 39.2% recall, 81.7% precision** |
| **Degradation recovery in tolerance** | closed (`35`, 0.055 pp) |
| **Cost ranking differs from energy** | **blocked on counterfactual attribution** |
| Documentation matches implementation | closed (`36`, `43`) |

---

## 5. One Bug Found

The price lookup synthesised an index starting `2025-01-01 00:00 UTC` while
ERCOT prices start `06:00 UTC` - midnight Central. The coverage guard rejected
a full valid year for being six hours late, and reported "no cached real
prices".

It now reads the dataset's own timestamps and tolerates a lead-in under a day.

**The same shape as several defects in `43`**: a guard comparing two things
that were not quite the same kind of thing.
