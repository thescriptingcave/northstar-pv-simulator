"""Tests for the NorthStar Phase 1 plant model.

The suite is built around one principle: capacity is derived, so a test that
hard-codes an expected nameplate is testing the test. Assertions here check
*relationships* - that the nameplate follows from module count and rating, that
identifiers are stable, that electrical limits bind - rather than restating
numbers the implementation already computes.
"""

from __future__ import annotations

import pytest
from northstar_sim.assets import AssetType, Plant, Position
from northstar_sim.builder import (
    block_positions,
    build_plant,
    site_extent,
    weather_station_positions,
)
from northstar_sim.plant_config import (
    GridConfig,
    InverterType,
    LayoutConfig,
    ModuleType,
    PlantConfig,
    SiteConfig,
    TopologyConfig,
    TrackerConfig,
    TransformerType,
)
from northstar_sim.validation import (
    check_dc_ac_ratio,
    check_string_voltage,
    check_transformer_rating,
    validate_plant,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_config(**overrides) -> PlantConfig:
    """Build a plant configuration matching the locked reference design.

    Args:
        **overrides: Topology fields to replace, for tests that need to break
            one constraint at a time.

    Returns:
        A validated :class:`PlantConfig`.
    """
    topology = {
        "power_blocks": 10,
        "inverters_per_block": 4,
        "combiners_per_inverter": 13,
        "strings_per_combiner": 16,
        "modules_per_string": 26,
        "weather_stations": 3,
    }
    topology.update(overrides)

    return PlantConfig(
        config_version="test.1",
        site=SiteConfig(
            name="northstar",
            latitude=31.35,
            longitude=-103.30,
            elevation_m=850.0,
            timezone="America/Chicago",
        ),
        module=ModuleType(
            model="Test TOPCon 585W",
            rated_power_w=585.0,
            voc_v=52.1,
            vmp_v=43.4,
            isc_a=14.25,
            imp_a=13.48,
            efficiency=0.226,
            temp_coeff_pmax_per_c=-0.0029,
            temp_coeff_voc_per_c=-0.0025,
            bifaciality=0.70,
        ),
        inverter=InverterType(
            model="Test Central 2500 kW",
            rated_ac_kw=2500.0,
            max_dc_input_kw=3750.0,
            max_dc_voltage_v=1500.0,
            mppt_channels=6,
            peak_efficiency=0.988,
            thermal_derate_onset_c=45.0,
            trip_temp_c=75.0,
            startup_poa_wm2=20.0,
            night_standby_kw=0.25,
        ),
        transformer=TransformerType(
            model="Test 12.5 MVA",
            rated_mva=12.5,
            primary_voltage_kv=34.5,
            secondary_voltage_kv=0.69,
            no_load_loss_kw=9.0,
            load_loss_kw_at_rated=62.0,
            thermal_time_constant_min=45.0,
            high_temp_alarm_c=95.0,
            trip_temp_c=110.0,
        ),
        tracker=TrackerConfig(),
        topology=TopologyConfig(**topology),
        grid=GridConfig(poi_export_limit_kw=100000.0),
        layout=LayoutConfig(),
    )


@pytest.fixture
def config() -> PlantConfig:
    """Provide the reference configuration.

    Returns:
        A validated :class:`PlantConfig`.
    """
    return make_config()


@pytest.fixture
def plant(config: PlantConfig) -> Plant:
    """Provide the instantiated reference plant.

    Args:
        config: Reference configuration.

    Returns:
        The built :class:`Plant`.
    """
    return build_plant(config)


# --------------------------------------------------------------------------
# Derived capacity
# --------------------------------------------------------------------------


def test_capacity_is_derived_from_module_count_not_asserted(
    config: PlantConfig,
) -> None:
    """Plant DC equals module count times module rating, exactly."""
    expected_kw = config.total_modules * config.module.rated_power_w / 1000.0
    assert config.plant_dc_kw == pytest.approx(expected_kw)


def test_module_count_follows_the_topology_chain(config: PlantConfig) -> None:
    """Every count in the chain multiplies through to the module total."""
    t = config.topology
    assert config.total_modules == (
        t.power_blocks
        * t.inverters_per_block
        * t.combiners_per_inverter
        * t.strings_per_combiner
        * t.modules_per_string
    )


def test_ac_nameplate_follows_inverter_count(config: PlantConfig) -> None:
    """AC nameplate is inverter count times inverter rating."""
    assert config.plant_ac_kw == pytest.approx(
        config.total_inverters * config.inverter.rated_ac_kw
    )


def test_changing_a_count_changes_the_nameplate(config: PlantConfig) -> None:
    """Capacity tracks configuration rather than being fixed."""
    bigger = make_config(power_blocks=12)
    assert bigger.plant_dc_kw > config.plant_dc_kw
    assert bigger.plant_ac_kw > config.plant_ac_kw
    assert bigger.dc_ac_ratio == pytest.approx(config.dc_ac_ratio)


# --------------------------------------------------------------------------
# Cold-temperature string voltage - the constraint that caught a design error
# --------------------------------------------------------------------------


def test_open_circuit_voltage_rises_as_temperature_falls(config: PlantConfig) -> None:
    """Voc increases below STC, which is what constrains string length."""
    assert config.module.voc_at(-10.0) > config.module.voc_at(25.0)
    assert config.module.voc_at(25.0) == pytest.approx(config.module.voc_v)


def test_twenty_eight_modules_per_string_is_rejected() -> None:
    """The original design figure of 28 modules exceeds the 1500 V ceiling.

    Design document 03 v2.0 specified 28 modules per string and claimed roughly
    1,494 V at -10 C. The true figure is 1,586 V. This test pins the correction
    so the error cannot be reintroduced.
    """
    broken = make_config(modules_per_string=28)
    result = check_string_voltage(broken)

    assert not result.passed
    assert result.blocking
    assert broken.string_voc_cold_v > broken.inverter.max_dc_voltage_v
    assert broken.string_voc_cold_v == pytest.approx(1586.4, abs=0.5)


def test_twenty_six_modules_per_string_is_the_maximum(config: PlantConfig) -> None:
    """26 modules fit; 27 do not."""
    assert config.max_modules_per_string == 26
    assert check_string_voltage(config).passed
    assert not check_string_voltage(make_config(modules_per_string=27)).passed


def test_voc_margin_is_thin_and_positive(config: PlantConfig) -> None:
    """The design sits close to the ceiling, as a real design would."""
    assert 0 < config.voc_margin_v < 50


def test_a_warmer_design_temperature_permits_longer_strings() -> None:
    """String length is a function of the assumed extreme, not a constant."""
    mild = make_config()
    mild.site.design_min_temp_c = 5.0
    assert mild.max_modules_per_string > make_config().max_modules_per_string


# --------------------------------------------------------------------------
# Other electrical limits
# --------------------------------------------------------------------------


def test_reference_dc_ac_ratio_is_in_the_useful_clipping_band(
    config: PlantConfig,
) -> None:
    """The ratio produces recurring but non-saturating clipping."""
    assert check_dc_ac_ratio(config).passed
    assert 1.15 < config.dc_ac_ratio < 1.45


def test_an_undersized_dc_array_fails_the_ratio_check() -> None:
    """Too little DC removes clipping from the dataset entirely."""
    assert not check_dc_ac_ratio(make_config(combiners_per_inverter=8)).passed


def test_transformer_carries_its_block_at_full_output(config: PlantConfig) -> None:
    """Block peak AC is within the transformer rating."""
    assert check_transformer_rating(config).passed


def test_an_overloaded_transformer_is_rejected() -> None:
    """Six inverters per block exceed a 12.5 MVA transformer."""
    assert not check_transformer_rating(make_config(inverters_per_block=6)).passed


# --------------------------------------------------------------------------
# Asset model
# --------------------------------------------------------------------------


def test_asset_counts_match_the_configuration(config: PlantConfig, plant: Plant) -> None:
    """Every instantiated asset type is present in the expected quantity."""
    counts = plant.counts()
    assert counts[AssetType.INVERTER.value] == config.total_inverters
    assert counts[AssetType.COMBINER.value] == config.total_combiners
    assert counts[AssetType.POWER_BLOCK.value] == config.topology.power_blocks
    assert counts[AssetType.TRANSFORMER.value] == config.topology.power_blocks
    assert counts[AssetType.TRACKER_ROW_BLOCK.value] == config.total_tracker_row_blocks
    assert counts[AssetType.WEATHER_STATION.value] == config.topology.weather_stations
    assert counts[AssetType.SITE.value] == 1


def test_asset_ids_are_stable_across_builds(config: PlantConfig) -> None:
    """Two builds of one configuration produce identical identifiers in order.

    Longitudinal analysis joins on asset id. If identifiers moved between runs,
    degradation estimation and peer comparison would be silently wrong.
    """
    first = [a.asset_id for a in build_plant(config)]
    second = [a.asset_id for a in build_plant(config)]
    assert first == second


def test_asset_ids_encode_hierarchy_position(plant: Plant) -> None:
    """Identifiers are derived from position, not generated."""
    combiner = plant.of_type(AssetType.COMBINER)[0]
    assert combiner.asset_id.startswith("NORTHSTA-BLK01-INV1-CMB")
    assert combiner.parent_id == "NORTHSTA-BLK01-INV1"


def test_duplicate_asset_ids_are_rejected() -> None:
    """Constructing a plant with a repeated identifier raises."""
    from northstar_sim.assets import Asset

    duplicate = Asset("X", AssetType.SITE, None)
    with pytest.raises(ValueError, match="duplicate asset id"):
        Plant([duplicate, duplicate], config_version="t")


def test_hierarchy_navigates_up_and_down(plant: Plant) -> None:
    """Ancestors and descendants resolve consistently."""
    combiner = plant.of_type(AssetType.COMBINER)[0]
    ancestors = [a.asset_type for a in plant.ancestors(combiner.asset_id)]
    assert ancestors == [
        AssetType.INVERTER,
        AssetType.POWER_BLOCK,
        AssetType.SITE,
    ]

    block = plant.of_type(AssetType.POWER_BLOCK)[0]
    descendant_ids = {a.asset_id for a in plant.descendants(block.asset_id)}
    assert combiner.asset_id in descendant_ids


def test_modules_and_strings_are_not_instantiated_as_assets(plant: Plant) -> None:
    """216,000 module objects would cost memory for no analytical benefit."""
    assert len(plant) < 1000


def test_telemetry_bearing_set_excludes_structural_assets(plant: Plant) -> None:
    """Substation, controller and interconnection carry no telemetry stream."""
    telemetry_types = {a.asset_type for a in plant.telemetry_assets()}
    assert AssetType.SUBSTATION not in telemetry_types
    assert AssetType.PLANT_CONTROLLER not in telemetry_types
    assert AssetType.POINT_OF_INTERCONNECTION not in telemetry_types
    assert AssetType.INVERTER in telemetry_types


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def test_every_telemetry_asset_has_a_position(plant: Plant) -> None:
    """Positions drive the advected cloud field; a gap breaks spatial analysis."""
    assert all(a.position is not None for a in plant.telemetry_assets())


def test_blocks_are_laid_out_on_a_grid(config: PlantConfig) -> None:
    """Block centroids are distinct and fill rows before columns."""
    positions = block_positions(config)
    assert len(positions) == config.topology.power_blocks
    assert len({(p.x_m, p.y_m) for p in positions}) == len(positions)
    # First and sixth blocks share a column when there are five per row.
    assert positions[0].x_m == pytest.approx(positions[5].x_m)
    assert positions[5].y_m > positions[0].y_m


def test_inverters_within_a_block_are_spread_east_to_west(plant: Plant) -> None:
    """Spread is what gives a crossing cloud different arrival times."""
    block_inverters = [
        a for a in plant.of_type(AssetType.INVERTER) if a.parent_id == "NORTHSTA-BLK01"
    ]
    xs = [a.position.x_m for a in block_inverters if a.position]
    assert len(set(xs)) == len(xs)
    assert max(xs) - min(xs) > 100.0


def test_weather_stations_span_the_site(config: PlantConfig) -> None:
    """Co-located stations would agree trivially and teach nothing."""
    positions = weather_station_positions(config)
    width, _ = site_extent(config)
    spread = max(p.x_m for p in positions) - min(p.x_m for p in positions)
    assert spread == pytest.approx(width)


def test_a_single_weather_station_is_placed_at_centre(config: PlantConfig) -> None:
    """The spanning logic degrades sensibly to one station."""
    single = make_config(weather_stations=1)
    width, height = site_extent(single)
    assert weather_station_positions(single) == [Position(width / 2, height / 2)]


def test_site_extent_is_large_enough_for_observable_advection(
    config: PlantConfig,
) -> None:
    """A cloud must take longer than the telemetry interval to cross the site."""
    width, _ = site_extent(config)
    crossing_minutes = width / 8.0 / 60.0
    assert crossing_minutes > 2.0


# --------------------------------------------------------------------------
# Full validation gate
# --------------------------------------------------------------------------


def test_reference_configuration_passes_every_blocking_check(
    config: PlantConfig, plant: Plant
) -> None:
    """The locked design is buildable."""
    report = validate_plant(config, plant)
    assert report.ok, [c.detail for c in report.blocking_failures]


def test_capacity_reconciles_between_config_and_built_assets(
    config: PlantConfig, plant: Plant
) -> None:
    """Summing the asset tree reproduces the derived nameplate."""
    report = validate_plant(config, plant)
    result = next(c for c in report.results if c.name == "capacity_reconciliation")
    assert result.passed


def test_validation_fails_loudly_on_an_unbuildable_configuration() -> None:
    """A voltage violation blocks the whole configuration, not just one check."""
    broken = make_config(modules_per_string=28)
    report = validate_plant(broken, build_plant(broken))
    assert not report.ok
    assert any(c.name == "string_voc_cold" for c in report.blocking_failures)


def test_report_renders_a_verdict(config: PlantConfig, plant: Plant) -> None:
    """The rendered report states pass or fail explicitly."""
    rendered = validate_plant(config, plant).render()
    assert "PASS" in rendered
    assert "string_voc_cold" in rendered
