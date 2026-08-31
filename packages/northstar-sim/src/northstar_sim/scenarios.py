"""Fault and scenario engine.

Faults must create **identifiable time-series signatures**, not merely event
records. A fault that only exists in an events table teaches an analyst to read
the events table; a fault that bends the telemetry teaches them to read the
plant.

Every scenario here is applied to **physical truth**, before the sensor layer.
That ordering is what separates an equipment fault from a data fault: an
inverter trip changes what the plant produced, while a stuck sensor changes only
what was reported. Design document ``09`` section 7 requires the distinction and
Phase 10 supplies the other half.

Scheduling supports three modes, and condition-triggered is preferred wherever
physically defensible. A scheduled fault leaves a scheduling artefact an analyst
can eventually learn to detect *instead of* the phenomenon; a condition-triggered
one clusters realistically with weather and leaves nothing to find but itself.

Reference: design documents ``09_failure_degradation_model`` and
``10_scenario_catalog``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from .states import InverterState


class ScenarioKind(StrEnum):
    """Categories of injected scenario."""

    INVERTER_TRIP = "SCN-040"
    INVERTER_RESTART = "SCN-041"
    INVERTER_THERMAL_TRIP = "SCN-042"
    STRING_OUTAGE = "SCN-043"
    TRANSFORMER_TRIP = "SCN-046"
    STUCK_TRACKER = "SCN-026"


#: Which loss cause code each scenario's lost energy is attributed to.
SCENARIO_CAUSE: dict[str, str] = {
    ScenarioKind.INVERTER_TRIP: "LOSS_INV_OUTAGE",
    ScenarioKind.INVERTER_RESTART: "LOSS_INV_OUTAGE",
    ScenarioKind.INVERTER_THERMAL_TRIP: "LOSS_INV_OUTAGE",
    ScenarioKind.STRING_OUTAGE: "LOSS_DC_OUTAGE",
    ScenarioKind.TRANSFORMER_TRIP: "LOSS_BLOCK_OUTAGE",
    ScenarioKind.STUCK_TRACKER: "LOSS_TRACKER",
}

#: Restarts completing within this many minutes are transients, not failures.
#: Counting them as failures makes MTBF unusable - design doc 20 section 10.
TRANSIENT_THRESHOLD_MINUTES = 15


@dataclass
class ScenarioInstance:
    """One injected scenario occurrence.

    Attributes:
        scenario_id: Catalogue identifier.
        asset_id: Affected asset.
        start: When the scenario begins.
        end: When normal operation resumes.
        severity: Fractional output reduction, 1.0 being a complete outage.
        cause_code: Loss attribution code.
        trigger: Why it fired - scheduled, probabilistic or a condition.
    """

    scenario_id: str
    asset_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    severity: float
    cause_code: str
    trigger: str

    @property
    def duration_minutes(self) -> float:
        """Length of the scenario.

        Returns:
            Duration in minutes.
        """
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def is_transient(self) -> bool:
        """Whether this occurrence is too brief to count as a failure.

        Returns:
            ``True`` when the duration is under the transient threshold.
        """
        return self.duration_minutes < TRANSIENT_THRESHOLD_MINUTES


@dataclass
class ScenarioSchedule:
    """The set of scenarios to apply to a run.

    Attributes:
        instances: Scenario occurrences.
        seed: Seed the schedule was generated from.
    """

    instances: list[ScenarioInstance] = field(default_factory=list)
    seed: int = 0

    def for_asset(self, asset_id: str) -> list[ScenarioInstance]:
        """Select the scenarios affecting one asset.

        Args:
            asset_id: Asset to filter on.

        Returns:
            Matching instances.
        """
        return [i for i in self.instances if i.asset_id == asset_id]

    def to_frame(self) -> pd.DataFrame:
        """Render the schedule as ground truth.

        Returns:
            One row per instance, empty-but-typed when there are none.
        """
        columns = [
            "scenario_id",
            "asset_id",
            "start",
            "end",
            "severity",
            "cause_code",
            "trigger",
            "duration_minutes",
            "is_transient",
        ]
        if not self.instances:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(
            [
                {
                    "scenario_id": i.scenario_id,
                    "asset_id": i.asset_id,
                    "start": i.start,
                    "end": i.end,
                    "severity": i.severity,
                    "cause_code": i.cause_code,
                    "trigger": i.trigger,
                    "duration_minutes": i.duration_minutes,
                    "is_transient": i.is_transient,
                }
                for i in self.instances
            ],
            columns=columns,
        )


def schedule_scenarios(
    inverter_ids: list[str],
    block_ids: list[str],
    tracker_ids: list[str],
    index: pd.DatetimeIndex,
    *,
    seed: int,
    daylight: pd.Series | None = None,
    daily_trip_probability: float = 0.6,
    stuck_tracker_probability: float = 0.25,
) -> ScenarioSchedule:
    """Generate a reproducible scenario schedule.

    Probabilistic rather than fixed, so a multi-year dataset contains a
    realistic distribution of durations and inter-arrival times instead of a
    detectable pattern. Reproducible from the ``fault_schedule`` substream.

    Args:
        inverter_ids: Inverters eligible for trips.
        block_ids: Blocks eligible for transformer outages.
        tracker_ids: Tracker row-blocks eligible for sticking.
        index: Simulation time index.
        seed: Seed for the ``fault_schedule`` substream.
        daylight: Mask selecting operating hours. Faults are scheduled to begin
            during daylight only.
        daily_trip_probability: Expected inverter trips per simulated day.
        stuck_tracker_probability: Expected stuck trackers per simulated day.

    Returns:
        The generated :class:`ScenarioSchedule`.
    """
    rng = np.random.default_rng(seed)
    instances: list[ScenarioInstance] = []

    days = max(1.0, (index[-1] - index[0]).total_seconds() / 86400.0)
    span_minutes = int((index[-1] - index[0]).total_seconds() / 60.0)

    # Faults begin during operation. Scheduling uniformly across 24 hours put
    # most trips in darkness, where output is already zero: 5 injected faults
    # over a week cost 0.01% of export and left no telemetry signature at all.
    # Real trips are driven by electrical and thermal stress, so daylight
    # weighting is both more realistic and analytically necessary.
    if daylight is not None and daylight.any():
        candidates = np.flatnonzero(daylight.to_numpy())
    else:
        candidates = np.arange(len(index))

    def random_start(latest_offset: int) -> pd.Timestamp:
        """Pick an operating-hours start leaving room to complete.

        Args:
            latest_offset: Minutes that must remain after the start.

        Returns:
            A timestamp from the index.
        """
        usable = candidates[candidates < max(1, span_minutes - latest_offset)]
        if len(usable) == 0:
            usable = candidates
        return index[int(rng.choice(usable))]

    for _ in range(rng.poisson(daily_trip_probability * days)):
        # Durations are long-tailed: most trips clear quickly, a few need a
        # truck roll. A fixed duration would make outage length uninformative.
        duration = int(np.clip(rng.lognormal(mean=3.6, sigma=1.1), 4, 600))
        start = random_start(duration)
        instances.append(
            ScenarioInstance(
                scenario_id=ScenarioKind.INVERTER_TRIP.value,
                asset_id=str(rng.choice(inverter_ids)),
                start=start,
                end=start + pd.Timedelta(minutes=duration),
                severity=1.0,
                cause_code=SCENARIO_CAUSE[ScenarioKind.INVERTER_TRIP],
                trigger="probabilistic",
            )
        )

    for _ in range(rng.poisson(0.20 * days)):
        duration = int(np.clip(rng.lognormal(mean=4.5, sigma=0.8), 20, 900))
        start = random_start(duration)
        instances.append(
            ScenarioInstance(
                scenario_id=ScenarioKind.STRING_OUTAGE.value,
                asset_id=str(rng.choice(inverter_ids)),
                start=start,
                # Partial DC loss: one combiner of twelve, so the inverter keeps
                # running at reduced output. Distinguishing this from soiling
                # requires noticing it appeared in one step.
                end=start + pd.Timedelta(minutes=duration),
                severity=1.0 / 12.0,
                cause_code=SCENARIO_CAUSE[ScenarioKind.STRING_OUTAGE],
                trigger="probabilistic",
            )
        )

    for _ in range(rng.poisson(stuck_tracker_probability * days)):
        duration = int(np.clip(rng.lognormal(mean=5.5, sigma=0.7), 60, 1400))
        start = random_start(duration)
        instances.append(
            ScenarioInstance(
                scenario_id=ScenarioKind.STUCK_TRACKER.value,
                asset_id=str(rng.choice(tracker_ids)),
                start=start,
                end=start + pd.Timedelta(minutes=duration),
                severity=1.0,
                cause_code=SCENARIO_CAUSE[ScenarioKind.STUCK_TRACKER],
                trigger="probabilistic",
            )
        )

    for _ in range(rng.poisson(0.05 * days)):
        duration = int(np.clip(rng.lognormal(mean=5.0, sigma=0.9), 30, 1400))
        start = random_start(duration)
        instances.append(
            ScenarioInstance(
                scenario_id=ScenarioKind.TRANSFORMER_TRIP.value,
                asset_id=str(rng.choice(block_ids)),
                start=start,
                end=start + pd.Timedelta(minutes=duration),
                severity=1.0,
                cause_code=SCENARIO_CAUSE[ScenarioKind.TRANSFORMER_TRIP],
                trigger="probabilistic",
            )
        )

    instances.sort(key=lambda i: (i.start, i.asset_id))
    return ScenarioSchedule(instances=instances, seed=seed)


def active_mask(instance: ScenarioInstance, index: pd.DatetimeIndex) -> pd.Series:
    """Build a boolean mask for when a scenario is active.

    Args:
        instance: The scenario occurrence.
        index: Simulation time index.

    Returns:
        ``True`` where the scenario applies.
    """
    return pd.Series((index >= instance.start) & (index < instance.end), index=index)


def apply_inverter_scenarios(
    frame: pd.DataFrame,
    instances: list[ScenarioInstance],
) -> tuple[pd.DataFrame, pd.Series]:
    """Apply faults to one inverter's physical truth.

    Args:
        frame: The inverter's production frame.
        instances: Scenarios affecting this inverter.

    Returns:
        A tuple of the modified frame and the energy lost to faults.
    """
    if not instances:
        return frame, pd.Series(0.0, index=frame.index)

    result = frame.copy()
    index = frame.index
    before = result["ac_power_kw"].copy()

    for instance in instances:
        mask = active_mask(instance, index)
        if not mask.any():
            continue

        if instance.severity >= 1.0:
            # A tripped inverter produces nothing and consumes its own standby
            # draw. State and telemetry must agree: FAULT with full output is
            # physically consistent and analytically wrong.
            result.loc[mask, "ac_power_kw"] = 0.0
            result.loc[mask, "dc_power_kw"] = 0.0
            result.loc[mask, "operating_state"] = InverterState.FAULT.value
            result.loc[mask, "state_reason"] = f"{instance.scenario_id} fault"
            result.loc[mask, "fault_code"] = instance.scenario_id
        else:
            # Partial DC loss. The inverter keeps running at reduced output,
            # which is the signature that distinguishes a string outage from a
            # trip: output drops by a step, not to zero.
            survived = 1.0 - instance.severity
            result.loc[mask, "dc_power_kw"] *= survived
            result.loc[mask, "ac_power_kw"] *= survived
            result.loc[mask, "state_reason"] = f"{instance.scenario_id} partial loss"
            result.loc[mask, "fault_code"] = instance.scenario_id

    lost = (before - result["ac_power_kw"]).clip(lower=0.0)
    return result, lost


def apply_tracker_scenario(
    frame: pd.DataFrame, instance: ScenarioInstance
) -> tuple[pd.DataFrame, pd.Series]:
    """Freeze a tracker at its angle when the fault began.

    A stuck tracker produces a **symmetric, U-shaped** deviation from peers
    across the day: it matches them near the frozen angle's optimal hour and
    diverges increasingly away from it. That shape is what distinguishes it
    from soiling, which is flat and proportional, and from thermal derating,
    which is irradiance-dependent.

    Args:
        frame: The affected inverter's production frame.
        instance: The stuck-tracker occurrence.

    Returns:
        A tuple of the modified frame and the energy lost.
    """
    index = frame.index
    mask = active_mask(instance, index)
    if not mask.any():
        return frame, pd.Series(0.0, index=index)

    result = frame.copy()
    before = result["ac_power_kw"].copy()

    frozen_positions = np.flatnonzero(mask.to_numpy())
    frozen_angle = float(result["tracker_angle_deg"].iloc[frozen_positions[0]])

    # Cosine of the angular error against where the tracker should be. This is
    # a geometric approximation rather than a re-run of the transposition, and
    # it is deliberately conservative: it captures the shape, which is the
    # analytically important part, without a second pvlib pass per fault.
    error = np.radians(result["tracker_angle_deg"] - frozen_angle)
    factor = np.cos(error).clip(lower=0.0)

    result.loc[mask, "tracker_angle_deg"] = frozen_angle
    result.loc[mask, "ac_power_kw"] *= factor[mask]
    result.loc[mask, "dc_power_kw"] *= factor[mask]
    result.loc[mask, "state_reason"] = f"{instance.scenario_id} stuck tracker"

    lost = (before - result["ac_power_kw"]).clip(lower=0.0)
    return result, lost


def scenario_events(schedule: ScenarioSchedule) -> pd.DataFrame:
    """Render scenarios as event records.

    Events describe discrete occurrences; telemetry describes continuous state.
    Design document ``12`` section 1 requires they not be collapsed.

    Args:
        schedule: The applied schedule.

    Returns:
        Two rows per instance - onset and clearance.
    """
    rows = []
    for instance in schedule.instances:
        rows.append(
            {
                "time": instance.start,
                "asset_id": instance.asset_id,
                "event_type": "FAULT",
                "scenario_id": instance.scenario_id,
                "severity": "MAJOR" if instance.severity >= 1.0 else "WARNING",
                "message": f"{instance.scenario_id} began ({instance.trigger})",
            }
        )
        rows.append(
            {
                "time": instance.end,
                "asset_id": instance.asset_id,
                "event_type": "RECOVERY",
                "scenario_id": instance.scenario_id,
                "severity": "INFO",
                "message": f"{instance.scenario_id} cleared",
            }
        )
    columns = ["time", "asset_id", "event_type", "scenario_id", "severity", "message"]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values("time")


def reliability_metrics(
    schedule: ScenarioSchedule, *, daylight_hours: float
) -> dict[str, float]:
    """Compute MTBF and MTTR from the applied schedule.

    Operating hours means **daylight** hours. Counting nighttime standby as
    operating inflates MTBF by roughly a factor of two, and transients are
    excluded because counting a 4-minute auto-restart as a failure makes the
    figure unusable.

    Args:
        schedule: The applied schedule.
        daylight_hours: Daylight operating hours in the record.

    Returns:
        Reliability figures keyed by metric name.
    """
    failures = [i for i in schedule.instances if not i.is_transient]
    transients = [i for i in schedule.instances if i.is_transient]

    if not failures:
        return {
            "failures": 0,
            "transients": len(transients),
            "mtbf_hours": float("inf"),
            "mttr_hours": 0.0,
        }

    repair_hours = sum(i.duration_minutes for i in failures) / 60.0
    return {
        "failures": len(failures),
        "transients": len(transients),
        "mtbf_hours": daylight_hours / len(failures),
        "mttr_hours": repair_hours / len(failures),
    }


@dataclass
class ScenarioGateResult:
    """Outcome of the Phase 8 fault engine acceptance checks.

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


def run_scenario_gate(clean, faulted, *, daylight: pd.Series) -> ScenarioGateResult:
    """Verify the fault engine meets its Phase 8 criteria.

    Args:
        clean: A fault-free ``PlantRunResult``.
        faulted: The same run with scenarios injected.
        daylight: Mask selecting operating hours.

    Returns:
        A :class:`ScenarioGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    schedule = faulted.schedule
    instances = schedule.instances if schedule else []

    checks.append(
        (
            "scenarios_injected",
            len(instances) > 0,
            f"{len(instances)} instances across "
            f"{len({i.scenario_id for i in instances})} scenario types",
        )
    )

    # A fault that only exists in an events table teaches an analyst to read
    # the events table. It must bend the telemetry.
    clean_export = clean.plant["grid_export_power_kw"]
    faulted_export = faulted.plant["grid_export_power_kw"]
    shortfall = float((clean_export - faulted_export).clip(lower=0.0).sum())
    relative = shortfall / max(float(clean_export.sum()), 1e-9)
    checks.append(
        (
            "faults_move_telemetry",
            relative > 1e-4,
            f"export shortfall {relative:.3%} of clean production",
        )
    )

    # Faults must be local. A trip on one inverter that degrades its peers
    # would make peer comparison useless, which is the main detection method.
    affected = {
        key for key, series in faulted.fault_loss_kw.items() if float(series.sum()) > 0
    }
    total = len(faulted.inverters)
    checks.append(
        (
            "faults_are_localised",
            0 < len(affected) < total,
            f"{len(affected)} of {total} inverters affected",
        )
    )

    # State and telemetry must agree during a fault, as they must outside one.
    inconsistent = 0
    for frame in faulted.inverters.values():
        if "operating_state" not in frame.columns:
            continue
        faulted_mask = frame["operating_state"] == InverterState.FAULT.value
        inconsistent += int((frame["ac_power_kw"][faulted_mask] > 1.0).sum())
    checks.append(
        (
            "faulted_assets_produce_nothing",
            inconsistent == 0,
            f"{inconsistent} samples with FAULT state and positive output",
        )
    )

    checks.append(
        (
            "events_bracket_each_scenario",
            len(faulted.fault_events) == 2 * len(instances),
            f"{len(faulted.fault_events)} events for {len(instances)} instances",
        )
    )

    # Durations must be long-tailed. A fixed duration makes outage length
    # uninformative and MTTR meaningless.
    durations = [i.duration_minutes for i in instances]
    spread = (max(durations) / min(durations)) if durations else 1.0
    checks.append(
        (
            "durations_are_varied",
            spread > 3.0,
            f"duration range {min(durations, default=0):.0f}-"
            f"{max(durations, default=0):.0f} min, ratio {spread:.1f}x",
        )
    )

    # Transients must be excluded from reliability figures.
    metrics = reliability_metrics(schedule, daylight_hours=float(daylight.sum()) / 60.0)
    checks.append(
        (
            "reliability_computable",
            metrics["failures"] > 0 and metrics["mttr_hours"] > 0,
            f"MTBF {metrics['mtbf_hours']:.1f} h, MTTR "
            f"{metrics['mttr_hours']:.2f} h, "
            f"{metrics['transients']} transient(s) excluded",
        )
    )

    # Faults must land during operating hours or they leave no signature.
    daylit = sum(
        1
        for i in instances
        if bool(daylight.reindex([i.start], method="nearest").iloc[0])
    )
    checks.append(
        (
            "faults_occur_in_daylight",
            daylit == len(instances),
            f"{daylit}/{len(instances)} began during operating hours",
        )
    )

    return ScenarioGateResult(checks=checks)
