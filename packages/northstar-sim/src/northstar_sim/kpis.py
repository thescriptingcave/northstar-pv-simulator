"""Normative KPI implementations.

Design document ``20`` makes these definitions normative because "availability"
alone has at least four defensible meanings, and two analysts computing it from
the same data will disagree by percentage points without a written rule.

Where this module and IEC 61724-1 differ, the standard governs and this module
is a bug.

Three rules here matter more than the formulas:

* **Never average ratios.** ``mean(monthly PR)`` is not ``annual PR``. Recompute
  from summed numerator and denominator.
* **Availability is energy-weighted.** An inverter that fails at 2 a.m. and is
  restored at 5 a.m. loses three hours of time-based availability and zero
  energy. Only the energy-weighted figure reflects what happened.
* **Temperature correction is mandatory for seasonal comparison.** A West Texas
  plant shows summer PR several points below winter PR with no change in health
  whatsoever.

Reference: design document ``20_kpi_definitions``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .plant_config import PlantConfig

#: Reference cell temperature for the STC convention, degrees Celsius.
T_REF_C = 25.0

#: Reference irradiance, W/m2.
G_STC = 1000.0

#: Intervals below this irradiance are excluded from performance ratio. The
#: ratio is numerically unstable near zero and the result is meaningless.
PR_MIN_IRRADIANCE_WM2 = 50.0

#: Solar zenith above which an interval is excluded from performance ratio.
PR_MAX_ZENITH_DEG = 85.0

#: Weather-station spread above which resource measurement is treated as
#: unreliable and the interval is filtered out.
PR_MAX_STATION_SPREAD = 0.08


@dataclass
class PerformanceMetrics:
    """Computed plant performance figures.

    Attributes:
        reference_yield_h: Insolation expressed as equivalent hours at STC.
        array_yield_h: DC energy per unit installed DC capacity.
        final_yield_h: Exported energy per unit installed DC capacity.
        performance_ratio: Final yield over reference yield.
        performance_ratio_corrected: Temperature-corrected performance ratio.
        capacity_factor_ac: Exported energy over AC nameplate times hours.
        capture_rate: Generation-weighted over time-weighted price.
        filtered_intervals: Intervals excluded by the filter rules.
        total_intervals: Intervals considered.
    """

    reference_yield_h: float
    array_yield_h: float
    final_yield_h: float
    performance_ratio: float
    performance_ratio_corrected: float
    capacity_factor_ac: float
    capture_rate: float
    filtered_intervals: int
    total_intervals: int


@dataclass
class AvailabilityMetrics:
    """The four availability definitions, all valid and all different.

    Attributes:
        time_based: Uptime over elapsed time. Largely meaningless for solar.
        daylight_weighted: Uptime over daylight time.
        energy_weighted: Actual energy over actual plus lost energy. The
            contractual definition.
        contractual: Energy-weighted with excluded causes removed.
    """

    time_based: float
    daylight_weighted: float
    energy_weighted: float
    contractual: float


def performance_filter(
    poa_global: pd.Series,
    solar_zenith: pd.Series,
    station_spread: pd.Series | None = None,
    curtailed: pd.Series | None = None,
) -> pd.Series:
    """Select intervals eligible for performance ratio calculation.

    These filters are **normative, not advisory**. They are the single most
    common source of disagreement between two analysts computing PR from the
    same data, so every reported figure must record the filter set applied.

    Args:
        poa_global: Plane-of-array irradiance.
        solar_zenith: Solar zenith angle.
        station_spread: Relative disagreement between weather stations.
        curtailed: Curtailed power, used to exclude commercial reductions.

    Returns:
        ``True`` where the interval may be used.
    """
    eligible = (poa_global >= PR_MIN_IRRADIANCE_WM2) & (solar_zenith <= PR_MAX_ZENITH_DEG)
    if station_spread is not None:
        eligible &= station_spread.reindex(eligible.index).fillna(0.0) <= (
            PR_MAX_STATION_SPREAD
        )
    if curtailed is not None:
        # A curtailed interval is not a performance shortfall. Including it
        # penalises the plant for a commercial decision.
        eligible &= curtailed.reindex(eligible.index).fillna(0.0) <= 0.0
    return eligible


def performance_metrics(
    config: PlantConfig,
    poa_global: pd.Series,
    cell_temperature: pd.Series,
    dc_power_kw: pd.Series,
    export_kw: pd.Series,
    solar_zenith: pd.Series,
    *,
    node_price: pd.Series | None = None,
    station_spread: pd.Series | None = None,
    curtailed: pd.Series | None = None,
) -> PerformanceMetrics:
    """Compute IEC 61724-1 yields and performance ratios.

    Args:
        config: Plant configuration.
        poa_global: Plane-of-array irradiance.
        cell_temperature: Cell temperature.
        dc_power_kw: Plant DC power.
        export_kw: Metered export power.
        solar_zenith: Solar zenith angle.
        node_price: Optional prices, enabling capture rate.
        station_spread: Optional weather-station disagreement.
        curtailed: Optional curtailed power.

    Returns:
        The computed :class:`PerformanceMetrics`.
    """
    from .market import capture_rate

    interval_hours = _interval_hours(poa_global.index)
    eligible = performance_filter(poa_global, solar_zenith, station_spread, curtailed)

    insolation = float((poa_global[eligible] * interval_hours).sum()) / G_STC
    dc_energy = float((dc_power_kw[eligible] * interval_hours).sum())
    export_energy = float((export_kw[eligible] * interval_hours).sum())

    reference_yield = insolation
    array_yield = dc_energy / config.plant_dc_kw if config.plant_dc_kw else 0.0
    final_yield = export_energy / config.plant_dc_kw if config.plant_dc_kw else 0.0

    ratio = final_yield / reference_yield if reference_yield else 0.0

    # Temperature-corrected PR. Without this, seasonal comparison is invalid:
    # the same plant reads several points lower in summer purely because cells
    # are hotter.
    correction = 1.0 + config.module.temp_coeff_pmax_per_c * (
        cell_temperature[eligible] - T_REF_C
    )
    corrected_denominator = float(
        (
            config.plant_dc_kw
            * (poa_global[eligible] / G_STC)
            * correction
            * interval_hours
        ).sum()
    )
    corrected = export_energy / corrected_denominator if corrected_denominator else 0.0

    total_hours = len(export_kw) * interval_hours
    capacity_factor = (
        float((export_kw * interval_hours).sum()) / (config.plant_ac_kw * total_hours)
        if total_hours
        else 0.0
    )

    return PerformanceMetrics(
        reference_yield_h=reference_yield,
        array_yield_h=array_yield,
        final_yield_h=final_yield,
        performance_ratio=ratio,
        performance_ratio_corrected=corrected,
        capacity_factor_ac=capacity_factor,
        capture_rate=(
            capture_rate(export_kw, node_price) if node_price is not None else 0.0
        ),
        filtered_intervals=int((~eligible).sum()),
        total_intervals=int(len(eligible)),
    )


def availability_metrics(
    export_kw: pd.Series,
    solar_zenith: pd.Series,
    lost_energy_kw: pd.Series,
    excluded_kw: pd.Series | None = None,
) -> AvailabilityMetrics:
    """Compute all four availability definitions.

    They are all valid and all different. Reporting one without saying which is
    the most common way availability figures become unfalsifiable.

    Args:
        export_kw: Metered export power.
        solar_zenith: Solar zenith angle.
        lost_energy_kw: Power lost to unavailability.
        excluded_kw: Portion of the loss excluded by contract, such as grid
            outages and planned maintenance.

    Returns:
        The computed :class:`AvailabilityMetrics`.
    """
    interval_hours = _interval_hours(export_kw.index)
    daylight = solar_zenith <= PR_MAX_ZENITH_DEG

    unavailable = lost_energy_kw > 0.0
    total_intervals = len(export_kw)
    daylight_intervals = int(daylight.sum())

    time_based = (
        1.0 - float(unavailable.sum()) / total_intervals if total_intervals else 1.0
    )
    daylight_weighted = (
        1.0 - float((unavailable & daylight).sum()) / daylight_intervals
        if daylight_intervals
        else 1.0
    )

    actual = float((export_kw.clip(lower=0.0) * interval_hours).sum())
    lost = float((lost_energy_kw.clip(lower=0.0) * interval_hours).sum())
    energy_weighted = actual / (actual + lost) if (actual + lost) > 0 else 1.0

    if excluded_kw is not None:
        excluded = float((excluded_kw.clip(lower=0.0) * interval_hours).sum())
        counted = max(0.0, lost - excluded)
    else:
        counted = lost
    contractual = actual / (actual + counted) if (actual + counted) > 0 else 1.0

    return AvailabilityMetrics(
        time_based=time_based,
        daylight_weighted=daylight_weighted,
        energy_weighted=energy_weighted,
        contractual=contractual,
    )


def guarantee_position(
    availability: AvailabilityMetrics,
    metrics: PerformanceMetrics,
    terms,
    *,
    annual_expected_mwh: float,
) -> dict[str, float]:
    """Compute the position against contractual guarantees.

    Args:
        availability: Availability figures.
        metrics: Performance figures.
        terms: Commercial terms carrying the guaranteed levels.
        annual_expected_mwh: Expected annual energy, the damages basis.

    Returns:
        Shortfalls and liquidated damages in dollars.
    """
    availability_shortfall = max(
        0.0, terms.availability_guarantee - availability.contractual
    )
    pr_shortfall = max(0.0, terms.pr_guarantee - metrics.performance_ratio_corrected)

    return {
        "availability_actual": availability.contractual,
        "availability_shortfall": availability_shortfall,
        "availability_ld_usd": availability_shortfall
        * annual_expected_mwh
        * terms.hedge_strike_usd_mwh,
        "pr_actual": metrics.performance_ratio_corrected,
        "pr_shortfall": pr_shortfall,
        "pr_ld_usd": (
            pr_shortfall
            / terms.pr_guarantee
            * annual_expected_mwh
            * terms.hedge_strike_usd_mwh
            if terms.pr_guarantee
            else 0.0
        ),
    }


def _interval_hours(index: pd.DatetimeIndex) -> float:
    """Determine the sampling interval in hours.

    Args:
        index: A regular time index.

    Returns:
        Interval width in hours.
    """
    if len(index) < 2:
        return 1.0 / 60.0
    return (index[1] - index[0]).total_seconds() / 3600.0
