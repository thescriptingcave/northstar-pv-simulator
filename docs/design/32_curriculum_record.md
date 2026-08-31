# 32 - Analytics Curriculum Record (Phase 12)

## Result: PASSED

```
  [PASS] every_exercise_executes       14 exercises run
  [PASS] exercises_return_answers      14 of 14 returned rows
  [PASS] difficulty_is_graded          tiers 1-7, two exercises each
  [PASS] contract_skills_covered       37 skills covered
  [PASS] every_exercise_has_an_insight each exercise states what its answer means
  [PASS] dual_dialect_where_it_matters 1 distinct TimescaleDB form
```

---

## 1. Every Exercise Is Executed, Not Reviewed

A curriculum of SQL that has never run is a list of plausible-looking queries. The subtly wrong ones are exactly the ones a learner cannot diagnose - they run, they return something, and the something is wrong in a way that requires already knowing the answer to spot.

The gate exports a fresh dataset and runs all fourteen against it. The window is deliberately hot and curtailed, because an exercise that separates four low-output conditions cannot be validated on a window containing two of them.

---

## 2. EX-601 Was Wrong Three Times, Each Time Cleanly

The stuck-sensor scan is worth documenting in full, because every wrong version executed without error and returned something plausible.

**Attempt 1 - scanned only `ac_power_kw`.** Returned nothing. The injected freezes were on `internal_temp_c` and `ac_preclip_kw`. A stuck channel can be any of them, and which one is not knowable in advance.

**Attempt 2 - hand-listed four channels.** Still missed `ac_preclip_kw`. Hand-enumeration is the wrong method; `UNPIVOT` scans them all.

**Attempt 3 - unpivoted every channel, no daylight filter.** Returned overnight standby at the top: `ac_power_kw` held at exactly `-0.7` kW for 646 minutes, and `internal_temp_c` constant at 50.0 C all night. Both entirely legitimate.

**Attempt 4 - excluded zeros but still no daylight filter.** Same problem, plus curtailed inverters holding output at exactly 0.0 kW for hours, which is real behaviour and not a frozen sensor.

**Correct version** filters to operating hours *before* unpivoting, then excludes remaining constants. On the development dataset it recovered both injected `ac_preclip_kw` freezes with **zero false positives**.

The exercise's insight now names all three traps in the order a learner hits them.

---

## 3. Recall Is Not Asserted, and That Is Deliberate

The test asserts **no false positives**. It does *not* assert that every injected defect is found.

A freeze occurring at night, or while the inverter is curtailed, is invisible to a scan filtered to operating hours with output above zero. On one test window the query correctly returned nothing at all, because all three freezes fell outside the detectable region.

That is a real limitation of the method rather than a defect in the query, and an analyst should understand it. A test asserting recall would have forced the query to be loosened until it produced false positives.

---

## 4. A Test That Enshrined a Misreading

`test_efficiency_rises_with_load` asserted monotonic increase. It failed at 0.708 against 0.963.

The assertion was wrong. Measured efficiency by DC load band:

| DC band | Efficiency |
|---|---|
| 0-250 kW | 0.964 |
| 1000 kW | **0.9886** (peak) |
| 2500 kW | 0.857 |
| 2750 kW | 0.727 |

Above the AC cap, `ac / dc` stops measuring conversion efficiency and starts measuring **clipping**; under thermal derating it falls further. EX-202's insight already said so. The test asserted the exact misreading the exercise exists to correct.

Now it asserts the real shape: a rise off low load, a peak below unity, and a fall in the top band.

---

## 5. The Curriculum

Seven tiers, two exercises each, every tier building on the last.

| Tier | Focus | Exercises |
|---|---|---|
| 1 | Reading the data | Daily energy from power; night must be zero |
| 2 | Window functions | Steepest ramps; rolling efficiency |
| 3 | Peer comparison | Underperformer ranking; deviation from peers |
| 4 | Events and durations | Outage durations (gaps and islands); before and after |
| 5 | Discrimination | Why is output low; curtailment looks like a fault |
| 6 | Data quality | Find stuck sensors; missing data inventory |
| 7 | Economics and shape | Production by local hour; block-to-block |

### Answers worth seeing

**EX-102** - night maximum AC power is **-0.7 kW**, not zero. An energised inverter consumes. A daylight filter that keeps these rows biases every efficiency calculation slightly negative.

**EX-501** on a hot, curtailed week separates all four conditions:

| Condition | Minutes | Mean AC | Mean POA |
|---|---|---|---|
| Thermal derate | 140,915 | 2,252 kW | 960 W/m2 |
| Normal | 43,916 | 1,360 kW | 519 W/m2 |
| Low resource | 27,001 | 178 kW | 72 W/m2 |
| **Curtailed** | **15,000** | **0 kW** | **1,012 W/m2** |

The last row is the teaching artifact: full irradiance, zero output, no fault code anywhere.

**EX-701** - overnight plant export is **-120 kW**: forty inverters at -0.75 kW standby plus ten transformers at -9 kW no-load. Station service is real and it appears in the meter.

---

## 6. Dual Dialect Where It Matters

Only one exercise carries a genuinely distinct TimescaleDB form. That is the finding, not a shortfall: most of the curriculum needs no time-series-specific feature, and pretending otherwise would teach that `time_bucket` is required where `date_trunc` suffices.

Where the two differ - hierarchical continuous aggregates, gap filling, retention - the difference is real and worth the second version.

---

## 7. What Remains

- Grafana dashboards (`16 §15`) - deferred; the queries here are the substance behind them
- Python notebooks for forecasting and change-point work
- The TimescaleDB leg of the three-way reconciliation, still outstanding from Phase 11

---

## 8. Downstream Document Updates Required

- `02 §11`: record the 37 skills covered against the contract
- `16 §15`: curriculum is exercise-driven and executed in the gate, not authored and shelved
- `20 §11`: EX-602 pairs data availability with every performance figure, as required


---

## 9. Amendment: Both Dialects Are Now Runnable

The generated files originally emitted the DuckDB query plus a comment reading
"the TimescaleDB form is identical apart from the schema prefix".

That was **wrong for three of fourteen exercises** and unhelpful for all of
them: tables live in the `telemetry` schema, not `public`, so pasting the
portable form into a database client fails with
`relation "inverter_telemetry" does not exist`.

Three genuine dialect incompatibilities were hiding behind that claim -
`INTERVAL 60 MINUTE`, `UNPIVOT`, and a `::DOUBLE` cast - each now carrying a
hand-written TimescaleDB form, with the difference called out in its hint.

**14/14 execute against PostgreSQL**, enforced by
`db/tests/test_exercises_postgres.py` in `make db-test`.

`sql/timeseries/` was added alongside for queries that genuinely require
TimescaleDB; see `41 §5`.
