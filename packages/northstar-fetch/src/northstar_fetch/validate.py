"""Tier 0 acquisition validation.

These checks run at cache-write time. A failing blocking check aborts the write,
so a bad partition is never cached: it is far cheaper to refuse the write than
to discover at Phase 3 that a dataset was built on a coordinate error.

Reference: design documents ``19_external_data_acquisition`` section 8 and
``15_validation_acceptance_specification`` section 3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Physically plausible bounds for the locked West Texas site. Values outside
# these ranges indicate an acquisition error rather than unusual weather.
IRRADIANCE_BOUNDS: dict[str, tuple[float, float]] = {
    "ghi": (0.0, 1400.0),
    "dni": (0.0, 1100.0),
    "dhi": (0.0, 800.0),
}
TEMP_AIR_BOUNDS = (-25.0, 55.0)
WIND_SPEED_BOUNDS = (0.0, 45.0)

# ERCOT administrative price floor and cap. Both are periodically revised by the
# PUCT, so these are treated as a sanity envelope rather than as exact limits.
PRICE_BOUNDS = (-251.0, 5000.0)


@dataclass
class CheckResult:
    """Outcome of a single validation check.

    Attributes:
        name: Short identifier for the check.
        passed: Whether the check succeeded.
        blocking: Whether a failure should prevent the partition being cached.
        detail: Human-readable explanation, populated on failure.
    """

    name: str
    passed: bool
    blocking: bool
    detail: str = ""


@dataclass
class ValidationReport:
    """Aggregated results for one validated partition.

    Attributes:
        label: Partition identifier, used in log output.
        results: Individual check outcomes, in execution order.
    """

    label: str
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        """Append one check outcome.

        Args:
            result: Outcome to record.
        """
        self.results.append(result)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        """Failures severe enough to prevent caching.

        Returns:
            Every failed check whose ``blocking`` flag is set.
        """
        return [r for r in self.results if not r.passed and r.blocking]

    @property
    def warnings(self) -> list[CheckResult]:
        """Failures that are recorded but do not prevent caching.

        Returns:
            Every failed check whose ``blocking`` flag is unset.
        """
        return [r for r in self.results if not r.passed and not r.blocking]

    @property
    def ok(self) -> bool:
        """Whether the partition may be written.

        Returns:
            ``True`` when there are no blocking failures.
        """
        return not self.blocking_failures

    def summary(self) -> str:
        """Render a one-line summary for logging.

        Returns:
            A string such as ``12 passed, 1 warning, 0 blocking``.
        """
        passed = sum(1 for r in self.results if r.passed)
        return (
            f"{passed} passed, {len(self.warnings)} warning, "
            f"{len(self.blocking_failures)} blocking"
        )


def check_non_empty(frame: pd.DataFrame, *, label: str = "") -> CheckResult:
    """Verify the partition contains any rows at all.

    **Every other structural check passes trivially on an empty frame.**
    Timezone awareness, monotonicity, duplicate detection and value bounds are
    all vacuously true over zero rows, so an empty partition validates cleanly,
    is written to the cache as valid, and is skipped on the next run.

    Observed: an ERCOT month returned 0 rows and was reported as
    "4 passed, 0 warning, 0 blocking" - a successful fetch of nothing. A full
    445-partition run would have completed with no price data and no failure.

    Args:
        frame: Data to check.
        label: Partition identifier, for the failure detail.

    Returns:
        A blocking :class:`CheckResult`.
    """
    rows = len(frame)
    return CheckResult(
        name="non_empty",
        passed=rows > 0,
        blocking=True,
        detail=(
            ""
            if rows > 0
            else f"partition {label or '(unlabelled)'} returned 0 rows - the "
            "request succeeded but matched no data. Check the query window, "
            "the settlement point identifier, and whether the provider "
            "paginates."
        ),
    )


def check_no_duplicate_times(
    frame: pd.DataFrame, *, time_column: str = "time", subset: list[str] | None = None
) -> CheckResult:
    """Verify that no timestamp key repeats.

    Args:
        frame: Data to check.
        time_column: Timestamp column name.
        subset: Additional key columns, for series that carry several entities.

    Returns:
        A blocking :class:`CheckResult`.
    """
    keys = [time_column] + (subset or [])
    duplicated = int(frame.duplicated(subset=keys).sum())
    return CheckResult(
        name="no_duplicate_timestamps",
        passed=duplicated == 0,
        blocking=True,
        detail="" if duplicated == 0 else f"{duplicated} duplicate key rows",
    )


def check_monotonic_time(
    frame: pd.DataFrame, *, time_column: str = "time"
) -> CheckResult:
    """Verify that timestamps increase monotonically.

    Args:
        frame: Data to check.
        time_column: Timestamp column name.

    Returns:
        A blocking :class:`CheckResult`.
    """
    times = pd.to_datetime(frame[time_column], utc=True)
    monotonic = bool(times.is_monotonic_increasing)
    return CheckResult(
        name="monotonic_timestamps",
        passed=monotonic,
        blocking=True,
        detail="" if monotonic else "timestamps are not sorted ascending",
    )


def check_timezone_aware(
    frame: pd.DataFrame, *, time_column: str = "time"
) -> CheckResult:
    """Verify that timestamps carry an explicit UTC offset.

    Naive timestamps are the most common cause of a whole-dataset time shift, so
    this is treated as blocking despite being trivially checkable.

    Args:
        frame: Data to check.
        time_column: Timestamp column name.

    Returns:
        A blocking :class:`CheckResult`.
    """
    dtype = frame[time_column].dtype
    aware = isinstance(dtype, pd.DatetimeTZDtype)
    return CheckResult(
        name="timezone_aware",
        passed=aware,
        blocking=True,
        detail="" if aware else f"timestamp dtype {dtype} is not timezone-aware",
    )


def check_expected_row_count(
    frame: pd.DataFrame, *, expected: int, tolerance: float = 0.02
) -> CheckResult:
    """Verify that the partition holds roughly the expected number of rows.

    A tolerance is allowed because providers vary in leap-day and end-of-year
    handling, but a large shortfall means a truncated download.

    Args:
        frame: Data to check.
        expected: Row count implied by the interval and period.
        tolerance: Permitted fractional deviation.

    Returns:
        A blocking :class:`CheckResult`.
    """
    actual = len(frame)
    deviation = abs(actual - expected) / expected if expected else 1.0
    return CheckResult(
        name="expected_row_count",
        passed=deviation <= tolerance,
        blocking=True,
        detail=(
            ""
            if deviation <= tolerance
            else f"expected ~{expected} rows, found {actual} ({deviation:.1%} deviation)"
        ),
    )


def check_value_bounds(
    frame: pd.DataFrame,
    column: str,
    bounds: tuple[float, float],
    *,
    blocking: bool = True,
) -> CheckResult:
    """Verify that a numeric column lies within physical bounds.

    Args:
        frame: Data to check.
        column: Column name. A missing column passes, so that optional fields do
            not fail the check.
        bounds: Inclusive ``(minimum, maximum)`` pair.
        blocking: Whether a failure should prevent caching.

    Returns:
        A :class:`CheckResult` with the requested severity.
    """
    if column not in frame.columns:
        return CheckResult(f"bounds_{column}", True, blocking, "column absent")
    low, high = bounds
    series = frame[column].dropna()
    outside = int(((series < low) | (series > high)).sum())
    return CheckResult(
        name=f"bounds_{column}",
        passed=outside == 0,
        blocking=blocking,
        detail=(
            ""
            if outside == 0
            else f"{outside} values outside [{low}, {high}] "
            f"(min {series.min():.1f}, max {series.max():.1f})"
        ),
    )


def check_irradiance_closure(
    frame: pd.DataFrame, *, tolerance_wm2: float = 60.0, max_fail_fraction: float = 0.05
) -> CheckResult:
    """Verify the GHI, DNI and DHI closure relationship.

    Tests ``GHI ~= DHI + DNI * cos(zenith)`` where a zenith angle is available.
    Satellite-derived components do not close exactly, so both an absolute
    tolerance and an allowed failure fraction are permitted; a systematic
    breakdown still indicates a decomposition or unit error.

    Args:
        frame: Data carrying ``ghi``, ``dni``, ``dhi`` and ``solar_zenith``.
        tolerance_wm2: Permitted absolute residual per sample.
        max_fail_fraction: Permitted fraction of daylight samples exceeding it.

    Returns:
        A non-blocking :class:`CheckResult`, since the provider owns the
        decomposition and a warning is the appropriate response.
    """
    required = {"ghi", "dni", "dhi", "solar_zenith"}
    if not required.issubset(frame.columns):
        return CheckResult("irradiance_closure", True, False, "required columns absent")

    daylight = frame[frame["solar_zenith"] < 85.0].dropna(
        subset=["ghi", "dni", "dhi", "solar_zenith"]
    )
    if daylight.empty:
        return CheckResult("irradiance_closure", True, False, "no daylight samples")

    reconstructed = daylight["dhi"] + daylight["dni"] * np.cos(
        np.radians(daylight["solar_zenith"])
    )
    residual = (daylight["ghi"] - reconstructed).abs()
    fail_fraction = float((residual > tolerance_wm2).mean())
    return CheckResult(
        name="irradiance_closure",
        passed=fail_fraction <= max_fail_fraction,
        blocking=False,
        detail=(
            ""
            if fail_fraction <= max_fail_fraction
            else f"{fail_fraction:.1%} of daylight samples exceed "
            f"{tolerance_wm2} W/m2 residual"
        ),
    )


def check_night_irradiance_zero(
    frame: pd.DataFrame, *, zenith_threshold: float = 95.0, tolerance_wm2: float = 5.0
) -> CheckResult:
    """Verify that irradiance is zero when the sun is well below the horizon.

    Non-zero night irradiance almost always means a timezone error, which is
    why this simple check is blocking.

    Args:
        frame: Data carrying ``ghi`` and ``solar_zenith``.
        zenith_threshold: Zenith angle above which irradiance must vanish.
        tolerance_wm2: Permitted residual, allowing for provider rounding.

    Returns:
        A blocking :class:`CheckResult`.
    """
    if not {"ghi", "solar_zenith"}.issubset(frame.columns):
        return CheckResult("night_irradiance_zero", True, True, "required columns absent")
    night = frame[frame["solar_zenith"] > zenith_threshold]["ghi"].dropna()
    if night.empty:
        return CheckResult("night_irradiance_zero", True, True, "no night samples")
    offenders = int((night > tolerance_wm2).sum())
    return CheckResult(
        name="night_irradiance_zero",
        passed=offenders == 0,
        blocking=True,
        detail=(
            ""
            if offenders == 0
            else f"{offenders} night samples above {tolerance_wm2} W/m2 "
            f"(max {night.max():.1f}) - suspect a timezone error"
        ),
    )


def check_negative_prices_present(
    frame: pd.DataFrame, *, price_column: str = "price_usd_mwh"
) -> CheckResult:
    """Verify that a full year of West Texas prices contains negative values.

    Negative real-time prices are a routine feature of the ERCOT West zone. A
    year without any is not unusual weather; it means the fetch clipped, coerced
    or mis-parsed the price field. This check has caught more acquisition bugs
    than any bounds check.

    Args:
        frame: Price data.
        price_column: Column holding signed prices in dollars per MWh.

    Returns:
        A blocking :class:`CheckResult`.
    """
    if price_column not in frame.columns:
        return CheckResult(
            "negative_prices_present", False, True, f"missing {price_column}"
        )
    count = int((frame[price_column] < 0).sum())
    return CheckResult(
        name="negative_prices_present",
        passed=count > 0,
        blocking=True,
        detail=(
            ""
            if count > 0
            else "no negative prices found - the price field is probably "
            "clipped at zero or mis-parsed"
        ),
    )


def check_no_zero_filled_irradiance(frame: pd.DataFrame) -> CheckResult:
    """Verify that missing irradiance is ``NaN`` rather than zero.

    Zero-filled irradiance is indistinguishable from night, corrupts every
    daylight filter, and cannot be detected once cached.

    Args:
        frame: Data carrying ``ghi`` and ``solar_zenith``.

    Returns:
        A non-blocking :class:`CheckResult`, since a genuinely dark overcast
        sample can legitimately read zero.
    """
    if not {"ghi", "solar_zenith"}.issubset(frame.columns):
        return CheckResult("no_zero_filled_irradiance", True, False, "columns absent")
    daylight = frame[frame["solar_zenith"] < 80.0]["ghi"]
    if daylight.empty:
        return CheckResult("no_zero_filled_irradiance", True, False, "no samples")
    exact_zeros = int((daylight == 0.0).sum())
    fraction = exact_zeros / len(daylight)
    return CheckResult(
        name="no_zero_filled_irradiance",
        passed=fraction < 0.01,
        blocking=False,
        detail=(
            ""
            if fraction < 0.01
            else f"{fraction:.1%} of high-sun samples are exactly zero - "
            "suspect zero-filling of missing data"
        ),
    )


def validate_weather_partition(
    frame: pd.DataFrame, *, label: str, expected_rows: int
) -> ValidationReport:
    """Run the full T0 check set for a weather partition.

    Args:
        frame: Harmonized weather data.
        label: Partition identifier for reporting.
        expected_rows: Row count implied by the interval and period.

    Returns:
        A :class:`ValidationReport` covering structural and physical checks.
    """
    report = ValidationReport(label=label)
    # First, because timezone, monotonicity and duplicate checks are all
    # vacuously true over zero rows. The row-count check would catch an
    # empty frame as a 100% deviation; this names the actual cause.
    report.add(check_non_empty(frame, label=label))
    report.add(check_timezone_aware(frame))
    report.add(check_monotonic_time(frame))
    report.add(check_no_duplicate_times(frame))
    report.add(check_expected_row_count(frame, expected=expected_rows))
    for column, bounds in IRRADIANCE_BOUNDS.items():
        report.add(check_value_bounds(frame, column, bounds))
    report.add(check_value_bounds(frame, "temp_air", TEMP_AIR_BOUNDS))
    report.add(check_value_bounds(frame, "wind_speed", WIND_SPEED_BOUNDS))
    report.add(check_night_irradiance_zero(frame))
    report.add(check_irradiance_closure(frame))
    report.add(check_no_zero_filled_irradiance(frame))
    return report


def validate_price_partition(
    frame: pd.DataFrame, *, label: str, require_negatives: bool
) -> ValidationReport:
    """Run the full T0 check set for a market price partition.

    Args:
        frame: Harmonized price data.
        label: Partition identifier for reporting.
        require_negatives: Whether to assert that negative prices are present.
            Applied to full-year partitions only; a single month can legitimately
            contain none.

    Returns:
        A :class:`ValidationReport` covering structural and market checks.
    """
    report = ValidationReport(label=label)
    # First, because everything below is vacuously true over zero rows.
    report.add(check_non_empty(frame, label=label))
    report.add(check_timezone_aware(frame))
    report.add(check_monotonic_time(frame))
    report.add(check_no_duplicate_times(frame, subset=["settlement_point", "price_type"]))
    report.add(check_value_bounds(frame, "price_usd_mwh", PRICE_BOUNDS, blocking=False))
    if require_negatives:
        report.add(check_negative_prices_present(frame))
    return report


def cross_source_temperature_correlation(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    *,
    threshold: float = 0.90,
) -> CheckResult:
    """Compare ambient temperature between two independent providers.

    Two credible sources for the same coordinates must broadly agree. When they
    do not, the cause is almost always a coordinate, timezone or unit error
    rather than genuine disagreement, which is why this cross-check catches more
    integration bugs than any single-source test.

    Both series are resampled to hourly means before comparison so that
    differing native cadences do not affect the result.

    Args:
        primary: First provider's data, carrying ``time`` and ``temp_air``.
        secondary: Second provider's data, same columns.
        threshold: Minimum acceptable Pearson correlation.

    Returns:
        A blocking :class:`CheckResult`.
    """
    if "temp_air" not in primary.columns or "temp_air" not in secondary.columns:
        return CheckResult(
            "cross_source_temperature", False, True, "temp_air missing from a source"
        )

    left = (
        primary.set_index(pd.to_datetime(primary["time"], utc=True))["temp_air"]
        .resample("1h")
        .mean()
    )
    right = (
        secondary.set_index(pd.to_datetime(secondary["time"], utc=True))["temp_air"]
        .resample("1h")
        .mean()
    )
    joined = pd.concat([left, right], axis=1, join="inner").dropna()
    if len(joined) < 24:
        return CheckResult(
            "cross_source_temperature", False, True, "fewer than 24 overlapping hours"
        )

    correlation = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
    return CheckResult(
        name="cross_source_temperature",
        passed=correlation >= threshold,
        blocking=True,
        detail=(
            ""
            if correlation >= threshold
            else f"correlation {correlation:.3f} below {threshold} over "
            f"{len(joined)} hours - suspect a coordinate, timezone or unit error"
        ),
    )


# Registry of standalone checks, exposed so a caller can run a named subset.
CHECK_REGISTRY: dict[str, Callable[..., CheckResult]] = {
    "no_duplicate_timestamps": check_no_duplicate_times,
    "monotonic_timestamps": check_monotonic_time,
    "timezone_aware": check_timezone_aware,
    "night_irradiance_zero": check_night_irradiance_zero,
    "irradiance_closure": check_irradiance_closure,
    "negative_prices_present": check_negative_prices_present,
    "no_zero_filled_irradiance": check_no_zero_filled_irradiance,
}
