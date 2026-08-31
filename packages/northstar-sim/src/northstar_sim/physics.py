"""Production physics for a single inverter.

The chain is written as explicit, named pvlib calls rather than delegated to
``ModelChain``. That is deliberate: :mod:`northstar_sim.oracle` runs the same
physics *through* ``ModelChain`` and compares, so the two code paths must be
genuinely independent for the comparison to mean anything. A wrapper that called
``ModelChain`` internally would compare it against itself and always pass.

Nothing here applies faults, control setpoints, curtailment or sensor error.
This is unconstrained physical truth, which is what design document ``07``
section 4 calls the validation oracle baseline.

Reference: design document ``07_solar_production_model`` sections 2-3.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd
import pvlib

from .plant_config import PlantConfig
from .resource import location_from_config

#: Locked model chain. Every entry names a specific validated pvlib model, per
#: design decision DR-003. Hand-rolled physics is where simulators quietly
#: become wrong, and a wrong core invalidates everything downstream while
#: passing every correlation check.
MODEL_CHAIN = {
    "solar_position": "NREL SPA (pvlib.solarposition.spa_python)",
    "clear_sky": "Ineichen-Perez",
    "tracking": "pvlib.tracking.singleaxis, backtracking",
    "transposition": "Perez",
    "rear_irradiance": "pvlib.bifacial.infinite_sheds",
    "incidence_angle": "pvlib.iam.physical",
    "cell_temperature": "pvlib.temperature.faiman",
    "dc_model": "CEC single-diode (calcparams_cec + singlediode)",
    "ac_model": "Sandia (pvlib.inverter.sandia)",
}


@dataclass(frozen=True)
class EquipmentParameters:
    """Validated equipment parameters retrieved from the CEC databases.

    Attributes:
        module: CEC single-diode parameters for the selected module.
        inverter: Sandia model coefficients for the selected inverter.
        module_key: Database key of the module, recorded for lineage.
        inverter_key: Database key of the inverter.
    """

    module: pd.Series
    inverter: pd.Series
    module_key: str
    inverter_key: str


@lru_cache(maxsize=4)
def _retrieve_sam(name: str) -> pd.DataFrame:
    """Retrieve and cache a SAM equipment database.

    ``pvlib.pvsystem.retrieve_sam`` parses a bundled CSV on every call, which
    costs about 135 ms. At 40 inverters that is 5 seconds per simulated day
    spent re-reading two files whose contents never change within a run.

    Args:
        name: Database name, ``CECMod`` or ``CECInverter``.

    Returns:
        The parsed database.
    """
    return pvlib.pvsystem.retrieve_sam(name)


@dataclass(frozen=True)
class SiteGeometry:
    """Solar and tracker geometry, shared by every asset on the site.

    Solar position, tracker orientation, airmass and extraterrestrial
    irradiance depend only on location and time, not on the irradiance an
    individual asset receives. Computing them once and sharing them removes the
    largest source of duplicated work in a full-plant run.

    Attributes:
        solar_position: Apparent zenith and azimuth.
        surface_tilt: Tracker surface tilt, night-stowed flat.
        surface_azimuth: Tracker surface azimuth.
        tracker_angle: Signed rotation angle.
        aoi: Angle of incidence on the tracked surface.
        iam: Incidence angle modifier.
        dni_extra: Extraterrestrial irradiance.
        airmass: Relative airmass.
    """

    solar_position: pd.DataFrame
    surface_tilt: pd.Series
    surface_azimuth: pd.Series
    tracker_angle: pd.Series
    aoi: pd.Series
    iam: pd.Series
    dni_extra: pd.Series
    airmass: pd.Series


def build_site_geometry(config: PlantConfig, weather: pd.DataFrame) -> SiteGeometry:
    """Compute the geometry shared by every asset for a given time index.

    Args:
        config: Plant configuration.
        weather: Any frame carrying the target time index, ``temp_air`` and
            optionally ``pressure``. Only the shared columns are used.

    Returns:
        The computed :class:`SiteGeometry`.
    """
    location = location_from_config(config)

    # Ambient temperature and pressure enter the atmospheric refraction
    # correction. Omitting them leaves pvlib's 12 C default in place, which
    # shifts apparent zenith by up to 0.04 degrees - small, but it propagates
    # through tracking and transposition to a per-sample power error of about
    # 1%. ModelChain passes them, so the simulator must too.
    solar_position = location.get_solarposition(
        weather.index,
        temperature=weather.get("temp_air", 12.0),
        **({"pressure": weather["pressure"]} if "pressure" in weather else {}),
    )
    # `apparent_azimuth` was renamed to `solar_azimuth` in pvlib 0.13.1 and the
    # old name warns on every call. Azimuth has no refraction correction, so
    # the rename is cosmetic - but the old name will eventually be removed.
    tracking = pvlib.tracking.singleaxis(
        apparent_zenith=solar_position["apparent_zenith"],
        solar_azimuth=solar_position["azimuth"],
        axis_tilt=0.0,
        axis_azimuth=config.tracker.axis_azimuth_deg,
        max_angle=config.tracker.max_angle_deg,
        backtrack=config.tracker.backtracking,
        gcr=config.tracker.ground_coverage_ratio,
    )
    # At night the tracker has no defined orientation; stow flat so downstream
    # geometry stays finite rather than propagating NaN into the power series.
    surface_tilt = tracking["surface_tilt"].fillna(0.0)
    surface_azimuth = tracking["surface_azimuth"].fillna(config.tracker.axis_azimuth_deg)
    aoi = pvlib.irradiance.aoi(
        surface_tilt,
        surface_azimuth,
        solar_position["apparent_zenith"],
        solar_position["azimuth"],
    )
    return SiteGeometry(
        solar_position=solar_position,
        surface_tilt=surface_tilt,
        surface_azimuth=surface_azimuth,
        tracker_angle=tracking["tracker_theta"],
        aoi=aoi,
        iam=pvlib.iam.physical(aoi),
        dni_extra=pvlib.irradiance.get_extra_radiation(weather.index),
        airmass=location.get_airmass(solar_position=solar_position)["airmass_relative"],
    )


def load_equipment(config: PlantConfig) -> EquipmentParameters:
    """Retrieve module and inverter parameters from the CEC databases.

    Parameters are looked up, never invented. A configuration that names a key
    absent from the database fails here rather than silently falling back to
    plausible-looking coefficients.

    Args:
        config: Plant configuration carrying both database keys.

    Returns:
        The retrieved :class:`EquipmentParameters`.

    Raises:
        KeyError: If either database key is missing or not found.
    """
    module_key = config.module.cec_database_key
    inverter_key = config.inverter.cec_database_key
    if not module_key or not inverter_key:
        raise KeyError(
            "both module.cec_database_key and inverter.cec_database_key must be "
            "set; deriving single-diode or Sandia coefficients from datasheet "
            "values is not permitted (design doc 05)"
        )

    modules = _retrieve_sam("CECMod")
    inverters = _retrieve_sam("CECInverter")

    if module_key not in modules.columns:
        raise KeyError(f"module {module_key!r} not in the CEC module database")
    if inverter_key not in inverters.columns:
        raise KeyError(f"inverter {inverter_key!r} not in the CEC inverter database")

    return EquipmentParameters(
        module=modules[module_key],
        inverter=inverters[inverter_key],
        module_key=module_key,
        inverter_key=inverter_key,
    )


def temperature_parameters() -> dict[str, float]:
    """Return Faiman cell temperature model coefficients.

    Faiman is used rather than a NOCT approximation because it responds to wind
    speed, which is what produces the required behaviour that two timestamps at
    equal irradiance differ in output because one was windier.

    Returns:
        The ``u0`` and ``u1`` coefficients for an open-rack array.
    """
    return pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"][
        "open_rack_glass_glass"
    ] | {"u0": 25.0, "u1": 6.84}


def build_array_mount(config: PlantConfig) -> pvlib.pvsystem.SingleAxisTrackerMount:
    """Construct the tracker mount described by the configuration.

    Args:
        config: Plant configuration.

    Returns:
        A configured single-axis tracker mount.
    """
    tracker = config.tracker
    return pvlib.pvsystem.SingleAxisTrackerMount(
        axis_tilt=0.0,
        axis_azimuth=tracker.axis_azimuth_deg,
        max_angle=tracker.max_angle_deg,
        backtrack=tracker.backtracking,
        gcr=tracker.ground_coverage_ratio,
    )


def build_system(config: PlantConfig) -> pvlib.pvsystem.PVSystem:
    """Construct a pvlib system representing one inverter and its array.

    Args:
        config: Plant configuration.

    Returns:
        A :class:`pvlib.pvsystem.PVSystem` sized to a single inverter.
    """
    equipment = load_equipment(config)
    array = pvlib.pvsystem.Array(
        mount=build_array_mount(config),
        module_parameters=equipment.module.to_dict(),
        temperature_model_parameters={"u0": 25.0, "u1": 6.84},
        modules_per_string=config.topology.modules_per_string,
        strings=config.strings_per_inverter,
    )
    return pvlib.pvsystem.PVSystem(
        arrays=[array],
        inverter_parameters=equipment.inverter.to_dict(),
        albedo=config.site.albedo,
        name=f"{config.site.name}-inverter",
    )


def run_inverter_chain(
    config: PlantConfig,
    weather: pd.DataFrame,
    *,
    include_rear: bool = True,
    geometry: SiteGeometry | None = None,
    rear_irradiance: pd.Series | None = None,
    apply_plant_losses: bool = True,
) -> pd.DataFrame:
    """Compute unconstrained production for one inverter, step by explicit step.

    Args:
        config: Plant configuration.
        weather: 1-minute frame with ``ghi``, ``dni``, ``dhi``, ``temp_air`` and
            ``wind_speed``, indexed by UTC timestamp.
        include_rear: Whether to add bifacial rear irradiance. Disabled by the
            physics gate, because ``ModelChain`` has no equivalent step and the
            comparison must be like for like.
        geometry: Precomputed shared geometry. Supplied by the plant runner so
            solar position and tracking are computed once per timestep rather
            than once per inverter.
        rear_irradiance: Precomputed rear-side irradiance. The ground view
            factor integration depends on tracker geometry, which is identical
            across the site, so it is computed per block rather than per
            inverter.
        apply_plant_losses: Whether to apply degradation, mismatch, DC wiring
            and thermal derating. Disabled by the physics gate: ``ModelChain``
            models module and inverter physics only, so leaving plant losses on
            would compare a plant against a module and report the difference as
            an implementation error.

    Returns:
        A frame carrying tracker angle, plane-of-array components, effective
        irradiance, cell temperature, DC power and AC power.
    """
    equipment = load_equipment(config)
    module = equipment.module
    geom = geometry if geometry is not None else build_site_geometry(config, weather)
    solar_position = geom.solar_position

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=geom.surface_tilt,
        surface_azimuth=geom.surface_azimuth,
        solar_zenith=solar_position["apparent_zenith"],
        solar_azimuth=solar_position["azimuth"],
        dni=weather["dni"],
        ghi=weather["ghi"],
        dhi=weather["dhi"],
        dni_extra=geom.dni_extra,
        airmass=geom.airmass,
        albedo=config.site.albedo,
        model="perez",
    )

    effective = poa["poa_direct"] * geom.iam + poa["poa_diffuse"]

    rear = pd.Series(0.0, index=weather.index)
    if include_rear and config.module.bifaciality > 0:
        rear = (
            rear_irradiance
            if rear_irradiance is not None
            else compute_rear_irradiance(
                config, weather, solar_position, geom.surface_tilt
            )
        )
        effective = effective + config.module.bifaciality * rear

    cell_temperature = pvlib.temperature.faiman(
        poa_global=poa["poa_global"],
        temp_air=weather["temp_air"],
        wind_speed=weather["wind_speed"],
        u0=25.0,
        u1=6.84,
    )

    diode = pvlib.pvsystem.calcparams_cec(
        effective_irradiance=effective,
        temp_cell=cell_temperature,
        alpha_sc=float(module["alpha_sc"]),
        a_ref=float(module["a_ref"]),
        I_L_ref=float(module["I_L_ref"]),
        I_o_ref=float(module["I_o_ref"]),
        R_sh_ref=float(module["R_sh_ref"]),
        R_s=float(module["R_s"]),
        Adjust=float(module["Adjust"]),
    )
    # At night effective irradiance is zero, so the photocurrent and the
    # saturation currents are zero too and the bracketing solver inside
    # `singlediode` divides zero by zero. The result is correctly NaN and is
    # zero-filled below, but scipy emits a RuntimeWarning per call - roughly
    # forty lines of noise on any real run.
    #
    # Suppressed narrowly, at the one call that produces it, rather than with a
    # global filter that would also hide genuine numerical trouble.
    with np.errstate(divide="ignore", invalid="ignore"):
        single_diode = pvlib.pvsystem.singlediode(*diode)

    modules = config.topology.modules_per_string * config.strings_per_inverter
    dc_ideal_w = single_diode["p_mp"] * modules
    dc_voltage_v = single_diode["v_mp"] * config.topology.modules_per_string

    # DC-side losses that reduce output with no equipment fault. Applied as
    # multiplicative factors so each is exactly attributable in the waterfall.
    losses = config.losses
    dc_power_w = dc_ideal_w
    if apply_plant_losses:
        dc_power_w = (
            dc_ideal_w
            * degradation_series(config, weather.index)
            * (1.0 - losses.mismatch)
            * (1.0 - losses.dc_wiring)
        )

    inverter_parameters = equipment.inverter.to_dict()
    ac_preclip_w = sandia_preclip(
        dc_voltage_v.fillna(0.0), dc_power_w.fillna(0.0), inverter_parameters
    )
    ac_power_w = pvlib.inverter.sandia(
        v_dc=dc_voltage_v.fillna(0.0),
        p_dc=dc_power_w.fillna(0.0),
        inverter=inverter_parameters,
    )

    internal_temp = inverter_internal_temperature(
        config, ac_power_w / 1000.0, weather["temp_air"]
    )
    derate_factor = (
        thermal_derate_factor(config, internal_temp)
        if apply_plant_losses
        else pd.Series(1.0, index=weather.index)
    )
    ac_power_w = ac_power_w * derate_factor

    return pd.DataFrame(
        {
            "tracker_angle_deg": geom.tracker_angle,
            "surface_tilt": geom.surface_tilt,
            "poa_global": poa["poa_global"],
            "poa_direct": poa["poa_direct"],
            "poa_diffuse": poa["poa_diffuse"],
            "poa_rear": rear,
            "effective_irradiance": effective,
            "cell_temperature": cell_temperature,
            "dc_ideal_kw": dc_ideal_w / 1000.0,
            "dc_power_kw": dc_power_w / 1000.0,
            "dc_voltage_v": dc_voltage_v,
            "ac_preclip_kw": ac_preclip_w / 1000.0,
            "ac_power_kw": ac_power_w / 1000.0,
            "internal_temp_c": internal_temp,
            "thermal_derate_factor": derate_factor,
        },
        index=weather.index,
    )


def compute_rear_irradiance(
    config: PlantConfig,
    weather: pd.DataFrame,
    solar_position: pd.DataFrame,
    surface_tilt: pd.Series,
) -> pd.Series:
    """Compute rear-side plane-of-array irradiance for the tracked array.

    The rear face points the opposite way to the front. Its tilt is
    ``180 - front_tilt`` and its azimuth is ``front_azimuth + 180``. Passing the
    *front* orientation returns front-side irradiance from the infinite-sheds
    model, which then gets added back as bifacial gain and drives DC power to
    155% of nameplate - the symptom that exposed this.

    Rear irradiance is simulator truth with no corresponding sensor: real plants
    do not measure it. It is the clearest concrete example of the truth-versus-
    measurement distinction the telemetry specification requires.

    Args:
        config: Plant configuration.
        weather: Resource frame.
        solar_position: Solar position frame for the same index.
        surface_tilt: Front-surface tilt series.

    Returns:
        Rear-side irradiance in W/m2, zero where the geometry is undefined.
    """
    tracker = config.tracker
    front_azimuth = pd.Series(tracker.axis_azimuth_deg, index=weather.index)

    result = pvlib.bifacial.infinite_sheds.get_irradiance_poa(
        surface_tilt=180.0 - surface_tilt,
        surface_azimuth=(front_azimuth + 180.0) % 360.0,
        solar_zenith=solar_position["apparent_zenith"],
        solar_azimuth=solar_position["azimuth"],
        gcr=tracker.ground_coverage_ratio,
        height=tracker.module_height_m,
        pitch=1.0 / tracker.ground_coverage_ratio,
        ghi=weather["ghi"],
        dhi=weather["dhi"],
        dni=weather["dni"],
        albedo=config.site.albedo,
    )
    return pd.Series(result["poa_global"], index=weather.index).fillna(0.0)


def sandia_preclip(v_dc: pd.Series, p_dc: pd.Series, inverter: dict) -> pd.Series:
    """Evaluate the Sandia inverter model without the AC power cap.

    ``pvlib.inverter.sandia`` applies ``min(Paco, ...)`` internally, so clipped
    and unclipped output are indistinguishable in its result. Separating them is
    the only way to quantify clipping loss, which requires knowing what the
    inverter *would* have produced.

    The formulation is the published Sandia model with the cap omitted; the
    coefficients come from the same CEC database entry, so this is not an
    approximation of the efficiency curve.

    Args:
        v_dc: DC input voltage.
        p_dc: DC input power in watts.
        inverter: Sandia model coefficients.

    Returns:
        AC power in watts before clipping, floored at zero.
    """
    paco = float(inverter["Paco"])
    pdco = float(inverter["Pdco"])
    vdco = float(inverter["Vdco"])
    pso = float(inverter["Pso"])
    c0, c1, c2, c3 = (float(inverter[key]) for key in ("C0", "C1", "C2", "C3"))

    voltage_delta = v_dc - vdco
    a = pdco * (1.0 + c1 * voltage_delta)
    b = pso * (1.0 + c2 * voltage_delta)
    c = c0 * (1.0 + c3 * voltage_delta)

    denominator = (a - b).replace(0.0, float("nan"))
    power = ((paco / denominator) - c * (a - b)) * (p_dc - b) + c * (p_dc - b) ** 2
    return power.fillna(0.0).clip(lower=0.0)


def inverter_internal_temperature(
    config: PlantConfig, ac_power_kw: pd.Series, ambient_c: pd.Series
) -> pd.Series:
    """Model inverter internal temperature from ambient and loading.

    Internal temperature is what triggers thermal derating, and it lags loading
    because the enclosure has thermal mass. That lag matters analytically: an
    inverter can still be derating after irradiance has fallen, which looks like
    underperformance to anyone comparing output against instantaneous
    irradiance alone.

    Args:
        config: Plant configuration.
        ac_power_kw: Inverter AC output.
        ambient_c: Ambient temperature.

    Returns:
        Internal temperature in degrees Celsius.
    """
    losses = config.losses
    loading = (ac_power_kw / config.inverter.rated_ac_kw).clip(lower=0.0)
    steady_state = ambient_c + losses.inverter_thermal_rise_c * loading

    if len(ac_power_kw.index) < 2:
        return steady_state
    interval_minutes = (
        ac_power_kw.index[1] - ac_power_kw.index[0]
    ).total_seconds() / 60.0
    alpha = 1.0 - np.exp(-interval_minutes / losses.inverter_thermal_time_constant_min)
    return steady_state.ewm(alpha=alpha, adjust=False).mean()


def thermal_derate_factor(config: PlantConfig, internal_temp_c: pd.Series) -> pd.Series:
    """Compute the output reduction imposed by inverter thermal derating.

    The configured onset is an **ambient** threshold, as inverter datasheets
    state it. Comparing it directly to internal temperature confuses the two and
    makes derating fire constantly: at 42 C ambient the model hit its 30% cap
    and held AC output at 1,750 kW instead of 2,500. The internal onset is the
    ambient onset plus the full-load temperature rise.

    Args:
        config: Plant configuration.
        internal_temp_c: Inverter internal temperature.

    Returns:
        Multiplicative factor between ``1 - max_thermal_derate`` and 1.0.
    """
    losses = config.losses
    internal_onset = (
        config.inverter.thermal_derate_onset_c + losses.inverter_thermal_rise_c
    )
    excess = (internal_temp_c - internal_onset).clip(lower=0.0)
    reduction = (excess * losses.inverter_derate_slope_per_c).clip(
        upper=losses.max_thermal_derate
    )
    return 1.0 - reduction


def degradation_series(config: PlantConfig, index: pd.DatetimeIndex) -> pd.Series:
    """Compute module degradation progressing across a time window.

    Degradation **must vary within the record**, not be held at a single value.
    Applied as a scalar it is invisible to every longitudinal method: a
    year-on-year estimate over a multi-year dataset recovers a rate of exactly
    zero, and the strongest validation available - inject a known rate, recover
    it blind - cannot be run at all.

    Plant age is taken as the configured age at the window start, advancing with
    the index.

    Args:
        config: Plant configuration.
        index: Simulation time index.

    Returns:
        Remaining fraction of original rated power at each timestep.
    """
    if len(index) == 0:
        return pd.Series(dtype=float)

    start_age = config.losses.plant_age_years
    elapsed = (index - index[0]).total_seconds() / (365.25 * 86400.0)
    ages = start_age + np.asarray(elapsed)

    return pd.Series([config.degradation_at(float(age)) for age in ages], index=index)
