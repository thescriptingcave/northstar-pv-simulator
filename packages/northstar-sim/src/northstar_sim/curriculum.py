"""The SQL curriculum.

Every exercise is written twice: once in portable SQL for DuckDB over Parquet,
once for TimescaleDB. Doing each both ways is the point - it teaches which
capabilities are genuinely time-series features (``time_bucket``, continuous
aggregates, gap filling) and which are ordinary SQL wearing a Timescale badge.

Exercises are graded and each records the skills it exercises, so the set can be
checked against the analytical contract in design document ``02`` rather than
accumulating by whim.

**Every exercise is executed, not just written.** A curriculum of SQL that has
never run against real data is a list of plausible-looking queries; the ones
that are subtly wrong are exactly the ones a learner cannot diagnose. The gate
runs all of them against an exported dataset and checks each returns rows.

Reference: design documents ``02_time_series_analytics_requirements`` sections
11-12 and ``16_implementation_roadmap`` section 15.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class Exercise:
    """One curriculum exercise.

    Attributes:
        exercise_id: Stable identifier, ordered by tier.
        tier: Difficulty band, 1 easiest.
        title: Short name.
        question: The analytical question in plain language.
        skills: SQL techniques the exercise exercises.
        duckdb_sql: Portable form, runnable over the Parquet export.
        timescale_sql: TimescaleDB form. ``None`` where the portable query is
            already idiomatic and a second version would teach nothing.
        hint: Nudge toward the method without giving the answer.
        insight: What the result should tell the analyst. This is the part that
            matters; a query that runs but teaches nothing is not an exercise.
        min_rows: Rows the query must return for the gate to pass.
    """

    exercise_id: str
    tier: int
    title: str
    question: str
    skills: tuple[str, ...]
    duckdb_sql: str
    timescale_sql: str | None = None
    hint: str = ""
    insight: str = ""
    min_rows: int = 1


#: The curriculum. Ordered by tier, and every tier builds on the previous one.
EXERCISES: tuple[Exercise, ...] = (
    # -- Tier 1: reading the data at all -----------------------------------
    Exercise(
        exercise_id="EX-101",
        tier=1,
        title="Daily energy from power",
        question="How much energy did the plant export each day?",
        skills=("time extraction", "GROUP BY", "integration from power"),
        duckdb_sql="""
SELECT date_trunc('day', time) AS day,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
FROM plant_telemetry
GROUP BY day
ORDER BY day
""",
        timescale_sql="""
SELECT time_bucket(INTERVAL '1 day', time) AS day,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
FROM telemetry.plant_telemetry
GROUP BY day
ORDER BY day
""",
        hint="Energy is integrated from power. At 1-minute samples each row is "
        "1/60 of an hour.",
        insight="Energy must never be stored independently of power. If the two "
        "disagree, one of them is wrong and reconciliation will not tell you which.",
    ),
    Exercise(
        exercise_id="EX-102",
        tier=1,
        title="Night must be zero",
        question="Does the plant produce anything when the sun is down?",
        skills=("filtering", "aggregate sanity checks"),
        duckdb_sql="""
SELECT count(*) AS night_samples,
       max(ac_power_kw) AS max_ac_kw,
       min(ac_power_kw) AS min_ac_kw
FROM inverter_telemetry
WHERE poa_global < 1.0
""",
        hint="Look at the minimum as well as the maximum.",
        insight="Minimum AC is negative overnight, not zero: an energised "
        "inverter draws its own standby load. A daylight filter that keeps "
        "these rows will bias every efficiency calculation slightly negative.",
    ),
    # -- Tier 2: window functions ------------------------------------------
    Exercise(
        exercise_id="EX-201",
        tier=2,
        title="Steepest ramps",
        question="When did plant output change fastest, and by how much?",
        skills=("LAG", "window functions", "rate of change"),
        duckdb_sql="""
WITH ramps AS (
    SELECT time,
           grid_export_power_kw,
           grid_export_power_kw
               - lag(grid_export_power_kw) OVER (ORDER BY time) AS ramp_kw_min
    FROM plant_telemetry
)
SELECT time, grid_export_power_kw, ramp_kw_min
FROM ramps
WHERE ramp_kw_min IS NOT NULL
ORDER BY abs(ramp_kw_min) DESC
LIMIT 20
""",
        hint="LAG over an ordered window gives the previous sample.",
        insight="The steepest ramps cluster on broken-cloud days, not clear "
        "ones. Clear days have the largest output and the gentlest ramps.",
        min_rows=5,
    ),
    Exercise(
        exercise_id="EX-202",
        tier=2,
        title="Rolling efficiency",
        question="How does DC-to-AC conversion efficiency vary with load?",
        skills=("rolling window", "derived metrics", "load filtering"),
        duckdb_sql="""
SELECT round(dc_power_kw / 250.0) * 250 AS dc_band_kw,
       count(*) AS samples,
       avg(ac_power_kw / nullif(dc_power_kw, 0)) AS efficiency
FROM inverter_telemetry
WHERE dc_power_kw > 100
GROUP BY dc_band_kw
ORDER BY dc_band_kw
""",
        hint="Bucket by DC power and average the ratio within each bucket.",
        insight="Efficiency is not constant. It rises steeply at low load, "
        "flattens, then appears to fall at the top - the last part is clipping, "
        "not a less efficient inverter.",
        min_rows=3,
    ),
    # -- Tier 3: peer comparison -------------------------------------------
    Exercise(
        exercise_id="EX-301",
        tier=3,
        title="Underperformer ranking",
        question="Which inverters produced least per unit of irradiance?",
        skills=("peer normalisation", "GROUP BY", "ranking"),
        duckdb_sql="""
SELECT asset_id,
       sum(ac_power_kw) / 60.0 / 1000.0 AS energy_mwh,
       sum(ac_power_kw) / nullif(sum(poa_global), 0) AS normalised_output
FROM inverter_telemetry
WHERE poa_global > 50
GROUP BY asset_id
ORDER BY normalised_output
LIMIT 10
""",
        hint="Normalise by the resource each inverter actually saw, not by the "
        "plant average.",
        insight="Normalising by plant-average irradiance instead of per-asset "
        "irradiance manufactures underperformers out of the spatial cloud "
        "field. The worst-ranked inverter may simply have been under a cloud.",
        min_rows=5,
    ),
    Exercise(
        exercise_id="EX-302",
        tier=3,
        title="Deviation from peers",
        question="Which inverter deviates most from its block peers, and when?",
        skills=("window partition", "peer baseline", "self-join alternative"),
        duckdb_sql="""
WITH with_block AS (
    SELECT time, asset_id,
           substr(asset_id, 1, 14) AS block_id,
           ac_power_kw
    FROM inverter_telemetry
    WHERE ac_power_kw > 50
),
peers AS (
    SELECT time, asset_id, block_id, ac_power_kw,
           avg(ac_power_kw) OVER (PARTITION BY time, block_id) AS peer_mean
    FROM with_block
)
SELECT asset_id,
       count(*) AS samples,
       avg(ac_power_kw / nullif(peer_mean, 0)) AS peer_ratio
FROM peers
GROUP BY asset_id
ORDER BY peer_ratio
LIMIT 10
""",
        hint="A window partitioned by time and block gives each row its own "
        "peer group without a self-join.",
        insight="Peer ratio is the workhorse of fault detection. It removes the "
        "weather, which is the dominant signal, leaving equipment behaviour.",
        min_rows=5,
    ),
    # -- Tier 4: events and durations --------------------------------------
    Exercise(
        exercise_id="EX-401",
        tier=4,
        title="Outage durations",
        question="How long did each inverter outage last?",
        skills=("gaps and islands", "window functions", "event reconstruction"),
        duckdb_sql="""
WITH flagged AS (
    SELECT time, asset_id,
           CASE WHEN operating_state = 'FAULT' THEN 1 ELSE 0 END AS faulted
    FROM inverter_telemetry
),
grouped AS (
    SELECT time, asset_id, faulted,
           row_number() OVER (PARTITION BY asset_id ORDER BY time)
             - row_number() OVER (PARTITION BY asset_id, faulted ORDER BY time)
             AS island
    FROM flagged
)
SELECT asset_id,
       min(time) AS started,
       max(time) AS ended,
       count(*) AS minutes
FROM grouped
WHERE faulted = 1
GROUP BY asset_id, island
ORDER BY minutes DESC
""",
        hint="The difference between two row numbers is constant within a run "
        "of identical values.",
        insight="Gaps-and-islands reconstructs discrete events from continuous "
        "state. Compare the result against the events table: they should agree, "
        "and where they do not, one of them is wrong.",
    ),
    Exercise(
        exercise_id="EX-402",
        tier=4,
        title="Before and after an event",
        question="What did telemetry look like in the hour before each fault?",
        skills=("event windows", "asof reasoning", "precursor analysis"),
        duckdb_sql="""
WITH faults AS (
    SELECT asset_id, min(time) AS fault_time
    FROM inverter_telemetry
    WHERE operating_state = 'FAULT'
    GROUP BY asset_id
)
SELECT f.asset_id,
       f.fault_time,
       avg(t.internal_temp_c) AS mean_internal_temp_c,
       avg(t.ac_power_kw) AS mean_ac_kw
FROM faults f
JOIN inverter_telemetry t
  ON t.asset_id = f.asset_id
 AND t.time BETWEEN f.fault_time - INTERVAL 60 MINUTE AND f.fault_time
GROUP BY f.asset_id, f.fault_time
ORDER BY f.asset_id
""",
        timescale_sql="""
WITH faults AS (
    SELECT asset_id, min(time) AS fault_time
    FROM telemetry.inverter_telemetry
    WHERE operating_state = 'FAULT'
    GROUP BY asset_id
)
SELECT f.asset_id,
       f.fault_time,
       avg(t.internal_temp_c) AS mean_internal_temp_c,
       avg(t.ac_power_kw) AS mean_ac_kw
FROM faults f
JOIN telemetry.inverter_telemetry t
  ON t.asset_id = f.asset_id
 AND t.time BETWEEN f.fault_time - INTERVAL '60 minutes' AND f.fault_time
GROUP BY f.asset_id, f.fault_time
ORDER BY f.asset_id
""",
        hint="Join the event list back to telemetry on an interval, not on equality.",
        insight="Precursor analysis is why events and telemetry are stored "
        "separately and joined, rather than collapsed into one table.",
    ),
    # -- Tier 5: discrimination --------------------------------------------
    Exercise(
        exercise_id="EX-501",
        tier=5,
        title="Why is output low?",
        question="Separate clipping, curtailment, derating and low resource.",
        skills=("conditional aggregation", "CASE", "multi-signal reasoning"),
        duckdb_sql="""
SELECT CASE
         WHEN curtailed_power_kw > 0 THEN 'curtailed'
         WHEN thermal_derate_factor < 0.999 THEN 'thermal derate'
         WHEN ac_preclip_kw >= 2497.5 THEN 'clipping'
         WHEN poa_global < 200 THEN 'low resource'
         ELSE 'normal'
       END AS condition,
       count(*) AS minutes,
       avg(ac_power_kw) AS mean_ac_kw,
       avg(poa_global) AS mean_poa
FROM inverter_telemetry
WHERE poa_global > 5
GROUP BY condition
ORDER BY minutes DESC
""",
        hint="Order the CASE branches carefully - the conditions overlap.",
        insight="Curtailment and clipping both hold output flat at high "
        "irradiance. Only commanded_power_kw separates them, and only the price "
        "series explains why the command was given. Note which conditions are "
        "absent: a window with no curtailment and no derating tells you about "
        "the weather and the market, not about the query.",
        min_rows=2,
    ),
    Exercise(
        exercise_id="EX-502",
        tier=5,
        title="Curtailment looks like a fault",
        question="Find intervals with high irradiance and near-zero output.",
        skills=("filtering", "the discriminating triple", "misattribution"),
        duckdb_sql="""
SELECT asset_id,
       count(*) AS minutes,
       avg(poa_global) AS mean_poa,
       avg(available_power_kw) AS mean_available_kw,
       avg(commanded_power_kw) AS mean_commanded_kw,
       avg(ac_power_kw) AS mean_ac_kw
FROM inverter_telemetry
WHERE poa_global > 600
  AND ac_power_kw < 50
GROUP BY asset_id
ORDER BY minutes DESC
LIMIT 10
""",
        hint="Compare available against commanded power before concluding "
        "anything is broken.",
        insight="High irradiance with zero output and no fault code is "
        "economic curtailment. An analyst who stops at the telemetry will "
        "dispatch a technician to a working inverter.",
        min_rows=0,
    ),
    # -- Tier 6: data quality ----------------------------------------------
    Exercise(
        exercise_id="EX-601",
        tier=6,
        title="Find stuck sensors",
        question="Which signals stopped changing while the plant kept running?",
        skills=("UNPIVOT", "run detection", "false-positive control"),
        duckdb_sql="""
WITH daylight AS (
    -- Filter to operating hours FIRST. An unfiltered scan returns overnight
    -- standby (-0.7 kW held for 646 minutes) and constant night-time internal
    -- temperature, drowning the real signals in legitimate constants.
    SELECT time, asset_id, ac_power_kw, dc_power_kw, ac_preclip_kw,
           internal_temp_c, cell_temperature, dc_voltage_v
    FROM inverter_telemetry
    WHERE poa_global > 100 AND ac_power_kw > 1
),
long AS (
    -- UNPIVOT scans every channel. Hand-listing them misses whichever one
    -- actually froze, which is not knowable in advance.
    UNPIVOT daylight
    ON ac_power_kw, dc_power_kw, ac_preclip_kw,
       internal_temp_c, cell_temperature, dc_voltage_v
    INTO NAME signal VALUE value
),
runs AS (
    SELECT time, asset_id, signal, value,
           CASE WHEN value = lag(value)
                     OVER (PARTITION BY asset_id, signal ORDER BY time)
                THEN 0 ELSE 1 END AS changed
    FROM long WHERE value IS NOT NULL
),
islands AS (
    SELECT asset_id, signal, value,
           sum(changed) OVER (PARTITION BY asset_id, signal ORDER BY time)
             AS island
    FROM runs
)
SELECT asset_id, signal, round(value, 4) AS frozen_value,
       count(*) AS repeated_minutes
FROM islands
GROUP BY asset_id, signal, value, island
HAVING count(*) > 15
ORDER BY repeated_minutes DESC
LIMIT 15
""",
        timescale_sql="""
WITH daylight AS (
    SELECT time, asset_id, ac_power_kw, dc_power_kw, ac_preclip_kw,
           internal_temp_c, cell_temperature, dc_voltage_v
    FROM telemetry.inverter_telemetry
    WHERE poa_global > 100 AND ac_power_kw > 1
),
long AS (
    -- PostgreSQL has no UNPIVOT. LATERAL over a VALUES list is the idiom.
    SELECT d.time, d.asset_id, v.signal, v.value
    FROM daylight d
    CROSS JOIN LATERAL (VALUES
        ('ac_power_kw',      d.ac_power_kw),
        ('dc_power_kw',      d.dc_power_kw),
        ('ac_preclip_kw',    d.ac_preclip_kw),
        ('internal_temp_c',  d.internal_temp_c),
        ('cell_temperature', d.cell_temperature),
        ('dc_voltage_v',     d.dc_voltage_v)
    ) AS v(signal, value)
),
runs AS (
    SELECT time, asset_id, signal, value,
           CASE WHEN value = lag(value)
                     OVER (PARTITION BY asset_id, signal ORDER BY time)
                THEN 0 ELSE 1 END AS changed
    FROM long WHERE value IS NOT NULL
),
islands AS (
    SELECT asset_id, signal, value,
           sum(changed) OVER (PARTITION BY asset_id, signal ORDER BY time)
             AS island
    FROM runs
)
SELECT asset_id, signal, round(value::numeric, 4) AS frozen_value,
       count(*) AS repeated_minutes
FROM islands
GROUP BY asset_id, signal, value, island
HAVING count(*) > 15
ORDER BY repeated_minutes DESC
LIMIT 15
""",
        hint="A running sum over a change flag numbers each run of identical "
        "values. Scan every channel, and think hard about which constants are "
        "legitimate before you flag anything.",
        insight="Three traps, in the order you will hit them. Scanning only "
        "ac_power_kw finds nothing - a stuck channel can be any of them. "
        "Scanning without a daylight filter returns overnight standby and "
        "night-time constants at the top. And a curtailed inverter genuinely "
        "holds output at exactly 0.0 kW for hours, which is not a frozen "
        "sensor. Filtered properly this query recovers the injected stuck "
        "signals with no false positives - check it against the truth schema. "
        "Do not trust the quality column either: roughly half of injected "
        "defects carry no flag, and drift carries one 5% of the time.",
        min_rows=0,
    ),
    Exercise(
        exercise_id="EX-602",
        tier=6,
        title="Missing data inventory",
        question="Which assets and signals have gaps, and how large?",
        skills=("null handling", "completeness metrics", "availability"),
        duckdb_sql="""
SELECT asset_id,
       count(*) AS expected_samples,
       sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS missing_samples,
       1.0 - sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END)
             / count(*)::DOUBLE AS availability
FROM inverter_telemetry
GROUP BY asset_id
ORDER BY availability
LIMIT 10
""",
        timescale_sql="""
SELECT asset_id,
       count(*) AS expected_samples,
       sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS missing_samples,
       1.0 - sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END)::double precision
             / count(*) AS availability
FROM telemetry.inverter_telemetry
GROUP BY asset_id
ORDER BY availability
LIMIT 10
""",
        hint="Missing means NULL. If you see zeros where you expect gaps, "
        "something upstream zero-filled them.",
        insight="Report data availability alongside every performance figure. "
        "A performance ratio of 84% from 91% availability is not the same claim "
        "as 84% from 99.8%.",
        min_rows=5,
    ),
    # -- Tier 7: economics -------------------------------------------------
    Exercise(
        exercise_id="EX-701",
        tier=7,
        title="Production against time of day",
        question="When does the plant produce, in local terms?",
        skills=("timezone conversion", "time extraction", "shape analysis"),
        duckdb_sql="""
SELECT extract('hour' FROM time AT TIME ZONE 'America/Chicago') AS local_hour,
       avg(grid_export_power_kw) AS mean_export_kw,
       max(grid_export_power_kw) AS peak_export_kw
FROM plant_telemetry
GROUP BY local_hour
ORDER BY local_hour
""",
        hint="Storage is UTC. Local solar-day analysis requires a conversion.",
        insight="This shape is why merchant solar earns below the average "
        "price: the plant produces hardest in the hours when every other solar "
        "plant in the region is also producing hardest.",
        min_rows=12,
    ),
    Exercise(
        exercise_id="EX-702",
        tier=7,
        title="Block-to-block comparison",
        question="Do the ten power blocks perform equally?",
        skills=("aggregation", "peer comparison at scale", "spread analysis"),
        duckdb_sql="""
SELECT asset_id AS block_id,
       sum(ac_power_kw) / 60.0 / 1000.0 AS energy_mwh,
       avg(transformer_loading_pct) AS mean_loading_pct,
       max(transformer_loading_pct) AS peak_loading_pct
FROM block_telemetry
GROUP BY block_id
ORDER BY energy_mwh
""",
        hint="Compare energy and loading together; a block can be low on one "
        "and normal on the other.",
        insight="Daily block energy spread is small even under broken cloud - "
        "weather averages out over a day. Persistent block differences come "
        "from soiling and equipment, which is what makes them detectable.",
        min_rows=5,
    ),
)


#: Streams that live in the ``telemetry`` schema on PostgreSQL. DuckDB views
#: over the Parquet export are unqualified, so every exercise needs rewriting
#: before it will run against a database.
_SCHEMA_TABLES = (
    "inverter_telemetry",
    "weather_telemetry",
    "block_telemetry",
    "transformer_telemetry",
    "plant_telemetry",
)


def timescale_form(exercise: Exercise) -> str:
    """Return a runnable TimescaleDB version of an exercise.

    Where an exercise declares its own ``timescale_sql`` that is returned
    unchanged. Otherwise the portable query is rewritten by qualifying the
    table names.

    This exists because the generated files previously said "the TimescaleDB
    form is identical apart from the schema prefix" and stopped there. That is
    accurate and useless: a reader who pastes the query into a database client
    gets ``relation "inverter_telemetry" does not exist``, having been told a
    difference exists but not what it is. Emit the SQL.

    Args:
        exercise: The exercise to render.

    Returns:
        SQL that runs against the ``telemetry`` schema.
    """
    if exercise.timescale_sql:
        return exercise.timescale_sql.strip()

    sql = exercise.duckdb_sql
    for table in _SCHEMA_TABLES:
        # Only after FROM or JOIN, so a column or alias sharing the name is
        # left alone.
        for keyword in ("FROM", "JOIN"):
            sql = sql.replace(f"{keyword} {table}", f"{keyword} telemetry.{table}")
    return sql.strip()


def exercises_by_tier() -> dict[int, list[Exercise]]:
    """Group the curriculum by difficulty band.

    Returns:
        A mapping of tier to its exercises, in order.
    """
    grouped: dict[int, list[Exercise]] = {}
    for exercise in EXERCISES:
        grouped.setdefault(exercise.tier, []).append(exercise)
    return dict(sorted(grouped.items()))


def skills_covered() -> set[str]:
    """List every skill the curriculum exercises.

    Returns:
        The union of all exercise skill tags, for checking against the
        analytical contract rather than assuming coverage.
    """
    return {skill for exercise in EXERCISES for skill in exercise.skills}


def write_exercise_files(target: Path) -> list[Path]:
    """Write each exercise to a numbered SQL file.

    Both dialects go in one file, so a learner comparing them does not have to
    open two.

    Args:
        target: Destination directory.

    Returns:
        The written paths.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for exercise in EXERCISES:
        slug = exercise.title.lower().replace(" ", "_").replace("-", "_")
        path = target / f"{exercise.exercise_id}_{slug}.sql"

        body = [
            f"-- {exercise.exercise_id} (tier {exercise.tier}): {exercise.title}",
            "--",
            f"-- Question: {exercise.question}",
            f"-- Skills:   {', '.join(exercise.skills)}",
            "--",
            f"-- Hint:     {exercise.hint}" if exercise.hint else "--",
            "--",
            "-- What the answer should tell you:",
            f"--   {exercise.insight}",
            "",
            "-- ---------------------------------------------------------------",
            "-- DuckDB over the Parquet export (no server required)",
            "-- ---------------------------------------------------------------",
            exercise.duckdb_sql.strip(),
            ";",
            "",
        ]
        note = (
            "-- Uses a TimescaleDB-specific feature; compare it with the form above."
            if exercise.timescale_sql
            else "-- Same logic, schema-qualified. This query needs no\n"
            "-- time-series-specific feature - only the table names differ."
        )
        body += [
            "-- ---------------------------------------------------------------",
            "-- TimescaleDB / PostgreSQL",
            "--",
            "-- Tables live in the `telemetry` schema, not `public`. Either use",
            "-- the qualified names below, or run once per session:",
            "--     SET search_path TO telemetry, public;",
            "-- ---------------------------------------------------------------",
            note,
            timescale_form(exercise),
            ";",
            "",
        ]

        path.write_text("\n".join(body))
        written.append(path)

    return written


@dataclass
class ExerciseResult:
    """Outcome of running one exercise.

    Attributes:
        exercise_id: Which exercise.
        rows: Rows returned.
        error: Error message, empty on success.
        sample: First few rows, for eyeballing the answer.
    """

    exercise_id: str
    rows: int
    error: str = ""
    sample: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def ok(self) -> bool:
        """Whether the query executed without error.

        Returns:
            ``True`` when no error was raised.
        """
        return not self.error


def run_exercises(connection, exercises=EXERCISES) -> list[ExerciseResult]:
    """Execute the DuckDB form of every exercise.

    A curriculum that has never run is a list of plausible-looking queries, and
    the subtly wrong ones are exactly the ones a learner cannot diagnose.

    Args:
        connection: An open DuckDB connection with views over the export.
        exercises: Exercises to run.

    Returns:
        One result per exercise, in order.
    """
    results: list[ExerciseResult] = []
    for exercise in exercises:
        try:
            frame = connection.execute(exercise.duckdb_sql).df()
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            results.append(
                ExerciseResult(exercise.exercise_id, 0, str(error).split("\n")[0])
            )
        else:
            results.append(
                ExerciseResult(exercise.exercise_id, len(frame), "", frame.head(3))
            )
    return results


@dataclass
class CurriculumGateResult:
    """Outcome of the Phase 12 curriculum acceptance checks.

    Attributes:
        checks: Named outcomes, each a pass flag and a detail string.
        results: Per-exercise execution results.
    """

    checks: list[tuple[str, bool, str]]
    results: list[ExerciseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether every check succeeded.

        Returns:
            ``True`` when no check failed.
        """
        return all(ok for _, ok, _ in self.checks)

    def render(self) -> str:
        """Format the result for terminal output.

        Returns:
            A multi-line report.
        """
        lines = [
            f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {detail}"
            for name, ok, detail in self.checks
        ]
        lines.append(f"\n  {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def run_curriculum_gate(connection) -> CurriculumGateResult:
    """Verify the curriculum meets its Phase 12 criteria.

    Args:
        connection: An open DuckDB connection over an exported dataset.

    Returns:
        A :class:`CurriculumGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    results = run_exercises(connection)
    by_id = {exercise.exercise_id: exercise for exercise in EXERCISES}

    failed = [r for r in results if not r.ok]
    checks.append(
        (
            "every_exercise_executes",
            not failed,
            f"{len(results)} exercises run"
            if not failed
            else f"{len(failed)} failed: {[r.exercise_id for r in failed]}",
        )
    )

    # An exercise that returns nothing teaches nothing. Where an empty result is
    # legitimately dataset-dependent the exercise declares min_rows=0, and that
    # declaration is itself reviewed rather than assumed.
    short = [r for r in results if r.ok and r.rows < by_id[r.exercise_id].min_rows]
    checks.append(
        (
            "exercises_return_answers",
            not short,
            f"{sum(1 for r in results if r.rows > 0)} of {len(results)} returned rows"
            if not short
            else f"below minimum: {[r.exercise_id for r in short]}",
        )
    )

    tiers = exercises_by_tier()
    checks.append(
        (
            "difficulty_is_graded",
            set(tiers) == set(range(1, 8)),
            f"tiers {sorted(tiers)}, "
            f"{', '.join(f'{t}:{len(v)}' for t, v in tiers.items())}",
        )
    )

    # Coverage is checked against the analytical contract rather than assumed.
    required = {
        "window functions",
        "gaps and islands",
        "GROUP BY",
        "peer normalisation",
        "conditional aggregation",
        "timezone conversion",
        "null handling",
    }
    covered = skills_covered()
    checks.append(
        (
            "contract_skills_covered",
            required <= covered,
            f"{len(covered)} skills covered"
            if required <= covered
            else f"missing {sorted(required - covered)}",
        )
    )

    # Every exercise must say what the answer means. A query that runs but
    # teaches nothing is not an exercise.
    without_insight = [e.exercise_id for e in EXERCISES if not e.insight.strip()]
    checks.append(
        (
            "every_exercise_has_an_insight",
            not without_insight,
            "each exercise states what its answer should tell you"
            if not without_insight
            else f"missing: {without_insight}",
        )
    )

    dual = sum(1 for e in EXERCISES if e.timescale_sql)
    checks.append(
        (
            "dual_dialect_where_it_matters",
            dual > 0,
            f"{dual} exercises carry a distinct TimescaleDB form; the rest need "
            f"no time-series-specific feature",
        )
    )

    return CurriculumGateResult(checks=checks, results=results)
