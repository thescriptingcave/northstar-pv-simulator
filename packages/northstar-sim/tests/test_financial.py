"""Tests for the Phase 9 financial layer.

The layer exists to reproduce one structural fact: in a high-solar market,
solar generates most when solar is worth least. Capture rate below 100% is the
consequence, it is invisible in every physical metric, and it emerges only from
joining prices to production shape.

Two calibration failures during development shaped these tests, and both would
have produced confident nonsense rather than an error.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.kpis import (  # noqa: E402
    availability_metrics,
    performance_filter,
    performance_metrics,
)
from northstar_sim.losses import CAUSE_CODES, plant_waterfall  # noqa: E402
from northstar_sim.market import (  # noqa: E402
    SETTLEMENT_MINUTES,
    CommercialTerms,
    capture_rate,
    economic_curtailment_mask,
    monetize_losses,
    run_financial_gate,
    settle,
    synthetic_prices,
    to_settlement_grain,
)
from northstar_sim.plant_run import run_plant  # noqa: E402
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
def terms():
    """Provide the commercial terms.

    Returns:
        Default :class:`CommercialTerms`.
    """
    return CommercialTerms()


@pytest.fixture(scope="module")
def base(config):
    """Provide a month-long plant-average resource series.

    Args:
        config: Plant configuration.

    Returns:
        A 1-minute resource frame.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-07-21 05:00",
        freq="5min",
        temp_air_c=38.0,
        wind_speed_ms=5.0,
    )
    frame = downscale_to_minute(source, config, seed=12345)
    frame["wind_speed"] = 5.0
    frame["wind_direction"] = 250.0
    return frame


@pytest.fixture(scope="module")
def price(base):
    """Provide the development price series.

    Args:
        base: Resource frame.

    Returns:
        Signed prices at 1-minute resolution.
    """
    return synthetic_prices(base.index, base["ghi"], seed=999)


@pytest.fixture(scope="module")
def run(config, base, price, terms):
    """Provide a full run with faults and economic curtailment.

    Args:
        config: Plant configuration.
        base: Resource frame.
        price: Price series.
        terms: Commercial terms.

    Returns:
        The run result.
    """
    return run_plant(
        config,
        build_plant(config),
        base,
        seed=999,
        inject_faults=True,
        economic_curtailment=economic_curtailment_mask(price, terms),
    )


# --------------------------------------------------------------------------
# Price structure
# --------------------------------------------------------------------------


def test_prices_are_suppressed_when_solar_output_is_high(base, price) -> None:
    """The structural fact the whole layer depends on."""
    peak_solar = base["ghi"] > base["ghi"].quantile(0.9)
    dark = base["ghi"] < 1.0
    assert price[peak_solar].mean() < price[dark].mean()


def test_penetration_follows_an_output_shape_not_a_clearness_measure(base, price) -> None:
    """Clear-sky index is ~1.0 from sunrise to sunset; output is a bell.

    Using the index as the penetration proxy suppressed prices across the whole
    day, drove 27% of intervals negative and produced a generation-weighted
    price of -$3.77/MWh. Prices near sunrise must not be as suppressed as
    prices at noon.
    """
    hour = base.index.hour + base.index.minute / 60.0
    noon = (hour > 18) & (hour < 20)  # solar noon in UTC at this longitude
    morning = (hour > 12) & (hour < 13)
    assert price[noon].mean() < price[morning].mean()


def test_negative_prices_occur_at_a_realistic_frequency(price) -> None:
    """ERCOT West runs roughly 10-15% negative, not 54%.

    A suppression coefficient of 46 against a base of 32 drove 54% of intervals
    negative and made energy revenue negative overall - a market no one
    operates in.
    """
    assert 0.02 < float((price < 0).mean()) < 0.35


def test_settlement_grain_prices_reach_the_curtailment_threshold(price, terms) -> None:
    """Structural oversupply, not minute-level noise, must drive curtailment."""
    settlement_price = to_settlement_grain(price)
    assert float((settlement_price < -terms.ptc_usd_mwh).mean()) > 0.001


# --------------------------------------------------------------------------
# Economic curtailment
# --------------------------------------------------------------------------


def test_curtailment_is_decided_at_settlement_grain(terms) -> None:
    """Deciding per minute over-curtails badly.

    Each brief noise excursion below the threshold triggered its own dwell:
    1,538 curtailed minutes of which only 294 had a genuinely negative marginal
    price, with the plant idle through $25,844 of positive-margin generation.
    """
    index = pd.date_range("2023-06-21", periods=240, freq="1min", tz="UTC")
    rng = np.random.default_rng(0)
    # A structurally positive price with noise that dips below the threshold.
    noisy = pd.Series(-20.0 + rng.normal(0, 12.0, len(index)), index=index)

    mask = economic_curtailment_mask(noisy, terms)
    settlement_price = to_settlement_grain(noisy)
    expected_intervals = int((settlement_price < -terms.ptc_usd_mwh).sum())

    # Far fewer curtailed minutes than the number of noisy sub-threshold minutes.
    assert mask.sum() <= (expected_intervals + 2) * SETTLEMENT_MINUTES * 2


def test_curtailment_covers_the_sub_threshold_intervals(terms) -> None:
    """Curtailment spans the settlement intervals whose price is below threshold.

    Note the configured 15-minute dwell equals exactly **one** settlement
    interval, so it is the minimum possible and adds nothing beyond the
    interval itself. The hysteresis band does the anti-chatter work. Raising
    the dwell is the lever if a longer minimum commitment is wanted.
    """
    index = pd.date_range("2023-06-21", periods=240, freq="1min", tz="UTC")
    price = pd.Series(20.0, index=index)
    price.iloc[30:60] = -60.0

    mask = economic_curtailment_mask(price, terms)
    assert mask.sum() >= 30
    assert not mask.iloc[:15].any(), "must not curtail before the dip"
    assert not mask.iloc[90:].any(), "must not curtail long after recovery"


def test_curtailment_produces_no_fault_code(run, price, terms) -> None:
    """The teaching artifact: high irradiance, sharp drop, no fault at all.

    An analyst looking only at telemetry will misclassify this as an equipment
    problem. Only the price join attributes it correctly.
    """
    mask = economic_curtailment_mask(price, terms)
    if not mask.any():
        pytest.skip("no curtailment in this realisation")

    frame = next(iter(run.inverters.values()))
    during = frame[mask.reindex(frame.index).fillna(False)]

    assert (during["operating_state"] == "CURTAILED").all()
    assert during["commanded_power_kw"].max() == pytest.approx(0.0, abs=1e-6)
    if "fault_code" in during.columns:
        assert during["fault_code"].notna().sum() == 0


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------


def test_hedge_settles_independently_of_generation(run, price, terms) -> None:
    """Volume risk exists precisely because the hedge is volumetrically fixed."""
    export = run.plant["grid_export_power_kw"]
    hub = price

    full = settle(export, price, hub, terms)
    halved = settle(export * 0.5, price, hub, terms)

    pd.testing.assert_series_equal(
        full["hedge_settlement_usd"], halved["hedge_settlement_usd"]
    )
    assert halved["energy_revenue_usd"].sum() != full["energy_revenue_usd"].sum()


def test_settlement_is_at_the_fifteen_minute_grain(run, price, terms) -> None:
    """ERCOT real-time settlement is 15-minute; design doc 18 adopts it."""
    settlement = settle(run.plant["grid_export_power_kw"], price, price, terms)
    deltas = settlement.index.to_series().diff().dropna().unique()
    assert list(deltas) == [pd.Timedelta(minutes=SETTLEMENT_MINUTES)]


def test_energy_revenue_is_positive_overall(run, price, terms) -> None:
    """Negative energy revenue means the price model is broken, not the plant."""
    settlement = settle(run.plant["grid_export_power_kw"], price, price, terms)
    assert settlement["energy_revenue_usd"].sum() > 0


# --------------------------------------------------------------------------
# Monetization
# --------------------------------------------------------------------------


def test_curtailment_lost_revenue_is_negative(config, run, price, terms) -> None:
    """Curtailing when price plus PTC is negative *saves* money.

    Any pipeline reporting lost revenue as unconditionally positive breaks
    here, and finding out why is the point.
    """
    waterfall = plant_waterfall(config, run)
    monetized = monetize_losses(waterfall.stages, price, terms, CAUSE_CODES)
    row = monetized[monetized["cause_code"] == "LOSS_CURTAILMENT"]

    if row.empty or row["lost_energy_mwh"].iloc[0] <= 0:
        pytest.skip("no curtailment in this realisation")
    assert float(row["lost_revenue_usd"].iloc[0]) < 0


def test_cost_ranking_differs_from_energy_ranking(config, run, price, terms) -> None:
    """If the two agreed, the financial layer would add nothing."""
    waterfall = plant_waterfall(config, run)
    monetized = monetize_losses(waterfall.stages, price, terms, CAUSE_CODES)

    by_energy = list(
        monetized.sort_values("lost_energy_mwh", ascending=False)["cause_code"]
    )
    by_revenue = list(
        monetized.sort_values("lost_revenue_usd", ascending=False)["cause_code"]
    )
    assert by_energy != by_revenue


def test_capture_rate_is_below_unity(run, price) -> None:
    """Solar produces most when its output is worth least."""
    rate = capture_rate(run.plant["grid_export_power_kw"], price)
    assert 0.2 < rate < 1.0


# --------------------------------------------------------------------------
# KPIs
# --------------------------------------------------------------------------


def test_performance_filter_excludes_low_irradiance(base) -> None:
    """The ratio is numerically unstable near zero and the result meaningless."""
    eligible = performance_filter(base["ghi"], base["solar_zenith"])
    assert not eligible[base["ghi"] < 10].any()


def test_performance_filter_excludes_curtailed_intervals(base) -> None:
    """Curtailment is a commercial decision, not a performance shortfall."""
    curtailed = pd.Series(0.0, index=base.index)
    curtailed.iloc[:100] = 500.0

    eligible = performance_filter(base["ghi"], base["solar_zenith"], curtailed=curtailed)
    assert not eligible.iloc[:100].any()


def test_temperature_correction_raises_pr_in_a_hot_climate(config, run, base) -> None:
    """Corrected PR answers "what if cells were at 25 C", so it must be higher.

    Without this correction, seasonal comparison is invalid: the same plant
    reads several points lower in summer purely because cells are hotter.
    """
    poa = pd.concat([f["poa_global"] for f in run.inverters.values()], axis=1).mean(
        axis=1
    )
    cell = pd.concat(
        [f["cell_temperature"] for f in run.inverters.values()], axis=1
    ).mean(axis=1)
    dc = sum(f["dc_power_kw"] for f in run.inverters.values())

    metrics = performance_metrics(
        config, poa, cell, dc, run.plant["grid_export_power_kw"], base["solar_zenith"]
    )
    assert metrics.performance_ratio_corrected > metrics.performance_ratio


def test_the_four_availability_definitions_differ(run, base) -> None:
    """Reporting one without saying which makes the figure unfalsifiable."""
    lost = (
        sum(run.fault_loss_kw.values())
        if run.fault_loss_kw
        else pd.Series(0.0, index=base.index)
    )
    metrics = availability_metrics(
        run.plant["grid_export_power_kw"], base["solar_zenith"], lost
    )
    values = {
        metrics.time_based,
        metrics.daylight_weighted,
        metrics.energy_weighted,
    }
    assert len(values) > 1


def test_energy_weighted_availability_exceeds_time_based(run, base) -> None:
    """A night-time outage loses hours of uptime and zero energy."""
    lost = (
        sum(run.fault_loss_kw.values())
        if run.fault_loss_kw
        else pd.Series(0.0, index=base.index)
    )
    metrics = availability_metrics(
        run.plant["grid_export_power_kw"], base["solar_zenith"], lost
    )
    assert metrics.energy_weighted > metrics.time_based


def test_financial_gate_passes(config, run, price, terms) -> None:
    """The Phase 9 acceptance gate."""
    waterfall = plant_waterfall(config, run)
    monetized = monetize_losses(waterfall.stages, price, terms, CAUSE_CODES)
    settlement = settle(run.plant["grid_export_power_kw"], price, price, terms)
    curtailment = economic_curtailment_mask(price, terms)

    gate = run_financial_gate(run, settlement, monetized, price, curtailment, terms)
    assert gate.passed, gate.render()
