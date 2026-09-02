# 40 - Dashboard Verification

## The question, answered precisely

**Grafana is a verification block, not an implementation block.**

The dashboards are implemented. What was unverified was whether the panels
*work*, and that question splits into two parts which had been collapsed into
one:

| Question | Status |
|---|---|
| Do the panel queries execute and return data? | **VERIFIED - 11/11** |
| Does Grafana render them? | Not verified here |

The first is the substantive one. A dashboard fails in practice because its
query errors or returns nothing - not because the JSON is malformed. Conflating
the two let the important half sit unverified behind the unimportant half.

---

## 1. Every Panel Executes Against Live TimescaleDB

Run against the TimescaleDB instance from doc 39, with 645,184 rows loaded and
Grafana's `$__timeFilter` macro expanded as the server would:

```
northstar-overview
  [ok   ] Grid export                          1,093 rows
  [ok   ] Daily energy                             5 rows
  [ok   ] DC, inverter AC and export           1,093 rows
  [ok   ] Curtailed power                      1,093 rows

northstar-inverters
  [ok   ] Normalised output by inverter           40 rows
  [ok   ] Peer ratio over time                 7,774 rows
  [ok   ] Operating state distribution             5 rows
  [ok   ] Inverter internal temperature          365 rows

northstar-data-quality
  [ok   ] Data availability by inverter           20 rows
  [ok   ] Missing samples over time              365 rows
  [ok   ] Weather station disagreement           203 rows

11/11 panels return data
```

Every query is syntactically valid **against the real schema**, references only
columns that exist, and returns a non-empty result over the loaded window.

`db/tests/test_dashboard_panels.py` makes this repeatable. It treats an **empty
result as a failure**, not a pass: an empty panel renders as a blank graph
rather than an error, which is precisely how a broken dashboard survives
unnoticed.

---

## 2. Why Grafana Itself Was Not Started Here

Attempted, not assumed. Three routes, all closed:

| Route | Result |
|---|---|
| `apt.grafana.com` | HTTP 403 |
| `dl.grafana.com` official tarball | HTTP 403 |
| `api.github.com` releases | HTTP 403 |
| **Source from `codeload.github.com`** | **200, 34 MB - reachable** |

Source is obtainable, so the blocker is the build, not the download.

- Grafana 11.3 requires Go 1.23.1; Ubuntu ships 1.22.2
- Grafana 10.2.6 requires Go 1.21 and does build in principle
- Its dependency tree fetched **970 MB** of modules without completing
- The build outlives no single command in this environment, and a backgrounded
  process does not survive between them

This is an environment limit, not a defect in the artefact. **It is exactly the
step that is trivial for anyone with Docker.**

---

## 3. What To Check When You Run It

```bash
make db-up
```

Grafana provisions `dashboards/*.json` at startup. Three things worth
confirming, in order of what they would catch:

1. **Startup logs are clean.** Grafana logs a warning per dashboard it cannot
   parse or provision. Silence here means the JSON and the provider config are
   accepted.
2. **All three dashboards appear** under the `northstar` tag with the expected
   UIDs: `northstar-overview`, `northstar-inverters`, `northstar-data-quality`.
3. **Panels draw rather than showing "No data".** The queries are verified to
   return rows, so a blank panel would point at the datasource `uid` -
   `northstar-timescaledb` must match the provisioned datasource name.

Item 3 is the only one the work here cannot pre-empt, because it depends on the
datasource wiring in your compose file rather than on the dashboards.

---

## 4. Remaining Unverified, Precisely Stated

**Grafana's rendering of these panels.** Not "the dashboards are unverified" -
their SQL is verified against the real schema with real data. What is unverified
is the JSON-to-visual step and the datasource uid resolution.

That is a materially smaller claim than the one this project carried for four
phases, and the difference came from separating two questions that had been
treated as one.

---

## 5. Downstream Document Updates Required

- `02 §13`: panel queries are verified against a live database; only rendering is outstanding


---

## 6. Rendered

Grafana renders all three dashboards against the live datasource. The last item
in the "written but never executed" category is closed.

### The bug that made every panel look broken

Panels showed "No data" while the connection tested valid and the SQL was
correct. Widening the time picker by hand made data appear immediately.

`dataset_time_range` read timestamps back from DuckDB in the **session's local
timezone**, so `isoformat()` produced `2023-06-20T22:00:00-07:00`. Grafana
stores that verbatim and re-interprets it against the dashboard timezone, which
defaults to browser time - shifting the default window off the data.

Every individual component was correct. The datasource authenticated, the uid
resolved, the SQL was schema-qualified, the panels returned rows when asked.
Only the **default window** was wrong, and it presented identically to a
connection failure.

Now emitted as UTC with a `Z` suffix, which has no offset to re-interpret.

### Grafana is now pinned

`grafana/grafana-oss:latest` moved to a major version with reworked
provisioning partway through development, which is why the logs showed
`provisioning-repository-controller` and no `provisioning.datasource` line.

Everything else in this project is pinned - pvlib 0.15.2, Python 3.13,
timescaledb-ha:pg16. `latest` on an infrastructure image was an oversight, and
it cost real diagnostic time chasing a version difference that looked like a
configuration error.

Pinned to `11.3.0`.
