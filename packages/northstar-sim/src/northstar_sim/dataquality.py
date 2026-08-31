"""Data quality defect injection.

Defects are applied to **measured telemetry only**, after the sensor layer,
never to physical truth. That is the whole point of the phase and the reason it
comes last in the pipeline:

    A stuck AC-power sensor reports constant output while the actual inverter
    continues changing. An inverter failure changes actual physical output.

Design document ``09`` section 7 requires that distinction, and it is only
meaningful because faults (Phase 8) act on truth while defects (here) act on
what was reported. An analyst who cannot separate them will attribute a
communications outage to an equipment failure and go looking for a technician.

**Quality flags are themselves measured artefacts and are deliberately
sometimes wrong.** A stuck sensor commonly reports ``GOOD`` throughout - the
instrument has no idea it has frozen. Trusting the flag rather than testing the
data is a mistake the dataset should permit, and doc ``11 §12`` requires it.

Reference: design documents ``02_time_series_analytics_requirements`` section 9
and ``11_telemetry_specification`` section 12.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd


class DefectKind(StrEnum):
    """Categories of injected data-quality defect."""

    GAP = "SCN-060"
    STUCK = "SCN-061"
    DRIFT = "SCN-062"
    SPIKE = "SCN-063"
    COMMS_OUTAGE = "SCN-064"
    DUPLICATE = "SCN-065"
    TIMESTAMP_SKEW = "SCN-066"


class Quality(StrEnum):
    """Quality flag values carried on every telemetry sample."""

    GOOD = "GOOD"
    SUSPECT = "SUSPECT"
    STALE = "STALE"
    MISSING = "MISSING"
    ESTIMATED = "ESTIMATED"


#: Probability that a defect is correctly flagged. Deliberately below 1.0.
#:
#: A frozen instrument does not know it has frozen, and a drifting one reports
#: plausible values throughout. Setting this to 1.0 would make the quality
#: column a complete oracle and reduce every data-quality exercise to a filter.
FLAG_DETECTION_RATE: dict[str, float] = {
    DefectKind.GAP: 1.00,  # an absent sample is unambiguous
    DefectKind.COMMS_OUTAGE: 1.00,
    DefectKind.SPIKE: 0.65,  # range checks catch the obvious ones
    DefectKind.STUCK: 0.30,  # stale detection needs a long enough run
    DefectKind.DRIFT: 0.05,  # essentially undetectable in-band
    DefectKind.DUPLICATE: 0.50,
    DefectKind.TIMESTAMP_SKEW: 0.40,
}


@dataclass
class DefectInstance:
    """One injected data-quality defect.

    Attributes:
        kind: Defect category.
        asset_id: Affected asset.
        quantity: Affected telemetry field, or ``"*"`` for whole-asset defects.
        start: When the defect begins.
        end: When reporting returns to normal.
        magnitude: Defect-specific parameter - drift rate, spike multiplier.
        flagged: Whether the quality column marks it.
    """

    kind: str
    asset_id: str
    quantity: str
    start: pd.Timestamp
    end: pd.Timestamp
    magnitude: float = 0.0
    flagged: bool = True

    @property
    def duration_minutes(self) -> float:
        """Length of the defect.

        Returns:
            Duration in minutes.
        """
        return (self.end - self.start).total_seconds() / 60.0


@dataclass
class DefectSchedule:
    """The set of defects applied to a run.

    Attributes:
        instances: Defect occurrences.
        seed: Seed the schedule was generated from.
    """

    instances: list[DefectInstance] = field(default_factory=list)
    seed: int = 0

    def for_asset(self, asset_id: str) -> list[DefectInstance]:
        """Select the defects affecting one asset.

        Args:
            asset_id: Asset to filter on.

        Returns:
            Matching instances.
        """
        return [i for i in self.instances if i.asset_id == asset_id]

    def to_frame(self) -> pd.DataFrame:
        """Render the schedule as ground truth.

        This is what makes an analyst's defect detection **scoreable**. It sits
        in the restricted truth schema alongside the fault schedule.

        Returns:
            One row per instance, empty-but-typed when there are none.
        """
        columns = [
            "kind",
            "asset_id",
            "quantity",
            "start",
            "end",
            "magnitude",
            "flagged",
            "duration_minutes",
        ]
        if not self.instances:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(
            [
                {
                    "kind": i.kind,
                    "asset_id": i.asset_id,
                    "quantity": i.quantity,
                    "start": i.start,
                    "end": i.end,
                    "magnitude": i.magnitude,
                    "flagged": i.flagged,
                    "duration_minutes": i.duration_minutes,
                }
                for i in self.instances
            ],
            columns=columns,
        )


def schedule_defects(
    assets_and_fields: list[tuple[str, str]],
    index: pd.DatetimeIndex,
    *,
    seed: int,
    daily_rate_per_asset: float = 0.16,
) -> DefectSchedule:
    """Generate a reproducible data-quality defect schedule.

    Args:
        assets_and_fields: Candidate asset and field pairs.
        index: Simulation time index.
        seed: Seed for the ``dataquality_injection`` substream.
        daily_rate_per_asset: Expected defects per asset per simulated day.
            Scaling with fleet size matters: a flat plant-wide rate of 1.2 per
            day spread across 585 assets corrupted 0.017% of samples, which is
            an order of magnitude below what a real SCADA system produces and
            far too sparse for any data-quality exercise to have material to
            work with.

            The default targets roughly 0.5% of samples carrying a non-GOOD
            flag, matching the acceptance targets in design doc 20 section 11:
            data availability above 99%, flagged fraction below 1%, stuck
            fraction below 0.1%.

    Returns:
        The generated :class:`DefectSchedule`.
    """
    rng = np.random.default_rng(seed)
    days = max(1.0, (index[-1] - index[0]).total_seconds() / 86400.0)
    span = len(index)

    distinct_assets = len({asset for asset, _ in assets_and_fields})
    daily_rate = daily_rate_per_asset * max(1, distinct_assets)

    kinds = list(FLAG_DETECTION_RATE)
    # Communications outages are weighted up because they take every field on
    # an asset at once. Single-signal defects barely move the fleet-wide
    # corrupted-sample share, which is what an analyst's availability metric
    # actually measures.
    weights = np.array([0.18, 0.12, 0.14, 0.14, 0.28, 0.07, 0.07])
    weights = weights / weights.sum()

    instances: list[DefectInstance] = []
    for _ in range(rng.poisson(daily_rate * days)):
        kind = str(rng.choice(kinds, p=weights))
        asset_id, quantity = assets_and_fields[
            int(rng.integers(0, len(assets_and_fields)))
        ]

        duration = int(np.clip(rng.lognormal(mean=3.9, sigma=1.2), 3, 2880))
        offset = int(rng.integers(0, max(1, span - duration)))
        start = index[offset]

        instances.append(
            DefectInstance(
                kind=kind,
                # A communications outage takes every field on the asset, not
                # one signal. Modelling it per-field would make it look like a
                # sensor fault instead of a network one.
                quantity="*" if kind == DefectKind.COMMS_OUTAGE else quantity,
                asset_id=asset_id,
                start=start,
                end=start + pd.Timedelta(minutes=duration),
                magnitude=float(rng.uniform(1.5, 6.0)),
                flagged=bool(rng.random() < FLAG_DETECTION_RATE[kind]),
            )
        )

    instances.sort(key=lambda i: (i.start, i.asset_id))
    return DefectSchedule(instances=instances, seed=seed)


def apply_defects(
    measured: dict[str, pd.DataFrame],
    schedule: DefectSchedule,
    *,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Corrupt measured telemetry and produce quality flags.

    The input frames are never modified. Corruption is applied to copies,
    because the caller still holds the uncorrupted measured frames and the
    truth frames behind them.

    Args:
        measured: Per-asset measured frames from the sensor layer.
        schedule: Defects to apply.
        seed: Seed for the noise realisation.

    Returns:
        A tuple of corrupted frames and per-asset quality flag frames.
    """
    corrupted = {key: frame.copy() for key, frame in measured.items()}
    flags = {
        key: pd.DataFrame(Quality.GOOD.value, index=frame.index, columns=frame.columns)
        for key, frame in measured.items()
    }

    for position, instance in enumerate(schedule.instances):
        if instance.asset_id not in corrupted:
            continue
        frame = corrupted[instance.asset_id]
        flag_frame = flags[instance.asset_id]

        active = (frame.index >= instance.start) & (frame.index < instance.end)
        if not active.any():
            continue

        numeric = [
            column
            for column in frame.columns
            if pd.api.types.is_numeric_dtype(frame[column])
        ]
        targets = (
            numeric
            if instance.quantity == "*"
            else [instance.quantity]
            if instance.quantity in numeric
            else []
        )
        if not targets:
            continue

        rng = np.random.default_rng(abs(hash((seed, position))) % (2**32))
        _apply_one(frame, flag_frame, active, targets, instance, rng)

    return corrupted, flags


def _apply_one(
    frame: pd.DataFrame,
    flags: pd.DataFrame,
    active: np.ndarray,
    targets: list[str],
    instance: DefectInstance,
    rng: np.random.Generator,
) -> None:
    """Apply one defect in place to a copied frame.

    Args:
        frame: Corrupted frame being built.
        flags: Quality flag frame for the same asset.
        active: Boolean mask of affected samples.
        targets: Columns to corrupt.
        instance: The defect.
        rng: Seeded generator.
    """
    kind = instance.kind

    if kind in (DefectKind.GAP, DefectKind.COMMS_OUTAGE):
        # Missing values stay NaN and are never zero-filled. Zero-filled
        # irradiance is indistinguishable from night and cannot be recovered.
        for column in targets:
            frame.loc[active, column] = np.nan
            if instance.flagged:
                flags.loc[active, column] = Quality.MISSING.value

    elif kind == DefectKind.STUCK:
        for column in targets:
            first = frame.loc[active, column].iloc[0]
            frame.loc[active, column] = first
            if instance.flagged:
                flags.loc[active, column] = Quality.STALE.value

    elif kind == DefectKind.DRIFT:
        # Slow, monotonic and in-band. A drifting sensor reports plausible
        # values throughout, which is why it is essentially undetectable
        # without a second reference.
        span = int(active.sum())
        ramp = np.linspace(0.0, instance.magnitude / 100.0, span)
        for column in targets:
            frame.loc[active, column] = frame.loc[active, column] * (1.0 + ramp)
            if instance.flagged:
                flags.loc[active, column] = Quality.SUSPECT.value

    elif kind == DefectKind.SPIKE:
        positions = np.flatnonzero(active)
        chosen = rng.choice(positions, size=max(1, len(positions) // 20), replace=False)
        for column in targets:
            values = frame[column].to_numpy(copy=True)
            values[chosen] = values[chosen] * instance.magnitude
            frame[column] = values
            if instance.flagged:
                flag_values = flags[column].to_numpy(copy=True)
                flag_values[chosen] = Quality.SUSPECT.value
                flags[column] = flag_values

    elif kind == DefectKind.TIMESTAMP_SKEW:
        # A clock offset shifts the values against the true time base. The
        # magnitude of the shift is what makes cross-asset correlation collapse
        # while each asset's own series still looks entirely reasonable.
        shift = max(1, int(instance.magnitude))
        for column in targets:
            shifted = frame[column].shift(shift)
            frame.loc[active, column] = shifted[active]
            if instance.flagged:
                flags.loc[active, column] = Quality.SUSPECT.value


def inject_duplicates(
    frame: pd.DataFrame, schedule: DefectSchedule, asset_id: str
) -> pd.DataFrame:
    """Produce a staging frame containing duplicate rows.

    Duplicates cannot live in a table whose primary key forbids them. Design
    document ``13 §11`` resolves this by injecting them into a **staging** frame
    without the constraint: finding and resolving them before load is the
    exercise, which is exactly how real ingestion pipelines work.

    Args:
        frame: The asset's corrupted telemetry.
        schedule: The defect schedule.
        asset_id: Asset to check for duplicate defects.

    Returns:
        A frame with duplicated rows appended and the index re-sorted.
    """
    duplicates = [
        i for i in schedule.for_asset(asset_id) if i.kind == DefectKind.DUPLICATE
    ]
    if not duplicates:
        return frame

    extra = []
    for instance in duplicates:
        window = frame[(frame.index >= instance.start) & (frame.index < instance.end)]
        if len(window):
            extra.append(window)

    if not extra:
        return frame
    return pd.concat([frame, *extra]).sort_index()


def quality_summary(flags: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Summarise quality flags across the fleet.

    Args:
        flags: Per-asset quality flag frames.

    Returns:
        Counts and shares by flag value.
    """
    counts: dict[str, int] = {}
    total = 0
    for frame in flags.values():
        stacked = frame.stack()
        total += len(stacked)
        for value, count in stacked.value_counts().items():
            counts[str(value)] = counts.get(str(value), 0) + int(count)

    return pd.DataFrame(
        [
            {"quality": value, "samples": count, "share": count / max(total, 1)}
            for value, count in sorted(counts.items())
        ]
    )


def undetected_defect_share(schedule: DefectSchedule) -> float:
    """Compute the fraction of defects the quality column does not flag.

    Args:
        schedule: The applied schedule.

    Returns:
        Share of unflagged instances. Zero would make the quality column a
        complete oracle and every data-quality exercise a filter.
    """
    if not schedule.instances:
        return 0.0
    unflagged = sum(1 for i in schedule.instances if not i.flagged)
    return unflagged / len(schedule.instances)


@dataclass
class DataQualityGateResult:
    """Outcome of the Phase 10 data-quality acceptance checks.

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


def run_dataquality_gate(clean, corrupted) -> DataQualityGateResult:
    """Verify data-quality injection meets its Phase 10 criteria.

    Args:
        clean: A run with faults but no data-quality defects.
        corrupted: The same run with defects injected.

    Returns:
        A :class:`DataQualityGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    schedule = corrupted.defects
    instances = schedule.instances if schedule else []

    checks.append(
        (
            "defects_injected",
            len(instances) > 0,
            f"{len(instances)} defects across "
            f"{len({i.kind for i in instances})} of {len(DefectKind)} types",
        )
    )

    # The property the entire phase exists to guarantee. A measurement defect
    # that changed physical truth would make equipment faults and data faults
    # indistinguishable, and every exercise in doc 02 section 9 meaningless.
    export_identical = clean.plant["grid_export_power_kw"].equals(
        corrupted.plant["grid_export_power_kw"]
    )
    truth_identical = all(
        clean.inverters[key].equals(corrupted.inverters[key]) for key in clean.inverters
    )
    checks.append(
        (
            "physical_truth_unaltered",
            export_identical and truth_identical,
            "plant export and every inverter truth frame bit-identical",
        )
    )

    changed = sum(
        1
        for key in corrupted.measured
        if key in clean.measured
        and not corrupted.measured[key].equals(clean.measured[key])
    )
    checks.append(
        (
            "measured_telemetry_corrupted",
            changed > 0,
            f"{changed} of {len(corrupted.measured)} measured frames differ",
        )
    )

    summary = quality_summary(corrupted.quality)
    good = summary[summary["quality"] == Quality.GOOD.value]
    corrupted_share = 1.0 - (float(good["share"].iloc[0]) if len(good) else 1.0)
    checks.append(
        (
            "corruption_within_target",
            0.0002 < corrupted_share < 0.01,
            f"{corrupted_share:.3%} of samples flagged (doc 20 target: under 1%)",
        )
    )

    # A quality column that flags every defect is a complete oracle, and every
    # data-quality exercise collapses into a filter. A frozen instrument does
    # not know it has frozen.
    unflagged = undetected_defect_share(schedule)
    checks.append(
        (
            "quality_flags_are_fallible",
            unflagged > 0.05,
            f"{unflagged:.0%} of defects carry no flag",
        )
    )

    # The defining case from doc 09 section 7.
    stuck = [i for i in instances if i.kind == DefectKind.STUCK and i.quantity != "*"]
    stuck_verified = False
    detail = "no stuck-sensor instance in this realisation"
    for instance in stuck:
        # `or` on a DataFrame raises: pandas refuses to evaluate truthiness.
        truth_frame = corrupted.inverters.get(instance.asset_id)
        if truth_frame is None:
            truth_frame = corrupted.weather.get(instance.asset_id)
        measured_frame = corrupted.measured.get(instance.asset_id)
        if truth_frame is None or measured_frame is None:
            continue
        if instance.quantity not in truth_frame.columns:
            continue
        window = (measured_frame.index >= instance.start) & (
            measured_frame.index < instance.end
        )
        measured_unique = measured_frame.loc[window, instance.quantity].nunique()
        truth_unique = truth_frame.loc[window, instance.quantity].nunique()
        if truth_unique > 1:
            stuck_verified = measured_unique == 1
            detail = (
                f"{instance.asset_id}/{instance.quantity}: measured "
                f"{measured_unique} distinct value, truth {truth_unique}"
            )
            break
    checks.append(("stuck_sensor_freezes_only_reporting", stuck_verified, detail))

    # Missing values must be NaN, never zero. Zero-filled irradiance is
    # indistinguishable from night and cannot be recovered once cached.
    zero_filled = 0
    for instance in instances:
        if instance.kind not in (DefectKind.GAP, DefectKind.COMMS_OUTAGE):
            continue
        frame = corrupted.measured.get(instance.asset_id)
        if frame is None:
            continue
        window = (frame.index >= instance.start) & (frame.index < instance.end)
        targets = (
            [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c])]
            if instance.quantity == "*"
            else [instance.quantity]
        )
        for column in targets:
            if column in frame.columns:
                zero_filled += int((frame.loc[window, column] == 0.0).sum())
    checks.append(
        (
            "gaps_are_nan_not_zero",
            zero_filled == 0,
            f"{zero_filled} zero-filled samples inside gaps",
        )
    )

    return DataQualityGateResult(checks=checks)
