"""Tests for the Phase 8 fault and scenario engine.

The governing requirement is that faults create **identifiable time-series
signatures**, not merely event records. A fault that only exists in an events
table teaches an analyst to read the events table.

Ordering is the other load-bearing property. Faults are applied to physical
truth, before the sensor layer, which is what separates an equipment fault
(changes what the plant produced) from a data fault (changes only what was
reported).
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.plant_run import energy_mwh, run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402
from northstar_sim.scenarios import (  # noqa: E402
    TRANSIENT_THRESHOLD_MINUTES,
    ScenarioInstance,
    ScenarioKind,
    apply_inverter_scenarios,
    apply_tracker_scenario,
    reliability_metrics,
    run_scenario_gate,
    schedule_scenarios,
)
from northstar_sim.states import InverterState  # noqa: E402

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
    """Provide a fault-free run.

    Args:
        config: Plant configuration.
        base: Resource frame.

    Returns:
        The run result.
    """
    return run_plant(config, build_plant(config), base, seed=999)


@pytest.fixture(scope="module")
def faulted(config, base):
    """Provide the same run with scenarios injected.

    Args:
        config: Plant configuration.
        base: Resource frame.

    Returns:
        The run result.
    """
    return run_plant(config, build_plant(config), base, seed=999, inject_faults=True)


def _instance(kind, asset_id, index, start_min, duration, severity=1.0):
    """Build a scenario instance over a time index.

    Args:
        kind: Scenario kind.
        asset_id: Affected asset.
        index: Simulation index.
        start_min: Offset in minutes from the start.
        duration: Duration in minutes.
        severity: Fractional output reduction.

    Returns:
        The instance.
    """
    start = index[0] + pd.Timedelta(minutes=start_min)
    return ScenarioInstance(
        scenario_id=kind.value,
        asset_id=asset_id,
        start=start,
        end=start + pd.Timedelta(minutes=duration),
        severity=severity,
        cause_code="LOSS_INV_OUTAGE",
        trigger="test",
    )


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


def test_schedule_is_deterministic_for_a_seed(base) -> None:
    """A dataset must be regenerable, faults included."""
    args = (["INV1", "INV2"], ["BLK1"], ["TRK1"], base.index)
    first = schedule_scenarios(*args, seed=5)
    second = schedule_scenarios(*args, seed=5)
    assert first.to_frame().equals(second.to_frame())


def test_faults_are_scheduled_during_operating_hours(base) -> None:
    """A fault at night leaves no signature to find.

    Scheduling uniformly across 24 hours put most trips in darkness: five
    injected faults over a week cost 0.01% of export and moved no telemetry.
    """
    daylight = base["solar_zenith"] < 85.0
    schedule = schedule_scenarios(
        ["INV1", "INV2"], ["BLK1"], ["TRK1"], base.index, seed=5, daylight=daylight
    )
    assert schedule.instances
    for instance in schedule.instances:
        assert bool(daylight.loc[instance.start])


def test_durations_are_long_tailed(base) -> None:
    """A fixed duration makes outage length uninformative and MTTR meaningless."""
    daylight = base["solar_zenith"] < 85.0
    schedule = schedule_scenarios(
        [f"INV{i}" for i in range(40)],
        ["BLK1"],
        ["TRK1"],
        base.index,
        seed=11,
        daylight=daylight,
    )
    durations = [i.duration_minutes for i in schedule.instances]
    assert max(durations) / min(durations) > 3.0


def test_transients_are_distinguished_from_failures(base) -> None:
    """A four-minute auto-restart is not a failure; counting it ruins MTBF."""
    brief = _instance(ScenarioKind.INVERTER_TRIP, "INV1", base.index, 60, 5)
    lasting = _instance(ScenarioKind.INVERTER_TRIP, "INV1", base.index, 600, 120)

    assert brief.is_transient
    assert not lasting.is_transient
    assert brief.duration_minutes < TRANSIENT_THRESHOLD_MINUTES


def test_reliability_excludes_transients(base) -> None:
    """MTBF counts failures, not every state change."""
    from northstar_sim.scenarios import ScenarioSchedule

    schedule = ScenarioSchedule(
        instances=[
            _instance(ScenarioKind.INVERTER_TRIP, "INV1", base.index, 60, 5),
            _instance(ScenarioKind.INVERTER_TRIP, "INV1", base.index, 600, 120),
        ]
    )
    metrics = reliability_metrics(schedule, daylight_hours=100.0)

    assert metrics["failures"] == 1
    assert metrics["transients"] == 1
    assert metrics["mttr_hours"] == pytest.approx(2.0)


# --------------------------------------------------------------------------
# Fault application
# --------------------------------------------------------------------------


def test_a_trip_zeroes_output_and_sets_the_state(config, clean, base) -> None:
    """State and telemetry must agree during a fault as much as outside one."""
    asset_id = next(iter(clean.inverters))
    frame = clean.inverters[asset_id]
    instance = _instance(ScenarioKind.INVERTER_TRIP, asset_id, frame.index, 700, 60)

    faulted, lost = apply_inverter_scenarios(frame, [instance])
    active = (faulted.index >= instance.start) & (faulted.index < instance.end)

    assert (faulted.loc[active, "ac_power_kw"] == 0.0).all()
    assert (faulted.loc[active, "operating_state"] == InverterState.FAULT.value).all()
    assert lost.sum() > 0


def test_a_partial_dc_loss_leaves_the_inverter_running(config, clean) -> None:
    """A string outage drops output by a step, not to zero.

    That distinction is the signature separating a string fault from a trip,
    and separating both from soiling, which arrives gradually.
    """
    asset_id = next(iter(clean.inverters))
    frame = clean.inverters[asset_id]
    instance = _instance(
        ScenarioKind.STRING_OUTAGE, asset_id, frame.index, 700, 120, severity=1 / 12
    )

    faulted, lost = apply_inverter_scenarios(frame, [instance])
    active = (faulted.index >= instance.start) & (faulted.index < instance.end)
    generating = active & (frame["ac_power_kw"] > 100)

    assert (faulted.loc[generating, "ac_power_kw"] > 0).all()
    assert (
        faulted.loc[generating, "ac_power_kw"] < frame.loc[generating, "ac_power_kw"]
    ).all()
    assert lost.sum() > 0


def test_a_stuck_tracker_freezes_its_angle(config, clean) -> None:
    """The frozen angle is what produces the U-shaped deviation from peers."""
    asset_id = next(iter(clean.inverters))
    frame = clean.inverters[asset_id]
    instance = _instance(ScenarioKind.STUCK_TRACKER, "TRK1", frame.index, 700, 240)

    faulted, lost = apply_tracker_scenario(frame, instance)
    active = (faulted.index >= instance.start) & (faulted.index < instance.end)

    assert faulted.loc[active, "tracker_angle_deg"].nunique() == 1
    assert lost.sum() > 0


def test_applying_no_scenarios_is_a_no_op(clean) -> None:
    """A fault-free asset must be untouched, not merely unchanged in aggregate."""
    asset_id = next(iter(clean.inverters))
    frame = clean.inverters[asset_id]
    result, lost = apply_inverter_scenarios(frame, [])
    assert result is frame
    assert lost.sum() == 0


# --------------------------------------------------------------------------
# Integrated behaviour
# --------------------------------------------------------------------------


def test_faults_reduce_plant_export(clean, faulted) -> None:
    """A fault that does not move telemetry is not a fault."""
    clean_energy = energy_mwh(clean.plant["grid_export_power_kw"])
    faulted_energy = energy_mwh(faulted.plant["grid_export_power_kw"])
    assert faulted_energy < clean_energy


def test_faults_are_localised_to_their_assets(faulted) -> None:
    """A trip that degrades peers would destroy peer comparison."""
    affected = {key for key, series in faulted.fault_loss_kw.items() if series.sum() > 0}
    assert 0 < len(affected) < len(faulted.inverters)


def test_a_clean_run_injects_nothing(clean) -> None:
    """Fault injection is opt-in, so earlier gates keep testing a clean plant."""
    assert clean.schedule is None
    assert len(clean.fault_events) == 0


def test_every_scenario_produces_onset_and_clearance_events(faulted) -> None:
    """Events are discrete; telemetry is continuous. Both are required."""
    assert len(faulted.fault_events) == 2 * len(faulted.schedule.instances)
    assert set(faulted.fault_events["event_type"]) == {"FAULT", "RECOVERY"}


def test_schedule_is_ground_truth_for_blind_scoring(faulted) -> None:
    """An analyst's reconstruction must be scoreable against what was injected."""
    frame = faulted.schedule.to_frame()
    assert {"scenario_id", "asset_id", "start", "end", "cause_code"} <= set(frame.columns)


def test_scenario_gate_passes(clean, faulted, base) -> None:
    """The Phase 8 acceptance gate."""
    gate = run_scenario_gate(clean, faulted, daylight=base["solar_zenith"] < 85.0)
    assert gate.passed, gate.render()
