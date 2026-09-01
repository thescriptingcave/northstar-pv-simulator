"""Tests for loading fetched observations into the simulator."""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.observed import (  # noqa: E402
    REQUIRED_COLUMNS,
    align_prices,
    available_years,
    real_prices,
    real_resource,
)
from northstar_sim.plant_config import load_plant_config  # noqa: E402
from northstar_sim.resource import clearsky_resource  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def config():
    """Provide the plant configuration.

    Returns:
        The loaded configuration.
    """
    return load_plant_config(REPO / "config" / "northstar.toml")


@pytest.fixture(scope="module")
def cache(config, tmp_path_factory):
    """Write a physically consistent weather and price cache.

    Built from a clear-sky series with cloud applied to all three components,
    rather than inventing them. An earlier fixture invented `dni` and `dhi`
    independently, violated ``GHI ~= DHI + DNI * cos(z)``, and drove
    transposition to NaN - which then nulled the entire measured series.

    Args:
        config: Plant configuration.
        tmp_path_factory: pytest temporary directory factory.

    Returns:
        The cache root.
    """
    clear = clearsky_resource(config, "2025-06-20", "2025-06-23", freq="5min")
    rng = np.random.default_rng(7)
    cloud = np.clip(1 - 0.5 * rng.random(len(clear)), 0.15, 1.0)

    root = tmp_path_factory.mktemp("cache")
    shutil.rmtree(root, ignore_errors=True)
    weather = root / "weather" / "source=nsrdb_goes_conus_v4" / "year=2025"
    weather.mkdir(parents=True)
    pd.DataFrame(
        {
            "time": clear.index,
            "ghi": clear["ghi"].to_numpy() * cloud,
            "dni": clear["dni"].to_numpy() * cloud**2,
            "dhi": clear["dhi"].to_numpy() * (2 - cloud),
            "temp_air": 30.0,
            "wind_speed": 4.0,
        }
    ).to_parquet(weather / "part-0.parquet", index=False)

    prices = root / "prices" / "source=ercot_rt_spp" / "point=HRNT_SLR_RN" / "year=2025"
    prices.mkdir(parents=True)
    index = pd.date_range("2025-06-20", "2025-06-23", freq="15min", tz="UTC")
    pd.DataFrame(
        {"time": index, "price_usd_mwh": np.linspace(-10, 90, len(index))}
    ).to_parquet(prices / "part-0.parquet", index=False)

    return root


def test_loaded_frame_satisfies_the_resource_contract(config, cache) -> None:
    """It must drop into `downscale_to_minute` unchanged."""
    frame = real_resource(config, 2025, cache_root=cache).frame

    for column in REQUIRED_COLUMNS:
        assert column in frame.columns, column
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing


def test_the_plant_runs_on_loaded_observations(config, cache) -> None:
    """The point of the whole exercise.

    Every published figure came from the clear-sky stand-in because nothing
    read the fetch cache.
    """
    from northstar_sim.builder import build_plant
    from northstar_sim.plant_run import run_plant
    from northstar_sim.resource import downscale_to_minute

    frame = real_resource(config, 2025, cache_root=cache).frame
    base = downscale_to_minute(frame, config, seed=1)
    base["wind_speed"] = 4.0
    base["wind_direction"] = 250.0

    result = run_plant(
        config,
        build_plant(config),
        base.head(1440),
        seed=1,
        inject_faults=False,
        inject_defects=False,
    )
    measured = result.measured["NORTHSTA-BLK01-INV1"]

    # A single NaN in POA truth poisons the sensor layer's cumulative state and
    # nulls the entire measured series, so this is a stronger check than it
    # looks.
    assert measured["poa_global"].notna().all()
    assert result.plant["grid_export_power_kw"].max() > 0


def test_night_clearness_index_is_zero_not_nan(config, cache) -> None:
    """The clear-sky reference is zero at night, so the ratio is undefined.

    NaN there would propagate through the downscaler; a night with no sun is
    perfectly clear by any useful definition.
    """
    frame = real_resource(config, 2025, cache_root=cache).frame
    night = frame.loc[frame["clearsky_ghi"] <= 1.0, "clearsky_index"]

    assert night.notna().all()
    assert (night == 0.0).all()


def test_missing_year_names_what_is_available(config, cache) -> None:
    """A traceback listing the cached years beats one that does not."""
    with pytest.raises(FileNotFoundError, match="2025"):
        real_resource(config, 1999, cache_root=cache)


def test_available_years_reports_the_cache(config, cache) -> None:
    """Asking is cheaper than a traceback."""
    assert available_years(config, cache_root=cache) == [2025]


def test_prices_load_and_align_without_interpolating(config, cache) -> None:
    """A price is a clearing outcome for an interval, not a sample.

    Interpolating invents intermediate values that never cleared.
    """
    prices = real_prices(config, 2025, cache_root=cache)
    assert len(prices) > 0

    minute = pd.date_range("2025-06-20", periods=60, freq="1min", tz="UTC")
    aligned = align_prices(prices, minute)

    assert aligned.notna().all()
    # Held constant across the settlement interval, so far fewer distinct
    # values than rows.
    assert aligned.nunique() < len(aligned)


def test_a_cache_with_unmapped_headers_still_loads(config, tmp_path) -> None:
    """An existing cache must not need a 197-request refetch to be usable.

    Partitions written before the harmonizer handled space-separated names
    carry the raw provider headers. The loader re-canonicalizes on read.
    """
    clear = clearsky_resource(config, "2025-06-20", "2025-06-22", freq="5min")
    directory = tmp_path / "weather" / "source=nsrdb_goes_conus_v4" / "year=2025"
    directory.mkdir(parents=True)

    pd.DataFrame(
        {
            "time": clear.index,
            "ghi": clear["ghi"].to_numpy(),
            "dni": clear["dni"].to_numpy(),
            "dhi": clear["dhi"].to_numpy(),
            "temp_air": 30.0,
            "Wind Speed": 4.0,
            "Wind Direction": 250.0,
        }
    ).to_parquet(directory / "part-0.parquet", index=False)

    frame = real_resource(config, 2025, cache_root=tmp_path).frame

    assert "wind_speed" in frame.columns
    assert frame["wind_speed"].iloc[0] == 4.0


def test_canonical_columns_are_never_overwritten(config, tmp_path) -> None:
    """A canonical column already present wins over a raw duplicate."""
    clear = clearsky_resource(config, "2025-06-20", "2025-06-21", freq="5min")
    directory = tmp_path / "weather" / "source=nsrdb_goes_conus_v4" / "year=2025"
    directory.mkdir(parents=True)

    pd.DataFrame(
        {
            "time": clear.index,
            "ghi": clear["ghi"].to_numpy(),
            "dni": clear["dni"].to_numpy(),
            "dhi": clear["dhi"].to_numpy(),
            "temp_air": 30.0,
            "wind_speed": 7.0,
            "Wind Speed": 4.0,
        }
    ).to_parquet(directory / "part-0.parquet", index=False)

    frame = real_resource(config, 2025, cache_root=tmp_path).frame
    assert frame["wind_speed"].iloc[0] == 7.0


def test_closure_repair_never_produces_unphysical_dni(config, tmp_path) -> None:
    """Dividing GHI by cos(zenith) near the horizon implies enormous DNI.

    At 89.5 degrees a GHI of 50 implies 5,000 W/m2 against a solar constant of
    1,367. On real NSRDB data this drove POA and AC to **five times nameplate**
    - a measured peak of 12,617 kW on a 2,500 kW inverter.
    """
    from northstar_sim.observed import SOLAR_CONSTANT

    index = pd.date_range("2025-06-20", "2025-06-21", freq="5min", tz="UTC")
    directory = tmp_path / "weather" / "source=nsrdb_goes_conus_v4" / "year=2025"
    directory.mkdir(parents=True)

    # Components that violate closure at every hour, including twilight.
    pd.DataFrame(
        {
            "time": index,
            "ghi": 50.0,
            "dni": 900.0,
            "dhi": 400.0,
            "temp_air": 30.0,
            "wind_speed": 4.0,
        }
    ).to_parquet(directory / "part-0.parquet", index=False)

    frame = real_resource(config, 2025, cache_root=tmp_path).frame

    assert frame["dni"].max() <= SOLAR_CONSTANT
    assert frame["dhi"].max() <= SOLAR_CONSTANT
    assert (frame["dni"] >= 0).all()


def test_low_sun_intervals_are_left_alone(config, tmp_path) -> None:
    """Twilight carries no energy, so repairing it buys nothing and risks much."""
    index = pd.date_range("2025-06-20", "2025-06-21", freq="5min", tz="UTC")
    directory = tmp_path / "weather" / "source=nsrdb_goes_conus_v4" / "year=2025"
    directory.mkdir(parents=True)
    pd.DataFrame(
        {
            "time": index,
            "ghi": 50.0,
            "dni": 900.0,
            "dhi": 400.0,
            "temp_air": 30.0,
            "wind_speed": 4.0,
        }
    ).to_parquet(directory / "part-0.parquet", index=False)

    frame = real_resource(config, 2025, cache_root=tmp_path).frame
    assert frame.attrs.get("closure_skipped_low_sun", 0) > 0
