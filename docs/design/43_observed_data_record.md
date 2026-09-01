# 43 - Running on Observed Data

Phase 0.5 fetched real NSRDB irradiance and real ERCOT prices into a
checksummed cache. **Nothing read them.** Every gate, every dataset and every
figure this project has published came from `clearsky_resource` and
`synthetic_prices` - both of which document themselves as development
substitutes for exactly that data.

This phase connects them. It is **incomplete**: week-scale runs pass acceptance
on real data, a full simulated year does not.

---

## 1. What Works

```bash
uv run northstar-sim generate --real --year 2025 \
    --start "2025-06-20 00:00" --end "2025-06-27 00:00" \
    --out datasets/week --run-id week
uv run northstar-sim accept --dataset datasets/week --run-id week
```

`VERDICT: ACCEPTED (24 checks, 0 failures)` — the first dataset built from real
irradiance and real settlement prices to pass.

`northstar_sim/observed.py` provides:

| | |
|---|---|
| `real_resource` | Reads cached NSRDB into the frame shape `clearsky_resource` returns, so it drops into `downscale_to_minute` unchanged |
| `real_prices` | Reads cached ERCOT prices for a settlement point |
| `align_prices` | Holds prices constant across their settlement interval |
| `available_years` | Reports coverage before a load fails |

Two decisions worth stating:

**Gaps are interpolated only across short outages and never zero-filled.** Zero
irradiance is a physically meaningful value - it means night - so filling a
daytime gap with zero fabricates an outage that did not happen.

**Prices are held, not interpolated.** A price is the clearing outcome for a
whole interval, not a sample of a continuous quantity; interpolating invents
values that never cleared.

---

## 2. A Live Bug in Cached Data

The fetch harmonizer matched provider columns with `column.lower()` against
underscored keys. NSRDB returns `"Wind Speed"` with a **space**, which lowers to
`"wind speed"` and never matched.

Five columns were silently unmapped in **every cached partition**: wind speed,
wind direction, dew point, relative humidity and surface albedo. `GHI`, `DNI`,
`DHI` and `Temperature` are single words and matched, which is what kept it
hidden until something asked for `wind_speed`.

**Values were never affected - only names.** The lookup now normalizes
separators as well as case, and the loader re-canonicalizes on read so an
existing cache stays usable rather than costing a 197-request refetch.

---

## 3. Closure Repair, and the Bug in the Repair

Transposition requires `GHI ~= DHI + DNI * cos(zenith)`. Components that
contradict it drive Perez to NaN, and **a single NaN poisons the sensor layer's
cumulative state for the entire series** - one bad interval nulls a whole year
of measured irradiance.

The first repair divided GHI by `cos(zenith)` with a 0.01 floor. At 89.5
degrees a GHI of 50 implies a DNI of **5,000 W/m2** against a solar constant of
1,367, which drove measured AC to **12,617 kW on a 2,500 kW inverter** - 405% of
nameplate.

Now bounded: repair only above 84 degrees zenith, and clamp both components to
the solar constant. Twilight carries no energy, so skipping it costs nothing.

**The repair runs at load time, not fetch time.** The cache stays a faithful
record of what the provider sent, but that means changes to this logic alter
what the simulator sees with no cache invalidation. `closure_repaired` is
recorded on the frame's attrs and is **not yet surfaced** in the CLI output.

---

## 4. The Acceptance Report Was Scoring the Wrong Prices

`command_accept` always regenerated a **synthetic** price series from the
dataset's own irradiance. A dataset built with `--real` was therefore scored
against prices that had nothing to do with the ones it was settled against.

Capture rate came out at 123-128% at every scale - above 1.0, which the check
itself documents as meaning the join is wrong - because the fault was
independent of the data.

It now prefers cached real prices, falls back to synthetic only when nothing
covers the range, and prints which it used. On a week-long run: **123.2% ->
101.9%**. The ceiling moved to 1.05, because prices uncorrelated with
generation sit at 1.0 by construction.

---

## 5. Checks That Crashed Instead of Failing

Five checks formatted a possibly-`None` aggregate and died three frames deep
with a `TypeError` naming a format string. An aggregate over zero rows returns
`None`.

They now report `no data` as a **named failure**. Unevaluated must never read
as satisfied - treating "no rows matched" as a pass would let an empty dataset
clear the entire report.

This happened twice: fixed on `night_generation`, then reappeared on
`fleet_poa_spread` because only the instance was fixed and not the pattern.

---

## 6. Open: The Scale Defect

A full simulated year is **not usable**.

| | |
|---|---|
| Truth POA | 20,929,040 non-null, correct |
| **Measured POA** | **33,360 non-null, every value exactly zero** |
| Measured AC | correct - 830.57 kW mean against 834.67 truth |
| Measured cell temperature | 1.6 to 11.7 C, impossible for West Texas |

Irradiance and temperature channels are destroyed; power channels are intact.
Every downstream failure follows: NaN correlations, empty fleet spread, PR of
0.0000, and all 525,596 intervals filtered because the KPI filter needs POA.

**Eliminated by experiment, not argument:**

- injected defects - a `--no-defects` run is identical
- the closure repair - truth is clean, peak DC 106.9% of nameplate
- sensor soiling - 3%/year gives 0.97, not zero
- record duration - measured POA survives a 365-day *index* when only one day
  is simulated

**Not eliminated: simulation volume.** Working runs were 300,000 rows; the
failing one is 21 million. A bisect on window length should bracket it in three
runs now that `--start`/`--end` work on the `--real` path.

---

## 7. Open: Illegal State Transitions

```
82 illegal inverter transitions, first: STARTING -> CURTAILED
```

Curtailment is commanded while an inverter is still inside its startup dwell,
and `07`'s transition table has no edge for it.

**Only real price data reaches this.** Synthetic prices rarely go negative in
the minutes after sunrise, so the path was never exercised. This is a genuine
gap in the state machine rather than a check problem.

---

## 8. What This Says About the Checks

Five acceptance checks failed on real data in this phase - the `None` crashes,
the sensor-error bound, the synthetic prices, the capture-rate ceiling. The
pattern is consistent and worth stating plainly:

**each was written against a clean, short fixture, and real year-long data with
defects broke it.**

Before the next batch of checks, one pass over `acceptance.py` asking of each
*"what data was this written against, and has it seen a defective, year-long
dataset?"* would find the remainder more cheaply than discovering them one
failed run at a time.

---

## 9. Downstream Document Updates Required

- `06`: the environmental model has an observed-data path; `clearsky_resource`
  is the fallback, not the only source **(applied)**
- `19 §16`: harmonization normalizes separators, not only case **(applied)**
- `15 §12`: the acceptance report prefers real prices where they exist
