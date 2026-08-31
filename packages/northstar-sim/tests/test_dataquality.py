"""Tests for Phase 10 data-quality injection.

One property dominates every other consideration:

    A stuck AC-power sensor reports constant output while the actual inverter
    continues changing. An inverter failure changes actual physical output.

That distinction is only meaningful because faults act on truth and defects act
on what was reported. Several tests here check object identity and frame
equality rather than numeric values, because the guarantee is structural.
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.dataquality import (  # noqa: E402
    FLAG_DETECTION_RATE,
    DefectInstance,
    DefectKind,
    DefectSchedule,
    Quality,
    apply_defects,
    inject_duplicates,
    quality_summary,
    run_dataquality_gate,
    schedule_defects,
    undetected_defect_share,
)
from northstar_sim.plant_run import energy_mwh, run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402

from .test_physics import real_config  # noqa: E402


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A plant configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def base(config):
    """Provide a week-long plant-average resource series.

    Args:
        config: Plant configuration.

    Returns:
        A 1-minute resource frame.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-28 05:00",
        freq="5min",
        temp_air_c=38.0,
        wind_speed_ms=5.0,
    )
    frame = downscale_to_minute(source, config, seed=12345)
    frame["wind_speed"] = 5.0
    frame["wind_direction"] = 250.0
    return frame


@pytest.fixture(scope="module")
def clean(config, base):
    """Provide a run with faults but no data-quality defects.

    Args:
        config: Plant configuration.
        base: Resource frame.

    Returns:
        The run result.
    """
    return run_plant(config, build_plant(config), base, seed=999, inject_faults=True)


@pytest.fixture(scope="module")
def corrupted(config, base):
    """Provide the same run with defects injected.

    Args:
        config: Plant configuration.
        base: Resource frame.

    Returns:
        The run result.
    """
    return run_plant(
        config,
        build_plant(config),
        base,
        seed=999,
        inject_faults=True,
        inject_defects=True,
    )


def _frame(values: dict[str, list[float]]) -> pd.DataFrame:
    """Build a minute-indexed frame.

    Args:
        values: Column name to values.

    Returns:
        A UTC-indexed frame.
    """
    length = len(next(iter(values.values())))
    index = pd.date_range("2023-06-21", periods=length, freq="1min", tz="UTC")
    return pd.DataFrame(values, index=index)


def _defect(kind, quantity, frame, start=2, duration=5, magnitude=3.0, flagged=True):
    """Build a defect instance over a frame's index.

    Args:
        kind: Defect kind.
        quantity: Affected column, or ``"*"``.
        frame: Frame supplying the index.
        start: Offset in minutes.
        duration: Duration in minutes.
        magnitude: Defect-specific parameter.
        flagged: Whether the quality column marks it.

    Returns:
        The instance.
    """
    begin = frame.index[0] + pd.Timedelta(minutes=start)
    return DefectInstance(
        kind=kind.value if hasattr(kind, "value") else kind,
        asset_id="A",
        quantity=quantity,
        start=begin,
        end=begin + pd.Timedelta(minutes=duration),
        magnitude=magnitude,
        flagged=flagged,
    )


# --------------------------------------------------------------------------
# The non-negotiable property
# --------------------------------------------------------------------------


def test_physical_truth_is_bit_identical(clean, corrupted) -> None:
    """Defects change reporting, never production.

    A measurement defect that altered truth would make equipment faults and
    data faults indistinguishable, which is the one distinction doc 09
    section 7 exists to preserve.
    """
    assert clean.plant["grid_export_power_kw"].equals(
        corrupted.plant["grid_export_power_kw"]
    )
    for key in clean.inverters:
        assert clean.inverters[key].equals(corrupted.inverters[key])


def test_plant_energy_is_unchanged(clean, corrupted) -> None:
    """The aggregate consequence of the same property."""
    assert energy_mwh(clean.plant["grid_export_power_kw"]) == pytest.approx(
        energy_mwh(corrupted.plant["grid_export_power_kw"])
    )


def test_apply_defects_does_not_mutate_its_input() -> None:
    """Corruption applies to copies; the caller still holds clean frames."""
    frame = _frame({"ghi": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    original = frame.copy()
    schedule = DefectSchedule(instances=[_defect(DefectKind.STUCK, "ghi", frame)])

    apply_defects({"A": frame}, schedule, seed=1)
    pd.testing.assert_frame_equal(frame, original)


def test_stuck_sensor_freezes_reporting_only() -> None:
    """The defining case: measured constant, truth still varying."""
    frame = _frame({"ghi": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    schedule = DefectSchedule(
        instances=[_defect(DefectKind.STUCK, "ghi", frame, start=1, duration=4)]
    )

    result, flags = apply_defects({"A": frame}, schedule, seed=1)
    window = slice(1, 5)

    assert result["A"]["ghi"].iloc[window].nunique() == 1
    assert frame["ghi"].iloc[window].nunique() == 4
    assert (flags["A"]["ghi"].iloc[window] == Quality.STALE.value).all()


# --------------------------------------------------------------------------
# Individual defects
# --------------------------------------------------------------------------


def test_gaps_are_nan_never_zero() -> None:
    """Zero-filled irradiance is indistinguishable from night."""
    frame = _frame({"ghi": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    schedule = DefectSchedule(
        instances=[_defect(DefectKind.GAP, "ghi", frame, start=1, duration=3)]
    )

    result, flags = apply_defects({"A": frame}, schedule, seed=1)
    affected = result["A"]["ghi"].iloc[1:4]

    assert affected.isna().all()
    assert (affected == 0.0).sum() == 0
    assert (flags["A"]["ghi"].iloc[1:4] == Quality.MISSING.value).all()


def test_communications_outage_takes_every_field() -> None:
    """A network fault is not a sensor fault and must not look like one."""
    frame = _frame(
        {"ghi": [100.0] * 6, "temp_air": [25.0] * 6, "ac_power_kw": [500.0] * 6}
    )
    schedule = DefectSchedule(
        instances=[_defect(DefectKind.COMMS_OUTAGE, "*", frame, start=2, duration=2)]
    )

    result, _ = apply_defects({"A": frame}, schedule, seed=1)
    assert result["A"].iloc[2:4].isna().all().all()


def test_drift_is_slow_monotonic_and_in_band() -> None:
    """A drifting sensor reports plausible values throughout."""
    frame = _frame({"ghi": [1000.0] * 20})
    schedule = DefectSchedule(
        instances=[
            _defect(DefectKind.DRIFT, "ghi", frame, start=0, duration=20, magnitude=5.0)
        ]
    )

    result, _ = apply_defects({"A": frame}, schedule, seed=1)
    series = result["A"]["ghi"]

    assert series.iloc[-1] > series.iloc[0]
    assert series.is_monotonic_increasing
    assert series.iloc[-1] < series.iloc[0] * 1.10


def test_spikes_affect_isolated_samples() -> None:
    """A spike is a single implausible reading, not a shifted series."""
    frame = _frame({"ghi": [500.0] * 60})
    schedule = DefectSchedule(
        instances=[
            _defect(DefectKind.SPIKE, "ghi", frame, start=0, duration=60, magnitude=4.0)
        ]
    )

    result, _ = apply_defects({"A": frame}, schedule, seed=1)
    spiked = (result["A"]["ghi"] > 1000.0).sum()

    assert 0 < spiked < 20


def test_duplicates_go_to_a_staging_frame() -> None:
    """Duplicates cannot live in a table whose key forbids them.

    Doc 13 section 11 resolves this by injecting them into a staging frame
    without the constraint. Finding and resolving them before load is the
    exercise, and it is how real ingestion pipelines work.
    """
    frame = _frame({"ghi": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    schedule = DefectSchedule(
        instances=[_defect(DefectKind.DUPLICATE, "ghi", frame, start=1, duration=2)]
    )

    staged = inject_duplicates(frame, schedule, "A")
    assert len(staged) > len(frame)
    assert staged.index.duplicated().sum() == 2


# --------------------------------------------------------------------------
# Quality flags
# --------------------------------------------------------------------------


def test_quality_flags_are_deliberately_fallible() -> None:
    """A flag on every defect would make the column a complete oracle.

    A frozen instrument does not know it has frozen; a drifting one reports
    plausible values throughout.
    """
    assert FLAG_DETECTION_RATE[DefectKind.DRIFT] < 0.2
    assert FLAG_DETECTION_RATE[DefectKind.STUCK] < 0.5
    assert FLAG_DETECTION_RATE[DefectKind.GAP] == 1.0


def test_an_unflagged_defect_still_corrupts_the_data() -> None:
    """The corruption is real whether or not the flag notices."""
    frame = _frame({"ghi": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    schedule = DefectSchedule(
        instances=[
            _defect(DefectKind.STUCK, "ghi", frame, start=1, duration=4, flagged=False)
        ]
    )

    result, flags = apply_defects({"A": frame}, schedule, seed=1)

    assert result["A"]["ghi"].iloc[1:5].nunique() == 1
    assert (flags["A"]["ghi"] == Quality.GOOD.value).all()


def test_some_defects_go_unflagged_in_a_real_run(corrupted) -> None:
    """Otherwise every data-quality exercise collapses into a filter."""
    assert undetected_defect_share(corrupted.defects) > 0.05


def test_corruption_share_meets_the_acceptance_target(corrupted) -> None:
    """Doc 20 section 11: flagged fraction under 1%, availability above 99%."""
    summary = quality_summary(corrupted.quality)
    good = float(summary[summary["quality"] == Quality.GOOD.value]["share"].iloc[0])
    assert 0.0002 < (1.0 - good) < 0.01


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


def test_defect_rate_scales_with_fleet_size(base) -> None:
    """A flat plant-wide rate corrupts almost nothing on a large fleet.

    At 1.2 defects per day spread across the fleet, 0.017% of samples were
    affected - an order of magnitude below a real SCADA system.
    """
    small = schedule_defects([("A", "ghi")], base.index, seed=3)
    large = schedule_defects(
        [(f"ASSET{i}", "ghi") for i in range(40)], base.index, seed=3
    )
    assert len(large.instances) > len(small.instances)


def test_schedule_is_deterministic_for_a_seed(base) -> None:
    """A dataset must be regenerable, defects included."""
    pairs = [("A", "ghi"), ("B", "temp_air")]
    first = schedule_defects(pairs, base.index, seed=4)
    second = schedule_defects(pairs, base.index, seed=4)
    assert first.to_frame().equals(second.to_frame())


def test_schedule_is_ground_truth_for_blind_scoring(corrupted) -> None:
    """An analyst's defect detection must be scoreable."""
    frame = corrupted.defects.to_frame()
    assert {"kind", "asset_id", "quantity", "start", "end", "flagged"} <= set(
        frame.columns
    )


def test_a_clean_run_injects_no_defects(clean) -> None:
    """Injection is opt-in, so earlier gates keep testing clean telemetry."""
    assert clean.defects is None
    assert clean.quality == {}


def test_dataquality_gate_passes(clean, corrupted) -> None:
    """The Phase 10 acceptance gate."""
    gate = run_dataquality_gate(clean, corrupted)
    assert gate.passed, gate.render()
