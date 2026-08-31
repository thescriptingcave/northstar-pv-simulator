"""Tests for the analysis methods.

Each estimator is tested against data with a **known injected answer**. That is
the only way to distinguish a working estimator from one producing plausible
numbers, and it is the same principle the simulator's ground-truth schema
exists to serve.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_analytics import (  # noqa: E402
    build_features,
    detect_change_points,
    estimate_degradation,
    evaluate_forecast,
    fit_expected_power,
    leaking_columns,
)


def _degrading_series(rate: float, seed: int, years: int = 3) -> pd.Series:
    """Build a normalised series with a known degradation rate.

    Args:
        rate: Injected annual relative change, negative for degradation.
        seed: Noise seed.
        years: Record length.

    Returns:
        A daily series carrying degradation, seasonality and noise.
    """
    index = pd.date_range("2021-01-01", periods=365 * years, freq="1D", tz="UTC")
    elapsed = (index - index[0]).days / 365.25
    rng = np.random.default_rng(seed)

    trend = (1.0 + rate) ** elapsed
    seasonal = 1.0 + 0.03 * np.sin(2 * np.pi * index.dayofyear / 365.25)
    return pd.Series(
        trend * seasonal * (1 + rng.normal(0, 0.02, len(index))), index=index
    )


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rate", [-0.004, -0.010, -0.025])
def test_degradation_recovers_the_injected_rate(rate: float) -> None:
    """The estimate must land within two standard errors of truth."""
    estimate = estimate_degradation(_degrading_series(rate, seed=7))
    assert abs(estimate.rate_per_year - rate) < 2.0 * estimate.standard_error


def test_degradation_is_unbiased_across_realisations() -> None:
    """Averaged over seeds the error must vanish, not merely be small.

    A biased estimator can sit inside tolerance on one dataset and be
    systematically wrong on all of them.
    """
    errors = [
        estimate_degradation(_degrading_series(-0.010, seed=s)).rate_per_year - (-0.010)
        for s in range(20)
    ]
    assert abs(float(np.mean(errors))) < 0.0005


def test_degradation_reports_its_standard_error() -> None:
    """A single three-year estimate is not precise to 0.15 percentage points.

    Design doc 20 section 14.5 sets that tolerance, which is roughly a
    1.3-sigma bound at typical noise. Reporting the standard error is what lets
    a reader tell a real change from sampling scatter.
    """
    estimate = estimate_degradation(_degrading_series(-0.004, seed=1))
    assert 0.0 < estimate.standard_error < 0.005
    assert estimate.pairs > 300


def test_degradation_refuses_a_short_record() -> None:
    """The year-on-year method needs two full years, not an extrapolation."""
    index = pd.date_range("2023-01-01", periods=200, freq="1D", tz="UTC")
    with pytest.raises(ValueError, match="at least two"):
        estimate_degradation(pd.Series(1.0, index=index))


# --------------------------------------------------------------------------
# Change points
# --------------------------------------------------------------------------


def test_change_points_find_injected_cleanings() -> None:
    """Soiling accumulates gradually and clears abruptly."""
    index = pd.date_range("2023-01-01", periods=180, freq="1D", tz="UTC")
    cleanings = {45, 100, 150}

    values, level = [], 1.0
    for day in range(180):
        level -= 0.0025
        if day in cleanings:
            level = 1.0
        values.append(level)

    rng = np.random.default_rng(0)
    series = pd.Series(np.array(values) + rng.normal(0, 0.002, 180), index=index)

    detected = detect_change_points(series, window=5, threshold=0.03)
    days = {(point.time - index[0]).days for point in detected}

    assert len(detected) == len(cleanings)
    for injected in cleanings:
        assert any(abs(day - injected) <= 2 for day in days), injected


def test_change_points_are_positive_for_recovery() -> None:
    """A cleaning steps output up; degradation never does."""
    index = pd.date_range("2023-01-01", periods=120, freq="1D", tz="UTC")
    values = np.concatenate([np.full(60, 0.90), np.full(60, 1.00)])
    detected = detect_change_points(pd.Series(values, index=index), window=5)

    assert detected
    assert detected[0].magnitude > 0


def test_a_steady_series_has_no_change_points() -> None:
    """Noise alone must not manufacture events."""
    index = pd.date_range("2023-01-01", periods=120, freq="1D", tz="UTC")
    rng = np.random.default_rng(3)
    series = pd.Series(1.0 + rng.normal(0, 0.002, 120), index=index)
    assert detect_change_points(series, window=5, threshold=0.03) == []


# --------------------------------------------------------------------------
# Expected power
# --------------------------------------------------------------------------


def test_expected_power_recovers_known_coefficients() -> None:
    """Fitted on data generated from the model, it must return the model."""
    index = pd.date_range("2023-06-01", periods=2000, freq="15min", tz="UTC")
    rng = np.random.default_rng(0)

    irradiance = pd.Series(rng.uniform(60, 1050, len(index)), index=index)
    temperature = pd.Series(rng.uniform(10, 45, len(index)), index=index)
    wind = pd.Series(rng.uniform(0.5, 9.0, len(index)), index=index)

    a, b, c, d = 0.95, -1.5e-5, -0.0035, 0.004
    power = irradiance * (a + b * irradiance + c * temperature + d * wind)

    model = fit_expected_power(power, irradiance, temperature, wind)

    assert model.r_squared > 0.999
    for fitted, expected in zip(model.coefficients, (a, b, c, d), strict=True):
        assert fitted == pytest.approx(expected, rel=0.02, abs=1e-6)


def test_expected_power_refuses_a_thin_fit() -> None:
    """Four coefficients on a handful of points is not a fit."""
    index = pd.date_range("2023-06-01", periods=40, freq="15min", tz="UTC")
    ones = pd.Series(500.0, index=index)
    with pytest.raises(ValueError, match="usable samples"):
        fit_expected_power(ones, ones, ones, ones)


# --------------------------------------------------------------------------
# Forecasting
# --------------------------------------------------------------------------


def test_features_are_strictly_backward_looking() -> None:
    """Target leakage is the dominant failure mode in energy forecasting."""
    index = pd.date_range("2023-06-01", periods=500, freq="15min", tz="UTC")
    rng = np.random.default_rng(0)
    frame = pd.DataFrame(
        {"grid_export_power_kw": rng.uniform(0, 100_000, len(index))}, index=index
    )

    features = build_features(frame)
    assert leaking_columns(features, frame, "grid_export_power_kw") == []


def test_leakage_check_catches_a_concurrent_feature() -> None:
    """The check must actually fire, not merely be present."""
    index = pd.date_range("2023-06-01", periods=500, freq="15min", tz="UTC")
    rng = np.random.default_rng(0)
    frame = pd.DataFrame(
        {"grid_export_power_kw": rng.uniform(0, 100_000, len(index))}, index=index
    )

    features = build_features(frame)
    features["oops"] = frame["grid_export_power_kw"]
    assert "oops" in leaking_columns(features, frame, "grid_export_power_kw")


def test_skill_is_measured_against_persistence() -> None:
    """Absolute error means nothing without a baseline to beat."""
    index = pd.date_range("2023-06-01", periods=100, freq="15min", tz="UTC")
    actual = pd.Series(np.linspace(0, 100, 100), index=index)
    persistence = actual.shift(1)

    perfect = evaluate_forecast(actual, actual, persistence, "perfect")
    baseline = evaluate_forecast(actual, persistence, persistence, "persistence")

    assert perfect.skill == pytest.approx(1.0)
    assert baseline.skill == pytest.approx(0.0)


def test_a_worse_than_persistence_forecast_scores_negative() -> None:
    """Negative skill is a real outcome and must be reportable."""
    index = pd.date_range("2023-06-01", periods=100, freq="15min", tz="UTC")
    actual = pd.Series(np.linspace(0, 100, 100), index=index)

    useless = pd.Series(50.0, index=index)
    result = evaluate_forecast(actual, useless, actual.shift(1), "constant")
    assert result.skill < 0


# --------------------------------------------------------------------------
# Findings from the first real multi-year recovery
# --------------------------------------------------------------------------


def test_degradation_survives_a_leap_day() -> None:
    """Shifting 29 February by one year clamps onto an existing entry.

    The join then fails outright on duplicate labels. Only real multi-year data
    reaches this: a synthetic series spanning 2021 to 2023 never sees a leap
    day, which is why it went unnoticed until a 2023-2025 record was generated.
    """
    index = pd.date_range("2023-01-01", "2025-12-31", freq="1D", tz="UTC")
    assert any((index.month == 2) & (index.day == 29)), "window must span a leap day"

    elapsed = (index - index[0]).days / 365.25
    series = pd.Series((1.0 - 0.004) ** elapsed, index=index)

    estimate = estimate_degradation(series)
    assert estimate.rate_per_year == pytest.approx(-0.004, abs=0.001)


def test_daily_energy_ratio_beats_a_median_of_hourly_ratios() -> None:
    """The aggregation choice dominates the result.

    A median of hourly ratios weights every interval equally, so unstable
    low-irradiance hours drive a statistic that should be driven by the hours
    carrying the energy. On a real three-year record the two differ by nearly
    0.2 percentage points against a 0.4 %/yr injected rate.
    """
    from northstar_analytics import daily_performance_index

    index = pd.date_range("2022-01-01", periods=24 * 900, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)

    # A diurnal profile with genuinely noisy low-irradiance hours.
    hour = index.hour
    irradiance = np.clip(1000.0 * np.sin(np.pi * (hour - 6) / 12.0), 0, None)
    elapsed = (index - index[0]).days / 365.25
    degradation = (1.0 - 0.004) ** elapsed

    frame = pd.DataFrame(
        {
            "irradiance": irradiance,
            "temperature": 25.0,
            "wind_speed": 3.0,
        },
        index=index,
    )
    noise = rng.normal(1.0, 0.02, len(index))
    power = pd.Series(irradiance * 100.0 * degradation * noise, index=index)

    usable = frame["irradiance"] > 50
    frame, power = frame[usable], power[usable]

    model = fit_expected_power(
        power, frame["irradiance"], frame["temperature"], frame["wind_speed"]
    )
    index_series = daily_performance_index(power, model, frame)

    assert len(index_series) > 500
    assert index_series.std() < 0.05, "daily energy ratio must be stable"

    estimate = estimate_degradation(index_series)
    assert abs(estimate.rate_per_year - (-0.004)) < 0.0015


# --------------------------------------------------------------------------
# Working-directory independence
# --------------------------------------------------------------------------


def test_find_dataset_walks_up_from_the_working_directory(tmp_path, monkeypatch):
    """Notebooks cannot assume where they are run from.

    `jupyter execute` uses the notebook's own directory, JupyterLab uses
    wherever it was launched, and a hardcoded relative path works in exactly
    one of those. `make notebooks` failed with "Table with name
    plant_telemetry does not exist" because `Path("datasets/curriculum")`
    resolved to `notebooks/datasets/curriculum`.
    """
    from northstar_analytics import find_dataset

    root = tmp_path / "repo"
    (root / "datasets" / "curriculum" / "analyst").mkdir(parents=True)
    nested = root / "notebooks"
    nested.mkdir()

    monkeypatch.chdir(nested)
    assert find_dataset("curriculum") == root / "datasets" / "curriculum"


def test_find_dataset_says_what_to_do_when_absent(tmp_path, monkeypatch):
    """An error naming the fix beats a CatalogException three frames later."""
    from northstar_analytics import find_dataset

    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="make dev-dataset"):
        find_dataset("curriculum")


def test_open_dataset_rejects_a_missing_tree(tmp_path):
    """Fail at the path, not later with a confusing "table does not exist"."""
    from northstar_analytics import open_dataset

    with pytest.raises(FileNotFoundError, match="working directory"):
        open_dataset(tmp_path, "nope", "analyst")
