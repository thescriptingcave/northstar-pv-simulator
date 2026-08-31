"""Normalization applied to every acquired series before caching.

Providers disagree about column names, units, wind measurement height and
whether a timestamp labels the beginning or the end of its interval. Resolving
that once, here, means the simulator consumes a single consistent schema.

Two rules in this module are load-bearing rather than cosmetic:

* Missing values stay ``NaN`` and are never zero-filled. Zero-filled irradiance
  is indistinguishable from night, which corrupts every daylight filter
  downstream and is silently undetectable.
* Interval convention is normalized to interval-beginning. Getting this wrong
  shifts every series by one interval and does not surface until financial
  reconciliation fails.

Reference: design document ``19_external_data_acquisition`` section 5.3.
"""

from __future__ import annotations

import math

import pandas as pd

from .config import HarmonizationConfig

# Provider column names mapped onto pvlib naming conventions. Keys are lowercase
# so that matching is case-insensitive across providers.
CANONICAL_COLUMNS: dict[str, str] = {
    "ghi": "ghi",
    "dni": "dni",
    "dhi": "dhi",
    "temperature": "temp_air",
    "air_temperature": "temp_air",
    "temperature_2m": "temp_air",
    "temp_air": "temp_air",
    "dew_point": "temp_dew",
    "dewpoint": "temp_dew",
    "wind_speed": "wind_speed",
    "wind_speed_10m": "wind_speed",
    "wind_direction": "wind_direction",
    "wind_direction_10m": "wind_direction",
    "relative_humidity": "relative_humidity",
    "relative_humidity_2m": "relative_humidity",
    "surface_pressure": "pressure",
    "pressure": "pressure",
    "surface_albedo": "albedo",
    "albedo": "albedo",
    "precipitation": "precipitation",
    "rain": "rain",
    "snowfall": "snowfall",
    "cloud_cover": "cloud_cover",
    "solar_zenith_angle": "solar_zenith",
}


def rename_to_canonical(frame: pd.DataFrame) -> pd.DataFrame:
    """Map provider column names onto pvlib conventions.

    Columns with no mapping are preserved unchanged, so provider-specific extras
    survive into the cache rather than being silently discarded.

    Args:
        frame: Raw provider data.

    Returns:
        A new frame with recognised columns renamed.
    """
    mapping = {
        column: CANONICAL_COLUMNS[column.lower()]
        for column in frame.columns
        if column.lower() in CANONICAL_COLUMNS
    }
    return frame.rename(columns=mapping)


def normalize_time(
    frame: pd.DataFrame,
    *,
    source_convention: str,
    target_convention: str,
    interval_minutes: int,
    time_column: str = "time",
) -> pd.DataFrame:
    """Convert timestamps to UTC and to the target interval convention.

    Args:
        frame: Data carrying a timestamp column.
        source_convention: ``"beginning"`` or ``"ending"``, as the provider
            labels its intervals.
        target_convention: Convention to store, normally ``"beginning"``.
        interval_minutes: Nominal interval width, used to shift when the
            conventions differ.
        time_column: Name of the timestamp column.

    Returns:
        A new frame whose timestamp column is timezone-aware UTC and labelled
        according to ``target_convention``.

    Raises:
        KeyError: If the timestamp column is absent.
        ValueError: If either convention is not recognised.
    """
    if time_column not in frame.columns:
        raise KeyError(f"missing timestamp column '{time_column}'")
    valid = {"beginning", "ending"}
    if source_convention not in valid or target_convention not in valid:
        raise ValueError(
            f"conventions must be one of {sorted(valid)}, "
            f"got {source_convention!r} and {target_convention!r}"
        )

    result = frame.copy()
    times = pd.to_datetime(result[time_column], utc=True)

    if source_convention != target_convention:
        shift = pd.Timedelta(minutes=interval_minutes)
        # An interval-ending label sits one interval later than the equivalent
        # interval-beginning label, so converting to beginning subtracts.
        times = times - shift if target_convention == "beginning" else times + shift

    result[time_column] = times
    return result


def correct_wind_height(
    wind_speed: pd.Series,
    *,
    from_height_m: float,
    to_height_m: float,
    roughness_length_m: float,
) -> pd.Series:
    """Adjust wind speed from measurement height to module height.

    Uses the neutral-stability logarithmic wind profile. Skipping this
    correction overestimates convective cooling and biases every
    temperature-dependent analysis downstream.

    Args:
        wind_speed: Wind speed at ``from_height_m``, in metres per second.
        from_height_m: Provider measurement height.
        to_height_m: Height expected by the cell temperature model.
        roughness_length_m: Surface roughness length for the site.

    Returns:
        Wind speed adjusted to ``to_height_m``, with ``NaN`` preserved.

    Raises:
        ValueError: If any height is not strictly greater than the roughness
            length, which would make the logarithm undefined.
    """
    if roughness_length_m <= 0:
        raise ValueError("roughness_length_m must be positive")
    if from_height_m <= roughness_length_m or to_height_m <= roughness_length_m:
        raise ValueError(
            "measurement and target heights must exceed the roughness length"
        )
    ratio = math.log(to_height_m / roughness_length_m) / math.log(
        from_height_m / roughness_length_m
    )
    return wind_speed * ratio


def apply_harmonization(
    frame: pd.DataFrame,
    config: HarmonizationConfig,
    *,
    source_convention: str,
    interval_minutes: int,
    correct_wind: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """Run the full harmonization pipeline over one acquired series.

    Steps are applied in a fixed order and each is recorded, so the manifest
    documents exactly what was done to the provider's numbers.

    Args:
        frame: Raw provider data with a ``time`` column.
        config: Harmonization rules.
        source_convention: Provider's interval labelling convention.
        interval_minutes: Nominal interval width in minutes.
        correct_wind: Whether to apply the wind height correction. Disabled for
            sources whose wind is already at the target height, or which carry
            no wind at all.

    Returns:
        A tuple of the harmonized frame and the ordered list of transformation
        descriptions to record in the manifest.
    """
    steps: list[str] = []

    result = rename_to_canonical(frame)
    steps.append("renamed columns to pvlib conventions")

    result = normalize_time(
        result,
        source_convention=source_convention,
        target_convention=config.interval_convention,
        interval_minutes=interval_minutes,
    )
    steps.append(
        f"timestamps to UTC, interval-{config.interval_convention} "
        f"(source: interval-{source_convention})"
    )

    if correct_wind and "wind_speed" in result.columns:
        result["wind_speed"] = correct_wind_height(
            result["wind_speed"],
            from_height_m=config.wind_measurement_height_m,
            to_height_m=config.wind_target_height_m,
            roughness_length_m=config.roughness_length_m,
        )
        steps.append(
            f"wind speed log-profile corrected from "
            f"{config.wind_measurement_height_m} m to "
            f"{config.wind_target_height_m} m "
            f"(z0={config.roughness_length_m} m)"
        )

    result = result.sort_values("time").reset_index(drop=True)
    steps.append("sorted by time ascending")

    return result, steps
