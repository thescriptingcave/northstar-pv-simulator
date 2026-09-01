"""Operating states and the transitions between them.

A state model earns its place by constraining behaviour, not by labelling it.
Two properties matter and are enforced here rather than assumed:

* **Only legal transitions occur.** An inverter cannot go from ``OFF`` straight
  to ``RUNNING``; it passes through ``STARTING``. Without enforcement the state
  column becomes decorative and an analyst who filters on it gets the wrong
  answer for reasons they cannot see.
* **State and telemetry agree.** A ``FAULT`` inverter reporting full output is
  not a hard error anywhere in the physics, so nothing else would catch it.

Every transition carries a timestamp and a reason. A state change with no
reason cannot be joined to an event, and design document ``08`` section 5
requires that join.

Reference: design document ``08_operating_state_model``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd


class InverterState(StrEnum):
    """Inverter operating states.

    ``DERATED`` and ``FAULT`` are defined now and driven in later phases, so the
    legal-transition map does not have to be reopened when faults arrive.
    """

    OFF = "OFF"
    STANDBY = "STANDBY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DERATED = "DERATED"
    CURTAILED = "CURTAILED"
    FAULT = "FAULT"
    MAINTENANCE = "MAINTENANCE"
    SHUTDOWN = "SHUTDOWN"


class TransformerState(StrEnum):
    """Block transformer states."""

    ENERGIZED = "ENERGIZED"
    DERATED = "DERATED"
    HIGH_TEMPERATURE = "HIGH_TEMPERATURE"
    TRIPPED = "TRIPPED"
    MAINTENANCE = "MAINTENANCE"
    OFFLINE = "OFFLINE"


class TrackerState(StrEnum):
    """Tracker row-block states."""

    TRACKING = "TRACKING"
    BACKTRACKING = "BACKTRACKING"
    STOWED_WIND = "STOWED_WIND"
    STOWED_NIGHT = "STOWED_NIGHT"
    STOWED_MAINT = "STOWED_MAINT"
    STUCK = "STUCK"
    FAULT = "FAULT"


class PlantState(StrEnum):
    """Plant-level states."""

    NIGHT = "NIGHT"
    DAWN_STARTUP = "DAWN_STARTUP"
    NORMAL = "NORMAL"
    DERATED = "DERATED"
    CURTAILED = "CURTAILED"
    PARTIAL_OUTAGE = "PARTIAL_OUTAGE"
    GRID_DISCONNECTED = "GRID_DISCONNECTED"
    MAINTENANCE = "MAINTENANCE"
    EMERGENCY_SHUTDOWN = "EMERGENCY_SHUTDOWN"


#: Legal inverter transitions. The startup path is deliberately narrow: an
#: inverter reaches ``RUNNING`` only through ``STARTING``, which is what makes
#: the sunrise sequence visible in telemetry instead of instantaneous.
INVERTER_TRANSITIONS: dict[InverterState, frozenset[InverterState]] = {
    InverterState.OFF: frozenset({InverterState.STANDBY, InverterState.MAINTENANCE}),
    InverterState.STANDBY: frozenset(
        {
            InverterState.STARTING,
            InverterState.OFF,
            InverterState.FAULT,
            InverterState.MAINTENANCE,
        }
    ),
    # CURTAILED is reachable from STARTING: a curtailment command does not wait
    # for the startup dwell to finish, and an inverter told to stop while
    # starting stops.
    #
    # Omitted originally because synthetic prices rarely go negative in the
    # minutes after sunrise, so the path was never exercised. Real ERCOT prices
    # produced 82 of these in a single year - logged as ERROR on every run
    # while the dataset still passed acceptance.
    InverterState.STARTING: frozenset(
        {
            InverterState.RUNNING,
            InverterState.FAULT,
            InverterState.STANDBY,
            InverterState.CURTAILED,
        }
    ),
    InverterState.RUNNING: frozenset(
        {
            InverterState.DERATED,
            InverterState.CURTAILED,
            InverterState.FAULT,
            InverterState.SHUTDOWN,
            InverterState.STANDBY,
        }
    ),
    InverterState.DERATED: frozenset(
        {
            InverterState.RUNNING,
            InverterState.CURTAILED,
            InverterState.FAULT,
            InverterState.SHUTDOWN,
            InverterState.STANDBY,
        }
    ),
    InverterState.CURTAILED: frozenset(
        {
            InverterState.RUNNING,
            InverterState.DERATED,
            InverterState.FAULT,
            InverterState.SHUTDOWN,
            InverterState.STANDBY,
        }
    ),
    InverterState.FAULT: frozenset(
        {InverterState.OFF, InverterState.MAINTENANCE, InverterState.STANDBY}
    ),
    InverterState.MAINTENANCE: frozenset({InverterState.OFF, InverterState.STANDBY}),
    InverterState.SHUTDOWN: frozenset({InverterState.OFF, InverterState.STANDBY}),
}

#: Legal plant-level transitions.
PLANT_TRANSITIONS: dict[PlantState, frozenset[PlantState]] = {
    PlantState.NIGHT: frozenset(
        {PlantState.DAWN_STARTUP, PlantState.MAINTENANCE, PlantState.GRID_DISCONNECTED}
    ),
    PlantState.DAWN_STARTUP: frozenset(
        {PlantState.NORMAL, PlantState.PARTIAL_OUTAGE, PlantState.NIGHT}
    ),
    PlantState.NORMAL: frozenset(
        {
            PlantState.DERATED,
            PlantState.CURTAILED,
            PlantState.PARTIAL_OUTAGE,
            PlantState.GRID_DISCONNECTED,
            PlantState.EMERGENCY_SHUTDOWN,
            PlantState.NIGHT,
        }
    ),
    PlantState.DERATED: frozenset(
        {
            PlantState.NORMAL,
            PlantState.CURTAILED,
            PlantState.PARTIAL_OUTAGE,
            PlantState.NIGHT,
        }
    ),
    PlantState.CURTAILED: frozenset(
        {
            PlantState.NORMAL,
            PlantState.DERATED,
            PlantState.PARTIAL_OUTAGE,
            PlantState.NIGHT,
        }
    ),
    PlantState.PARTIAL_OUTAGE: frozenset(
        {PlantState.NORMAL, PlantState.DERATED, PlantState.CURTAILED, PlantState.NIGHT}
    ),
    PlantState.GRID_DISCONNECTED: frozenset({PlantState.NIGHT, PlantState.DAWN_STARTUP}),
    PlantState.MAINTENANCE: frozenset({PlantState.NIGHT, PlantState.DAWN_STARTUP}),
    PlantState.EMERGENCY_SHUTDOWN: frozenset({PlantState.NIGHT, PlantState.MAINTENANCE}),
}


class IllegalTransitionError(RuntimeError):
    """Raised when a state change violates the transition map."""


@dataclass(frozen=True)
class StateTransition:
    """A recorded change of state.

    Attributes:
        time: When the change occurred.
        asset_id: Asset that changed.
        from_state: Previous state.
        to_state: New state.
        reason: Why it changed. Required, because a transition without a reason
            cannot be joined to an event record.
    """

    time: pd.Timestamp
    asset_id: str
    from_state: str
    to_state: str
    reason: str


def is_legal(from_state: str, to_state: str, transitions: dict) -> bool:
    """Test whether a transition is permitted.

    Args:
        from_state: Current state.
        to_state: Proposed state.
        transitions: Transition map for the asset class.

    Returns:
        ``True`` when the transition is permitted, or when the state is
        unchanged.
    """
    if from_state == to_state:
        return True
    allowed = transitions.get(from_state)
    return allowed is not None and to_state in allowed


def extract_transitions(
    states: pd.Series,
    asset_id: str,
    reasons: pd.Series | None = None,
) -> list[StateTransition]:
    """Convert a state time series into discrete transition records.

    Telemetry describes continuous state; events describe discrete occurrences.
    Design document ``12`` section 1 requires they not be collapsed, so the
    series is reduced to the moments it changes.

    Args:
        states: State at each timestep.
        asset_id: Asset the series belongs to.
        reasons: Optional per-timestep reason, sampled at the change.

    Returns:
        One record per change, in time order.
    """
    changed = states.ne(states.shift())
    changed.iloc[0] = False  # the first sample is an initial condition

    records: list[StateTransition] = []
    for timestamp in states.index[changed]:
        previous = states.shift().loc[timestamp]
        records.append(
            StateTransition(
                time=timestamp,
                asset_id=asset_id,
                from_state=str(previous),
                to_state=str(states.loc[timestamp]),
                reason=(
                    str(reasons.loc[timestamp]) if reasons is not None else "unspecified"
                ),
            )
        )
    return records


def validate_transitions(
    transitions: list[StateTransition], transition_map: dict
) -> list[StateTransition]:
    """Find transitions that violate the legal map.

    Args:
        transitions: Recorded transitions.
        transition_map: Legal transitions for the asset class.

    Returns:
        The illegal transitions, empty when all are permitted.
    """
    return [
        transition
        for transition in transitions
        if not is_legal(transition.from_state, transition.to_state, transition_map)
    ]


def transitions_to_frame(transitions: list[StateTransition]) -> pd.DataFrame:
    """Render transition records as a frame ready for the events table.

    Args:
        transitions: Recorded transitions.

    Returns:
        A frame with one row per transition, empty-but-typed when there are
        none.
    """
    if not transitions:
        return pd.DataFrame(
            columns=["time", "asset_id", "from_state", "to_state", "reason"]
        )
    return pd.DataFrame(
        [
            {
                "time": t.time,
                "asset_id": t.asset_id,
                "from_state": t.from_state,
                "to_state": t.to_state,
                "reason": t.reason,
            }
            for t in transitions
        ]
    )
