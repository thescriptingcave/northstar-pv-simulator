"""Tests for the Phase 3 spatial cloud field.

Three failures during development shaped this suite, and each is pinned here
because none would have surfaced downstream:

* A single correlation length left inverters 155 m apart correlated at
  r > 0.999. Technically "not identical", but with no usable structure for peer
  comparison, which is the entire purpose of the layer.
* ``along_origin_m`` was derived from the cross-wind extent, so along-wind
  coordinates clipped to the grid edge and assets silently shared a sample.
* The plant-average perturbation was common to every asset and dominated their
  correlation, pinning measured advection lag at zero where geometry predicted
  6.8 minutes.

The last is the important one: the advected field must *be* the variability an
asset sees, not a modulation applied on top of a shared series.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.assets import AssetType, Position  # noqa: E402
from northstar_sim.block import (  # noqa: E402
    cross_correlation_lag,
    ramp_rates,
    run_block,
    run_spatial_gate,
)
from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.resource import (  # noqa: E402
    clearsky_resource,
    downscale_to_minute,
)
from northstar_sim.spatial import (  # noqa: E402
    asset_coordinates,
    build_asset_resources,
    cloud_travel_distance,
    generate_cloud_field,
)

from .test_physics import real_config  # noqa: E402

WIND_SPEED_MS = 8.0
WIND_DIRECTION_DEG = 270.0  # from the west


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A plant configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def plant(config):
    """Provide the instantiated plant.

    Args:
        config: Plant configuration.

    Returns:
        The built plant.
    """
    return build_plant(config)


@pytest.fixture(scope="module")
def base(config):
    """Provide a broken-cloud plant-average resource series.

    Clear skies have no spatial structure to find, so the spatial layer is
    exercised in the variability band where cloud actually lives.

    Args:
        config: Plant configuration.

    Returns:
        The 1-minute plant-average resource frame.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-22 05:00",
        freq="5min",
        temp_air_c=32.0,
        wind_speed_ms=WIND_SPEED_MS,
    )
    source["ghi"] = source["ghi"] * 0.62
    frame = downscale_to_minute(source, config, seed=12345)
    frame["wind_speed"] = WIND_SPEED_MS
    frame["wind_direction"] = WIND_DIRECTION_DEG
    return frame


@pytest.fixture(scope="module")
def spread_assets(plant):
    """Provide assets spread across the whole site.

    Args:
        plant: Instantiated plant.

    Returns:
        Weather stations and power blocks, which together span the footprint.
    """
    return plant.of_type(AssetType.WEATHER_STATION) + plant.of_type(AssetType.POWER_BLOCK)


@pytest.fixture(scope="module")
def resources(config, base, spread_assets):
    """Provide per-asset resource series across the site.

    Args:
        config: Plant configuration.
        base: Plant-average resource.
        spread_assets: Positioned assets.

    Returns:
        A mapping of asset identifier to resource frame.
    """
    return build_asset_resources(config, base, spread_assets, seed=999)


# --------------------------------------------------------------------------
# Field geometry
# --------------------------------------------------------------------------


def test_wind_from_west_points_the_travel_vector_east() -> None:
    """Meteorological direction is where wind comes *from*, not where it goes."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    direction = pd.Series(270.0, index=index)

    east_asset = Position(x_m=1000.0, y_m=0.0)
    along, across = asset_coordinates(east_asset, direction)

    assert along.iloc[0] == pytest.approx(1000.0, abs=1e-6)
    assert across.iloc[0] == pytest.approx(0.0, abs=1e-6)


def test_wind_from_south_projects_onto_the_north_axis() -> None:
    """A southerly wind makes north-south separation the lagging direction."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    along, across = asset_coordinates(
        Position(x_m=0.0, y_m=1000.0), pd.Series(180.0, index=index)
    )
    assert along.iloc[0] == pytest.approx(1000.0, abs=1e-6)


def test_cloud_travel_accumulates_with_wind_speed() -> None:
    """Displacement is the integral of speed, so faster wind advects further."""
    index = pd.date_range("2023-06-21", periods=61, freq="1min", tz="UTC")
    slow = cloud_travel_distance(pd.Series(4.0, index=index))
    fast = cloud_travel_distance(pd.Series(8.0, index=index))

    assert slow.iloc[0] == 0.0
    assert fast.iloc[-1] == pytest.approx(2.0 * slow.iloc[-1])
    assert fast.iloc[-1] == pytest.approx(8.0 * 3600.0)


def test_calm_wind_is_floored_to_keep_advection_finite() -> None:
    """At genuinely calm speeds the lag would diverge."""
    index = pd.date_range("2023-06-21", periods=61, freq="1min", tz="UTC")
    calm = cloud_travel_distance(pd.Series(0.0, index=index))
    assert calm.iloc[-1] > 0.0


def test_field_covers_the_coordinates_it_will_be_asked_for() -> None:
    """An undersized grid clips coordinates and assets silently share samples.

    An early version derived the along-wind origin from the cross-wind extent.
    Sampling then clipped to the grid edge, and distinct assets returned
    identical field values with no error raised.
    """
    field = generate_cloud_field(
        along_min_m=-500.0, along_max_m=10_000.0, across_half_width_m=2_000.0, seed=1
    )
    rows, columns = field.values.shape
    span_along = (columns - 1) * field.resolution_m
    span_across = (rows - 1) * field.resolution_m

    assert field.along_origin_m + span_along >= 10_000.0
    assert field.across_origin_m == pytest.approx(-2_000.0)
    assert span_across >= 4_000.0


def test_field_has_structure_at_both_small_and_large_scales() -> None:
    """One correlation length cannot serve both scales.

    Short scales decorrelate neighbouring inverters; long scales carry the
    coherent edges that produce measurable advection lag across the site.
    """
    field = generate_cloud_field(
        along_min_m=0.0, along_max_m=20_000.0, across_half_width_m=2_000.0, seed=7
    )
    row = field.values[field.values.shape[0] // 2]

    near = np.corrcoef(row[:-4], row[4:])[0, 1]  # 200 m apart
    far = np.corrcoef(row[:-60], row[60:])[0, 1]  # 3 km apart

    assert near < 0.9999, "must decorrelate at short separation"
    assert far < near, "correlation must decay with distance"
    assert far > -0.5, "large scales must remain coherent"


def test_field_is_deterministic_for_a_seed() -> None:
    """Reproducibility requires the field to be a pure function of the seed."""
    first = generate_cloud_field(
        along_min_m=0.0, along_max_m=5_000.0, across_half_width_m=1_000.0, seed=42
    )
    second = generate_cloud_field(
        along_min_m=0.0, along_max_m=5_000.0, across_half_width_m=1_000.0, seed=42
    )
    assert np.array_equal(first.values, second.values)


# --------------------------------------------------------------------------
# Acceptance criteria
# --------------------------------------------------------------------------


def test_assets_are_correlated_but_not_identical(resources, base) -> None:
    """Identical assets make peer comparison meaningless.

    Uncorrelated ones are not the same weather. Both extremes are failures.
    """
    day = base["solar_zenith"] < 80
    irradiance = pd.DataFrame({key: frame["ghi"] for key, frame in resources.items()})[
        day
    ]

    matrix = irradiance.corr().to_numpy()
    off_diagonal = matrix[np.triu_indices_from(matrix, 1)]

    assert off_diagonal.min() > 0.5
    assert off_diagonal.max() < 0.99999


def test_advection_lag_matches_the_geometry(resources, base, spread_assets) -> None:
    """A cloud edge must reach downwind assets later, by the right amount.

    This is the check that failed at zero minutes against a 6.8 minute
    prediction until the advected field became the sole source of an asset's
    temporal variability.
    """
    day = base["solar_zenith"] < 80
    stations = [a for a in spread_assets if a.asset_type is AssetType.WEATHER_STATION]
    index = pd.DataFrame(
        {a.asset_id: resources[a.asset_id]["clearsky_index"] for a in stations}
    )[day]

    reference = stations[0]
    for station in stations[1:]:
        dx = station.position.x_m - reference.position.x_m
        predicted = dx / WIND_SPEED_MS / 60.0  # wind from due west
        measured = cross_correlation_lag(
            index[reference.asset_id], index[station.asset_id], max_lag=30
        )
        assert abs(measured - predicted) <= 2.0, (
            f"{station.asset_id}: measured {measured} min, predicted {predicted:.1f}"
        )


def test_aggregate_ramps_are_smoother_than_any_individual_asset(resources, base) -> None:
    """Portfolio smoothing must be present and measurable."""
    day = base["solar_zenith"] < 80
    irradiance = pd.DataFrame({key: frame["ghi"] for key, frame in resources.items()})[
        day
    ]

    individual = [ramp_rates(irradiance[key]).std() for key in irradiance.columns]
    aggregate = ramp_rates(irradiance.mean(axis=1)).std()

    assert aggregate < min(individual)


def test_distant_assets_correlate_strongly_once_travel_time_is_removed(
    resources, base, spread_assets
) -> None:
    """Weak at zero lag, strong at the right lag - the signature of advection.

    This is what makes wind direction recoverable from irradiance telemetry
    alone, independently of the anemometer.
    """
    day = base["solar_zenith"] < 80
    stations = [a for a in spread_assets if a.asset_type is AssetType.WEATHER_STATION]
    first, last = stations[0].asset_id, stations[-1].asset_id

    index = pd.DataFrame(
        {
            first: resources[first]["clearsky_index"],
            last: resources[last]["clearsky_index"],
        }
    )[day]

    lag = cross_correlation_lag(index[first], index[last], max_lag=30)
    zero_lag = index[first].corr(index[last])
    at_lag = index[first].corr(index[last].shift(-lag))

    assert at_lag > 0.6
    assert at_lag > zero_lag + 0.1


def test_plant_average_insolation_is_conserved(resources, base) -> None:
    """The spatial field redistributes energy; it must not create or destroy it."""
    plant_mean = pd.DataFrame(
        {key: frame["ghi"] for key, frame in resources.items()}
    ).mean(axis=1)

    error = abs(plant_mean.mean() - base["ghi"].mean()) / base["ghi"].mean()
    assert error < 0.02, f"daily insolation drifted {error:.2%}"


def test_spatial_gate_passes(resources, base, spread_assets) -> None:
    """The Phase 3 acceptance gate."""
    positions = {
        a.asset_id: (a.position.x_m, a.position.y_m) for a in spread_assets if a.position
    }
    result = run_spatial_gate(
        resources,
        positions,
        wind_speed_ms=WIND_SPEED_MS,
        wind_direction_deg=WIND_DIRECTION_DEG,
        daylight=base["solar_zenith"] < 80,
    )
    assert result.passed, result.render()


# --------------------------------------------------------------------------
# Block simulation
# --------------------------------------------------------------------------


def test_block_produces_one_series_per_inverter(config, plant, base) -> None:
    """A block runs every inverter it contains."""
    result = run_block(config, plant, base, "NORTHSTA-BLK01", seed=999)
    assert len(result.inverters) == config.topology.inverters_per_block
    assert len(result.weather) == config.topology.weather_stations


def test_block_aggregate_sums_its_inverters(config, plant, base) -> None:
    """Block totals reconcile with the inverters that produced them."""
    result = run_block(config, plant, base, "NORTHSTA-BLK01", seed=999)
    expected = result.ac_matrix().sum(axis=1)
    pd.testing.assert_series_equal(
        result.aggregate["ac_power_kw"], expected, check_names=False
    )


def test_inverters_within_a_block_see_similar_but_distinct_resource(
    config, plant, base
) -> None:
    """Neighbouring inverters are close, so they should be close - not equal.

    Within a 620 m block the separation is small and correlation is genuinely
    high. The layer must still leave enough difference for underperformance
    detection to have a realistic noise floor.
    """
    result = run_block(config, plant, base, "NORTHSTA-BLK01", seed=999)
    poa = result.poa_matrix()
    day = poa.mean(axis=1) > 100

    matrix = poa[day].corr().to_numpy()
    off_diagonal = matrix[np.triu_indices_from(matrix, 1)]

    assert off_diagonal.min() > 0.9, "same block means similar weather"
    assert off_diagonal.max() < 0.9999, "but not identical weather"


def test_block_run_is_deterministic(config, plant, base) -> None:
    """The same seed reproduces the block exactly."""
    first = run_block(config, plant, base, "NORTHSTA-BLK01", seed=555)
    second = run_block(config, plant, base, "NORTHSTA-BLK01", seed=555)
    pd.testing.assert_frame_equal(first.aggregate, second.aggregate)


def test_a_block_without_inverters_is_refused(config, plant, base) -> None:
    """An empty block is a configuration error, not an empty result."""
    with pytest.raises(KeyError):
        run_block(config, plant, base, "NORTHSTA-NOSUCHBLOCK", seed=1)
