"""Instantiate a complete plant from configuration.

Construction is deterministic: the same configuration always produces the same
assets with the same identifiers and the same coordinates. Nothing here consults
a random number generator, and nothing depends on dictionary or set iteration
order.

Reference: design document ``16_implementation_roadmap`` section 4.
"""

from __future__ import annotations

from .assets import Asset, AssetType, Plant, Position
from .plant_config import PlantConfig


def _site_id(config: PlantConfig) -> str:
    """Derive the site identifier prefix from the site name.

    Args:
        config: Plant configuration.

    Returns:
        An uppercase prefix used by every descendant identifier.
    """
    return config.site.name.upper()[:8]


def block_positions(config: PlantConfig) -> list[Position]:
    """Compute the centroid of each power block in the site frame.

    Blocks are laid out on a regular grid, filling west to east and then north.
    Real sites follow parcel boundaries and terrain, but a regular grid is
    sufficient for the purpose these coordinates serve: giving the advected
    cloud field a geometry across which to lag irradiance between assets.

    Args:
        config: Plant configuration.

    Returns:
        One position per power block, in block order.
    """
    layout = config.layout
    columns = layout.block_columns
    pitch_x = layout.block_width_m + layout.block_spacing_m
    pitch_y = layout.block_height_m + layout.block_spacing_m

    positions: list[Position] = []
    for index in range(config.topology.power_blocks):
        column = index % columns
        row = index // columns
        positions.append(
            Position(
                x_m=column * pitch_x + layout.block_width_m / 2.0,
                y_m=row * pitch_y + layout.block_height_m / 2.0,
            )
        )
    return positions


def site_extent(config: PlantConfig) -> tuple[float, float]:
    """Compute the overall site footprint.

    Args:
        config: Plant configuration.

    Returns:
        A tuple of east-west and north-south extent in metres.
    """
    layout = config.layout
    blocks = config.topology.power_blocks
    columns = min(layout.block_columns, blocks)
    rows = -(-blocks // layout.block_columns)  # ceiling division

    width = columns * layout.block_width_m + (columns - 1) * layout.block_spacing_m
    height = rows * layout.block_height_m + (rows - 1) * layout.block_spacing_m
    return width, height


def weather_station_positions(config: PlantConfig) -> list[Position]:
    """Place weather stations to span the site footprint.

    Stations are spread along the long axis and offset from block centroids.
    Spacing matters: co-located stations would agree trivially, defeating the
    weather-station comparison in design document ``02`` section 4, while
    stations at the extremes see genuinely different cloud arrival times.

    Args:
        config: Plant configuration.

    Returns:
        One position per configured weather station.
    """
    width, height = site_extent(config)
    count = config.topology.weather_stations

    if count == 1:
        return [Position(width / 2.0, height / 2.0)]

    positions: list[Position] = []
    for index in range(count):
        fraction = index / (count - 1)
        # Alternate north and south of centre so stations are not collinear with
        # the block grid, which would make them redundant with block telemetry.
        offset = height * (0.25 if index % 2 == 0 else 0.75)
        positions.append(Position(x_m=fraction * width, y_m=offset))
    return positions


def build_plant(config: PlantConfig) -> Plant:
    """Instantiate every asset described by a configuration.

    Args:
        config: Validated plant configuration.

    Returns:
        A :class:`Plant` containing the full asset hierarchy.
    """
    prefix = _site_id(config)
    assets: list[Asset] = []

    width, height = site_extent(config)
    centre = Position(width / 2.0, height / 2.0)

    assets.append(
        Asset(
            asset_id=prefix,
            asset_type=AssetType.SITE,
            parent_id=None,
            position=centre,
            rated_capacity_kw=config.plant_ac_kw,
            attributes={
                "dc_capacity_kw": round(config.plant_dc_kw, 3),
                "dc_ac_ratio": round(config.dc_ac_ratio, 4),
                "latitude": config.site.latitude,
                "longitude": config.site.longitude,
                "timezone": config.site.timezone,
                "albedo": config.site.albedo,
                "extent_ew_m": round(width, 1),
                "extent_ns_m": round(height, 1),
            },
        )
    )

    # Plant-level singletons. These carry no position of their own because they
    # are electrical or logical boundaries rather than distributed assets.
    for asset_type, suffix in (
        (AssetType.SUBSTATION, "SUB"),
        (AssetType.REVENUE_METER, "METER"),
        (AssetType.PLANT_CONTROLLER, "CTRL"),
        (AssetType.POINT_OF_INTERCONNECTION, "POI"),
    ):
        assets.append(
            Asset(
                asset_id=f"{prefix}-{suffix}",
                asset_type=asset_type,
                parent_id=prefix,
                position=centre,
                rated_capacity_kw=(
                    config.grid.poi_export_limit_kw
                    if asset_type is AssetType.POINT_OF_INTERCONNECTION
                    else None
                ),
            )
        )

    for index, position in enumerate(weather_station_positions(config), start=1):
        assets.append(
            Asset(
                asset_id=f"{prefix}-WS{index}",
                asset_type=AssetType.WEATHER_STATION,
                parent_id=prefix,
                position=position,
            )
        )

    block_dc = config.inverter_dc_kw * config.topology.inverters_per_block
    block_ac = config.inverter.rated_ac_kw * config.topology.inverters_per_block

    for block_index, block_position in enumerate(block_positions(config), start=1):
        block_id = f"{prefix}-BLK{block_index:02d}"
        assets.append(
            Asset(
                asset_id=block_id,
                asset_type=AssetType.POWER_BLOCK,
                parent_id=prefix,
                position=block_position,
                rated_capacity_kw=block_ac,
                attributes={"dc_capacity_kw": round(block_dc, 3)},
            )
        )

        assets.append(
            Asset(
                asset_id=f"{block_id}-XFMR",
                asset_type=AssetType.TRANSFORMER,
                parent_id=block_id,
                position=block_position,
                rated_capacity_kw=config.transformer.rated_mva * 1000.0,
                attributes={
                    "model": config.transformer.model,
                    "primary_kv": config.transformer.primary_voltage_kv,
                    "secondary_kv": config.transformer.secondary_voltage_kv,
                    "thermal_time_constant_min": (
                        config.transformer.thermal_time_constant_min
                    ),
                },
            )
        )

        assets.extend(_tracker_row_blocks(config, block_id, block_position))
        assets.extend(_inverters(config, block_id, block_position))

    return Plant(assets, config_version=config.config_version)


def _tracker_row_blocks(
    config: PlantConfig, block_id: str, block_position: Position
) -> list[Asset]:
    """Create the tracker row-blocks within one power block.

    Row-blocks are spread across the block's north-south extent so that a stuck
    tracker affects a spatially coherent portion of the array rather than a
    scattered set of rows.

    Args:
        config: Plant configuration.
        block_id: Parent power block identifier.
        block_position: Centroid of the parent block.

    Returns:
        One asset per tracker row-block.
    """
    count = config.tracker.row_blocks_per_power_block
    height = config.layout.block_height_m
    assets: list[Asset] = []

    for index in range(1, count + 1):
        offset = height * ((index - 0.5) / count - 0.5)
        assets.append(
            Asset(
                asset_id=f"{block_id}-TRK{index}",
                asset_type=AssetType.TRACKER_ROW_BLOCK,
                parent_id=block_id,
                position=Position(
                    x_m=block_position.x_m, y_m=block_position.y_m + offset
                ),
                attributes={
                    "axis_azimuth_deg": config.tracker.axis_azimuth_deg,
                    "max_angle_deg": config.tracker.max_angle_deg,
                    "backtracking": config.tracker.backtracking,
                    "ground_coverage_ratio": config.tracker.ground_coverage_ratio,
                    "module_height_m": config.tracker.module_height_m,
                    "stow_wind_speed_ms": config.tracker.stow_wind_speed_ms,
                },
            )
        )
    return assets


def _inverters(
    config: PlantConfig, block_id: str, block_position: Position
) -> list[Asset]:
    """Create the inverters and their combiners within one power block.

    Inverters are spread across the block's east-west extent, so a cloud
    crossing the site reaches them at measurably different times.

    Args:
        config: Plant configuration.
        block_id: Parent power block identifier.
        block_position: Centroid of the parent block.

    Returns:
        Inverter assets, each immediately followed by its combiners.
    """
    count = config.topology.inverters_per_block
    width = config.layout.block_width_m
    assets: list[Asset] = []

    for index in range(1, count + 1):
        offset = width * ((index - 0.5) / count - 0.5)
        position = Position(x_m=block_position.x_m + offset, y_m=block_position.y_m)
        inverter_id = f"{block_id}-INV{index}"

        assets.append(
            Asset(
                asset_id=inverter_id,
                asset_type=AssetType.INVERTER,
                parent_id=block_id,
                position=position,
                rated_capacity_kw=config.inverter.rated_ac_kw,
                attributes={
                    "model": config.inverter.model,
                    "dc_capacity_kw": round(config.inverter_dc_kw, 3),
                    "max_dc_voltage_v": config.inverter.max_dc_voltage_v,
                    "mppt_channels": config.inverter.mppt_channels,
                    "strings": config.strings_per_inverter,
                    "startup_poa_wm2": config.inverter.startup_poa_wm2,
                    "night_standby_kw": config.inverter.night_standby_kw,
                },
            )
        )

        combiner_dc = config.topology.strings_per_combiner * config.string_dc_kw
        for combiner_index in range(1, config.topology.combiners_per_inverter + 1):
            assets.append(
                Asset(
                    asset_id=f"{inverter_id}-CMB{combiner_index:02d}",
                    asset_type=AssetType.COMBINER,
                    parent_id=inverter_id,
                    position=position,
                    rated_capacity_kw=round(combiner_dc, 3),
                    attributes={
                        "strings": config.topology.strings_per_combiner,
                        "modules": (
                            config.topology.strings_per_combiner
                            * config.topology.modules_per_string
                        ),
                        "modules_per_string": config.topology.modules_per_string,
                    },
                )
            )
    return assets
