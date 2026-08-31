"""Tests for the Phase 4 full-plant scale-out.

Three findings shaped this suite:

* Rear irradiance was computed with the *front* surface orientation, so
  ``infinite_sheds`` returned front-side irradiance and 70% of it was added
  back as bifacial gain. Peak DC reached 155% of nameplate.
* Transformer output was clipped at zero, discarding overnight no-load loss.
  The loss chain then failed to close by 0.14% of daily export - small enough
  to read as rounding, large enough to corrupt loss attribution.
* Solar position, tracking and the bifacial view factors were recomputed per
  inverter despite being identical across the site, costing 17x more than
  necessary and putting a simulated year over its throughput budget.
"""

from __future__ import annotations

import time
import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.physics import (  # noqa: E402
    build_site_geometry,
    compute_rear_irradiance,
    run_inverter_chain,
)
from northstar_sim.plant_run import (  # noqa: E402
    collection_loss_kw,
    energy_mwh,
    run_plant,
    run_plant_gate,
    transformer_response,
)
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402

from .test_physics import real_config  # noqa: E402


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


def _weather(config, *, kt: float, temp_air_c: float = 33.0):
    """Build a plant-average resource series scaled to a clear-sky index.

    Args:
        config: Plant configuration.
        kt: Clear-sky index to scale the source to.
        temp_air_c: Constant ambient temperature.

    Returns:
        A 1-minute resource frame with wind fields populated.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-22 05:00",
        freq="5min",
        temp_air_c=temp_air_c,
        wind_speed_ms=7.0,
    )
    source["ghi"] = source["ghi"] * kt
    frame = downscale_to_minute(source, config, seed=12345)
    frame["wind_speed"] = 7.0
    frame["wind_direction"] = 250.0
    return frame


@pytest.fixture(scope="module")
def clear_run(config, plant):
    """Provide a full-plant run on a clear day.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.

    Returns:
        The run result.
    """
    return run_plant(config, plant, _weather(config, kt=1.0), seed=999)


@pytest.fixture(scope="module")
def cloudy_run(config, plant):
    """Provide a full-plant run on a broken-cloud day.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.

    Returns:
        The run result.
    """
    return run_plant(config, plant, _weather(config, kt=0.62), seed=999)


# --------------------------------------------------------------------------
# Bifacial rear irradiance
# --------------------------------------------------------------------------


def test_rear_irradiance_is_a_small_fraction_of_front(config) -> None:
    """Rear gain is real but modest; front-magnitude rear means wrong geometry.

    Passing the front surface orientation to the infinite-sheds model returns
    front-side irradiance. Adding 70% of that back drove peak DC to 155% of
    nameplate.
    """
    weather = _weather(config, kt=1.0)
    geometry = build_site_geometry(config, weather)
    rear = compute_rear_irradiance(
        config, weather, geometry.solar_position, geometry.surface_tilt
    )
    front = run_inverter_chain(config, weather, geometry=geometry)["poa_global"]

    daylight = front > 200
    ratio = (rear[daylight] / front[daylight]).mean()
    assert 0.02 < ratio < 0.30, f"rear/front ratio {ratio:.3f} is not plausible"


def test_clear_sky_peak_dc_is_bounded_before_and_after_plant_losses(
    config, clear_run
) -> None:
    """Bifacial gain lifts ideal DC above nameplate; plant losses pull it back.

    Both bounds matter. Ideal DC exceeding nameplate confirms bifacial gain is
    present and sane - 155% was the symptom of passing front-surface geometry
    to the rear irradiance model. Delivered DC sitting below nameplate confirms
    degradation, mismatch and wiring losses are actually applied.
    """
    ideal = sum(frame["dc_ideal_kw"] for frame in clear_run.inverters.values()).max()
    delivered = clear_run.plant["total_dc_power_kw"].max()

    assert 1.0 <= ideal / config.plant_dc_kw <= 1.12
    assert delivered < ideal
    assert 0.92 < delivered / config.plant_dc_kw < 1.0


# --------------------------------------------------------------------------
# Transformer
# --------------------------------------------------------------------------


def test_transformer_losses_are_load_dependent(config) -> None:
    """A constant efficiency would erase transformer health monitoring."""
    index = pd.date_range("2023-06-21", periods=10, freq="1min", tz="UTC")
    ambient = pd.Series(25.0, index=index)

    low = transformer_response(pd.Series(2_000.0, index=index), config, ambient)
    high = transformer_response(pd.Series(10_000.0, index=index), config, ambient)

    assert high["load_loss_kw"].iloc[-1] > low["load_loss_kw"].iloc[-1]
    assert high["efficiency_pct"].iloc[-1] > low["efficiency_pct"].iloc[-1]


def test_transformer_thermal_lag_matches_its_time_constant(config) -> None:
    """Winding temperature must lag loading by the configured constant.

    Measured with a step input, because a real day's load plateaus under
    clipping and the peak then falls at the end of the plateau rather than one
    time constant after it.
    """
    index = pd.date_range("2023-06-21", periods=400, freq="1min", tz="UTC")
    step = pd.Series(np.where(np.arange(400) < 100, 0.0, 10_000.0), index=index)
    response = transformer_response(step, config, pd.Series(25.0, index=index))

    winding = response["winding_temp_c"]
    initial, final = winding.iloc[99], winding.iloc[-1]
    target = initial + 0.632 * (final - initial)
    reached = winding[100:][winding[100:] >= target].index[0]
    minutes = (reached - index[100]).total_seconds() / 60.0

    expected = config.transformer.thermal_time_constant_min
    assert abs(minutes - expected) < 0.1 * expected


def test_transformer_draws_no_load_loss_in_darkness(config) -> None:
    """An energised transformer consumes even with no generation.

    Clipping output at zero instead discards that energy and breaks the loss
    chain by roughly 0.14% of daily export.
    """
    index = pd.date_range("2023-06-21", periods=10, freq="1min", tz="UTC")
    response = transformer_response(
        pd.Series(0.0, index=index), config, pd.Series(25.0, index=index)
    )
    assert (response["output_power_kw"] < 0).all()
    assert response["output_power_kw"].iloc[0] == pytest.approx(
        -config.transformer.no_load_loss_kw
    )


def test_collection_loss_scales_with_the_square_of_loading(config) -> None:
    """Halving the flow quarters the resistive loss."""
    rated = config.plant_ac_kw
    full = collection_loss_kw(pd.Series([rated]), rated).iloc[0]
    half = collection_loss_kw(pd.Series([rated / 2]), rated).iloc[0]
    assert half == pytest.approx(full / 4.0)


# --------------------------------------------------------------------------
# Full plant
# --------------------------------------------------------------------------


def test_every_inverter_and_block_is_simulated(config, clear_run) -> None:
    """The full reference plant runs, not a subset."""
    assert len(clear_run.inverters) == config.total_inverters
    assert len(clear_run.blocks) == config.topology.power_blocks
    assert len(clear_run.transformers) == config.topology.power_blocks


def test_hierarchy_reconciles_from_inverters_to_plant(clear_run) -> None:
    """Block totals are the sum of their inverters, exactly."""
    inverter_sum = clear_run.ac_matrix().sum(axis=1)
    block_sum = sum(frame["inverter_ac_power_kw"] for frame in clear_run.blocks.values())
    assert (inverter_sum - block_sum).abs().max() < 1e-6


def test_energy_chain_closes_to_machine_precision(clear_run) -> None:
    """Export equals generation less every accounted loss."""
    frame = clear_run.plant
    residual = energy_mwh(
        frame["total_inverter_ac_power_kw"]
        - frame["transformer_loss_kw"]
        - frame["collection_loss_kw"]
        - frame["poi_limited_kw"]
        - frame["grid_export_power_kw"]
    )
    assert abs(residual) < 1e-9


def test_inverter_ac_never_exceeds_plant_nameplate(config, clear_run) -> None:
    """Clipping binds at the aggregate as it does at each inverter."""
    peak = clear_run.plant["total_inverter_ac_power_kw"].max()
    assert peak <= config.plant_ac_kw * 1.001


def test_blocks_are_correlated_but_distinguishable(config, cloudy_run) -> None:
    """Identical blocks make block-level underperformance undetectable."""
    blocks = cloudy_run.block_matrix()
    daylight = blocks.mean(axis=1) > 0.02 * config.plant_ac_kw
    matrix = blocks[daylight].corr().to_numpy()
    off_diagonal = matrix[np.triu_indices_from(matrix, 1)]

    assert off_diagonal.min() > 0.85
    assert off_diagonal.max() < 0.99999


def test_fleet_output_is_smoother_than_any_single_inverter(cloudy_run) -> None:
    """Forty assets over 3.26 km must smooth in aggregate."""
    per_inverter = cloudy_run.ac_matrix()
    individual = np.mean(
        [per_inverter[c].diff().dropna().std() for c in per_inverter.columns]
    )
    aggregate = (
        (cloudy_run.plant["grid_export_power_kw"] / len(per_inverter.columns))
        .diff()
        .dropna()
        .std()
    )
    assert aggregate < individual


def test_clear_day_capacity_factor_is_plausible(config, clear_run) -> None:
    """A June solstice clear day is the best day of the year, not an absurd one."""
    export = energy_mwh(clear_run.plant["grid_export_power_kw"])
    capacity_factor = export / (config.plant_ac_kw / 1000.0 * 24.0)
    assert 0.35 < capacity_factor < 0.55


def test_run_is_deterministic(config, plant) -> None:
    """The same seed reproduces the whole plant exactly."""
    weather = _weather(config, kt=0.8)
    first = run_plant(config, plant, weather, seed=321)
    second = run_plant(config, plant, weather, seed=321)
    pd.testing.assert_frame_equal(first.plant, second.plant)


def test_shared_geometry_matches_per_inverter_computation(config) -> None:
    """The optimisation must not change results, only their cost."""
    weather = _weather(config, kt=0.9)
    geometry = build_site_geometry(config, weather)
    rear = compute_rear_irradiance(
        config, weather, geometry.solar_position, geometry.surface_tilt
    )

    shared = run_inverter_chain(config, weather, geometry=geometry, rear_irradiance=rear)
    standalone = run_inverter_chain(config, weather)
    pd.testing.assert_frame_equal(shared, standalone)


def test_throughput_meets_the_annual_budget(config, plant) -> None:
    """A simulated year must fit the wall-clock budget in doc 14 section 4.1."""
    weather = _weather(config, kt=0.85)
    started = time.perf_counter()
    result = run_plant(config, plant, weather, seed=999)
    elapsed = time.perf_counter() - started

    gate = run_plant_gate(config, plant, result, seconds_per_day=elapsed)
    projected = elapsed * 365.0 / 60.0
    assert projected < 60.0, f"projected {projected:.1f} min per simulated year"
    assert gate.passed, gate.render()


def test_plant_gate_passes(config, plant, cloudy_run) -> None:
    """The Phase 4 acceptance gate."""
    gate = run_plant_gate(config, plant, cloudy_run, seconds_per_day=2.5)
    assert gate.passed, gate.render()
