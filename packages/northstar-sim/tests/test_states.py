"""Tests for Phase 5 state machines and plant control.

The state column is only useful if it constrains behaviour. Two properties are
enforced rather than assumed, because neither is a hard error anywhere in the
physics and so nothing else would catch a violation:

* only legal transitions occur - an inverter reaches RUNNING through STARTING,
  never directly, which is what makes sunrise observable rather than instant
* state and telemetry agree - a sleeping inverter reporting generation is
  physically consistent and analytically wrong
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.control import (  # noqa: E402
    STARTUP_DWELL_MINUTES,
    PlantController,
    apply_setpoint,
    apply_state_to_output,
    resolve_inverter_states,
    run_state_gate,
)
from northstar_sim.plant_run import run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402
from northstar_sim.states import (  # noqa: E402
    INVERTER_TRANSITIONS,
    InverterState,
    StateTransition,
    extract_transitions,
    is_legal,
    validate_transitions,
)

from .test_physics import real_config  # noqa: E402


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A plant configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def run(config):
    """Provide a full-plant run over a clear day.

    Args:
        config: Plant configuration.

    Returns:
        The run result.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-22 05:00",
        freq="5min",
        temp_air_c=33.0,
        wind_speed_ms=7.0,
    )
    base = downscale_to_minute(source, config, seed=12345)
    base["wind_speed"] = 7.0
    base["wind_direction"] = 250.0
    return run_plant(config, build_plant(config), base, seed=999)


def _irradiance(values: list[float]) -> pd.Series:
    """Build a plane-of-array irradiance series.

    Args:
        values: Irradiance values in W/m2.

    Returns:
        A minute-indexed series.
    """
    index = pd.date_range("2023-06-21", periods=len(values), freq="1min", tz="UTC")
    return pd.Series(values, index=index)


# --------------------------------------------------------------------------
# Transition legality
# --------------------------------------------------------------------------


def test_running_is_only_reachable_through_starting() -> None:
    """A direct jump to RUNNING would make sunrise instantaneous."""
    assert not is_legal(
        InverterState.STANDBY.value, InverterState.RUNNING.value, INVERTER_TRANSITIONS
    )
    assert is_legal(
        InverterState.STANDBY.value, InverterState.STARTING.value, INVERTER_TRANSITIONS
    )
    assert is_legal(
        InverterState.STARTING.value, InverterState.RUNNING.value, INVERTER_TRANSITIONS
    )


def test_an_unchanged_state_is_always_legal() -> None:
    """Holding a state is not a transition."""
    assert is_legal(
        InverterState.FAULT.value, InverterState.FAULT.value, INVERTER_TRANSITIONS
    )


def test_a_faulted_inverter_cannot_resume_directly() -> None:
    """Recovery passes through OFF, STANDBY or MAINTENANCE."""
    assert not is_legal(
        InverterState.FAULT.value, InverterState.RUNNING.value, INVERTER_TRANSITIONS
    )


def test_illegal_transitions_are_detected() -> None:
    """The validator finds violations rather than trusting the producer."""
    bad = StateTransition(
        time=pd.Timestamp("2023-06-21", tz="UTC"),
        asset_id="INV1",
        from_state=InverterState.OFF.value,
        to_state=InverterState.RUNNING.value,
        reason="test",
    )
    assert validate_transitions([bad], INVERTER_TRANSITIONS) == [bad]


def test_transitions_are_extracted_only_at_changes() -> None:
    """Telemetry is continuous; events are discrete. They must not be conflated."""
    index = pd.date_range("2023-06-21", periods=5, freq="1min", tz="UTC")
    states = pd.Series(
        ["STANDBY", "STANDBY", "STARTING", "STARTING", "RUNNING"], index=index
    )
    records = extract_transitions(states, "INV1")

    assert len(records) == 2
    assert records[0].to_state == "STARTING"
    assert records[1].to_state == "RUNNING"


def test_every_transition_carries_a_reason() -> None:
    """A transition without a reason cannot be joined to an event."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    states = pd.Series(["STANDBY", "STARTING", "STARTING"], index=index)
    reasons = pd.Series(["dark", "irradiance rose", "dwell"], index=index)

    records = extract_transitions(states, "INV1", reasons)
    assert records[0].reason == "irradiance rose"


# --------------------------------------------------------------------------
# State resolution
# --------------------------------------------------------------------------


def test_startup_holds_in_starting_for_the_dwell_period(config) -> None:
    """Grid synchronisation takes time, and that makes sunrise observable."""
    poa = _irradiance([0.0] * 3 + [500.0] * 10)
    state, _ = resolve_inverter_states(config, poa)

    starting = (state == InverterState.STARTING.value).sum()
    assert starting == STARTUP_DWELL_MINUTES
    assert state.iloc[-1] == InverterState.RUNNING.value


def test_hysteresis_prevents_chatter_at_the_threshold(config) -> None:
    """Without hysteresis an inverter oscillates at dawn and dusk."""
    threshold = config.inverter.startup_poa_wm2
    # Hovering just below the startup threshold but above shutdown.
    poa = _irradiance([500.0] * 6 + [threshold * 0.8] * 6)
    state, _ = resolve_inverter_states(config, poa)

    assert state.iloc[-1] == InverterState.RUNNING.value, "should not drop out yet"


def test_falling_irradiance_returns_the_inverter_to_standby(config) -> None:
    """Sunset must be reached, not merely approached."""
    poa = _irradiance([500.0] * 6 + [0.0] * 6)
    state, _ = resolve_inverter_states(config, poa)
    assert state.iloc[-1] == InverterState.STANDBY.value


def test_startup_aborts_if_irradiance_collapses(config) -> None:
    """A cloud during startup returns the inverter to standby, not to running."""
    poa = _irradiance([0.0, 500.0, 0.0, 0.0, 0.0])
    state, _ = resolve_inverter_states(config, poa)
    assert state.iloc[-1] == InverterState.STANDBY.value


def test_standby_output_is_the_parasitic_draw(config) -> None:
    """A sleeping inverter consumes; it does not sit at exactly zero."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    state = pd.Series([InverterState.STANDBY.value] * 3, index=index)
    delivered = apply_state_to_output(
        config, state, pd.Series([0.0, 0.0, 0.0], index=index)
    )
    assert (delivered == -config.inverter.night_standby_kw).all()


def test_starting_output_ramps_rather_than_stepping(config) -> None:
    """A step to full output at startup is neither real nor distinguishable."""
    index = pd.date_range("2023-06-21", periods=2, freq="1min", tz="UTC")
    state = pd.Series([InverterState.STARTING.value] * 2, index=index)
    available = pd.Series([1000.0, 1000.0], index=index)

    delivered = apply_state_to_output(config, state, available)
    assert (delivered > 0).all()
    assert (delivered < available).all()


# --------------------------------------------------------------------------
# Plant controller
# --------------------------------------------------------------------------


def test_controller_does_not_constrain_below_the_export_limit(config) -> None:
    """No limit means no setpoint reduction, and therefore no curtailment."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    available = {"A": pd.Series(1000.0, index=index), "B": pd.Series(1000.0, index=index)}

    commanded = PlantController(config).dispatch(
        available, delivery_efficiency=pd.Series(1.0, index=index)
    )
    assert (commanded["A"] == available["A"]).all()


def test_controller_shares_curtailment_pro_rata(config) -> None:
    """Uniform reduction keeps curtailment from looking like one bad inverter."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    limit = config.grid.poi_export_limit_kw
    available = {
        "A": pd.Series(limit, index=index),
        "B": pd.Series(limit, index=index),
    }

    commanded = PlantController(config).dispatch(
        available, delivery_efficiency=pd.Series(1.0, index=index)
    )
    total = commanded["A"] + commanded["B"]

    assert total.max() <= limit * (1.0 + 1e-9)
    assert commanded["A"].equals(commanded["B"]), "reduction must be uniform"


def test_a_constrained_inverter_is_relabelled_curtailed(config) -> None:
    """Held below available power is CURTAILED, not RUNNING at lower output.

    Without the relabel, curtailment is indistinguishable from
    underperformance in the state column - the exact discrimination the
    design exists to enable.
    """
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    state = pd.Series([InverterState.RUNNING.value] * 3, index=index)
    reason = pd.Series(["normal operation"] * 3, index=index)
    available = pd.Series(2000.0, index=index)
    commanded = pd.Series(1500.0, index=index)

    operation = apply_setpoint(state, reason, available, commanded, available)

    assert (operation.state == InverterState.CURTAILED.value).all()
    assert (operation.ac_power_kw == 1500.0).all()
    assert (operation.curtailed_power_kw == 500.0).all()


def test_curtailment_is_not_triggered_by_floating_point_noise(config) -> None:
    """A setpoint equal to available power is not a constraint."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    state = pd.Series([InverterState.RUNNING.value] * 3, index=index)
    reason = pd.Series(["normal operation"] * 3, index=index)
    available = pd.Series(2000.0, index=index)

    operation = apply_setpoint(state, reason, available, available, available)
    assert (operation.state == InverterState.RUNNING.value).all()
    assert (operation.curtailed_power_kw == 0.0).all()


# --------------------------------------------------------------------------
# Integrated behaviour
# --------------------------------------------------------------------------


def test_full_run_produces_no_illegal_transitions(run) -> None:
    """The whole plant obeys the transition map over a full day."""
    records = [
        StateTransition(
            time=row.time,
            asset_id=row.asset_id,
            from_state=row.from_state,
            to_state=row.to_state,
            reason=row.reason,
        )
        for row in run.events.itertuples()
    ]
    assert records, "a full day must produce transitions"
    assert validate_transitions(records, INVERTER_TRANSITIONS) == []


def test_sunrise_sequence_appears_in_telemetry(run) -> None:
    """The startup path is observable, which is the point of the dwell."""
    observed = set(zip(run.events["from_state"], run.events["to_state"], strict=True))
    assert (InverterState.STANDBY.value, InverterState.STARTING.value) in observed
    assert (InverterState.STARTING.value, InverterState.RUNNING.value) in observed


def test_sleeping_inverters_never_report_generation(run) -> None:
    """State and telemetry must agree; the physics alone would not object."""
    states = run.state_matrix()
    power = run.ac_matrix()
    asleep = states.isin([InverterState.STANDBY.value, InverterState.OFF.value])
    assert (power.where(asleep).stack() <= 0).all()


def test_running_inverters_always_generate(run) -> None:
    """The converse: a RUNNING inverter producing nothing is inconsistent."""
    states = run.state_matrix()
    power = run.ac_matrix()
    running = states.eq(InverterState.RUNNING.value)
    assert (power.where(running).stack() > 0).all()


def test_available_and_commanded_power_are_both_recorded(run) -> None:
    """Curtailment cannot be told from failure without both signals."""
    frame = next(iter(run.inverters.values()))
    for column in ("available_power_kw", "commanded_power_kw", "curtailed_power_kw"):
        assert column in frame.columns


def test_backtracking_is_visible_at_low_sun(run) -> None:
    """At sunrise a backtracking tracker sits near horizontal, not at its limit.

    An unbacktracked tracker would be at maximum rotation at first light. The
    signature of backtracking is that the extreme angle occurs mid-morning.
    """
    frame = next(iter(run.inverters.values()))
    daylight = frame[frame["poa_global"] > 5.0]
    angle = daylight["tracker_angle_deg"].dropna()

    assert abs(angle.iloc[0]) < 0.5 * angle.abs().max()


def test_tracker_respects_its_rotation_limit(config, run) -> None:
    """Rotation never exceeds the configured maximum."""
    frame = next(iter(run.inverters.values()))
    assert frame["tracker_angle_deg"].abs().max() <= config.tracker.max_angle_deg + 1e-6


def test_plant_state_reflects_the_fleet(run) -> None:
    """Plant state is derived from its inverters, not asserted independently."""
    observed = set(run.plant["plant_state"].unique())
    assert {"NIGHT", "NORMAL"} <= observed


def test_state_gate_passes(config, run) -> None:
    """The Phase 5 acceptance gate."""
    gate = run_state_gate(config, run)
    assert gate.passed, gate.render()
