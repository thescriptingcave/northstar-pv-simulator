# Time-Series Queries

Queries that **need** TimescaleDB. Everything in `../exercises/` runs on plain
PostgreSQL and is portable to DuckDB; nothing here is.

```bash
psql "$NORTHSTAR_DSN" -f sql/timeseries/01_time_bucket.sql
```

| File | Covers |
|---|---|
| `01_time_bucket.sql` | `time_bucket`, `first`/`last`, timezone-aware buckets |
| `02_gaps.sql` | `time_bucket_gapfill`, `locf`, `interpolate`, and their limits |
| `03_aggregates_and_ramps.sql` | continuous aggregates, `histogram`, ramp attribution |
| `04_joins_and_lag.sql` | cadence alignment, advection lag, peer normalisation |

Every query has been executed against a loaded database. The outputs quoted in
the comments are real.

## The four findings worth reading for

**The UTC-day trap (TS-102/103).** Bucketing a West Texas plant by UTC day
splits the solar day: local noon is around 18:50 UTC. The symptom is a
"generating span" of 23:59 - the day begins with the plant already producing
and ends with it still producing. `time_bucket` takes a timezone argument, and
that is the reason to prefer it over `date_trunc`.

**`locf` does not fill NULLs (TS-104/105).** It fills buckets that `gapfill`
*created*. A communications outage that writes rows with NULL columns - which
is what this simulator does, and what `19 §5.3` requires - leaves the buckets
present, so `gapfill` creates nothing and `locf` has nothing to fill. The
counting-window idiom does work, and then shows you why you should not use it:
it carries -0.7 kW of overnight standby across three hours of daylight.

**The steepest ramps are commercial (TS-108).** Curtailed buckets ramp at
3.53 MW on average against 0.85 MW for weather - four times steeper. No cloud
moves that fast across a 3.26 km site. An analyst hunting cloud ramps without
joining the curtailment signal will find the controller and conclude the site
has extraordinary weather.

**Detrend before correlating (TS-110).** Correlating raw irradiance between two
assets gives 0.998 at *every* offset from -12 to +12 minutes, because the
diurnal cycle is common to both and swamps everything. Subtract the plant mean
first; the residual peaks cleanly at -9 minutes, r = 0.141. The low correlation
is the correct answer - a clean interior peak matters, not a large number.

## Not covered here

`timescaledb_toolkit` hyperfunctions - `time_weight`, `counter_agg`,
`stats_agg`, `approx_percentile`, `lttb` - are **not** used. The toolkit ships
in the `timescaledb-ha` image these queries run against, but it was unavailable
in the environment where they were verified, and nothing here is shipped
unexecuted. `time_weight` in particular would improve the energy integrals.
