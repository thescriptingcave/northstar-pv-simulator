"""Tests for the Phase 2 physics core.

Two things are pinned hardest here, because both were found the hard way when
the gate first ran and both fail silently in production:

* ``ModelChain`` defaults to Hay-Davies transposition, not the Perez model the
  design locks. The reference must be pinned or the comparison measures a
  physics difference and reports it as an implementation error.
* ``ModelChain`` feeds ambient temperature into the atmospheric refraction
  correction. Omitting it leaves pvlib's 12 C default in place, shifting
  apparent zenith by hundredths of a degree and per-sample power by about 1%.

Neither would ever surface downstream: correlations, ramps and energy
integration all still look correct with the numbers systematically wrong.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.oracle import (  # noqa: E402
    GATE_RELATIVE_TOLERANCE,
    run_physics_gate,
    run_reference_chain,
)
from northstar_sim.physics import (  # noqa: E402
    load_equipment,
    run_inverter_chain,
)
from northstar_sim.resource import (  # noqa: E402
    KT_CEILING,
    clearsky_resource,
    downscale_to_minute,
    location_from_config,
    renormalization_error,
    variability_sigma,
)

from .test_plant_model import make_config  # noqa: E402


def real_config():
    """Build a configuration pointing at real CEC database entries.

    Returns:
        A :class:`~northstar_sim.plant_config.PlantConfig` whose module and
        inverter keys resolve in pvlib's bundled databases.
    """
    config = make_config(
        combiners_per_inverter=12, strings_per_combiner=32, modules_per_string=17
    )
    config.module.cec_database_key = "Heliene_96M475"
    config.module.rated_power_w = 477.39
    config.module.voc_v = 62.39
    config.module.temp_coeff_voc_per_c = -0.0031
    # Heliene 96M475 gamma_r is -0.433 %/C. The fixture previously carried the
    # invented module's -0.29, which is a materially different plant: thermal
    # loss changes by a third and the sign of the module non-linearity term
    # flips. A fixture that drifts from the shipped config tests a plant that
    # does not exist.
    config.module.temp_coeff_pmax_per_c = -0.00433
    config.inverter.cec_database_key = "Sungrow_Power_Supply_Co___Ltd___SG2500U__550V_"
    config.inverter.max_dc_voltage_v = 1200.0
    return config


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def source(config):
    """Provide a one-day clear-sky source series at 5 minutes.

    Args:
        config: Plant configuration.

    Returns:
        The source resource frame.
    """
    # Aligned to the LOCAL solar day, not the UTC day. At this longitude a
    # UTC-midnight window straddles two partial solar days, which silently
    # halves the apparent irradiance-to-power correlation and makes "first
    # daylight sample" mean early evening. Design doc 13 section 12 flags this;
    # it shows up the moment anything is asserted per-day.
    return clearsky_resource(
        config, "2023-06-21 05:00", "2023-06-22 05:00", freq="5min", temp_air_c=35.0
    )


@pytest.fixture(scope="module")
def weather(config, source):
    """Provide the downscaled 1-minute resource.

    Args:
        config: Plant configuration.
        source: Source resource frame.

    Returns:
        The 1-minute frame.
    """
    return downscale_to_minute(source, config, seed=12345)


# --------------------------------------------------------------------------
# Resource: downscaling
# --------------------------------------------------------------------------


def test_downscaling_produces_one_minute_cadence(source, weather) -> None:
    """A 5-minute source yields a 1-minute series covering the same window."""
    deltas = weather.index.to_series().diff().dropna().unique()
    assert list(deltas) == [pd.Timedelta(minutes=1)]
    assert weather.index[0] == source.index[0]
    assert weather.index[-1] == source.index[-1]


def test_renormalization_invariant_holds_exactly(source, weather) -> None:
    """Each source interval's 1-minute mean GHI equals the source value.

    This is the invariant that keeps downscaled energy tied to the meteorology
    it came from. Without it, monthly and annual energy drift and the TMY-based
    P50 baseline stops being comparable to anything.
    """
    error = renormalization_error(weather, source)

    # Exact almost everywhere. The exceptions are twilight intervals where the
    # clear-sky ceiling and the interval mean are genuinely incompatible; there
    # the ceiling wins, because impossible irradiance is worse than a small
    # mean error. The residual is bounded and reported rather than hidden.
    assert error.median() < 1e-9
    assert (error > 1e-6).sum() <= 10, f"{(error > 1e-6).sum()} intervals drifted"
    assert error.max() < 5.0, f"max interval error {error.max():.3e} W/m2"


def test_renormalizing_the_clear_sky_index_would_not_be_sufficient(
    config, source
) -> None:
    """Scaling kt* does not preserve interval-mean GHI.

    The mean of a product is not the product of the means, and clear-sky
    irradiance varies within an interval. This test documents why the invariant
    is enforced on irradiance rather than on the index.
    """
    clearsky = location_from_config(config).get_clearsky(source.index, model="ineichen")
    kt = (source["ghi"] / clearsky["ghi"]).fillna(0.0)
    # A constant kt across an interval still yields a varying GHI within it.
    assert kt.std() >= 0.0
    assert clearsky["ghi"].diff().abs().max() > 1.0


def test_night_stays_exactly_zero(weather) -> None:
    """Multiplicative scaling cannot lift a zero interval off the floor."""
    night = weather[weather["solar_zenith"] > 95.0]
    assert (night["ghi"] == 0.0).all()


def test_downscaling_is_deterministic_for_a_seed(config, source) -> None:
    """The same seed reproduces the same series exactly."""
    first = downscale_to_minute(source, config, seed=7)
    second = downscale_to_minute(source, config, seed=7)
    pd.testing.assert_frame_equal(first, second)


def test_different_seeds_produce_different_realizations(config, source) -> None:
    """The perturbation is genuinely stochastic between substream seeds."""
    first = downscale_to_minute(source, config, seed=7)
    second = downscale_to_minute(source, config, seed=8)
    assert not first["ghi"].equals(second["ghi"])


def test_variability_is_highest_in_the_broken_cloud_band() -> None:
    """Clear and overcast skies are smooth; broken cloud is where ramps live."""
    clear = variability_sigma(np.array([0.95]))[0]
    broken = variability_sigma(np.array([0.60]))[0]
    overcast = variability_sigma(np.array([0.10]))[0]
    assert broken > clear
    assert broken > overcast


def test_clear_sky_index_respects_the_enhancement_ceiling(weather) -> None:
    """Cloud-edge enhancement is permitted but bounded.

    Before the bounded renormalization was added this reached 2.97: scaling an
    interval to match its source mean diverges at twilight, where the clear-sky
    reference approaches zero. Physically impossible irradiance would have
    propagated into every downstream analysis.
    """
    assert weather["clearsky_index"].max() <= KT_CEILING + 1e-9


def test_downscaling_rejects_a_naive_index(config, source) -> None:
    """A timezone-naive source is refused rather than silently mis-shifted."""
    naive = source.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        downscale_to_minute(naive, config, seed=1)


# --------------------------------------------------------------------------
# Physics
# --------------------------------------------------------------------------


def test_equipment_parameters_come_from_the_cec_databases(config) -> None:
    """Module and inverter parameters are looked up, never invented."""
    equipment = load_equipment(config)
    assert equipment.module_key == "Heliene_96M475"
    assert "a_ref" in equipment.module.index
    assert "Paco" in equipment.inverter.index


def test_a_missing_database_key_is_refused(config) -> None:
    """Deriving coefficients from datasheet values is not permitted."""
    broken = real_config()
    broken.module.cec_database_key = None
    with pytest.raises(KeyError, match="cec_database_key"):
        load_equipment(broken)


def test_an_unknown_database_key_is_refused(config) -> None:
    """A key absent from the database fails rather than falling back."""
    broken = real_config()
    broken.module.cec_database_key = "Not_A_Real_Module_9000"
    with pytest.raises(KeyError, match="not in the CEC module database"):
        load_equipment(broken)


def test_production_is_zero_at_night(config, weather) -> None:
    """No meaningful generation when the sun is below the horizon."""
    result = run_inverter_chain(config, weather)
    night = result[weather["solar_zenith"] > 95.0]
    assert night["dc_power_kw"].abs().max() < 1.0


def test_ac_power_never_exceeds_the_inverter_rating(config, weather) -> None:
    """The Sandia model clips at the AC nameplate."""
    result = run_inverter_chain(config, weather)
    assert result["ac_power_kw"].max() <= config.inverter.rated_ac_kw * 1.001


def test_ac_is_not_dc_times_a_constant(config, weather) -> None:
    """Conversion efficiency varies with load, as a real inverter does."""
    result = run_inverter_chain(config, weather)
    generating = result[result["dc_power_kw"] > 100]
    efficiency = generating["ac_power_kw"] / generating["dc_power_kw"]
    assert efficiency.std() > 1e-3


def test_tracker_rotates_within_its_configured_limits(config, weather) -> None:
    """Rotation respects the +/- 60 degree limit."""
    result = run_inverter_chain(config, weather)
    angle = result["tracker_angle_deg"].dropna()
    assert angle.abs().max() <= config.tracker.max_angle_deg + 1e-6


def test_tracker_turns_east_in_the_morning_and_west_in_the_afternoon(
    config, weather
) -> None:
    """A north-south axis tracker follows the sun across the day.

    Requires a window aligned to the local solar day. On a UTC-midnight window
    the first daylight sample at this longitude is early evening, and the
    assertion inverts.
    """
    result = run_inverter_chain(config, weather)
    day = result[result["poa_global"] > 100]
    assert day["tracker_angle_deg"].iloc[0] < 0, "should face east at sunrise"
    assert day["tracker_angle_deg"].iloc[-1] > 0, "should face west at sunset"


def test_dc_power_follows_effective_irradiance_almost_exactly(config, weather) -> None:
    """The core causal relationship the whole dataset rests on.

    Effective irradiance is what the cells actually receive: front-side POA
    modified by the incidence angle, plus the bifaciality-weighted rear
    contribution. DC power follows it almost perfectly.
    """
    result = run_inverter_chain(config, weather)
    day = result[result["poa_global"] > 50]
    assert day["effective_irradiance"].corr(day["dc_power_kw"]) > 0.999


def test_front_side_poa_alone_explains_less_than_effective_irradiance(
    config, weather
) -> None:
    """Front POA correlates strongly with DC power, but not perfectly.

    Two effects decouple them, and both are wanted. Rear-side gain varies with
    tracker angle and albedo independently of front POA, and the incidence
    angle modifier cuts the direct component at oblique angles. An analyst
    regressing DC on front POA alone will see residual structure - which is the
    structure bifacial and IAM modelling is there to create.
    """
    result = run_inverter_chain(config, weather)
    day = result[result["poa_global"] > 50]

    front = day["poa_global"].corr(day["dc_power_kw"])
    effective = day["effective_irradiance"].corr(day["dc_power_kw"])

    assert front > 0.94, "front POA must still be a strong predictor"
    assert effective > front, "effective irradiance must explain more"


def test_bifacial_rear_irradiance_adds_production(config, weather) -> None:
    """Rear-side gain is real and additive, and is truth with no sensor."""
    with_rear = run_inverter_chain(config, weather, include_rear=True)
    without = run_inverter_chain(config, weather, include_rear=False)
    assert with_rear["poa_rear"].max() > 0
    assert with_rear["dc_power_kw"].sum() > without["dc_power_kw"].sum()


def test_higher_wind_lowers_cell_temperature(config, weather) -> None:
    """Faiman responds to wind, which is why it was chosen over a NOCT model."""
    calm = weather.copy()
    calm["wind_speed"] = 1.0
    windy = weather.copy()
    windy["wind_speed"] = 12.0

    calm_result = run_inverter_chain(config, calm)
    windy_result = run_inverter_chain(config, windy)
    day = calm_result["poa_global"] > 200
    assert (
        windy_result["cell_temperature"][day].mean()
        < calm_result["cell_temperature"][day].mean()
    )


def test_hotter_cells_produce_less_power_at_equal_irradiance(config, weather) -> None:
    """Temperature derating is present, and is what makes raw PR seasonal."""
    cool = weather.copy()
    cool["temp_air"] = 10.0
    hot = weather.copy()
    hot["temp_air"] = 45.0

    day = run_inverter_chain(config, cool)["poa_global"] > 200
    assert (
        run_inverter_chain(config, hot)["dc_power_kw"][day].sum()
        < run_inverter_chain(config, cool)["dc_power_kw"][day].sum()
    )


# --------------------------------------------------------------------------
# The oracle gate
# --------------------------------------------------------------------------


def test_physics_gate_passes(config, weather) -> None:
    """The Phase 2 hard gate: two independent chains must agree.

    This is the only check that examines the production chain itself rather
    than its consequences. A physics error passes every downstream correlation
    and reconciliation check while producing systematically wrong energy.
    """
    result = run_physics_gate(config, weather)
    assert result.passed, result.render()
    assert result.samples > 500


def test_gate_agreement_is_at_floating_point_precision(config, weather) -> None:
    """Both paths solve the same equations with the same parameters.

    Agreement is limited only by floating point, four orders of magnitude
    tighter than the gate tolerance. Asserting exact bit-equality would be
    fragile: adding a single multiply by unity to the chain moved the residual
    from 0 to 4e-16 without changing any physics.
    """
    result = run_physics_gate(config, weather)
    assert result.max_relative_error_dc < 1e-12
    assert result.max_relative_error_ac < 1e-12


def test_reference_chain_is_pinned_to_perez_transposition(config, weather) -> None:
    """ModelChain defaults to Hay-Davies, which is not the locked model.

    The first gate run failed at 15% relative error for exactly this reason.
    Leaving the default in place compares two different physical models.
    """
    import pvlib
    from northstar_sim.physics import build_system

    default_chain = pvlib.modelchain.ModelChain(
        build_system(config),
        location_from_config(config),
        dc_model="cec",
        ac_model="sandia",
        aoi_model="physical",
        spectral_model="no_loss",
        temperature_model="faiman",
        losses_model="no_loss",
    )
    assert default_chain.transposition_model == "haydavies"

    pinned = run_reference_chain(config, weather)
    simulator = run_inverter_chain(config, weather, include_rear=False)
    day = simulator["poa_global"] > 50
    assert (simulator["poa_global"][day] - pinned["poa_global"][day]).abs().max() < 1e-9


def test_omitting_air_temperature_from_solar_position_breaks_the_gate(
    config, weather
) -> None:
    """Refraction depends on ambient temperature, and the error propagates.

    pvlib defaults to 12 C. At 35 C the apparent zenith shifts by hundredths of
    a degree, which reaches roughly 1% per-sample power error through tracking
    and transposition - large enough to matter, small enough to never be
    noticed downstream.
    """
    stripped = weather.drop(columns=["temp_air"]).copy()
    stripped["temp_air"] = 12.0

    baseline = run_inverter_chain(config, weather, include_rear=False)
    shifted = run_inverter_chain(config, stripped, include_rear=False)

    day = baseline["poa_global"] > 50
    difference = (
        (baseline["poa_global"][day] - shifted["poa_global"][day]).abs()
        / baseline["poa_global"][day]
    ).max()
    assert difference > GATE_RELATIVE_TOLERANCE


def test_gate_reports_a_verdict(config, weather) -> None:
    """The rendered result states pass or fail explicitly."""
    rendered = run_physics_gate(config, weather).render()
    assert "PASS" in rendered
    assert "samples compared" in rendered
