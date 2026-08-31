"""Run a power block: several inverters over a shared, advected cloud field.

Phase 3 expands the model from one inverter to one block. That expansion is the
first point at which peer comparison means anything, because it is the first
point at which two assets see genuinely different irradiance.

The block runner also produces the three weather stations' series. In Phase 3
those stations disagree purely because they occupy different positions in the
cloud field. Instrument bias, drift and noise arrive in Phase 6 and are a
separate cause of disagreement, layered on top of this one.

Reference: design document ``16_implementation_roadmap`` section 6.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .assets import Asset, AssetType, Plant
from .physics import run_inverter_chain
from .plant_config import PlantConfig
from .spatial import build_asset_resources

#: Temporal smoothing per asset class, in minutes, representing the ground each
#: asset averages over. A pyranometer is effectively a point; a power block
#: spans hundreds of metres and smooths correspondingly.
FOOTPRINT_SMOOTHING_MINUTES: dict[str, float] = {
    AssetType.WEATHER_STATION.value: 0.0,
    AssetType.INVERTER.value: 2.0,
    AssetType.COMBINER.value: 1.0,
    AssetType.POWER_BLOCK.value: 4.0,
}


@dataclass
class BlockResult:
    """Output of a block simulation.

    Attributes:
        inverters: Per-inverter production frames, keyed by asset identifier.
        weather: Per-weather-station resource frames.
        aggregate: Block totals at 1-minute cadence.
    """

    inverters: dict[str, pd.DataFrame]
    weather: dict[str, pd.DataFrame]
    aggregate: pd.DataFrame

    @property
    def inverter_ids(self) -> list[str]:
        """Identifiers of the simulated inverters.

        Returns:
            Sorted identifiers.
        """
        return sorted(self.inverters)

    def ac_matrix(self) -> pd.DataFrame:
        """Assemble per-inverter AC power into one frame.

        Returns:
            A frame with one column per inverter, indexed by time.
        """
        return pd.DataFrame(
            {key: frame["ac_power_kw"] for key, frame in self.inverters.items()}
        )

    def poa_matrix(self) -> pd.DataFrame:
        """Assemble per-inverter plane-of-array irradiance into one frame.

        Returns:
            A frame with one column per inverter, indexed by time.
        """
        return pd.DataFrame(
            {key: frame["poa_global"] for key, frame in self.inverters.items()}
        )


def run_block(
    config: PlantConfig,
    plant: Plant,
    base_weather: pd.DataFrame,
    block_id: str,
    *,
    seed: int,
) -> BlockResult:
    """Simulate one power block over a shared cloud field.

    Every inverter in the block, and every plant weather station, samples the
    same advected field at its own position. That single mechanism produces
    lagged correlation between inverters, aggregate smoothing, and
    weather-station disagreement, with no separate code for any of them.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.
        base_weather: Plant-average 1-minute resource series.
        block_id: Power block to simulate.
        seed: Seed for the ``cloud_field`` substream.

    Returns:
        The :class:`BlockResult`.

    Raises:
        KeyError: If the block does not exist.
        ValueError: If the block contains no inverters.
    """
    block = plant.get(block_id)
    inverters = [
        asset
        for asset in plant.children(block_id)
        if asset.asset_type is AssetType.INVERTER
    ]
    if not inverters:
        raise ValueError(f"{block_id} has no inverters")

    stations = plant.of_type(AssetType.WEATHER_STATION)
    positioned: list[Asset] = [*inverters, *stations, block]

    resources = build_asset_resources(
        config,
        base_weather,
        positioned,
        seed=seed,
        footprint_smoothing=FOOTPRINT_SMOOTHING_MINUTES,
    )

    inverter_results = {
        asset.asset_id: run_inverter_chain(config, resources[asset.asset_id])
        for asset in inverters
    }
    weather_results = {
        station.asset_id: resources[station.asset_id] for station in stations
    }

    return BlockResult(
        inverters=inverter_results,
        weather=weather_results,
        aggregate=_aggregate(inverter_results, resources[block_id]),
    )


def _aggregate(
    inverters: dict[str, pd.DataFrame], block_resource: pd.DataFrame
) -> pd.DataFrame:
    """Sum inverter output to block totals.

    Args:
        inverters: Per-inverter production frames.
        block_resource: The block's own resource series, used for the
            block-representative irradiance an operator would report.

    Returns:
        A frame of block totals indexed by time.
    """
    ac = sum(frame["ac_power_kw"] for frame in inverters.values())
    dc = sum(frame["dc_power_kw"] for frame in inverters.values())
    poa = pd.concat([frame["poa_global"] for frame in inverters.values()], axis=1).mean(
        axis=1
    )

    return pd.DataFrame(
        {
            "ac_power_kw": ac,
            "dc_power_kw": dc,
            "poa_global_mean": poa,
            "poa_global_block": block_resource["ghi"],
            "active_inverters": len(inverters),
        }
    )


def cross_correlation_lag(left: pd.Series, right: pd.Series, *, max_lag: int = 30) -> int:
    """Find the lag at which two series correlate most strongly.

    Used to verify that irradiance reaches downwind assets later than upwind
    ones, and that the delay is consistent with the wind actually blowing.

    Args:
        left: Reference series.
        right: Series to shift.
        max_lag: Maximum shift to test, in samples.

    Returns:
        The lag in samples maximising correlation. Positive means ``right``
        lags ``left``.
    """
    best_lag, best_score = 0, -np.inf
    for lag in range(-max_lag, max_lag + 1):
        score = left.corr(right.shift(-lag))
        if pd.notna(score) and score > best_score:
            best_lag, best_score = lag, score
    return best_lag


def ramp_rates(series: pd.Series) -> pd.Series:
    """Compute per-sample change, the quantity ramp analysis operates on.

    Args:
        series: A power or irradiance series.

    Returns:
        First differences, with the leading NaN dropped.
    """
    return series.diff().dropna()


@dataclass
class SpatialGateResult:
    """Outcome of the Phase 3 spatial acceptance checks.

    Attributes:
        checks: Named check outcomes, each a tuple of pass flag and detail.
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


def run_spatial_gate(
    resources: dict[str, pd.DataFrame],
    positions: dict[str, tuple[float, float]],
    *,
    wind_speed_ms: float,
    wind_direction_deg: float,
    daylight: pd.Series,
) -> SpatialGateResult:
    """Verify the spatial layer meets its acceptance criteria.

    Checks the four properties design document ``16`` section 6 requires, plus
    the lag-versus-geometry agreement that proves advection is real rather than
    decorative.

    Args:
        resources: Per-asset resource frames, keyed by asset identifier.
        positions: Asset positions in the site frame, keyed by identifier.
        wind_speed_ms: Prevailing wind speed used for the predicted lag.
        wind_direction_deg: Prevailing wind direction, degrees from north.
        daylight: Boolean mask selecting daylight samples.

    Returns:
        A :class:`SpatialGateResult`.
    """
    keys = sorted(resources)
    index = pd.DataFrame({key: resources[key]["clearsky_index"] for key in keys})[
        daylight
    ]
    irradiance = pd.DataFrame({key: resources[key]["ghi"] for key in keys})[daylight]

    checks: list[tuple[str, bool, str]] = []

    # Correlated but not identical: identical assets make peer comparison
    # meaningless, uncorrelated ones are not the same weather.
    matrix = irradiance.corr().to_numpy()
    off_diagonal = matrix[np.triu_indices_from(matrix, 1)]
    low, high = float(off_diagonal.min()), float(off_diagonal.max())
    checks.append(
        (
            "correlated_not_identical",
            low > 0.5 and high < 0.99999,
            f"GHI correlation {low:.4f} .. {high:.4f}",
        )
    )

    # Advection lag must match the geometry, not merely be non-zero.
    radians = np.radians(wind_direction_deg)
    east, north = -np.sin(radians), -np.cos(radians)
    reference = keys[0]
    errors = []
    for key in keys[1:]:
        dx = positions[key][0] - positions[reference][0]
        dy = positions[key][1] - positions[reference][1]
        predicted = (dx * east + dy * north) / wind_speed_ms / 60.0
        measured = cross_correlation_lag(index[reference], index[key], max_lag=30)
        errors.append(abs(measured - predicted))
    worst = max(errors) if errors else 0.0
    checks.append(
        (
            "advection_lag_matches",
            worst <= 2.0,
            f"worst lag error {worst:.1f} min against geometry",
        )
    )

    # Aggregate smoothing: the mean of many assets must ramp less sharply than
    # any one of them, or portfolio smoothing analysis has nothing to find.
    individual = np.mean([ramp_rates(irradiance[key]).std() for key in keys])
    aggregate = ramp_rates(irradiance.mean(axis=1)).std()
    ratio = float(aggregate / individual) if individual else 1.0
    checks.append(
        (
            "aggregate_smoothing",
            ratio < 0.98,
            f"ramp std ratio {ratio:.4f} (aggregate / individual)",
        )
    )

    # Distant assets must correlate strongly once cloud travel time is removed.
    # Weak zero-lag correlation with strong lagged correlation is the signature
    # that makes wind direction recoverable from telemetry alone.
    furthest = max(
        keys[1:],
        key=lambda k: np.hypot(
            positions[k][0] - positions[reference][0],
            positions[k][1] - positions[reference][1],
        ),
    )
    lag = cross_correlation_lag(index[reference], index[furthest], max_lag=30)
    lagged = float(index[reference].corr(index[furthest].shift(-lag)))
    zero = float(index[reference].corr(index[furthest]))
    checks.append(
        (
            "lagged_correlation_recovers",
            lagged > zero + 0.1 and lagged > 0.6,
            f"zero-lag {zero:.3f} -> {lagged:.3f} at {lag:+d} min",
        )
    )

    return SpatialGateResult(checks=checks)
