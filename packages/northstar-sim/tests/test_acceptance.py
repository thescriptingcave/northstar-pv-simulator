"""Tests for the dataset acceptance report.

Design doc 15 section 13 states the principle the report enforces: a dataset is
accepted because it satisfies documented checks, not because it looks realistic
on a graph.

The most important test here is that the report **rejects** bad data. A verdict
generator that always passes is worse than none: it converts an unchecked
dataset into an apparently checked one.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

from northstar_sim.acceptance import AcceptanceReport, build_report  # noqa: E402
from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.market import (  # noqa: E402
    CommercialTerms,
    economic_curtailment_mask,
    synthetic_prices,
)
from northstar_sim.plant_run import run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402
from northstar_sim.storage import export_parquet  # noqa: E402

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
    """Export a dataset with faults, defects and curtailment.

    Args:
        config: Plant configuration.
        tmp_path_factory: pytest temporary directory factory.

    Returns:
        A tuple of export root, run id and price series.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-24 05:00",
        freq="5min",
        temp_air_c=42.0,
        wind_speed_ms=3.0,
        temp_amplitude_c=9.0,
        seed=999,
    )
    base = downscale_to_minute(source, config, seed=999)
    base["wind_speed"] = 3.0
    base["wind_direction"] = 250.0

    prices = synthetic_prices(base.index, base["ghi"], seed=11)
    result = run_plant(
        config,
        build_plant(config),
        base,
        seed=999,
        inject_faults=True,
        inject_defects=True,
        economic_curtailment=economic_curtailment_mask(prices, CommercialTerms()),
    )

    root = tmp_path_factory.mktemp("accept")
    shutil.rmtree(root, ignore_errors=True)
    export_parquet(result, root, run_id="test")
    return Path(root), "test", prices


@pytest.fixture(scope="module")
def report(dataset, config):
    """Generate the acceptance report.

    Args:
        dataset: Export root, run id and prices.
        config: Plant configuration.

    Returns:
        The report.
    """
    root, run_id, prices = dataset
    return build_report(root, run_id, config=config, prices=prices)


# --------------------------------------------------------------------------
# The report accepts good data
# --------------------------------------------------------------------------


def test_a_valid_dataset_is_accepted(report) -> None:
    """A clean run must pass every check."""
    assert report.accepted, report.render()


def test_every_specified_section_is_present(report) -> None:
    """Doc 15 section 12 enumerates the sections; all must appear."""
    sections = {finding.section for finding in report.findings}
    required = {
        "provenance",
        "volume",
        "completeness",
        "distributions",
        "states",
        "scenarios",
        "events",
        "physics",
        "statistics",
        "energy",
        "financial",
        "kpis",
        "truth separation",
    }
    assert required <= sections, sorted(required - sections)


def test_the_report_carries_real_checks(report) -> None:
    """A report of purely informational lines decides nothing."""
    assert len(report.checks) >= 15


# --------------------------------------------------------------------------
# The report rejects bad data
# --------------------------------------------------------------------------


def test_the_report_rejects_when_a_check_fails() -> None:
    """A verdict generator that always passes is worse than none.

    It converts an unchecked dataset into an apparently checked one.
    """
    report = AcceptanceReport(run_id="x", generated_utc="now")
    report.add("physics", "fine", "ok", passed=True)
    report.add("physics", "broken", "12 violations", passed=False, detail="why")

    assert not report.accepted
    assert len(report.failures) == 1


def test_failures_are_itemised_not_summarised() -> None:
    """A bare count tells a reader nothing they can act on."""
    report = AcceptanceReport(run_id="x", generated_utc="now")
    report.add("physics", "broken", "12 violations", passed=False, detail="cause")

    rendered = report.render()
    assert "REJECTED" in rendered
    assert "physics/broken" in rendered
    assert "cause" in rendered


def test_informational_lines_do_not_vote(report) -> None:
    """Provenance is context, not a verdict."""
    provenance = [f for f in report.findings if f.section == "provenance"]
    assert provenance
    assert all(f.passed is None for f in provenance)


# --------------------------------------------------------------------------
# Invariants are checked against the tree where they hold
# --------------------------------------------------------------------------


def test_nameplate_is_enforced_on_truth_not_on_measurement(report) -> None:
    """An invariant belongs to one tree.

    Physical truth must respect the AC cap exactly. Measured telemetry need
    not: a power sensor with a calibration gain reading a clipped inverter
    legitimately reports above it. Applying the truth invariant to measured
    data rejected a good dataset over 15,562 "exceedances" that were the sensor
    layer working correctly - the same dataset had zero in truth.
    """
    names = {f.name for f in report.findings}
    assert "ac_within_nameplate_truth" in names
    assert "measured_ac_within_sensor_error" in names

    truth_check = next(
        f for f in report.findings if f.name == "ac_within_nameplate_truth"
    )
    assert truth_check.passed
    assert truth_check.value.startswith("0 ")


def test_measured_output_may_exceed_nameplate(report) -> None:
    """The bound is what the sensor model permits, not the cap."""
    check = next(
        f for f in report.findings if f.name == "measured_ac_within_sensor_error"
    )
    assert check.passed
    assert "+" in check.value, "measured peak is expected above the cap"


def test_no_ordering_is_asserted_between_irradiance_correlations(report) -> None:
    """That ordering was asserted and it was wrong.

    Effective irradiance carries rear-side gain that varies with tracker
    geometry, adding variance that does not map linearly onto DC. Measured in
    truth, POA correlates at 0.9927 and effective at 0.9921 - the assumption
    fails on clean data.
    """
    poa = next(f for f in report.findings if f.name == "poa_to_dc_correlation")
    effective = next(
        f for f in report.findings if f.name == "effective_to_dc_correlation"
    )
    assert poa.passed and effective.passed


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


def test_truth_separation_is_verified(report) -> None:
    """The analyst tree must carry measured data and no truth stream."""
    names = {f.name for f in report.findings}
    assert "no_truth_in_analyst_tree" in names
    assert "analyst_tree_is_measured" in names


def test_quality_flags_are_reported_as_fallible(report) -> None:
    """A complete oracle would make every data-quality exercise a filter."""
    check = next(f for f in report.findings if f.name == "unflagged_share")
    assert check.passed


def test_report_serialises_to_a_table(report) -> None:
    """The report is stored alongside the dataset it describes."""
    frame = report.to_frame()
    assert len(frame) == len(report.findings)
    assert {"section", "name", "value", "passed", "detail"} <= set(frame.columns)


def test_prices_are_optional(dataset, config) -> None:
    """A dataset without a price series is still assessable."""
    root, run_id, _ = dataset
    report = build_report(root, run_id, config=config, prices=None)
    assert report.accepted
    assert any(f.section == "financial" for f in report.findings)
