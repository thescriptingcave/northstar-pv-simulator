# 42 - ERCOT Retention and the Year-Overlap Constraint

Probing the live ERCOT API settled four open questions and exposed a
constraint the design had not accounted for.

---

## 1. Measured Retention

`make ercot-retention` against `HB_WEST`, run 2026-08-30:

| Year | Result |
|---|---|
| 2018 - 2024 | **no data** - HTTP 200, `totalRecords` 0 |
| 2025 | 192 records for a two-day probe window |
| 2026 | 192 records for a two-day probe window |

**ERCOT's public API retains roughly one year.** Every earlier year returns
`HTTP 200` with an empty result - success, semantically, and indistinguishable
from a bad request unless you read `_meta.totalRecords`.

This is why the original failure was so opaque: nothing errored. The request
succeeded, the payload parsed, the frame was empty, and every structural check
passed vacuously over zero rows.

---

## 2. The Constraint the Design Missed

`19` set one year list and applied it to every source. That is wrong, and not
by a small margin:

| Source | Available |
|---|---|
| NSRDB GOES CONUS | 2018 onward |
| Open-Meteo ERA5 | decades |
| **ERCOT prices** | **~1 year** |

Configured as 2019-2024 for all four, weather and prices had **no overlapping
year at all**. Settlement, capture rate, curtailment economics and lost-revenue
attribution all need weather and prices for the *same* period, so none of them
could have run on real data.

The simulator would still have produced datasets - with synthetic prices, which
is the development stand-in, not the intent.

### Resolution

| Source | Years | Why |
|---|---|---|
| SRC-WX-01, SRC-WX-03 | 2023, 2024, 2025 | Multi-year span for degradation (`03 §10`), ending on the price-overlap year |
| SRC-PX-01, SRC-PX-02 | 2025 | The only complete year ERCOT holds |

**2026 is deliberately excluded.** It is the current year and therefore partial;
its later months would return zero rows and fail the non-empty check.

Plan size drops from 445 partitions to **79**.

---

## 3. Consequences to Accept

**One year of real settlement.** Financial analysis against real prices is
limited to 2025. Multi-year financial work needs either a commercial data
source or prices accumulated by running the fetch periodically as the retention
window advances.

**Degradation analysis is unaffected.** It needs multi-year *weather* and a
plant age that advances, both of which are available. The blind recovery in
`35` used synthetic weather and did not depend on prices at all.

**The retention window moves.** These years are correct as of 2026-08-30 and
will not be next year. `make ercot-retention` prints the current answer and the
exact config line; it should be re-run before any long backfill.

---

## 4. Client Defects Found by the Same Probe

**Pagination was not implemented.** One month of one node reported
`totalRecords` 2,974 across 3 pages while returning 1,000 rows. The client read
page one and stopped - a silent loss of two thirds of every price month, which
validated cleanly because the rows present were well-formed. Now pages through
and raises if the total disagrees with the API's own count.

**Redirects were treated as success.** `if status < 400: return response`
returned a 302 to the caller, which then handed a redirect body to
`response.json()`. The failure surfaced as a JSON decode error naming nothing
useful. Now raises with the `Location` header.

**Empty partitions passed validation.** Covered in `41`; the non-empty check is
what turned this whole investigation from a silent success into a diagnosable
failure.

---

## 5. What Was Not Wrong

Worth recording, because two hours were spent suspecting them:

- **`HB_WEST` is a valid settlement point.** Probe G lists all five hubs;
  probe B returned it with `settlementPointType` `HU`.
- **`size=10000` is accepted.** Probe F returned HTTP 200 with it.
- **The endpoint and subscription are correct.** Probe C returned 324,310
  records unfiltered.

Every one of those was a plausible hypothesis, and the probe eliminated them in
a single run. Writing it was worth more than any amount of reasoning about the
symptom.

---

## 6. Downstream Document Updates Required

- `19 §4.4`: price years are not weather years; record the measured retention
- `03 §10`: multi-year financial analysis is limited by ERCOT retention, not by design
- `21 §5`: DR-015 node validation must use a year inside the retention window


---

## 7. Four Defects in Price Parsing

A full backfill fetched 56 of 79 partitions. The 18 failures resolved into
three symptoms and four underlying defects, none of which a smoke test on one
partition could reach.

### 7.1 Delivery dates were parsed as UTC

**The most serious of the four.** ERCOT reports delivery dates in Central
Prevailing Time. The parser did:

```python
times = pd.to_datetime(frame[date_column], utc=True) + minutes
```

Every price landed **5-6 hours from where it belonged**. Prices would have been
joined to the wrong hours of generation, silently corrupting capture rate,
curtailment economics and lost-revenue attribution. The series stays
well-formed and exactly the right length, so nothing downstream objects.

It survived because the smoke test only ever fetched January. A summer month
and a DST month are what expose it.

### 7.2 Timestamps were interval-ending

`11 §3` requires interval-beginning and warns that getting it wrong "shifts
everything by one interval and is invisible until reconciliation fails".

It is worse than invisible at a fall-back. The interval **ending** 02:00 is not
an ambiguous local time, so `DSTFlag` cannot separate the two occurrences and
they collapse into a single row. Interval-beginning puts all four inside the
repeated hour where the flag resolves them.

### 7.3 DST fall-back was not handled

November partitions carried exactly **4 duplicates for 15-minute data and 1 for
hourly** - one repeated hour. `DSTFlag` is the disambiguator: `True` is the
first (daylight) occurrence, `False` the second (standard), which matches
pandas' `ambiguous` convention directly.

Row counts confirm it: March returned 2,972 rows against a 2,976 nominal
(a 23-hour day) and November 2,888 against 2,880 (a 25-hour day).

### 7.4 A settlement point reported under two type codes

`LZ_WEST` is returned as both `LZ` and `LZEW` with **identical prices** - two
rows per interval differing in nothing but the label. Observed as 2,976
duplicates in a 2,976-row month.

Collapsed, but only while the prices agree; divergent prices raise, because
then the codes mean different things and silently keeping one would be a data
error rather than a tidy-up.

### 7.5 Rate limiting

196 requests in six minutes is roughly 33/min sustained, against a configured
`min_request_interval_s` of 0.5 permitting 120/min. The retry gave up after
five attempts - about 60 seconds of backoff - while ERCOT throttles for longer.

Now eight attempts (~254 s) and **`Retry-After` is honoured**. The server was
stating the required delay and the client was guessing.

---

## 8. Why a Smoke Test Could Not Find These

`--limit 1` fetches the first partition of each source: January, `HB_WEST`.
That single partition has no DST transition, no duplicate type codes, no
summer/winter offset difference, and generates too few requests to be
throttled.

It was still worth having - it caught retention, authentication and pagination.
But **the first full backfill is a distinct test**, and the failures it produced
were all real.


---

## 9. LZ and LZEW Are Not the Same Series

`§7.4` recorded that `LZ_WEST` is published under two type codes with identical
prices, and dropped the duplicate only when the prices agreed.

**That held for the sampled day and not for the year.** The first full backfill
raised on a month where they diverge, which stranded every remaining partition
in the run - a guard doing its job and, in doing so, making the run worse than
a silent wrong answer would have been.

### Resolution

Selection is by documented preference, `PREFERRED_POINT_TYPES = ("RN", "HU",
"LZ", "LZEW")`, with any disagreement logged at warning level. Resource nodes
and hubs are unambiguous; only load zones carry the alternative.

Deterministic ordering matters: choosing whichever row the API returned first
would make the price depend on response ordering, which no one would ever
notice.

### Measured: they are the same product

`make ercot-lz-compare` on `LZ_WEST` for June 2025:

| | LZ | LZEW |
|---|---|---|
| mean | 34.36 | 34.36 |
| min | -5.58 | -5.58 |
| max | 1629.89 | 1629.90 |
| negative intervals | 173 | 173 |

Identical on **80% of intervals**; where they differ the mean gap is **-0.012**
and the largest **0.96**.

Same mean, same minimum, same count of negative intervals. These are one
product with rounding or a settlement revision, not competing series. **The
`LZ` default is safe**, and the question raised below is closed.

### Originally open, now resolved

**Which series should be settled against is a question about ERCOT's products,
not about this code.** `make ercot-lz-compare` reports, for a chosen zone and
month, how many intervals differ and by how much.

- a small, mostly-zero difference means one product with rounding or revisions
- a large or structural difference means two products, and the default is a
  choice that needs justifying

The plant settles at its resource node (`HRNT_SLR_RN`) and hedges against the
hub (`HB_WEST`); `LZ_WEST` is a zonal reference only. The measurement above
confirms the default does not affect results either way.

### A note on guard design

The raise was correct as a detector and wrong as a response. A data-quality
guard on a **reference** series should not be able to abort a backfill of
primary data. The rule worth keeping: fail loudly where correctness depends on
it, warn and proceed where it does not.


---

## 10. The Truncation Guard Rejected Valid Data

The completeness check added in `§7` compared the **post-filter** row count
against the API's `totalRecords`:

```
paged fetch for LZ_WEST 2025-01-01..2025-01-31 returned 2976 rows
against a reported totalRecords of 5952
```

`totalRecords` counts raw rows. A load zone publishes every interval under two
type codes, so a complete January genuinely arrives as 5,952 rows and is
correctly reduced to 2,976 by type selection. The guard was comparing two
different quantities.

Now counts **rows received**, before filtering. Genuine truncation still
raises; a filtered month does not.

### The pattern, twice in one session

Both this and the LZ raise in `§9` were guards that fired on correct data. A
guard is code, gets the same review as any other code, and the question to ask
of it is not "does this catch the bad case" but **"what exactly is it comparing,
and are those two things the same kind of thing?"**

Here they were a filtered count and an unfiltered total. In `§9` it was a
reference series and a primary one.


---

## 11. The Day-Ahead Parser Inherited None of the Fixes

A full backfill reached **76 of 79 partitions**, and
`check_negative_prices_present` executed for the first time and **passed** for
`HB_WEST` 2025.

The three remaining failures were all day-ahead November, one duplicate each -
one repeated hour.

`ErcotDayAheadPriceSource` overrides `_parse_response`, and so carried its own
copy of all three timestamp defects from `§7`: dates parsed as UTC, labels left
interval-ending, no DST handling. Every fix went into the parent and none
reached it.

Real-time is 15-minute and day-ahead is hourly, so the same bug produced 4
duplicates in one place and 1 in the other - which read as two unrelated
problems rather than one unfixed parser.

### Resolution

Localisation is now a single shared `_localize` helper on the base class, used
by both parsers, with a test asserting they are literally the same object.

**The rule:** duplicated logic in a subclass override is how the day-ahead
parser stayed broken through three rounds of fixes to its parent. Anything true
of both belongs in one place, and a test should assert that it is one place.


---

## 12. Complete

```
Fetch run summary
  fetched : 79
  skipped : 0
  failed  : 0
  requests: 197
```

`negative prices present for HB_WEST 2025`.

**DR-015 resolved.** `HRNT_SLR_RN` returned all twelve months at both cadences,
so the plant's pricing node is confirmed rather than provisional (`21 §5`).

### Eleven defects, and what found each

| Defect | Found by |
|---|---|
| `.env` never loaded | Every credentialed source failing while Open-Meteo succeeded |
| Blank credentials passed validation | Reading the code after a 401 |
| `plan` never checked credentials | Asking what "no errors" actually proved |
| No `--limit` flag | It was described before it existed |
| Empty partitions validated cleanly | 0 rows reported as "4 passed" |
| Pagination unimplemented | `totalRecords` 2,974 against 1,000 rows returned |
| Redirects treated as success | HTTP 302 surfacing as a JSON decode error |
| Price years set to weather years | HTTP 200 with `totalRecords` 0 for 2019 |
| Delivery dates parsed as UTC | Chasing the DST duplicates |
| Interval-ending labels | The one duplicate the DST fix could not remove |
| Day-ahead override missed every fix | 1 duplicate where real-time had 4 |

**Nine of the eleven were invisible to a single-partition smoke test.** The
smoke test was still worth having - it caught retention, authentication and
pagination - but a full backfill is a categorically different test, and the
first one should be expected to fail.

### The two most instructive

**Delivery dates as UTC** was the most dangerous and the least visible. The
series stays well-formed, the right length, and correlates plausibly with
everything; only a join against generation would have revealed it, by which
point the conclusions would already be wrong.

**The day-ahead override** stayed broken through three rounds of fixes to its
parent because it held a duplicate copy of the logic. Both parsers now share
one `_localize`, with a test asserting they are the same object.
