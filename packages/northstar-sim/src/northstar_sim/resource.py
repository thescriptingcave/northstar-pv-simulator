"""Environmental resource: source data and temporal downscaling.

Design document ``06`` defines a five-layer resource architecture. This module
implements layers 1 and 2 for a single point:

* **Layer 1** - source data at its native cadence. In production this is the
  cached NSRDB GOES CONUS series at 5 minutes. For the Phase 2 physics gate a
  deterministic clear-sky series is used instead, because the gate compares two
  model chains against *identical* inputs and clear sky removes stochastic
  variation from the comparison entirely.
* **Layer 2** - stochastic downscaling to 1 minute in clear-sky-index space.

Layer 3 (the advected spatial cloud field) is Phase 3 and is not here.

The renormalization invariant in :func:`downscale_to_minute` is the load-bearing
part. Without it, downscaling silently shifts the resource, monthly and annual
energy drift away from real meteorology, and the TMY-based P50 baseline stops
being comparable to anything.

Reference: design document ``06_environmental_model`` sections 3-4.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

from .plant_config import PlantConfig

#: Clear-sky index bands and their relative variability. Variability is not
#: uniform: clear skies and solid overcast are both smooth, and broken cloud is
#: where ramps live. Conditioning on kt* is what makes passing-cloud and
#: rapid-ramp behaviour emerge from real weather rather than being scheduled.
VARIABILITY_BANDS: tuple[tuple[float, float, float], ...] = (
    (0.90, 1.20, 0.02),  # clear
    (0.80, 0.90, 0.06),  # thin cloud
    (0.40, 0.80, 0.18),  # broken cloud - maximum variability
    (0.25, 0.40, 0.09),  # heavy cloud
    (0.00, 0.25, 0.02),  # solid overcast
)

#: Upper bound on the clear-sky index. Values above 1.0 are physically real:
#: cloud edges scatter additional light onto the plane of array, producing brief
#: enhancement events. Excluding them would make every above-clear-sky reading
#: look like sensor error, which is a false lesson.
KT_CEILING = 1.15

#: Ornstein-Uhlenbeck mean reversion rate per minute. Sets how quickly a
#: perturbation decays, and therefore the autocorrelation of 1-minute ramps.
OU_REVERSION_PER_MIN = 0.35


def location_from_config(config: PlantConfig) -> pvlib.location.Location:
    """Build a pvlib location from the plant configuration.

    Args:
        config: Plant configuration.

    Returns:
        A :class:`pvlib.location.Location` for the site.
    """
    return pvlib.location.Location(
        latitude=config.site.latitude,
        longitude=config.site.longitude,
        altitude=config.site.elevation_m,
        tz="UTC",
        name=config.site.name,
    )


def clearsky_resource(
    config: PlantConfig,
    start: str,
    end: str,
    *,
    freq: str = "5min",
    temp_air_c: float = 25.0,
    wind_speed_ms: float = 3.0,
    temp_amplitude_c: float = 0.0,
    temp_lag_hours: float = 2.5,
    seed: int | None = None,
) -> pd.DataFrame:
    """Generate a deterministic clear-sky resource series.

    This is a **development and gate input**, not a dataset input. It exists so
    the physics gate can compare two model chains against inputs that contain no
    randomness at all: any difference between the chains is then attributable to
    the chains rather than to the weather.

    Ambient temperature and wind are held constant for the same reason. Realistic
    variation belongs to the cached resource, not here.

    Args:
        config: Plant configuration.
        start: Inclusive UTC start timestamp.
        end: Inclusive UTC end timestamp.
        freq: pandas frequency string for the source cadence.
        temp_air_c: Mean ambient temperature.
        wind_speed_ms: Constant wind speed at module height.
        temp_amplitude_c: Half-range of the diurnal ambient cycle. Zero holds
            ambient constant, which is what the physics gate wants.

            **Non-zero matters for any analysis that separates temperature from
            irradiance.** With constant ambient, cell temperature is a
            near-deterministic function of irradiance, the two are collinear,
            and a regression of output on both returns a temperature
            coefficient of the wrong sign. Real ambient varies, and its peak
            lags solar noon, which is what supplies the independent variation
            such a regression needs.
        temp_lag_hours: How far the ambient peak trails solar noon. Thermal
            mass in the ground and air makes mid-afternoon hotter than the
            equally-lit mid-morning.
        seed: Seed for day-to-day ambient variation. ``None`` disables it.

    Returns:
        A frame indexed by UTC timestamp with ``ghi``, ``dni``, ``dhi``,
        ``temp_air``, ``wind_speed`` and ``solar_zenith``.
    """
    location = location_from_config(config)
    times = pd.date_range(start, end, freq=freq, tz="UTC")

    clearsky = location.get_clearsky(times, model="ineichen")
    solar_position = location.get_solarposition(times)

    frame = clearsky[["ghi", "dni", "dhi"]].copy()

    if temp_amplitude_c > 0:
        # Diurnal cycle whose peak trails solar noon, plus day-to-day drift.
        # Both are needed: the lag decouples temperature from irradiance within
        # a day, the drift decouples it across days.
        hours = times.hour + times.minute / 60.0
        solar_noon = 12.0 - config.site.longitude / 15.0
        phase = 2.0 * np.pi * (hours - solar_noon - temp_lag_hours) / 24.0
        daily = np.cos(phase)

        drift = np.zeros(len(times))
        if seed is not None:
            rng = np.random.default_rng(seed)
            day_index = (times.normalize() - times.normalize()[0]).days
            offsets = rng.normal(0.0, temp_amplitude_c * 0.35, day_index.max() + 1)
            drift = offsets[day_index]

        frame["temp_air"] = temp_air_c + temp_amplitude_c * daily + drift
    else:
        frame["temp_air"] = temp_air_c
    frame["wind_speed"] = wind_speed_ms
    frame["solar_zenith"] = solar_position["apparent_zenith"]
    return frame


def variability_sigma(kt: np.ndarray) -> np.ndarray:
    """Map clear-sky index onto relative variability.

    Args:
        kt: Clear-sky index values.

    Returns:
        Per-sample relative standard deviation, from
        :data:`VARIABILITY_BANDS`.
    """
    sigma = np.full_like(kt, VARIABILITY_BANDS[0][2], dtype=float)
    for low, high, value in VARIABILITY_BANDS:
        sigma = np.where((kt >= low) & (kt < high), value, sigma)
    return sigma


def _ou_process(length: int, sigma: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Generate a mean-reverting perturbation series.

    An Ornstein-Uhlenbeck process is used rather than white noise so that
    perturbations persist across adjacent minutes. White noise would produce
    1-minute ramps with no temporal structure, which is precisely the artefact
    the design exists to avoid.

    Args:
        length: Number of samples.
        sigma: Per-sample target standard deviation.
        rng: Seeded generator.

    Returns:
        A zero-mean perturbation series of the requested length.
    """
    theta = OU_REVERSION_PER_MIN
    values = np.zeros(length)
    shocks = rng.standard_normal(length)
    for index in range(1, length):
        values[index] = (
            values[index - 1]
            - theta * values[index - 1]
            + np.sqrt(2.0 * theta) * sigma[index] * shocks[index]
        )
    return values


def downscale_to_minute(
    source: pd.DataFrame,
    config: PlantConfig,
    *,
    seed: int,
    enhancement: bool = True,
) -> pd.DataFrame:
    """Downscale a coarse resource series to 1-minute resolution.

    Operates in clear-sky-index space, never on raw irradiance, so that the
    synthetic variability is bounded by physics rather than by an arbitrary
    percentage of the value.

    The final step renormalizes each source interval so its 1-minute mean equals
    the source value exactly. This is asserted, not assumed: without it, the
    downscaled series would drift from the real meteorology it was derived from.

    Args:
        source: Coarse-cadence frame with ``ghi``, ``dni``, ``dhi`` and
            ``solar_zenith``, indexed by UTC timestamp.
        config: Plant configuration, used for the clear-sky reference.
        seed: Seed for the ``weather_downscale`` substream.
        enhancement: Whether to permit brief excursions above clear sky.

    Returns:
        A 1-minute frame with the same columns plus ``clearsky_ghi`` and
        ``clearsky_index``.

    Raises:
        ValueError: If the source index is not a regular timezone-aware series.
    """
    if not isinstance(source.index, pd.DatetimeIndex) or source.index.tz is None:
        raise ValueError("source must have a timezone-aware DatetimeIndex")

    location = location_from_config(config)
    minute_index = pd.date_range(
        source.index[0], source.index[-1], freq="1min", tz=source.index.tz
    )

    clearsky = location.get_clearsky(minute_index, model="ineichen")
    solar_position = location.get_solarposition(minute_index)

    # Clear-sky index at source cadence, then interpolated to minutes. Working
    # in index space means the interpolation cannot produce irradiance that
    # exceeds what the sky geometry allows.
    source_clearsky = location.get_clearsky(source.index, model="ineichen")
    with np.errstate(divide="ignore", invalid="ignore"):
        kt_source = (source["ghi"] / source_clearsky["ghi"]).replace(
            [np.inf, -np.inf], np.nan
        )
    kt_source = kt_source.fillna(0.0).clip(0.0, KT_CEILING)

    kt_minute = kt_source.reindex(minute_index).interpolate("time").ffill().bfill()

    rng = np.random.default_rng(seed)
    sigma = variability_sigma(kt_minute.to_numpy())
    perturbed = kt_minute.to_numpy() + _ou_process(len(minute_index), sigma, rng)

    ceiling = KT_CEILING if enhancement else 1.0
    perturbed = np.clip(perturbed, 0.0, ceiling)

    kt = pd.Series(perturbed, index=minute_index)

    # Renormalize GHI rather than the clear-sky index. Scaling kt does not
    # preserve interval-mean GHI, because clear-sky irradiance varies within an
    # interval and the mean of a product is not the product of the means. The
    # invariant that matters is on energy, so it is enforced on irradiance.
    #
    # Renormalization and the physical ceiling pull against each other at
    # twilight, where clear-sky irradiance approaches zero and the scale factor
    # diverges. Alternating the two to a fixed point satisfies both wherever
    # they are compatible, and leaves a small, reported residual only in the
    # few intervals where the ceiling genuinely binds.
    ghi = _renormalize_bounded(
        kt * clearsky["ghi"], source["ghi"], clearsky["ghi"] * ceiling
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        kt = (ghi / clearsky["ghi"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # Reconstruct the components from the downscaled GHI via Erbs, so that the
    # closure relationship GHI = DHI + DNI cos(zenith) continues to hold.
    components = pvlib.irradiance.erbs(
        ghi, solar_position["apparent_zenith"], minute_index
    )

    result = pd.DataFrame(
        {
            "ghi": ghi,
            "dni": components["dni"],
            "dhi": components["dhi"],
            "clearsky_ghi": clearsky["ghi"],
            "clearsky_index": kt,
            # The deterministic interpolation of the source, before any
            # stochastic perturbation. Layer 3 uses this as its envelope so the
            # advected field is the sole source of sub-interval variability;
            # otherwise a perturbation common to every asset dominates their
            # correlation and suppresses advection lag entirely.
            "kt_envelope": kt_minute,
            "solar_zenith": solar_position["apparent_zenith"],
        },
        index=minute_index,
    )

    for column in ("temp_air", "wind_speed"):
        if column in source.columns:
            result[column] = (
                source[column].reindex(minute_index).interpolate("time").ffill().bfill()
            )
    return result


def _renormalize_bounded(
    minute: pd.Series,
    source: pd.Series,
    ceiling: pd.Series,
    *,
    iterations: int = 8,
) -> pd.Series:
    """Renormalize while respecting a per-sample physical ceiling.

    Scaling an interval to match its source mean can push individual samples
    above what the sky geometry permits, most severely at twilight where the
    clear-sky reference approaches zero and the scale factor diverges. Clipping
    afterwards would then break the mean.

    Alternating the two operations converges quickly wherever they are
    compatible. Where they are not - a source interval whose mean genuinely
    exceeds the clear-sky ceiling - the ceiling wins, because producing
    physically impossible irradiance is worse than a small interval-mean error,
    and the residual is measurable by
    :func:`renormalization_error`.

    Args:
        minute: Perturbed 1-minute series.
        source: The same quantity at source cadence.
        ceiling: Per-sample upper bound.
        iterations: Maximum alternations.

    Returns:
        The bounded, renormalized 1-minute series.
    """
    result = minute
    for _ in range(iterations):
        result = _renormalize(result, source)
        clipped = pd.Series(
            np.minimum(result.to_numpy(), ceiling.to_numpy()), index=result.index
        )
        if np.allclose(clipped.to_numpy(), result.to_numpy(), rtol=0, atol=1e-9):
            return clipped
        result = clipped
    return result


def _renormalize(minute: pd.Series, source: pd.Series) -> pd.Series:
    """Force each source interval's 1-minute mean back onto the source value.

    Scaling is multiplicative, so an interval whose source value is zero stays
    exactly zero and night is preserved rather than acquiring a small positive
    bias.

    Args:
        minute: Perturbed 1-minute series.
        source: The same quantity at source cadence.

    Returns:
        The renormalized 1-minute series.
    """
    interval = source.index[1] - source.index[0]
    groups = minute.index.floor(interval)

    achieved = minute.groupby(groups).transform("mean").to_numpy()
    target = source.reindex(groups).to_numpy()

    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.where(achieved > 1e-12, target / achieved, 0.0)

    return pd.Series(minute.to_numpy() * scale, index=minute.index)


def renormalization_error(minute_frame: pd.DataFrame, source: pd.DataFrame) -> pd.Series:
    """Measure how far each source interval's 1-minute mean drifted.

    Args:
        minute_frame: Downscaled 1-minute frame carrying ``ghi``.
        source: Original coarse frame carrying ``ghi``.

    Returns:
        Absolute GHI error per source interval, in W/m2. The invariant requires
        this to be negligible everywhere.
    """
    interval = source.index[1] - source.index[0]
    grouped = minute_frame["ghi"].groupby(minute_frame.index.floor(interval)).mean()
    aligned = grouped.reindex(source.index)
    return (aligned - source["ghi"]).abs().dropna()
