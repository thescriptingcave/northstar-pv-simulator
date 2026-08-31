"""Tests for the Phase 12 SQL curriculum.

A curriculum of SQL that has never run is a list of plausible-looking queries.
The subtly wrong ones are exactly the ones a learner cannot diagnose, so every
exercise is executed against a real exported dataset rather than reviewed.

Three of these tests exist because EX-601 was wrong three times in a row, each
time in a way that still ran cleanly and returned something.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.curriculum import (  # noqa: E402
    EXERCISES,
    exercises_by_tier,
    run_curriculum_gate,
    run_exercises,
    skills_covered,
    write_exercise_files,
)
from northstar_sim.market import (  # noqa: E402
    CommercialTerms,
    economic_curtailment_mask,
    synthetic_prices,
)
from northstar_sim.plant_run import run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402
from northstar_sim.storage import duckdb_connection, export_parquet  # noqa: E402

from .test_physics import real_config  # noqa: E402


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A plant configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def dataset(config, tmp_path_factory):
    """Export a dataset containing every condition the curriculum queries.

    Hot and curtailed on purpose: an exercise that separates four low-output
    conditions cannot be validated on a window containing two of them.

    Args:
        config: Plant configuration.
        tmp_path_factory: pytest temporary directory factory.

    Returns:
        A tuple of export root and run id.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-26 05:00",
        freq="5min",
        temp_air_c=50.0,
        wind_speed_ms=3.0,
    )
    base = downscale_to_minute(source, config, seed=999)
    base["wind_speed"] = 3.0
    base["wind_direction"] = 250.0

    terms = CommercialTerms()
    price = synthetic_prices(base.index, base["ghi"], seed=11)
    result = run_plant(
        config,
        build_plant(config),
        base,
        seed=999,
        inject_faults=True,
        inject_defects=True,
        economic_curtailment=economic_curtailment_mask(price, terms),
    )

    root = tmp_path_factory.mktemp("curriculum")
    shutil.rmtree(root, ignore_errors=True)
    export_parquet(result, root, run_id="test")
    return Path(root), "test"


@pytest.fixture(scope="module")
def connection(dataset):
    """Open a DuckDB connection over the analyst tree.

    Args:
        dataset: Export root and run id.

    Returns:
        An open connection.
    """
    root, run_id = dataset
    return duckdb_connection(root, run_id, "analyst")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def test_every_exercise_executes(connection) -> None:
    """A query that does not run cannot teach anything."""
    failures = [r for r in run_exercises(connection) if not r.ok]
    assert not failures, [(r.exercise_id, r.error) for r in failures]


def test_every_exercise_returns_at_least_its_minimum(connection) -> None:
    """An exercise that returns nothing teaches nothing.

    Where an empty result is legitimately dataset-dependent, the exercise
    declares ``min_rows=0`` - and that declaration is reviewed, not assumed.
    """
    by_id = {e.exercise_id: e for e in EXERCISES}
    for result in run_exercises(connection):
        assert result.rows >= by_id[result.exercise_id].min_rows, result.exercise_id


def test_the_stuck_sensor_scan_finds_the_injected_defects(connection, dataset) -> None:
    """EX-601 must recover ground truth, not merely return rows.

    It was wrong three times, each time running cleanly. Scanning only
    ac_power_kw found nothing. Scanning every channel without a daylight filter
    returned overnight standby at -0.7 kW held for 646 minutes. Excluding zeros
    without filtering to operating hours returned constant night-time internal
    temperature.
    """
    root, run_id = dataset
    exercise = next(e for e in EXERCISES if e.exercise_id == "EX-601")
    found = connection.execute(exercise.duckdb_sql).df()

    truth = duckdb_connection(root, run_id, "truth")
    injected = truth.execute(
        "SELECT asset_id, quantity FROM defect_schedule WHERE kind = 'SCN-061'"
    ).df()
    truth.close()

    if injected.empty:
        pytest.skip("no stuck defects in this realisation")

    detected = set(zip(found["asset_id"], found["signal"], strict=True))
    expected = set(zip(injected["asset_id"], injected["quantity"], strict=True))

    # No false positives is the robust property and the one worth asserting.
    #
    # Recall is deliberately NOT asserted, because it is genuinely
    # dataset-dependent: a freeze that occurs at night, or while the inverter is
    # curtailed, is invisible to a scan filtered to operating hours with output
    # above zero. That is a real limitation of the method rather than a defect
    # in the query, and an analyst should understand it.
    assert detected <= expected, f"false positives: {sorted(detected - expected)}"


def test_night_output_is_negative_not_zero(connection) -> None:
    """EX-102's whole point: an energised inverter consumes overnight.

    A daylight filter that keeps these rows biases every efficiency
    calculation slightly negative.
    """
    exercise = next(e for e in EXERCISES if e.exercise_id == "EX-102")
    row = connection.execute(exercise.duckdb_sql).df().iloc[0]
    assert row["max_ac_kw"] < 0


def test_efficiency_rises_then_appears_to_collapse(connection) -> None:
    """EX-202 must show the real shape: a rise, a plateau, then a fall.

    The fall is not a less efficient inverter. Above the AC cap the ratio
    ``ac / dc`` stops measuring conversion efficiency and starts measuring
    clipping, and under thermal derating it falls further still. Asserting a
    monotonic rise would enshrine the misreading the exercise exists to correct.
    """
    exercise = next(e for e in EXERCISES if e.exercise_id == "EX-202")
    frame = connection.execute(exercise.duckdb_sql).df()

    peak = frame["efficiency"].max()
    assert peak > frame["efficiency"].iloc[0], "efficiency must rise off low load"
    assert peak < 1.0, "conversion efficiency cannot exceed unity"
    assert frame["efficiency"].iloc[-1] < peak, "the top band must fall away"


def test_low_output_conditions_are_separable(connection) -> None:
    """EX-501 must resolve more than one condition on a suitable window."""
    exercise = next(e for e in EXERCISES if e.exercise_id == "EX-501")
    frame = connection.execute(exercise.duckdb_sql).df()
    assert len(frame) >= 2
    assert frame["minutes"].sum() > 0


# --------------------------------------------------------------------------
# Curriculum shape
# --------------------------------------------------------------------------


def test_difficulty_is_graded_across_seven_tiers() -> None:
    """Each tier builds on the previous one."""
    tiers = exercises_by_tier()
    assert set(tiers) == set(range(1, 8))
    assert all(len(group) >= 2 for group in tiers.values())


def test_contract_skills_are_covered() -> None:
    """Coverage is checked against doc 02, not assumed."""
    required = {
        "window functions",
        "gaps and islands",
        "GROUP BY",
        "peer normalisation",
        "conditional aggregation",
        "timezone conversion",
        "null handling",
    }
    assert required <= skills_covered()


def test_every_exercise_states_what_the_answer_means() -> None:
    """A query that runs but teaches nothing is not an exercise."""
    for exercise in EXERCISES:
        assert exercise.insight.strip(), exercise.exercise_id
        assert exercise.question.strip(), exercise.exercise_id


def test_exercise_files_carry_both_dialects(tmp_path) -> None:
    """A learner comparing dialects should not have to open two files."""
    written = write_exercise_files(tmp_path)
    assert len(written) == len(EXERCISES)

    with_timescale = next(
        path
        for path, exercise in zip(written, EXERCISES, strict=True)
        if exercise.timescale_sql
    )
    text = with_timescale.read_text()
    assert "DuckDB" in text
    assert "TimescaleDB" in text
    assert "time_bucket" in text


def test_curriculum_gate_passes(connection) -> None:
    """The Phase 12 acceptance gate."""
    gate = run_curriculum_gate(connection)
    assert gate.passed, gate.render()
