"""Tests for the Phase 6 sensor layer.

The governing constraint is that **a sensor fault must never alter physical
truth**. A stuck pyranometer reports a constant while actual production
continues to vary. That only holds if measurement is a pure function applied
*to* truth rather than a modification *of* it, so several tests here check
object identity and immutability rather than numeric values.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.plant_run import run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402
from northstar_sim.sensors import (  # noqa: E402
    SensorSpec,
    apply_sensor,
    build_sensor_fleet,
    measure_frame,
    run_sensor_gate,
    station_spread,
)

from .test_physics import real_config  # noqa: E402


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A plant configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def base(config):
    """Provide a plant-average resource series.

    Args:
        config: Plant configuration.

    Returns:
        A 1-minute resource frame.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-22 05:00",
        freq="5min",
        temp_air_c=33.0,
        wind_speed_ms=7.0,
    )
    frame = downscale_to_minute(source, config, seed=12345)
    frame["wind_speed"] = 7.0
    frame["wind_direction"] = 250.0
    return frame


@pytest.fixture(scope="module")
def run(config, base):
    """Provide a full-plant run including the sensor layer.

    Args:
        config: Plant configuration.
        base: Plant-average resource.

    Returns:
        The run result.
    """
    return run_plant(config, build_plant(config), base, seed=999)


def _series(values: list[float], *, minutes: int = 1) -> pd.Series:
    """Build a time-indexed series.

    Args:
        values: Sample values.
        minutes: Sampling interval.

    Returns:
        A UTC-indexed series.
    """
    index = pd.date_range(
        "2023-06-21", periods=len(values), freq=f"{minutes}min", tz="UTC"
    )
    return pd.Series(values, index=index)


# --------------------------------------------------------------------------
# The non-negotiable property
# --------------------------------------------------------------------------


def test_measurement_does_not_mutate_truth() -> None:
    """A sensor fault must never change what the plant actually produced."""
    truth = _series([100.0, 200.0, 300.0])
    original = truth.copy()

    spec = SensorSpec(
        asset_id="X",
        quantity="ghi",
        sensor_class="irradiance",
        bias_gain=0.5,
        noise_sigma_rel=0.1,
    )
    apply_sensor(truth, spec, rng=np.random.default_rng(0))

    pd.testing.assert_series_equal(truth, original)


def test_measure_frame_returns_a_distinct_object(config, run) -> None:
    """Truth and measured frames must not be the same object."""
    asset_id = next(iter(run.inverters))
    assert run.inverters[asset_id] is not run.measured[asset_id]
    assert not run.inverters[asset_id].equals(run.measured[asset_id])


def test_uninstrumented_fields_pass_through_unchanged(config) -> None:
    """Only fields with a sensor are transformed; the rest remain truth."""
    truth = pd.DataFrame(
        {
            "ghi": [100.0, 200.0],
            "operating_state": ["RUNNING", "RUNNING"],
        },
        index=pd.date_range("2023-06-21", periods=2, freq="1min", tz="UTC"),
    )
    fleet = build_sensor_fleet([("X", "ghi")], seed=1)
    measured = measure_frame(truth, "X", fleet, seed=1)

    assert (measured["operating_state"] == truth["operating_state"]).all()
    assert not measured["ghi"].equals(truth["ghi"])


# --------------------------------------------------------------------------
# Individual effects
# --------------------------------------------------------------------------


def test_calibration_gain_scales_the_reading() -> None:
    """A gain error is multiplicative and does not vanish under averaging."""
    truth = _series([100.0, 200.0, 400.0])
    spec = SensorSpec("X", "ghi", "irradiance", bias_gain=1.02)
    measured = apply_sensor(truth, spec, rng=np.random.default_rng(0))
    assert (measured / truth).round(6).nunique() == 1


def test_temperature_bias_is_an_offset_not_a_gain() -> None:
    """A 0.3 K RTD offset is additive; scaling it would be wrong physics."""
    truth = _series([0.0, 20.0, 40.0])
    spec = SensorSpec("X", "temp_air", "temperature", bias_offset=0.5)
    measured = apply_sensor(truth, spec, rng=np.random.default_rng(0))
    assert (measured - truth).round(6).nunique() == 1


def test_drift_accumulates_over_the_record() -> None:
    """Drift is slow and monotonic - the mechanism behind SCN-062."""
    truth = pd.Series(
        1000.0,
        index=pd.date_range("2023-01-01", periods=500, freq="1D", tz="UTC"),
    )
    spec = SensorSpec("X", "ghi", "irradiance", drift_per_year=0.05)
    measured = apply_sensor(truth, spec, rng=np.random.default_rng(0))
    assert measured.iloc[-1] > measured.iloc[0]


def test_response_lag_smooths_a_step() -> None:
    """A thermopile pyranometer takes tens of seconds to settle."""
    truth = _series([0.0] * 5 + [1000.0] * 20)
    fast = SensorSpec("X", "ghi", "irradiance", response_seconds=0.0)
    slow = SensorSpec("X", "ghi", "irradiance", response_seconds=120.0)

    quick = apply_sensor(truth, fast, rng=np.random.default_rng(0))
    lagged = apply_sensor(truth, slow, rng=np.random.default_rng(0))

    assert quick.iloc[5] == pytest.approx(1000.0)
    assert lagged.iloc[5] < 1000.0
    assert lagged.iloc[-1] > lagged.iloc[5]


def test_quantization_snaps_to_the_reporting_resolution() -> None:
    """ADC resolution is invisible until someone looks for stuck values."""
    truth = _series([100.13, 100.27, 100.41])
    spec = SensorSpec("X", "ghi", "irradiance", quantization=0.5)
    measured = apply_sensor(truth, spec, rng=np.random.default_rng(0))
    assert ((measured / 0.5) % 1 == 0).all()


def test_pyranometer_soiling_makes_the_sensor_under_read() -> None:
    """A dirty pyranometer inflates performance ratio.

    It under-reads irradiance, so measured output over measured resource looks
    better than reality. An analyst chasing a suspiciously good PR is doing the
    right thing and the answer is a dirty sensor, not a good plant. SCN-067.
    """
    truth = pd.Series(
        1000.0,
        index=pd.date_range("2023-01-01", periods=365, freq="1D", tz="UTC"),
    )
    spec = SensorSpec("X", "ghi", "irradiance", soiling_per_year=0.05)
    measured = apply_sensor(truth, spec, rng=np.random.default_rng(0))

    assert measured.iloc[-1] < measured.iloc[0]
    assert measured.iloc[-1] < truth.iloc[-1]
    # Apparent performance ratio uses measured resource in the denominator.
    apparent = truth.iloc[-1] / measured.iloc[-1]
    assert apparent > 1.0, "under-reading resource inflates PR"


# --------------------------------------------------------------------------
# Fleet behaviour
# --------------------------------------------------------------------------


def test_fleet_is_deterministic_for_a_seed() -> None:
    """A dataset must be traceable to the instruments that produced it."""
    pairs = [("A", "ghi"), ("B", "ghi")]
    first = build_sensor_fleet(pairs, seed=7)
    second = build_sensor_fleet(pairs, seed=7)
    assert first.get("A", "ghi") == second.get("A", "ghi")


def test_adding_a_sensor_does_not_reshuffle_the_others() -> None:
    """Each instrument draws from its own substream.

    Otherwise instrumenting one more field would silently recalibrate every
    other sensor in the plant, and two datasets differing in scope would not be
    comparable.
    """
    small = build_sensor_fleet([("A", "ghi")], seed=7)
    large = build_sensor_fleet([("A", "ghi"), ("B", "ghi"), ("C", "ghi")], seed=7)
    assert small.get("A", "ghi") == large.get("A", "ghi")


def test_instruments_measuring_the_same_quantity_disagree() -> None:
    """Two sensors on the same field must have distinct personalities."""
    fleet = build_sensor_fleet([("A", "ghi"), ("B", "ghi")], seed=7)
    assert fleet.get("A", "ghi").bias_gain != fleet.get("B", "ghi").bias_gain


def test_unknown_quantities_are_not_instrumented() -> None:
    """Fields with no sensor class are left as truth."""
    fleet = build_sensor_fleet([("A", "scenario_id")], seed=7)
    assert fleet.get("A", "scenario_id") is None


def test_fleet_table_is_ground_truth_about_instrument_error(run) -> None:
    """A validator must be able to score an analyst's calibration estimate."""
    frame = run.sensors.to_frame()
    assert len(frame) > 0
    assert {"bias_gain", "drift_per_year", "soiling_per_year"} <= set(frame.columns)


# --------------------------------------------------------------------------
# Integrated behaviour
# --------------------------------------------------------------------------


def test_instrument_error_adds_to_spatial_disagreement(run, base) -> None:
    """Stations already differ by position; instruments widen the gap."""
    daylight = base["solar_zenith"] < 80
    stations = {k: v for k, v in run.measured.items() if k in run.weather}

    spatial_only = station_spread(run.weather, "ghi")[daylight].mean()
    with_instruments = station_spread(stations, "ghi")[daylight].mean()

    assert with_instruments > spatial_only
    assert with_instruments < 0.08, "must stay inside the usable range"


def test_systematic_error_tracks_gain_magnitude(run, base) -> None:
    """Weak residual correlation occurs where the sensor is nearly perfect.

    This is why the gate checks the fleet median rather than one instrument:
    a single near-unity sensor legitimately shows almost no systematic error.
    """
    daylight = base["solar_zenith"] < 80
    correlations, gain_errors = [], []

    for asset_id, truth in run.inverters.items():
        residual = (run.measured[asset_id]["poa_global"] - truth["poa_global"])[daylight]
        value = residual.corr(truth["poa_global"][daylight])
        if pd.notna(value):
            correlations.append(abs(value))
            gain_errors.append(abs(run.sensors.get(asset_id, "poa_global").bias_gain - 1))

    assert np.corrcoef(correlations, gain_errors)[0, 1] > 0.7


def test_sensor_gate_passes(run, base) -> None:
    """The Phase 6 acceptance gate."""
    gate = run_sensor_gate(run, daylight=base["solar_zenith"] < 80)
    assert gate.passed, gate.render()
