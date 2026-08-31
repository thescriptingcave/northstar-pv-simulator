"""Declarative plant configuration.

Everything that defines the physical plant lives here and is validated before a
simulation is allowed to start. Capacity is **derived** from module electrical
characteristics rather than asserted, so a configuration cannot claim a
nameplate its equipment does not produce.

The configuration file is shared with the fetch client. Both read the same
``[site]`` block, because a resource cache built for one location and a
simulation run for another is a failure mode that produces plausible-looking
output and no error.

Reference: design documents ``03_reference_solar_farm`` and
``05_equipment_catalog``.
"""

from __future__ import annotations

import math
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

#: Standard test condition cell temperature, degrees Celsius.
T_STC_C = 25.0

#: Standard test condition irradiance, W/m2.
G_STC_WM2 = 1000.0


class SiteConfig(BaseModel):
    """Geographic and market identity of the plant.

    Mirrors the fetch client's site block so both packages read one source of
    truth. Locked by design decision DR-001.

    Attributes:
        name: Short slug used in asset identifiers and cache paths.
        latitude: Decimal degrees north.
        longitude: Decimal degrees east, negative in the western hemisphere.
        elevation_m: Metres above sea level.
        timezone: IANA timezone, for local-solar-day analysis only.
        albedo: Ground reflectance, driving bifacial rear irradiance.
        design_min_temp_c: Extreme minimum ambient used for the cold-temperature
            open-circuit voltage check. This is a design input, not weather.
        area_acres: Site footprint.
    """

    name: str
    latitude: float
    longitude: float
    elevation_m: float
    timezone: str
    albedo: float = 0.25
    design_min_temp_c: float = -10.0
    area_acres: float = 700.0


class ModuleType(BaseModel):
    """PV module electrical and thermal characteristics.

    Attributes:
        model: Descriptive model name.
        cec_database_key: pvlib CEC module database key, recorded for lineage so
            single-diode parameters are looked up rather than invented.
        rated_power_w: Nameplate power at STC.
        voc_v: Open-circuit voltage at STC.
        vmp_v: Voltage at maximum power, STC.
        isc_a: Short-circuit current at STC.
        imp_a: Current at maximum power, STC.
        efficiency: Module conversion efficiency at STC, as a fraction.
        temp_coeff_pmax_per_c: Relative power change per degree Celsius.
            Negative for silicon.
        temp_coeff_voc_per_c: Relative open-circuit voltage change per degree
            Celsius. Negative, and the reason cold mornings limit string length.
        bifaciality: Rear-to-front power response ratio.
        degradation_year_one: First-year relative power loss, as a fraction.
        degradation_annual: Subsequent annual relative power loss.
    """

    model: str
    cec_database_key: str | None = None
    rated_power_w: float
    voc_v: float
    vmp_v: float
    isc_a: float
    imp_a: float
    efficiency: float
    temp_coeff_pmax_per_c: float
    temp_coeff_voc_per_c: float
    bifaciality: float = 0.0
    degradation_year_one: float = 0.01
    degradation_annual: float = 0.004

    def voc_at(self, temperature_c: float) -> float:
        """Compute open-circuit voltage at a given cell temperature.

        Args:
            temperature_c: Cell temperature in degrees Celsius.

        Returns:
            Open-circuit voltage in volts. Rises as temperature falls, which is
            what constrains string length on cold clear mornings.
        """
        return self.voc_v * (1.0 + self.temp_coeff_voc_per_c * (temperature_c - T_STC_C))


class InverterType(BaseModel):
    """Inverter ratings and operating thresholds.

    Attributes:
        model: Descriptive model name.
        cec_database_key: pvlib CEC inverter database key. Required: Sandia
            coefficients are looked up, never derived from datasheet values.
        rated_ac_kw: AC nameplate, the level at which clipping occurs.
        max_dc_input_kw: Maximum DC input the inverter accepts.
        max_dc_voltage_v: DC input voltage ceiling. The binding constraint on
            string length.
        mppt_channels: Independent maximum power point trackers.
        peak_efficiency: Best-case DC-to-AC conversion efficiency.
        thermal_derate_onset_c: Ambient temperature at which derating begins.
        trip_temp_c: Internal temperature causing a protective trip.
        startup_poa_wm2: Plane-of-array irradiance required to leave standby.
        night_standby_kw: Overnight parasitic draw, appearing as small negative
            AC power.
    """

    model: str
    rated_ac_kw: float
    max_dc_input_kw: float
    max_dc_voltage_v: float
    mppt_channels: int
    peak_efficiency: float
    thermal_derate_onset_c: float
    trip_temp_c: float
    startup_poa_wm2: float
    night_standby_kw: float
    cec_database_key: str | None = None


class LossConfig(BaseModel):
    """Static loss factors and derating behaviour.

    These are the losses that reduce output without any equipment being
    faulty. Each must be separately attributable, because design document
    ``02`` section 7 requires an analyst to tell recoverable losses (soiling)
    from structural ones (degradation, mismatch).

    Attributes:
        mismatch: Module-to-module and string-to-string mismatch, as a
            fraction of DC power.
        dc_wiring: Resistive DC-side loss at rated current.
        soiling_ratio: Fraction of incident irradiance reaching the modules.
            1.0 is perfectly clean. Driven by the soiling model in later work;
            held constant here so its attribution can be verified.
        plant_age_years: Plant age at the **start** of the simulated window.
            Degradation then progresses through the window rather than being
            held at this value - see
            :func:`northstar_sim.physics.degradation_series`.
        inverter_thermal_rise_c: Internal temperature rise above ambient at
            full load.
        inverter_thermal_time_constant_min: First-order lag of internal
            temperature behind loading.
        inverter_derate_slope_per_c: Fractional output reduction per degree
            above the derate onset temperature.
        max_thermal_derate: Largest fractional reduction thermal derating may
            impose before the unit trips instead.
    """

    mismatch: float = 0.02
    dc_wiring: float = 0.015
    soiling_ratio: float = 0.97
    plant_age_years: float = 2.0
    inverter_thermal_rise_c: float = 22.0
    inverter_thermal_time_constant_min: float = 12.0
    inverter_derate_slope_per_c: float = 0.02
    max_thermal_derate: float = 0.30


class TrackerConfig(BaseModel):
    """Single-axis tracker geometry and operating limits.

    Attributes:
        axis_azimuth_deg: Compass bearing of the rotation axis. 180 is a
            north-south axis, the standard horizontal single-axis arrangement.
        max_angle_deg: Rotation limit either side of horizontal.
        backtracking: Whether backtracking is enabled to avoid row-to-row
            shading at low sun angles.
        ground_coverage_ratio: Module area divided by ground area.
        module_height_m: Axis height above ground, driving rear irradiance.
        stow_wind_speed_ms: Wind speed triggering a protective stow.
        row_blocks_per_power_block: Independently actuated tracker groups per
            power block. This is the granularity at which tracker faults occur.
    """

    axis_azimuth_deg: float = 180.0
    max_angle_deg: float = 60.0
    backtracking: bool = True
    ground_coverage_ratio: float = 0.33
    module_height_m: float = 1.8
    stow_wind_speed_ms: float = 20.0
    row_blocks_per_power_block: int = 4


class TransformerType(BaseModel):
    """Block step-up transformer ratings and thermal behaviour.

    Attributes:
        model: Descriptive model name.
        rated_mva: Nameplate apparent power.
        primary_voltage_kv: Medium-voltage collection side.
        secondary_voltage_kv: Inverter side.
        no_load_loss_kw: Constant core loss, present whenever energised.
        load_loss_kw_at_rated: Copper loss at rated load, scaling with the
            square of loading.
        thermal_time_constant_min: First-order lag of winding temperature behind
            loading. The mechanism behind scenario SCN-045.
        high_temp_alarm_c: Winding temperature raising an alarm.
        trip_temp_c: Winding temperature causing a trip.
    """

    model: str
    rated_mva: float
    primary_voltage_kv: float
    secondary_voltage_kv: float
    no_load_loss_kw: float
    load_loss_kw_at_rated: float
    thermal_time_constant_min: float
    high_temp_alarm_c: float
    trip_temp_c: float


class TopologyConfig(BaseModel):
    """Plant build-out counts.

    Every capacity figure in the design is derived from these counts and the
    module rating. None of them may be set to a value that violates an
    electrical limit; that is enforced by
    :func:`northstar_sim.validation.validate_plant`.

    Attributes:
        power_blocks: Number of power blocks.
        inverters_per_block: Inverters served by one block transformer.
        combiners_per_inverter: Combiner boxes feeding one inverter.
        strings_per_combiner: String inputs per combiner.
        modules_per_string: Series-connected modules. Constrained by the
            inverter DC voltage ceiling at the design minimum temperature.
        weather_stations: Meteorological stations across the site.
    """

    power_blocks: int = Field(gt=0)
    inverters_per_block: int = Field(gt=0)
    combiners_per_inverter: int = Field(gt=0)
    strings_per_combiner: int = Field(gt=0)
    modules_per_string: int = Field(gt=0)
    weather_stations: int = Field(gt=0)


class GridConfig(BaseModel):
    """Point of interconnection limits.

    Attributes:
        poi_export_limit_kw: Maximum real power export.
        nominal_voltage_kv: Interconnection voltage.
        nominal_frequency_hz: System frequency.
    """

    poi_export_limit_kw: float
    nominal_voltage_kv: float = 345.0
    nominal_frequency_hz: float = 60.0


class LayoutConfig(BaseModel):
    """Site geometry, used to place assets for the spatial cloud field.

    Asset positions are not decoration. The advected cloud field in design
    document ``06`` section 5 samples each asset at a time offset derived from
    its position projected onto the wind vector. Without coordinates there is no
    spatial structure, and inverter peer comparison degrades to comparing
    identical inputs.

    Attributes:
        block_columns: Power blocks across the site, west to east.
        block_width_m: East-west extent of one power block.
        block_height_m: North-south extent of one power block.
        block_spacing_m: Gap between adjacent blocks.
    """

    block_columns: int = 5
    block_width_m: float = 620.0
    block_height_m: float = 460.0
    block_spacing_m: float = 40.0


class PlantConfig(BaseModel):
    """Complete plant definition.

    Attributes:
        config_version: Version stamp recorded with every simulation run.
        site: Geographic and market identity.
        module: PV module type.
        inverter: Inverter type.
        transformer: Block transformer type.
        tracker: Tracker geometry.
        losses: Static loss factors and derating behaviour.
        topology: Build-out counts.
        grid: Interconnection limits.
        layout: Site geometry.
    """

    config_version: str
    site: SiteConfig
    module: ModuleType
    inverter: InverterType
    transformer: TransformerType
    tracker: TrackerConfig
    losses: LossConfig = Field(default_factory=LossConfig)
    topology: TopologyConfig
    grid: GridConfig
    layout: LayoutConfig = Field(default_factory=LayoutConfig)

    @field_validator("config_version")
    @classmethod
    def _non_empty_version(cls, value: str) -> str:
        """Require a non-empty version stamp.

        Args:
            value: Candidate version string.

        Returns:
            The validated version.

        Raises:
            ValueError: If the version is blank. An unversioned configuration
                cannot be recorded against a dataset, which breaks
                reproducibility silently rather than loudly.
        """
        if not value.strip():
            raise ValueError("config_version must not be empty")
        return value

    # -- Derived capacity -------------------------------------------------

    @property
    def string_dc_kw(self) -> float:
        """DC nameplate of one string.

        Returns:
            Rated power in kilowatts.
        """
        return self.topology.modules_per_string * self.module.rated_power_w / 1000.0

    @property
    def strings_per_inverter(self) -> int:
        """Strings feeding one inverter.

        Returns:
            Combiner count multiplied by strings per combiner.
        """
        return self.topology.combiners_per_inverter * self.topology.strings_per_combiner

    @property
    def inverter_dc_kw(self) -> float:
        """DC nameplate connected to one inverter.

        Returns:
            Rated power in kilowatts.
        """
        return self.strings_per_inverter * self.string_dc_kw

    @property
    def total_inverters(self) -> int:
        """Inverters across the plant.

        Returns:
            Block count multiplied by inverters per block.
        """
        return self.topology.power_blocks * self.topology.inverters_per_block

    @property
    def total_strings(self) -> int:
        """Strings across the plant.

        Returns:
            Total string count.
        """
        return self.total_inverters * self.strings_per_inverter

    @property
    def total_modules(self) -> int:
        """Modules across the plant.

        Returns:
            Total module count.
        """
        return self.total_strings * self.topology.modules_per_string

    @property
    def plant_dc_kw(self) -> float:
        """Plant DC nameplate.

        Returns:
            Rated DC power in kilowatts, derived from module count and rating.
        """
        return self.total_modules * self.module.rated_power_w / 1000.0

    @property
    def plant_ac_kw(self) -> float:
        """Plant AC nameplate.

        Returns:
            Rated AC power in kilowatts.
        """
        return self.total_inverters * self.inverter.rated_ac_kw

    @property
    def dc_ac_ratio(self) -> float:
        """Ratio of DC to AC nameplate.

        Returns:
            Dimensionless ratio. Governs how often and how deeply the plant
            clips.
        """
        return self.plant_dc_kw / self.plant_ac_kw

    # -- Derived electrical limits ----------------------------------------

    def degradation_at(self, age_years: float) -> float:
        """Cumulative module degradation at a given plant age.

        Year one degrades faster than subsequent years, which is standard for
        crystalline silicon and is why the two rates are configured separately.

        Args:
            age_years: Plant age in years.

        Returns:
            Remaining fraction of original rated power.
        """
        if age_years <= 0:
            return 1.0
        first_year = 1.0 - self.module.degradation_year_one
        if age_years <= 1.0:
            return 1.0 - self.module.degradation_year_one * age_years
        remaining = age_years - 1.0
        return first_year * (1.0 - self.module.degradation_annual) ** remaining

    @property
    def degradation_factor(self) -> float:
        """Degradation at the start of the simulated window.

        Retained for single-window use. **Multi-year runs must not use this**:
        applied as a scalar it holds degradation constant across the whole
        record, and a year-on-year estimate then recovers a rate of zero. Use
        :func:`northstar_sim.physics.degradation_series` instead.

        Returns:
            Remaining fraction of original rated power at the window start.
        """
        return self.degradation_at(self.losses.plant_age_years)

    @property
    def string_voc_cold_v(self) -> float:
        """String open-circuit voltage at the design minimum temperature.

        This is the binding constraint on string length. Open-circuit voltage
        rises as temperature falls, so the worst case is a cold clear sunrise
        with the array illuminated and the inverter not yet running.

        Returns:
            String Voc in volts at ``site.design_min_temp_c``.
        """
        return self.topology.modules_per_string * self.module.voc_at(
            self.site.design_min_temp_c
        )

    @property
    def max_modules_per_string(self) -> int:
        """Largest string length that respects the inverter voltage ceiling.

        Returns:
            Module count, floored.
        """
        voc_cold = self.module.voc_at(self.site.design_min_temp_c)
        return int(math.floor(self.inverter.max_dc_voltage_v / voc_cold))

    @property
    def voc_margin_v(self) -> float:
        """Headroom between the cold string voltage and the inverter ceiling.

        Returns:
            Margin in volts. Negative means the configuration is unbuildable.
        """
        return self.inverter.max_dc_voltage_v - self.string_voc_cold_v

    @property
    def total_combiners(self) -> int:
        """Combiner boxes across the plant.

        Returns:
            Total combiner count.
        """
        return self.total_inverters * self.topology.combiners_per_inverter

    @property
    def total_tracker_row_blocks(self) -> int:
        """Independently actuated tracker groups across the plant.

        Returns:
            Total row-block count.
        """
        return self.topology.power_blocks * self.tracker.row_blocks_per_power_block


def load_plant_config(path: Path) -> PlantConfig:
    """Read and validate a plant configuration file.

    Args:
        path: Path to the TOML configuration.

    Returns:
        A validated :class:`PlantConfig`.

    Raises:
        FileNotFoundError: If the file does not exist.
        KeyError: If a required top-level section is absent.
        pydantic.ValidationError: If any section is malformed.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    required = (
        "plant",
        "site",
        "module",
        "inverter",
        "transformer",
        "topology",
        "grid",
    )
    missing = [section for section in required if section not in raw]
    if missing:
        raise KeyError(f"configuration is missing sections: {missing}")

    return PlantConfig.model_validate(
        {
            # TOML forbids bare keys after a table, so the version lives in its
            # own section rather than at the top level of a file that the fetch
            # client also appends sources to.
            "config_version": raw["plant"].get("config_version", ""),
            "site": raw["site"],
            "module": raw["module"],
            "inverter": raw["inverter"],
            "transformer": raw["transformer"],
            "tracker": raw.get("tracker", {}),
            "losses": raw.get("losses", {}),
            "topology": raw["topology"],
            "grid": raw["grid"],
            "layout": raw.get("layout", {}),
        }
    )
