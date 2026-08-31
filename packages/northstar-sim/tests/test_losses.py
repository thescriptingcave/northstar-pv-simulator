"""Tests for Phase 7 loss attribution.

Two properties dominate:

* **The waterfall closes.** A growing residual means an unattributed loss path
  exists, which is a correctness bug rather than a rounding issue. Design doc
  18 section 10 sets the limit at 0.5% of theoretical.
* **Attribution is cascading, not independent.** Losses multiply, so a stage is
  attributed what it removed *from what reached it*. Attributing every stage
  against the theoretical maximum over-counts whenever two or more stages act.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.losses import (  # noqa: E402
    CAUSE_CODES,
    RESIDUAL_TOLERANCE,
    inverter_waterfall,
    loss_signatures,
    plant_waterfall,
    run_loss_gate,
)
from northstar_sim.physics import (  # noqa: E402
    inverter_internal_temperature,
    run_inverter_chain,
    sandia_preclip,
    thermal_derate_factor,
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


def _run(config, ambient_c: float):
    """Run the full plant at a constant ambient temperature.

    Args:
        config: Plant configuration.
        ambient_c: Constant ambient temperature.

    Returns:
        The run result.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-22 05:00",
        freq="5min",
        temp_air_c=ambient_c,
        wind_speed_ms=4.0,
    )
    base = downscale_to_minute(source, config, seed=12345)
    base["wind_speed"] = 4.0
    base["wind_direction"] = 250.0
    return run_plant(config, build_plant(config), base, seed=999)


@pytest.fixture(scope="module")
def moderate(config):
    """Provide a run at moderate ambient temperature.

    Args:
        config: Plant configuration.

    Returns:
        The run result.
    """
    return _run(config, 40.0)


@pytest.fixture(scope="module")
def hot(config):
    """Provide a run at extreme ambient temperature.

    Args:
        config: Plant configuration.

    Returns:
        The run result.
    """
    return _run(config, 50.0)


# --------------------------------------------------------------------------
# Closure
# --------------------------------------------------------------------------


def test_plant_waterfall_closes_to_machine_precision(config, moderate) -> None:
    """Theoretical less every attributed loss equals exported energy."""
    waterfall = plant_waterfall(config, moderate)
    assert waterfall.closure_error() < 1e-9


def test_closure_holds_at_every_timestep(config, moderate) -> None:
    """An aggregate that closes can still hide compensating per-sample errors."""
    waterfall = plant_waterfall(config, moderate)
    assert waterfall.residual_kw().abs().max() < 1e-6


def test_inverter_waterfall_closes(config, moderate) -> None:
    """Attribution is correct per asset, not only in aggregate."""
    frame = next(iter(moderate.inverters.values()))
    waterfall = inverter_waterfall(config, frame)
    assert waterfall.closure_error() < RESIDUAL_TOLERANCE


def test_attribution_is_cascading_not_independent(config, moderate) -> None:
    """Summed losses must equal the actual shortfall, not exceed it.

    Attributing every stage against the theoretical maximum over-counts as soon
    as two stages act, because losses multiply rather than add.
    """
    waterfall = plant_waterfall(config, moderate)
    energy = waterfall.energy_mwh()
    attributed = sum(
        float(energy.get(code, 0.0)) for code in CAUSE_CODES if code in energy.index
    )
    shortfall = float(energy["THEORETICAL"] - energy["EXPORTED"])
    assert attributed == pytest.approx(shortfall, rel=1e-6)


# --------------------------------------------------------------------------
# The signed non-linearity term
# --------------------------------------------------------------------------


def test_module_nonlinearity_term_may_be_negative(config, moderate) -> None:
    """At high irradiance the single-diode solution beats the linear model.

    Clipping this term at zero left the cascade below actual DC power, which
    reported inverter conversion loss as 0.04% instead of about 1.5% and pushed
    a spurious -0.21% into the residual.
    """
    waterfall = plant_waterfall(config, moderate)
    energy = waterfall.energy_mwh()
    assert float(energy["LOSS_LOWLIGHT"]) < 0.0


def test_inverter_conversion_loss_is_realistic(config, moderate) -> None:
    """A modern central inverter loses roughly 1-2% in conversion."""
    energy = plant_waterfall(config, moderate).energy_mwh()
    share = float(energy["LOSS_INVERTER_EFF"]) / float(energy["THEORETICAL"])
    assert 0.005 < share < 0.04


# --------------------------------------------------------------------------
# Individual mechanisms
# --------------------------------------------------------------------------


def test_degradation_compounds_with_plant_age(config) -> None:
    """Year one degrades faster than subsequent years."""
    import copy

    young = copy.deepcopy(config)
    young.losses.plant_age_years = 1.0
    old = copy.deepcopy(config)
    old.losses.plant_age_years = 10.0

    assert young.degradation_factor > old.degradation_factor
    assert young.degradation_factor == pytest.approx(
        1.0 - config.module.degradation_year_one
    )


def test_a_new_plant_has_no_degradation(config) -> None:
    """Age zero means full rated output."""
    import copy

    new = copy.deepcopy(config)
    new.losses.plant_age_years = 0.0
    assert new.degradation_factor == 1.0


def test_preclip_power_exceeds_the_ac_cap_when_clipping(config) -> None:
    """Clipping loss cannot be measured without knowing the uncapped output.

    ``pvlib.inverter.sandia`` applies the cap internally, so clipped and
    unclipped output are indistinguishable in its result.
    """
    source = clearsky_resource(
        config, "2023-06-21 05:00", "2023-06-22 05:00", freq="5min", temp_air_c=30.0
    )
    weather = downscale_to_minute(source, config, seed=1)
    frame = run_inverter_chain(config, weather)

    assert frame["ac_preclip_kw"].max() > config.inverter.rated_ac_kw
    assert frame["ac_power_kw"].max() <= config.inverter.rated_ac_kw * 1.001


def test_sandia_preclip_matches_pvlib_below_the_cap(config) -> None:
    """The uncapped formulation is the same model, not an approximation."""
    import pvlib
    from northstar_sim.physics import load_equipment

    equipment = load_equipment(config)
    parameters = equipment.inverter.to_dict()

    index = pd.date_range("2023-06-21", periods=5, freq="1min", tz="UTC")
    v_dc = pd.Series(900.0, index=index)
    p_dc = pd.Series(1_000_000.0, index=index)  # well below Paco

    ours = sandia_preclip(v_dc, p_dc, parameters)
    theirs = pvlib.inverter.sandia(v_dc=v_dc, p_dc=p_dc, inverter=parameters)
    pd.testing.assert_series_equal(ours, theirs, check_names=False)


def test_inverter_internal_temperature_lags_loading(config) -> None:
    """Enclosure thermal mass means derating can outlast the heat that caused it."""
    index = pd.date_range("2023-06-21", periods=200, freq="1min", tz="UTC")
    step = pd.Series(
        np.where(np.arange(200) < 50, 0.0, config.inverter.rated_ac_kw), index=index
    )
    temperature = inverter_internal_temperature(
        config, step, pd.Series(30.0, index=index)
    )
    assert temperature.iloc[50] < temperature.iloc[-1]
    assert temperature.iloc[-1] == pytest.approx(
        30.0 + config.losses.inverter_thermal_rise_c, rel=0.02
    )


def test_derating_uses_an_internal_not_ambient_threshold(config) -> None:
    """The configured onset is ambient, as datasheets state it.

    Comparing it directly to internal temperature made derating fire at 42 C
    ambient and hold output at 1,750 kW instead of 2,500.
    """
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    onset = config.inverter.thermal_derate_onset_c
    rise = config.losses.inverter_thermal_rise_c

    just_below = pd.Series(onset + rise - 1.0, index=index)
    well_above = pd.Series(onset + rise + 10.0, index=index)

    assert (thermal_derate_factor(config, just_below) == 1.0).all()
    assert (thermal_derate_factor(config, well_above) < 1.0).all()


def test_derating_is_capped(config) -> None:
    """Beyond the cap an inverter trips rather than derating indefinitely."""
    index = pd.date_range("2023-06-21", periods=3, freq="1min", tz="UTC")
    extreme = pd.Series(200.0, index=index)
    factor = thermal_derate_factor(config, extreme)
    assert (factor >= 1.0 - config.losses.max_thermal_derate - 1e-9).all()


# --------------------------------------------------------------------------
# Interactions and classification
# --------------------------------------------------------------------------


def test_derating_reduces_clipping(config, moderate, hot) -> None:
    """The two losses are not independent and must not be treated as additive.

    A derating inverter clips less, because derating pulls output below the cap.
    """
    cool = plant_waterfall(config, moderate).energy_mwh()
    warm = plant_waterfall(config, hot).energy_mwh()

    assert float(warm["LOSS_INV_THERMAL"]) > float(cool.get("LOSS_INV_THERMAL", 0.0))
    assert float(warm["LOSS_CLIPPING"]) < float(cool["LOSS_CLIPPING"])


def test_thermal_loss_grows_with_ambient_temperature(config, moderate, hot) -> None:
    """Module temperature loss is what makes raw performance ratio seasonal."""
    cool = plant_waterfall(config, moderate).energy_mwh()
    warm = plant_waterfall(config, hot).energy_mwh()
    assert float(warm["LOSS_THERMAL"]) > float(cool["LOSS_THERMAL"])


def test_clipping_and_degradation_are_not_avoidable() -> None:
    """Reporting design consequences as recoverable is the classic error."""
    assert CAUSE_CODES["LOSS_CLIPPING"] is False
    assert CAUSE_CODES["LOSS_DEGRADATION"] is False
    assert CAUSE_CODES["LOSS_SOILING"] is True


def test_the_four_causes_of_low_output_are_separable(config, moderate) -> None:
    """Clipping, resource limitation, derating and curtailment must not overlap."""
    frame = next(iter(moderate.inverters.values()))
    signatures = loss_signatures(config, frame)

    assert signatures["clipping"].sum() > 0
    assert (signatures["resource_limited"] & signatures["clipping"]).sum() == 0


def test_summary_reports_share_of_theoretical(config, moderate) -> None:
    """The waterfall must be readable as a report, not only as arithmetic."""
    summary = plant_waterfall(config, moderate).summary()
    assert {"cause_code", "energy_mwh", "share_of_theoretical", "avoidable"} <= set(
        summary.columns
    )
    assert summary["energy_mwh"].abs().sum() > 0


def test_loss_gate_passes(config, moderate, hot) -> None:
    """The Phase 7 acceptance gate."""
    gate = run_loss_gate(config, moderate, hot)
    assert gate.passed, gate.render()


def test_degradation_progresses_within_a_run(config) -> None:
    """A scalar degradation factor is invisible to every longitudinal method.

    Applied uniformly across a record, a year-on-year estimate recovers a rate
    of exactly zero and doc 01 section 8's fourth success criterion - inject a
    known rate, recover it blind - cannot be run at all.
    """
    from northstar_sim.physics import degradation_series

    index = pd.date_range("2023-01-01", "2026-01-01", freq="30D", tz="UTC")
    series = degradation_series(config, index)

    assert series.iloc[-1] < series.iloc[0], "degradation must accumulate"
    assert series.is_monotonic_decreasing

    implied = (series.iloc[-1] / series.iloc[0]) ** (1 / 3) - 1
    assert implied == pytest.approx(-config.module.degradation_annual, abs=0.0005)


def test_first_year_degrades_faster(config) -> None:
    """Light-induced degradation makes year one steeper, as configured."""
    year_one = 1.0 - config.degradation_at(1.0)
    year_two = config.degradation_at(1.0) - config.degradation_at(2.0)
    assert year_one > year_two
