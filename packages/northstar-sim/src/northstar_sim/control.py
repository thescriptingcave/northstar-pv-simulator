"""Inverter state resolution and plant-level control.

Two things happen here, and keeping them separate matters analytically.

**State resolution** decides what each inverter is *doing*: asleep, starting,
running. It is driven by resource availability and dwell timers, and produces
the sunrise and sunset sequences that make a startup visible in telemetry
rather than instantaneous.

**Control** decides what each inverter is *allowed to do*: the plant controller
distributes an export limit as per-inverter setpoints. This is what creates the
distinction between available power and commanded power, and without that
distinction curtailment cannot be told apart from equipment failure.

Reference: design documents ``04_physical_architecture`` section 8 and
``08_operating_state_model``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .plant_config import PlantConfig
from .states import InverterState

#: Minutes an inverter spends in STARTING before it may reach RUNNING. Real
#: units perform grid synchronisation and insulation checks; the delay is what
#: makes the sunrise transition observable at 1-minute cadence.
STARTUP_DWELL_MINUTES = 3

#: Fraction of available power an inverter delivers while STARTING. Output ramps
#: rather than stepping, which is both realistic and a distinguishable signature.
STARTING_OUTPUT_FRACTION = 0.35

#: Plane-of-array irradiance below which a RUNNING inverter returns to STANDBY,
#: as a fraction of the startup threshold. The gap prevents an inverter
#: oscillating between states at dawn and dusk.
SHUTDOWN_HYSTERESIS = 0.6

#: Relative tolerance below which a commanded setpoint is treated as no
#: constraint at all, avoiding a CURTAILED label from floating-point noise.
CURTAILMENT_TOLERANCE = 1e-6


@dataclass
class InverterOperation:
    """Resolved operating state and output for one inverter.

    Attributes:
        state: Operating state at each timestep.
        reason: Why the state is what it is, carried into event records.
        ac_power_kw: Delivered AC power after state and setpoint are applied.
        available_power_kw: What the inverter could have produced. Simulator
            truth, and the reference against which curtailment is measured.
        commanded_power_kw: The controller's setpoint.
        curtailed_power_kw: Available less delivered, where a setpoint bound.
    """

    state: pd.Series
    reason: pd.Series
    ac_power_kw: pd.Series
    available_power_kw: pd.Series
    commanded_power_kw: pd.Series
    curtailed_power_kw: pd.Series


def resolve_inverter_states(
    config: PlantConfig,
    poa_global: pd.Series,
    *,
    startup_dwell_minutes: int = STARTUP_DWELL_MINUTES,
) -> tuple[pd.Series, pd.Series]:
    """Determine an inverter's state sequence from resource availability.

    The sequence is ``STANDBY -> STARTING -> RUNNING`` on the way up and
    ``RUNNING -> STANDBY`` on the way down, with hysteresis so an inverter does
    not chatter at the threshold.

    Args:
        config: Plant configuration.
        poa_global: Plane-of-array irradiance at this inverter.
        startup_dwell_minutes: Samples to hold in ``STARTING``.

    Returns:
        A tuple of state and reason series.
    """
    threshold = config.inverter.startup_poa_wm2
    shutdown_threshold = threshold * SHUTDOWN_HYSTERESIS

    irradiance = poa_global.to_numpy()
    count = len(irradiance)

    states = np.empty(count, dtype=object)
    reasons = np.empty(count, dtype=object)

    current = InverterState.STANDBY
    dwell = 0

    for index in range(count):
        value = irradiance[index]

        if current in (InverterState.STANDBY, InverterState.OFF):
            if value >= threshold:
                current, dwell = InverterState.STARTING, 0
                reason = f"POA {value:.0f} W/m2 above startup threshold"
            else:
                reason = "insufficient irradiance"
        elif current is InverterState.STARTING:
            dwell += 1
            if value < shutdown_threshold:
                current = InverterState.STANDBY
                reason = "irradiance fell during startup"
            elif dwell >= startup_dwell_minutes:
                current = InverterState.RUNNING
                reason = "startup sequence complete"
            else:
                reason = f"startup dwell {dwell}/{startup_dwell_minutes}"
        else:  # RUNNING and any downstream state resolved by the controller
            if value < shutdown_threshold:
                current = InverterState.STANDBY
                reason = "POA below shutdown threshold"
            else:
                current = InverterState.RUNNING
                reason = "normal operation"

        states[index] = current.value
        reasons[index] = reason

    return (
        pd.Series(states, index=poa_global.index, dtype="object"),
        pd.Series(reasons, index=poa_global.index, dtype="object"),
    )


def apply_state_to_output(
    config: PlantConfig,
    state: pd.Series,
    available_power_kw: pd.Series,
) -> pd.Series:
    """Convert available power into delivered power given the operating state.

    Args:
        config: Plant configuration.
        state: Operating state at each timestep.
        available_power_kw: Unconstrained AC power from the physics chain.

    Returns:
        Delivered AC power. Negative in ``STANDBY`` and ``OFF``, reflecting the
        inverter's own overnight parasitic draw.
    """
    standby = -config.inverter.night_standby_kw
    delivered = pd.Series(standby, index=available_power_kw.index, dtype=float)

    starting = state == InverterState.STARTING.value
    delivered[starting] = (available_power_kw[starting] * STARTING_OUTPUT_FRACTION).clip(
        lower=0.0
    )

    running = state.isin(
        [
            InverterState.RUNNING.value,
            InverterState.DERATED.value,
            InverterState.CURTAILED.value,
        ]
    )
    delivered[running] = available_power_kw[running].clip(lower=0.0)
    return delivered


class PlantController:
    """Enforces the point-of-interconnection export limit.

    The controller is what makes *commanded* power distinct from *available*
    power. An analyst who cannot see both has no way to distinguish a curtailed
    plant from a broken one, which is the discrimination design document ``07``
    section 9 exists to enable.

    Curtailment is distributed pro rata across inverters. Real controllers use
    various strategies; pro rata is the common default and keeps the per-asset
    signature uniform, so curtailment is not mistaken for one inverter
    underperforming.

    Args:
        config: Plant configuration.
    """

    def __init__(self, config: PlantConfig) -> None:
        """Store the configuration."""
        self.config = config

    def export_limit_kw(self, index: pd.DatetimeIndex) -> pd.Series:
        """Return the active export limit over a time index.

        Args:
            index: Time index to produce a limit for.

        Returns:
            The point-of-interconnection limit at each timestep.
        """
        return pd.Series(self.config.grid.poi_export_limit_kw, index=index, dtype=float)

    def dispatch(
        self,
        available_by_inverter: dict[str, pd.Series],
        *,
        delivery_efficiency: pd.Series,
        economic_curtailment: pd.Series | None = None,
    ) -> dict[str, pd.Series]:
        """Compute per-inverter setpoints that respect the export limit.

        The limit applies at the meter, but the setpoints apply at the
        inverters, and losses sit between them. The available inverter power is
        therefore converted to its expected export contribution before the
        limit is applied, then the required reduction is shared pro rata.

        Args:
            available_by_inverter: Unconstrained AC power per inverter.
            delivery_efficiency: Fraction of inverter AC power that reaches the
                meter, accounting for transformer and collection losses.
            economic_curtailment: Intervals where price plus the production tax
                credit is negative, so generating destroys value. The resulting
                output reduction is sharp, coincident with high irradiance, and
                accompanied by no fault code at all - which is precisely why it
                is mistaken for an equipment failure by anyone not joining the
                price series.

        Returns:
            A commanded setpoint series per inverter.
        """
        keys = sorted(available_by_inverter)
        total_available = sum(available_by_inverter[key] for key in keys)
        expected_export = total_available * delivery_efficiency
        limit = self.export_limit_kw(total_available.index)

        # A ratio of 1.0 means no constraint; below 1.0 the plant must back off.
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(
                expected_export.to_numpy() > 0.0,
                np.minimum(1.0, limit.to_numpy() / expected_export.to_numpy()),
                1.0,
            )
        ratio_series = pd.Series(ratio, index=total_available.index)

        # Economic curtailment overrides the export limit entirely: when price
        # plus the production tax credit is negative, the correct setpoint is
        # zero regardless of how much headroom the interconnection has.
        if economic_curtailment is not None:
            mask = economic_curtailment.reindex(total_available.index).fillna(False)
            ratio_series = ratio_series.where(~mask.astype(bool), 0.0)

        return {key: available_by_inverter[key] * ratio_series for key in keys}


def apply_setpoint(
    state: pd.Series,
    reason: pd.Series,
    available_power_kw: pd.Series,
    commanded_power_kw: pd.Series,
    delivered_power_kw: pd.Series,
) -> InverterOperation:
    """Bind a controller setpoint and relabel constrained timesteps.

    An inverter held below what it could produce is ``CURTAILED``, not
    ``RUNNING`` at lower output. Without the relabel, curtailment is
    indistinguishable from underperformance in the state column.

    Args:
        state: Operating state before the setpoint is considered.
        reason: Corresponding reason series.
        available_power_kw: Unconstrained AC power.
        commanded_power_kw: Controller setpoint.
        delivered_power_kw: Output after state resolution.

    Returns:
        The resolved :class:`InverterOperation`.
    """
    bound = (commanded_power_kw < available_power_kw * (1.0 - CURTAILMENT_TOLERANCE)) & (
        available_power_kw > 0
    )

    final = delivered_power_kw.where(
        ~bound, np.minimum(delivered_power_kw, commanded_power_kw)
    )

    updated_state = state.copy()
    updated_reason = reason.copy()
    relabel = bound & state.eq(InverterState.RUNNING.value)
    updated_state[relabel] = InverterState.CURTAILED.value
    updated_reason[relabel] = "plant controller setpoint below available power"

    curtailed = (available_power_kw - final).where(bound, 0.0).clip(lower=0.0)

    return InverterOperation(
        state=updated_state,
        reason=updated_reason,
        ac_power_kw=final,
        available_power_kw=available_power_kw,
        commanded_power_kw=commanded_power_kw,
        curtailed_power_kw=curtailed,
    )


def resolve_plant_state(
    plant_frame: pd.DataFrame,
    inverter_states: pd.DataFrame,
    *,
    total_inverters: int,
) -> pd.Series:
    """Derive the plant-level state from its inverters and export.

    Args:
        plant_frame: Plant-level totals.
        inverter_states: One column per inverter, holding its state.
        total_inverters: Expected inverter count.

    Returns:
        The plant state at each timestep.
    """
    from .states import PlantState

    running = inverter_states.isin(
        [
            InverterState.RUNNING.value,
            InverterState.DERATED.value,
            InverterState.CURTAILED.value,
        ]
    ).sum(axis=1)
    starting = inverter_states.eq(InverterState.STARTING.value).sum(axis=1)
    curtailed = inverter_states.eq(InverterState.CURTAILED.value).sum(axis=1)

    state = pd.Series(PlantState.NIGHT.value, index=plant_frame.index, dtype="object")
    state[starting > 0] = PlantState.DAWN_STARTUP.value
    state[running > 0] = PlantState.NORMAL.value
    state[(running > 0) & (running < total_inverters)] = PlantState.PARTIAL_OUTAGE.value
    state[curtailed > 0] = PlantState.CURTAILED.value
    return state


@dataclass
class StateGateResult:
    """Outcome of the Phase 5 state and control acceptance checks.

    Attributes:
        checks: Named outcomes, each a pass flag and a detail string.
    """

    checks: list[tuple[str, bool, str]]

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


def run_state_gate(config: PlantConfig, result) -> StateGateResult:
    """Verify state machines and control meet their Phase 5 criteria.

    Args:
        config: Plant configuration.
        result: A ``PlantRunResult`` from a full-plant run.

    Returns:
        A :class:`StateGateResult`.
    """
    from .states import INVERTER_TRANSITIONS, StateTransition, validate_transitions

    checks: list[tuple[str, bool, str]] = []
    events = result.events

    records = [
        StateTransition(
            time=row.time,
            asset_id=row.asset_id,
            from_state=row.from_state,
            to_state=row.to_state,
            reason=row.reason,
        )
        for row in events.itertuples()
    ]
    illegal = validate_transitions(records, INVERTER_TRANSITIONS)
    checks.append(
        (
            "only_legal_transitions",
            not illegal,
            f"{len(records)} transitions, {len(illegal)} illegal",
        )
    )

    observed = {(r.from_state, r.to_state) for r in records}
    startup_path = {
        (InverterState.STANDBY.value, InverterState.STARTING.value),
        (InverterState.STARTING.value, InverterState.RUNNING.value),
    }
    checks.append(
        (
            "startup_sequence_observed",
            startup_path <= observed,
            "STANDBY -> STARTING -> RUNNING present"
            if startup_path <= observed
            else f"missing {sorted(startup_path - observed)}",
        )
    )

    # State and telemetry must agree. A sleeping inverter reporting generation,
    # or a running one reporting the standby draw, is not a hard error anywhere
    # in the physics, so nothing else would catch it.
    states = result.state_matrix()
    power = result.ac_matrix()

    asleep = states.isin([InverterState.STANDBY.value, InverterState.OFF.value])
    asleep_power = power.where(asleep).stack()
    checks.append(
        (
            "standby_consumes_only",
            bool((asleep_power <= 0).all()),
            f"max standby output {asleep_power.max():.3f} kW "
            f"(expected {-config.inverter.night_standby_kw:.2f})",
        )
    )

    running = states.eq(InverterState.RUNNING.value)
    running_power = power.where(running).stack()
    checks.append(
        (
            "running_generates",
            bool((running_power > 0).all()),
            f"min running output {running_power.min():.2f} kW",
        )
    )

    # Available and commanded power must both exist, or curtailment cannot be
    # told apart from equipment failure.
    sample = next(iter(result.inverters.values()))
    has_signals = {
        "available_power_kw",
        "commanded_power_kw",
        "curtailed_power_kw",
    } <= set(sample.columns)
    checks.append(
        (
            "discriminating_signals",
            has_signals,
            "available, commanded and curtailed power present",
        )
    )

    # Backtracking signature: at low sun the tracker rotates back toward
    # horizontal to avoid row-to-row shading, so the extreme angle occurs
    # mid-morning rather than at sunrise.
    daylight = sample[sample["poa_global"] > 5.0]
    angle = daylight["tracker_angle_deg"].dropna()
    if len(angle) > 10:
        sunrise_angle = abs(angle.iloc[0])
        peak_angle = angle.abs().max()
        backtracking = sunrise_angle < 0.5 * peak_angle
    else:
        sunrise_angle, peak_angle, backtracking = 0.0, 0.0, False
    checks.append(
        (
            "backtracking_visible",
            backtracking,
            f"sunrise |angle| {sunrise_angle:.1f} deg against peak {peak_angle:.1f} deg",
        )
    )

    plant_states = result.plant["plant_state"]
    checks.append(
        (
            "plant_state_follows_fleet",
            {"NIGHT", "NORMAL"} <= set(plant_states.unique()),
            f"observed {sorted(plant_states.unique())}",
        )
    )

    return StateGateResult(checks=checks)
