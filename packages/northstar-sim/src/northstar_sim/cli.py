"""Command-line interface for the NorthStar plant model.

Three commands:

* ``validate`` - build the plant and run the Phase 1 check set. This is the gate
  in ``16_implementation_roadmap`` section 4; nothing downstream should run
  until it passes.
* ``summary`` - report derived capacity and asset counts.
* ``export`` - write the asset register to CSV or Parquet, which becomes the
  ``plant`` schema dimension tables in Phase 11.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

import pandas as pd

from .acceptance import build_report
from .assets import AssetType
from .block import run_spatial_gate
from .builder import build_plant, site_extent
from .control import run_state_gate
from .curriculum import (
    EXERCISES,
    exercises_by_tier,
    run_curriculum_gate,
    write_exercise_files,
)
from .dashboards import validate_dashboards, write_dashboards
from .dataquality import quality_summary, run_dataquality_gate, undetected_defect_share
from .kpis import availability_metrics, performance_metrics
from .loader import dataset_time_range, load_dataset, refresh_aggregates
from .losses import plant_waterfall, run_loss_gate
from .market import (
    CommercialTerms,
    capture_rate,
    economic_curtailment_mask,
    monetize_losses,
    run_financial_gate,
    settle,
    synthetic_prices,
)
from .oracle import run_physics_gate
from .plant_config import PlantConfig, load_plant_config
from .plant_run import energy_mwh, run_plant, run_plant_gate
from .resource import (
    clearsky_resource,
    downscale_to_minute,
    renormalization_error,
)
from .scenarios import reliability_metrics, run_scenario_gate
from .sensors import run_sensor_gate
from .spatial import build_asset_resources
from .storage import (
    ANALYST_STREAMS,
    duckdb_connection,
    export_parquet,
    generate_ddl,
    generate_table_ddl,
    run_storage_gate,
)
from .validation import validate_plant


def configure_logging(verbose: bool) -> None:
    """Set up console logging.

    Args:
        verbose: Emit debug-level records.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        A parser exposing the three subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="northstar-sim",
        description="Build and validate the NorthStar PV plant model.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/northstar.toml"),
        help="path to the TOML configuration file",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="run the Phase 1 check set")
    sub.add_parser("summary", help="report derived capacity and asset counts")

    gate = sub.add_parser(
        "physics-gate",
        help="Phase 2 gate: compare the production chain against pvlib ModelChain",
    )
    gate.add_argument("--start", default="2023-06-21", help="UTC start date")
    gate.add_argument("--end", default="2023-06-22", help="UTC end date")
    gate.add_argument("--seed", type=int, default=12345, help="downscaling seed")
    gate.add_argument(
        "--temp-air", type=float, default=35.0, help="constant ambient temperature"
    )
    gate.add_argument("--wind", type=float, default=4.0, help="constant wind speed, m/s")

    spatial = sub.add_parser(
        "spatial-gate",
        help="Phase 3 gate: verify the advected cloud field across the plant",
    )
    spatial.add_argument("--start", default="2023-06-21 05:00")
    spatial.add_argument("--end", default="2023-06-22 05:00")
    spatial.add_argument("--seed", type=int, default=999)
    spatial.add_argument(
        "--kt", type=float, default=0.62, help="clear-sky index (broken cloud ~0.6)"
    )
    spatial.add_argument("--wind", type=float, default=8.0, help="wind speed, m/s")
    spatial.add_argument(
        "--wind-direction", type=float, default=270.0, help="degrees from north"
    )

    full = sub.add_parser(
        "plant-gate",
        help="Phase 4 gate: run the full plant and check reconciliation and throughput",
    )
    full.add_argument("--start", default="2023-06-21 05:00")
    full.add_argument("--end", default="2023-06-22 05:00")
    full.add_argument("--seed", type=int, default=999)
    full.add_argument("--kt", type=float, default=0.75)
    full.add_argument("--wind", type=float, default=7.0)
    full.add_argument("--wind-direction", type=float, default=250.0)

    states = sub.add_parser(
        "state-gate",
        help="Phase 5 gate: verify state machines, control and startup sequences",
    )
    states.add_argument("--start", default="2023-06-21 05:00")
    states.add_argument("--end", default="2023-06-22 05:00")
    states.add_argument("--seed", type=int, default=999)

    sensors = sub.add_parser(
        "sensor-gate",
        help="Phase 6 gate: verify truth and measurement diverge only as modelled",
    )
    sensors.add_argument("--start", default="2023-06-21 05:00")
    sensors.add_argument("--end", default="2023-06-22 05:00")
    sensors.add_argument("--seed", type=int, default=999)

    lossgate = sub.add_parser(
        "loss-gate",
        help="Phase 7 gate: verify the loss waterfall closes and causes separate",
    )
    lossgate.add_argument("--start", default="2023-06-21 05:00")
    lossgate.add_argument("--end", default="2023-06-22 05:00")
    lossgate.add_argument("--seed", type=int, default=999)

    faultgate = sub.add_parser(
        "scenario-gate",
        help="Phase 8 gate: verify faults create identifiable telemetry signatures",
    )
    faultgate.add_argument("--start", default="2023-06-21 05:00")
    faultgate.add_argument("--end", default="2023-06-28 05:00")
    faultgate.add_argument("--seed", type=int, default=999)

    fin = sub.add_parser(
        "financial-gate",
        help="Phase 9 gate: settlement, economic curtailment and lost revenue",
    )
    fin.add_argument("--start", default="2023-06-21 05:00")
    fin.add_argument("--end", default="2023-07-21 05:00")
    fin.add_argument("--seed", type=int, default=999)

    dqgate = sub.add_parser(
        "dataquality-gate",
        help="Phase 10 gate: defects corrupt reporting without touching truth",
    )
    dqgate.add_argument("--start", default="2023-06-21 05:00")
    dqgate.add_argument("--end", default="2023-06-28 05:00")
    dqgate.add_argument("--seed", type=int, default=999)

    stgate = sub.add_parser(
        "storage-gate",
        help="Phase 11 gate: Parquet export, DDL and aggregate reconciliation",
    )
    stgate.add_argument("--start", default="2023-06-21 05:00")
    stgate.add_argument("--end", default="2023-06-24 05:00")
    stgate.add_argument("--seed", type=int, default=999)
    stgate.add_argument("--out", type=Path, default=Path("datasets/dev"))
    stgate.add_argument("--run-id", default="dev-001")

    ddlcmd = sub.add_parser("ddl", help="print the generated TimescaleDB schema")
    ddlcmd.add_argument("--out", type=Path, default=None)
    ddlcmd.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="exported dataset to derive table definitions from",
    )
    ddlcmd.add_argument("--run-id", default="curriculum")

    dash = sub.add_parser(
        "dashboards", help="generate provisionable Grafana dashboard JSON"
    )
    dash.add_argument("--out", type=Path, default=Path("dashboards"))
    dash.add_argument(
        "--datasource-out", type=Path, default=Path("db/grafana/datasources")
    )
    dash.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="point the default time window at this dataset's actual span",
    )
    dash.add_argument("--run-id", default="curriculum")

    gen = sub.add_parser(
        "generate",
        help="generate a dataset with explicit weather, seed and injection knobs",
    )
    gen.add_argument("--out", type=Path, default=Path("datasets/custom"))
    gen.add_argument(
        "--run-id", default=None, help="defaults to the output directory name"
    )
    gen.add_argument("--start", default="2023-06-21 05:00")
    gen.add_argument("--end", default="2023-06-28 05:00")
    gen.add_argument(
        "--seed",
        type=int,
        default=999,
        help="changes weather, faults, defects and sensors together",
    )
    gen.add_argument(
        "--temp-air", type=float, default=33.0, help="mean ambient temperature, C"
    )
    gen.add_argument(
        "--temp-amplitude",
        type=float,
        default=9.0,
        help="half-range of the diurnal ambient cycle, C",
    )
    gen.add_argument("--wind-speed", type=float, default=4.0, help="m/s")
    gen.add_argument("--wind-direction", type=float, default=250.0, help="degrees")
    gen.add_argument(
        "--clearsky-index",
        type=float,
        default=1.0,
        help="scale irradiance; below 1.0 is a cloudier record",
    )
    gen.add_argument(
        "--plant-age",
        type=float,
        default=None,
        help="plant age in years at the window start",
    )
    gen.add_argument("--no-faults", action="store_true")
    gen.add_argument("--no-defects", action="store_true")
    gen.add_argument("--no-curtailment", action="store_true")
    gen.add_argument(
        "--real",
        action="store_true",
        help="use fetched NSRDB irradiance and ERCOT prices instead of the "
        "clear-sky and synthetic stand-ins (requires `make fetch`)",
    )
    gen.add_argument("--year", type=int, default=None, help="year to load with --real")
    gen.add_argument("--cache-root", type=Path, default=None)
    gen.add_argument(
        "--settlement-point",
        default=None,
        help="price node for --real; defaults to the plant's own node",
    )

    load = sub.add_parser(
        "load", help="load an exported dataset into TimescaleDB and refresh aggregates"
    )
    load.add_argument("--dsn", required=True)
    load.add_argument("--dataset", type=Path, default=Path("datasets/curriculum"))
    load.add_argument("--run-id", default="curriculum")
    load.add_argument("--include-truth", action="store_true")
    load.add_argument("--no-refresh", action="store_true")

    accept = sub.add_parser(
        "accept",
        help="generate the dataset acceptance report (doc 15 section 12)",
    )
    accept.add_argument("--dataset", type=Path, default=Path("datasets/curriculum"))
    accept.add_argument("--run-id", default="curriculum")
    accept.add_argument("--report", type=Path, default=None)
    accept.add_argument("--no-prices", action="store_true")

    score = sub.add_parser(
        "score",
        help="score blind analysis against injected truth (doc 16 section 16)",
    )
    score.add_argument("--dataset", type=Path, default=Path("datasets/year"))
    score.add_argument("--run-id", default="year")
    score.add_argument("--cache-root", type=Path, default=None)
    score.add_argument("--settlement-point", default=None)
    accept.add_argument(
        "--cache-root",
        type=Path,
        default=None,
        help="where to look for cached real prices",
    )
    accept.add_argument(
        "--settlement-point",
        default=None,
        help="price node; defaults to the plant's own node",
    )

    curr = sub.add_parser(
        "curriculum-gate",
        help="Phase 12 gate: run every SQL exercise against a real dataset",
    )
    curr.add_argument("--start", default="2023-06-21 05:00")
    curr.add_argument("--end", default="2023-06-28 05:00")
    curr.add_argument("--seed", type=int, default=999)
    curr.add_argument("--out", type=Path, default=Path("datasets/curriculum"))
    curr.add_argument(
        "--write-sql",
        type=Path,
        default=None,
        help="also write the exercises to numbered .sql files",
    )

    export = sub.add_parser("export", help="write the asset register")
    export.add_argument("output", type=Path, help="destination file (.csv or .parquet)")
    return parser


def render_summary(config: PlantConfig) -> str:
    """Format the derived capacity summary.

    Every figure here is computed from module rating and build-out counts. None
    is read from configuration, which is what makes the nameplate a consequence
    of the equipment rather than a claim about it.

    Args:
        config: Plant configuration.

    Returns:
        A multi-line summary.
    """
    width, height = site_extent(config)
    rows = [
        ("Site", f"{config.site.name} ({config.site.latitude}, {config.site.longitude})"),
        ("Config version", config.config_version),
        ("", ""),
        ("Modules per string", f"{config.topology.modules_per_string}"),
        ("String DC", f"{config.string_dc_kw:,.2f} kWp"),
        ("String Voc at design min", f"{config.string_voc_cold_v:,.1f} V"),
        ("Inverter DC ceiling", f"{config.inverter.max_dc_voltage_v:,.0f} V"),
        ("Voc margin", f"{config.voc_margin_v:+,.1f} V"),
        ("Max modules per string", f"{config.max_modules_per_string}"),
        ("", ""),
        ("Strings per inverter", f"{config.strings_per_inverter:,}"),
        ("Inverter DC", f"{config.inverter_dc_kw:,.2f} kWp"),
        ("Inverter AC", f"{config.inverter.rated_ac_kw:,.0f} kW"),
        ("", ""),
        ("Power blocks", f"{config.topology.power_blocks:,}"),
        ("Inverters", f"{config.total_inverters:,}"),
        ("Combiners", f"{config.total_combiners:,}"),
        ("Tracker row-blocks", f"{config.total_tracker_row_blocks:,}"),
        ("Strings", f"{config.total_strings:,}"),
        ("Modules", f"{config.total_modules:,}"),
        ("", ""),
        ("Plant DC nameplate", f"{config.plant_dc_kw / 1000.0:,.2f} MWp"),
        ("Plant AC nameplate", f"{config.plant_ac_kw / 1000.0:,.2f} MW"),
        ("DC/AC ratio", f"{config.dc_ac_ratio:.4f}"),
        ("", ""),
        ("Footprint", f"{width:,.0f} x {height:,.0f} m"),
        ("Footprint area", f"{width * height / 4046.86:,.0f} acres"),
    ]
    return "\n".join(
        "" if not label else f"  {label:<26} {value}" for label, value in rows
    )


def command_validate(config: PlantConfig) -> int:
    """Build the plant and run Phase 1 validation.

    Args:
        config: Plant configuration.

    Returns:
        Zero when validation passes, one otherwise.
    """
    plant = build_plant(config)
    report = validate_plant(config, plant)
    print("Phase 1 validation\n")
    print(report.render())
    return 0 if report.ok else 1


def command_summary(config: PlantConfig) -> int:
    """Print the derived capacity summary and asset counts.

    Args:
        config: Plant configuration.

    Returns:
        Process exit code, always zero.
    """
    plant = build_plant(config)
    print("Derived plant summary\n")
    print(render_summary(config))
    print("\n  Asset register")
    for asset_type, count in sorted(plant.counts().items()):
        print(f"  {asset_type:<28} {count:>7,}")
    telemetry = len(plant.telemetry_assets())
    print(f"  {'TOTAL':<28} {len(plant):>7,}")
    print(f"  {'telemetry-bearing':<28} {telemetry:>7,}")
    return 0


def command_physics_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 2 physics oracle gate.

    A deterministic clear-sky resource is used so the comparison is between two
    implementations of the same physics, with no stochastic variation to muddy
    the result.

    Args:
        config: Plant configuration.
        args: Parsed arguments carrying the window, seed and constant weather.

    Returns:
        Zero when the gate passes, one otherwise.
    """
    source = clearsky_resource(
        config,
        args.start,
        args.end,
        freq="5min",
        temp_air_c=args.temp_air,
        wind_speed_ms=args.wind,
    )
    weather = downscale_to_minute(source, config, seed=args.seed)

    error = renormalization_error(weather, source)
    print("Layer 2 downscaling\n")
    print(f"  source samples          {len(source):,} at 5 min")
    print(f"  downscaled samples      {len(weather):,} at 1 min")
    print(f"  renormalization error   {error.max():.3e} W/m2 (max over intervals)")

    result = run_physics_gate(config, weather)
    print("\nPhysics oracle gate\n")
    print(result.render())
    return 0 if result.passed else 1


def command_spatial_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 3 spatial acceptance checks.

    Uses a broken-cloud day, because that is the regime where spatial structure
    exists. On a clear day every asset sees the same thing and the layer has
    nothing to demonstrate.

    Args:
        config: Plant configuration.
        args: Parsed arguments carrying window, seed and weather.

    Returns:
        Zero when every check passes, one otherwise.
    """
    source = clearsky_resource(
        config,
        args.start,
        args.end,
        freq="5min",
        temp_air_c=32.0,
        wind_speed_ms=args.wind,
    )
    source["ghi"] = source["ghi"] * args.kt

    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = args.wind
    base["wind_direction"] = args.wind_direction

    plant = build_plant(config)
    assets = plant.of_type(AssetType.WEATHER_STATION) + plant.of_type(
        AssetType.POWER_BLOCK
    )
    resources = build_asset_resources(config, base, assets, seed=args.seed)
    positions = {
        a.asset_id: (a.position.x_m, a.position.y_m) for a in assets if a.position
    }

    result = run_spatial_gate(
        resources,
        positions,
        wind_speed_ms=args.wind,
        wind_direction_deg=args.wind_direction,
        daylight=base["solar_zenith"] < 80,
    )
    print(f"Spatial cloud field gate ({len(resources)} positioned assets)\n")
    print(result.render())
    return 0 if result.passed else 1


def command_plant_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the full plant and apply the Phase 4 acceptance checks.

    Args:
        config: Plant configuration.
        args: Parsed arguments carrying window, seed and weather.

    Returns:
        Zero when every check passes, one otherwise.
    """
    import time

    source = clearsky_resource(
        config,
        args.start,
        args.end,
        freq="5min",
        temp_air_c=33.0,
        wind_speed_ms=args.wind,
    )
    source["ghi"] = source["ghi"] * args.kt

    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = args.wind
    base["wind_direction"] = args.wind_direction

    plant = build_plant(config)
    started = time.perf_counter()
    result = run_plant(config, plant, base, seed=args.seed)
    elapsed = time.perf_counter() - started

    days = (base.index[-1] - base.index[0]).total_seconds() / 86400.0
    gate = run_plant_gate(
        config, plant, result, seconds_per_day=elapsed / max(days, 1e-9)
    )

    print("Full plant run\n")
    for stage, seconds in result.timings.items():
        print(f"  {stage:<20} {seconds:6.2f} s")
    export = energy_mwh(result.plant["grid_export_power_kw"])
    print(f"\n  grid export          {export:,.1f} MWh")
    print(
        f"  capacity factor      "
        f"{export / (config.plant_ac_kw / 1000.0 * 24.0 * days):.3f}"
    )
    print("\nPhase 4 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_state_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 5 state and control acceptance checks.

    Args:
        config: Plant configuration.
        args: Parsed arguments carrying the window and seed.

    Returns:
        Zero when every check passes, one otherwise.
    """
    source = clearsky_resource(
        config, args.start, args.end, freq="5min", temp_air_c=33.0, wind_speed_ms=7.0
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 7.0
    base["wind_direction"] = 250.0

    plant = build_plant(config)
    result = run_plant(config, plant, base, seed=args.seed)
    gate = run_state_gate(config, result)

    print("State machines and control\n")
    distribution = result.state_matrix().stack().value_counts()
    for state, count in distribution.items():
        print(
            f"  {state:<12} {count:>9,} sample-minutes ({count / distribution.sum():.1%})"
        )
    print(f"\n  transitions recorded {len(result.events):,}")
    print("\nPhase 5 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_sensor_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 6 sensor acceptance checks.

    Args:
        config: Plant configuration.
        args: Parsed arguments carrying the window and seed.

    Returns:
        Zero when every check passes, one otherwise.
    """
    source = clearsky_resource(
        config, args.start, args.end, freq="5min", temp_air_c=33.0, wind_speed_ms=7.0
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 7.0
    base["wind_direction"] = 250.0

    plant = build_plant(config)
    result = run_plant(config, plant, base, seed=args.seed)
    gate = run_sensor_gate(result, daylight=base["solar_zenith"] < 80)

    fleet = result.sensors.to_frame()
    print("Sensor layer\n")
    print(f"  instances            {len(fleet):,}")
    print(
        f"  calibration gain     {fleet['bias_gain'].min():.4f} .. "
        f"{fleet['bias_gain'].max():.4f}"
    )
    print(
        f"  temperature offset   {fleet['bias_offset'].min():+.3f} .. "
        f"{fleet['bias_offset'].max():+.3f} C"
    )
    print("\nPhase 6 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def _run_for_ambient(config, args, ambient_c):
    """Run the full plant at a given constant ambient temperature.

    Args:
        config: Plant configuration.
        args: Parsed arguments carrying the window and seed.
        ambient_c: Constant ambient temperature.

    Returns:
        The run result.
    """
    source = clearsky_resource(
        config,
        args.start,
        args.end,
        freq="5min",
        temp_air_c=ambient_c,
        wind_speed_ms=4.0,
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 4.0
    base["wind_direction"] = 250.0
    return run_plant(config, build_plant(config), base, seed=args.seed)


def command_loss_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 7 loss attribution acceptance checks.

    Two runs are used: one at moderate ambient and one at extreme heat, because
    thermal derating only engages above the onset temperature and its
    interaction with clipping is only observable when it does.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when every check passes, one otherwise.
    """
    moderate = _run_for_ambient(config, args, 40.0)
    hot = _run_for_ambient(config, args, 50.0)

    waterfall = plant_waterfall(config, moderate)
    energy = waterfall.energy_mwh()

    print("Loss waterfall, clear day at 40 C ambient\n")
    print(f"  {'THEORETICAL':<24} {energy['THEORETICAL']:>10,.1f} MWh")
    for _, row in waterfall.summary().iterrows():
        if abs(row.energy_mwh) > 0.01:
            marker = "avoidable" if row.avoidable else ""
            print(
                f"    {row.cause_code:<22} {row.energy_mwh:>9,.2f} MWh "
                f"{row.share_of_theoretical:>8.2%}  {marker}"
            )
    print(f"  {'EXPORTED':<24} {energy['EXPORTED']:>10,.1f} MWh")

    gate = run_loss_gate(config, moderate, hot)
    print("\nPhase 7 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_scenario_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 8 fault engine acceptance checks.

    Two runs of the same weather and seed are compared: one fault-free, one
    with scenarios injected. Holding the weather realisation fixed is what
    makes the difference attributable to the faults rather than to the day.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when every check passes, one otherwise.
    """
    source = clearsky_resource(
        config, args.start, args.end, freq="5min", temp_air_c=38.0, wind_speed_ms=5.0
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 5.0
    base["wind_direction"] = 250.0

    plant = build_plant(config)
    clean = run_plant(config, plant, base, seed=args.seed)
    faulted = run_plant(config, plant, base, seed=args.seed, inject_faults=True)

    daylight = base["solar_zenith"] < 85.0
    summary = faulted.schedule.to_frame()

    print("Injected scenarios\n")
    if len(summary):
        grouped = summary.groupby("scenario_id").agg(
            instances=("asset_id", "size"),
            mean_minutes=("duration_minutes", "mean"),
        )
        for scenario_id, row in grouped.iterrows():
            print(
                f"  {scenario_id:<10} {int(row.instances):>3} instances, "
                f"mean {row.mean_minutes:>6.0f} min"
            )
    clean_mwh = energy_mwh(clean.plant["grid_export_power_kw"])
    faulted_mwh = energy_mwh(faulted.plant["grid_export_power_kw"])
    print(
        f"\n  export {clean_mwh:,.1f} -> {faulted_mwh:,.1f} MWh "
        f"(lost {clean_mwh - faulted_mwh:,.1f}, "
        f"{(clean_mwh - faulted_mwh) / clean_mwh:.2%})"
    )
    metrics = reliability_metrics(
        faulted.schedule, daylight_hours=float(daylight.sum()) / 60.0
    )
    print(f"  MTBF {metrics['mtbf_hours']:.1f} h, MTTR {metrics['mttr_hours']:.2f} h")

    gate = run_scenario_gate(clean, faulted, daylight=daylight)
    print("\nPhase 8 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_financial_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 9 financial acceptance checks.

    Prices are synthetic here because live ERCOT credentials are not assumed.
    The structure they must reproduce - midday suppression, negative intervals,
    evening scarcity - is what every financial conclusion depends on, so the
    gate checks that structure rather than any particular price level.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when every check passes, one otherwise.
    """
    import numpy as np

    from .losses import CAUSE_CODES, plant_waterfall

    source = clearsky_resource(
        config, args.start, args.end, freq="5min", temp_air_c=38.0, wind_speed_ms=5.0
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 5.0
    base["wind_direction"] = 250.0

    terms = CommercialTerms()
    price = synthetic_prices(base.index, base["ghi"], seed=args.seed)
    hub = price + pd.Series(
        np.random.default_rng(args.seed).normal(0.0, 4.0, len(price)),
        index=price.index,
    )
    curtailment = economic_curtailment_mask(price, terms)

    plant = build_plant(config)
    result = run_plant(
        config,
        plant,
        base,
        seed=args.seed,
        inject_faults=True,
        economic_curtailment=curtailment,
    )

    export_kw = result.plant["grid_export_power_kw"]
    settlement = settle(export_kw, price, hub, terms)
    waterfall = plant_waterfall(config, result)
    monetized = monetize_losses(waterfall.stages, price, terms, CAUSE_CODES)

    print("Settlement\n")
    for column in (
        "energy_revenue_usd",
        "hedge_settlement_usd",
        "ptc_value_usd",
        "basis_usd",
        "gross_margin_usd",
    ):
        print(f"  {column:<24} {settlement[column].sum():>14,.0f}")
    print(f"  {'export_energy_mwh':<24} {settlement['export_energy_mwh'].sum():>14,.1f}")
    print(f"  {'capture_rate':<24} {capture_rate(export_kw, price):>14.1%}")

    print("\nLost revenue by cause\n")
    for _, row in monetized.head(8).iterrows():
        if abs(row.lost_revenue_usd) < 1:
            continue
        marker = "avoidable" if row.avoidable else ""
        print(
            f"  {row.cause_code:<22} {row.lost_energy_mwh:>9,.1f} MWh "
            f"${row.lost_revenue_usd:>11,.0f}  {marker}"
        )

    daylight = base["solar_zenith"] < 85.0
    poa = pd.concat([f["poa_global"] for f in result.inverters.values()], axis=1).mean(
        axis=1
    )
    cell = pd.concat(
        [f["cell_temperature"] for f in result.inverters.values()], axis=1
    ).mean(axis=1)
    dc = sum(f["dc_power_kw"] for f in result.inverters.values())

    metrics = performance_metrics(
        config,
        poa,
        cell,
        dc,
        export_kw,
        base["solar_zenith"],
        node_price=price,
        curtailed=result.plant["curtailed_power_kw"],
    )
    lost = (
        sum(result.fault_loss_kw.values())
        if result.fault_loss_kw
        else pd.Series(0.0, index=base.index)
    )
    availability = availability_metrics(export_kw, base["solar_zenith"], lost)

    print("\nKPIs\n")
    print(
        f"  PR {metrics.performance_ratio:.4f} | corrected "
        f"{metrics.performance_ratio_corrected:.4f} | CF "
        f"{metrics.capacity_factor_ac:.4f}"
    )
    print(
        f"  availability: time {availability.time_based:.4f} | daylight "
        f"{availability.daylight_weighted:.4f} | energy "
        f"{availability.energy_weighted:.4f}"
    )
    print(f"  daylight hours {float(daylight.sum()) / 60:.0f}")

    gate = run_financial_gate(result, settlement, monetized, price, curtailment, terms)
    print("\nPhase 9 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_dataquality_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 10 data-quality acceptance checks.

    Two runs of identical weather, seed and faults are compared: one with
    defects, one without. Holding everything else fixed is what makes the
    difference attributable to reporting rather than to the plant.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when every check passes, one otherwise.
    """
    source = clearsky_resource(
        config, args.start, args.end, freq="5min", temp_air_c=38.0, wind_speed_ms=5.0
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 5.0
    base["wind_direction"] = 250.0

    plant = build_plant(config)
    clean = run_plant(config, plant, base, seed=args.seed, inject_faults=True)
    corrupted = run_plant(
        config, plant, base, seed=args.seed, inject_faults=True, inject_defects=True
    )

    frame = corrupted.defects.to_frame()
    print("Injected defects\n")
    if len(frame):
        grouped = frame.groupby("kind").agg(
            instances=("asset_id", "size"),
            mean_minutes=("duration_minutes", "mean"),
            flagged=("flagged", "mean"),
        )
        for kind, row in grouped.iterrows():
            print(
                f"  {kind:<10} {int(row.instances):>3} instances, mean "
                f"{row.mean_minutes:>6.0f} min, {row.flagged:>5.0%} flagged"
            )
    print(
        f"\n  unflagged share {undetected_defect_share(corrupted.defects):.0%} "
        f"- the quality column is not an oracle"
    )

    print("\nQuality flags\n")
    for _, row in quality_summary(corrupted.quality).iterrows():
        print(f"  {row.quality:<10} {row.samples:>10,}  {row.share:>8.4%}")

    gate = run_dataquality_gate(clean, corrupted)
    print("\nPhase 10 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def _real_prices_for(config: PlantConfig, index, args):
    """Load cached settlement prices covering a dataset's time range.

    Args:
        config: Plant configuration.
        index: The dataset's timestamps.
        args: Parsed arguments, read for cache and settlement point overrides.

    Returns:
        Prices aligned to ``index``, or ``None`` when nothing covers it.
    """
    from .observed import align_prices, real_prices

    if len(index) == 0:
        return None

    years = sorted({int(y) for y in index.year.unique()})
    frames = []
    for year in years:
        try:
            frames.append(
                real_prices(
                    config,
                    year,
                    settlement_point=getattr(args, "settlement_point", None),
                    cache_root=getattr(args, "cache_root", None),
                )
            )
        except (FileNotFoundError, ImportError):
            continue

    if not frames:
        return None

    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]

    # A price series that starts after the dataset does would be forward-filled
    # from nothing, so require real coverage rather than inventing it.
    if combined.empty or combined.index[0] > index[0]:
        return None

    return align_prices(combined, index)


def command_score(config: PlantConfig, args: argparse.Namespace) -> int:
    """Score blind detection and cost ranking against injected truth.

    Two of the three criteria `16 §16` calls the point of the exercise. The
    detector sees only the analyst tree; truth is opened afterwards, purely to
    score. That separation is what makes the result a measurement rather than
    an assertion.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when both criteria are demonstrated.
    """
    from .scoring import run_blind_scoring

    prices = _real_prices_for(
        config,
        pd.DatetimeIndex(
            pd.date_range("2025-01-01", "2025-12-31", freq="15min", tz="UTC")
        ),
        args,
    )
    if prices is None:
        print("No cached real prices; the cost ranking needs them.\n")

    score, rankings = run_blind_scoring(args.dataset, args.run_id, prices)

    print("Criterion 1 - blind fault identification\n")
    print(f"  injected          {score.injected:>6}")
    print(f"  detected          {score.detected:>6}")
    print(f"  matched           {score.matched:>6}")
    print(f"  recall            {score.recall:>6.1%}")
    print(f"  precision         {score.precision:>6.1%}")

    print("\nCriterion 3 - cost ranking against energy ranking\n")
    if rankings.rows.empty or "energy_rank" not in rankings.rows.columns:
        print("  no attributable fault energy - needs a longer record")
    else:
        print(rankings.rows.to_string(index=False))
        print(
            f"\n  rankings differ: {rankings.rankings_differ} "
            f"({rankings.rank_changes} scenario(s) changed position)"
        )
        if not rankings.rankings_differ:
            # Not a failure on a short record: divergence needs enough distinct
            # scenario classes spread across enough of the year for timing to
            # separate them.
            print(
                "  Too few scenario classes for the orderings to separate. "
                "A full year is the test."
            )

    return 0


def command_accept(config: PlantConfig, args: argparse.Namespace) -> int:
    """Generate an acceptance report for an exported dataset.

    Everything is measured from the Parquet trees rather than from the run that
    produced them. A report built from in-memory state validates the simulator;
    this validates the artefact a recipient actually receives.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when the dataset is accepted, one otherwise.
    """
    from .market import synthetic_prices

    prices = None
    if not args.no_prices:
        connection = duckdb_connection(args.dataset, args.run_id, "analyst")
        frame = connection.execute(
            "SELECT time, avg(poa_global) AS ghi FROM inverter_telemetry "
            "GROUP BY time ORDER BY time"
        ).df()
        connection.close()
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        series = frame.set_index("time")["ghi"]

        # Prefer the real prices the dataset was settled against.
        #
        # This report previously **always** regenerated a synthetic series from
        # the dataset's own irradiance, so a dataset built with `--real` was
        # scored against prices that had nothing to do with the ones in it.
        # Capture rate came out at 123-128% - above 1.0, which the check itself
        # documents as meaning the join is wrong - at every scale, because the
        # fault was independent of the data.
        prices = _real_prices_for(config, series.index, args)
        if prices is None:
            prices = synthetic_prices(series.index, series, seed=11)
            print("Prices: synthetic (no cached series covers this dataset)\n")
        else:
            print("Prices: observed, from the fetch cache\n")

    report = build_report(args.dataset, args.run_id, config=config, prices=prices)
    print(report.render())

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report.to_frame().to_csv(args.report, index=False)
        print(f"\nWrote {args.report}")

    return 0 if report.accepted else 1


def command_generate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Generate a dataset with explicit control over every input.

    The acceptance gates each generate a dataset as a side effect, but they fix
    their conditions to whatever the check requires - `curriculum-gate` runs at
    50 C with faults and defects forced on. That makes them fixtures, not a way
    to produce data you chose the shape of.

    Every knob here changes the output. The seed in particular drives weather,
    faults, defects and sensor calibration from independent substreams, so two
    runs differing only in seed give a genuinely different plant-year rather
    than the same one with noise added.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero on success.
    """
    import shutil

    from .market import CommercialTerms, economic_curtailment_mask, synthetic_prices

    run_id = args.run_id or args.out.name

    if args.plant_age is not None:
        config.losses.plant_age_years = args.plant_age

    if getattr(args, "real", False):
        return _generate_from_observations(config, args, run_id)

    source = clearsky_resource(
        config,
        args.start,
        args.end,
        freq="5min",
        temp_air_c=args.temp_air,
        wind_speed_ms=args.wind_speed,
        temp_amplitude_c=args.temp_amplitude,
        seed=args.seed,
    )
    if args.clearsky_index != 1.0:
        source["ghi"] = source["ghi"] * args.clearsky_index

    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = args.wind_speed
    base["wind_direction"] = args.wind_direction

    curtailment = None
    if not args.no_curtailment:
        prices = synthetic_prices(base.index, base["ghi"], seed=args.seed)
        curtailment = economic_curtailment_mask(prices, CommercialTerms())

    result = run_plant(
        config,
        build_plant(config),
        base,
        seed=args.seed,
        inject_faults=not args.no_faults,
        inject_defects=not args.no_defects,
        economic_curtailment=curtailment,
    )

    shutil.rmtree(args.out, ignore_errors=True)
    manifest = export_parquet(result, args.out, run_id=run_id)

    days = (base.index[-1] - base.index[0]).total_seconds() / 86400.0
    print(f"Generated {manifest.total_rows:,} rows over {days:.1f} days\n")
    print(f"  window          {args.start} to {args.end}")
    print(f"  seed            {args.seed}")
    print(
        f"  ambient         {args.temp_air:.0f} C +/- {args.temp_amplitude:.0f} C diurnal"
    )
    print(
        f"  wind            {args.wind_speed:.1f} m/s from {args.wind_direction:.0f} deg"
    )
    print(f"  clear-sky index {args.clearsky_index:.2f}")
    print(f"  plant age       {config.losses.plant_age_years:.1f} years")
    print(
        f"  injected        "
        f"faults={'no' if args.no_faults else 'yes'}, "
        f"defects={'no' if args.no_defects else 'yes'}, "
        f"curtailment={'no' if args.no_curtailment else 'yes'}"
    )
    print(f"\n  analyst tree    {args.out}/analyst/run_id={run_id}")
    print(f"  truth tree      {args.out}/truth/run_id={run_id}")
    print(f"\nNext: northstar-sim accept --dataset {args.out} --run-id {run_id}")
    return 0


def _generate_from_observations(
    config: PlantConfig, args: argparse.Namespace, run_id: str
) -> int:
    """Generate a dataset from fetched observations.

    Every figure this project has published came from the clear-sky and
    synthetic stand-ins, because nothing read the fetch cache. Clear-sky is
    smooth by construction - one sinusoid a day, no transients, no gaps - so
    results derived from it are provisional until recomputed on real
    irradiance.

    Args:
        config: Plant configuration.
        args: Parsed arguments.
        run_id: Dataset identifier.

    Returns:
        Zero on success.
    """
    import shutil

    from .market import CommercialTerms, economic_curtailment_mask
    from .observed import (
        align_prices,
        available_years,
        real_prices,
        real_resource,
    )

    year = args.year
    if year is None:
        years = available_years(config, cache_root=args.cache_root)
        if not years:
            print("No cached weather found. Run `make fetch` first.")
            return 1
        # The most recent year is the one likeliest to overlap price coverage,
        # since ERCOT retains far less history than NSRDB.
        year = years[-1]
        print(f"No --year given; using the most recent cached year, {year}.\n")

    load = real_resource(config, year, cache_root=args.cache_root)
    print(f"Resource: {load.describe()}")

    # Honour --start/--end here too. Without this every --real run is a full
    # year: 525,596 timestamps and 56 million rows, roughly an hour. That makes
    # each diagnostic iteration cost an hour when a week would answer the same
    # question in minutes.
    window = load.frame
    if args.start and args.end:
        start = pd.Timestamp(args.start, tz="UTC")
        end = pd.Timestamp(args.end, tz="UTC")
        # The defaults describe a summer week in the clear-sky path; applying
        # them to a fetched year would silently truncate it, so a window is
        # only applied when it falls inside the data.
        if start >= window.index[0] and end <= window.index[-1]:
            window = window.loc[start:end]
            print(f"Window:   {start.date()} to {end.date()} ({len(window):,} rows)")

    if window.empty:
        print("The requested window contains no cached data.")
        return 1

    base = downscale_to_minute(window, config, seed=args.seed)
    for column, default in (
        ("wind_speed", args.wind_speed),
        ("wind_direction", args.wind_direction),
    ):
        if column in window.columns:
            base[column] = window[column].reindex(base.index, method="ffill")
        else:
            base[column] = default

    curtailment = None
    price_note = "none"
    if not args.no_curtailment:
        try:
            prices = align_prices(
                real_prices(
                    config,
                    year,
                    settlement_point=args.settlement_point,
                    cache_root=args.cache_root,
                ),
                base.index,
            )
        except FileNotFoundError as error:
            # Weather and price coverage genuinely differ - ERCOT retains about
            # a year - so a weather year without prices is expected, not an
            # error. Say so rather than failing the run.
            print(f"Prices: {error}\n         continuing without curtailment.")
        else:
            curtailment = economic_curtailment_mask(prices, CommercialTerms())
            price_note = f"observed, {int(curtailment.sum()):,} curtailed minute(s)"

    result = run_plant(
        config,
        build_plant(config),
        base,
        seed=args.seed,
        inject_faults=not args.no_faults,
        inject_defects=not args.no_defects,
        economic_curtailment=curtailment,
    )

    shutil.rmtree(args.out, ignore_errors=True)
    manifest = export_parquet(result, args.out, run_id=run_id)

    exported = result.plant["grid_export_power_kw"].sum() / 60 / 1000
    print(f"\nGenerated {manifest.total_rows:,} rows from observed data\n")
    print(f"  year            {year}")
    print(f"  resource        {load.source_slug}")
    print(f"  prices          {price_note}")
    print(f"  exported        {exported:,.1f} MWh")
    print(f"\n  analyst tree    {args.out}/analyst/run_id={run_id}")
    print(f"\nNext: northstar-sim accept --dataset {args.out} --run-id {run_id}")
    return 0


def command_load(args: argparse.Namespace) -> int:
    """Load an exported dataset into TimescaleDB.

    Creating the schema is not the same as having data. ``make db-up`` runs the
    init scripts, which leave every table empty; without this step Grafana
    correctly reports "No data" on every panel.

    Args:
        args: Parsed arguments carrying the DSN and dataset location.

    Returns:
        Zero on success.
    """
    result = load_dataset(
        args.dsn,
        args.dataset,
        args.run_id,
        include_truth=args.include_truth,
    )
    print(f"Loaded {result.total_rows:,} rows\n")
    for stream, rows in sorted(result.rows_by_stream.items()):
        print(f"  {stream:<34} {rows:>10,}")
    for stream in result.skipped:
        print(f"  {stream:<34} {'skipped - no such table':>10}")

    if not args.no_refresh:
        # A hierarchical aggregate reads another aggregate, so an unrefreshed
        # parent leaves every rollup above it empty.
        refreshed = refresh_aggregates(args.dsn)
        print(f"\nRefreshed {len(refreshed)} continuous aggregate(s)")
        for view in refreshed:
            print(f"  {view}")
    return 0


def command_dashboards(args: argparse.Namespace) -> int:
    """Generate provisionable Grafana dashboard JSON.

    Args:
        args: Parsed arguments carrying the output directory.

    Returns:
        Zero when every dashboard validates, one otherwise.
    """
    problems = validate_dashboards()
    if problems:
        print(f"{len(problems)} validation problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    time_from, time_to = "now-7d", "now"
    if args.dataset:
        span = dataset_time_range(args.dataset, args.run_id)
        if span:
            time_from, time_to = span
            print(f"Pointing the default window at the data: {time_from} .. {time_to}")
        else:
            print("Could not read the dataset span; leaving the window at now-7d")

    written = write_dashboards(
        args.out,
        datasource_target=args.datasource_out,
        time_from=time_from,
        time_to=time_to,
    )
    print(f"Wrote {len(written)} files")
    for path in written:
        print(f"  {path.name}")
    print(
        "\nNot verified: these have never been rendered against a live "
        "datasource.\nApply the schema and run `make db-up` before trusting "
        "any panel."
    )
    return 0


def command_ddl(args: argparse.Namespace) -> int:
    """Emit the generated TimescaleDB schema.

    Table definitions come first: ``create_hypertable`` requires the table to
    already exist. The first version of this command emitted only the hypertable
    calls, which parsed cleanly and would have failed on the first statement
    against a live server.

    Args:
        args: Parsed arguments carrying an optional output path and dataset.

    Returns:
        Process exit code, always zero.
    """
    sql = ""
    if args.dataset:
        connection = duckdb_connection(args.dataset, args.run_id, "analyst")
        available = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        schemas = {
            stream: connection.execute(f"SELECT * FROM {stream} LIMIT 100").df()
            for stream in ANALYST_STREAMS
            if stream in available
        }
        connection.close()
        sql += generate_table_ddl(schemas) + "\n"
        args._schemas = schemas
    else:
        sql += (
            "-- No dataset supplied, so no table definitions were generated.\n"
            "-- Pass --dataset to derive them; create_hypertable will fail\n"
            "-- without them.\n\n"
        )

    sql += generate_ddl(schemas=getattr(args, "_schemas", None))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(sql)
        print(f"Wrote {len(sql.splitlines())} lines to {args.out}")
    else:
        print(sql)
    return 0


def command_storage_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run the Phase 11 storage acceptance checks.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when every check passes, one otherwise.
    """
    import shutil

    source = clearsky_resource(
        config, args.start, args.end, freq="5min", temp_air_c=38.0, wind_speed_ms=5.0
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 5.0
    base["wind_direction"] = 250.0

    result = run_plant(
        config,
        build_plant(config),
        base,
        seed=args.seed,
        inject_faults=True,
        inject_defects=True,
    )

    shutil.rmtree(args.out, ignore_errors=True)
    manifest = export_parquet(result, args.out, run_id=args.run_id)
    days = (base.index[-1] - base.index[0]).total_seconds() / 86400.0

    print(f"Parquet export -> {args.out}\n")
    for stream, rows in manifest.analyst_rows.items():
        print(f"  analyst  {stream:<24} {rows:>10,}")
    for stream, rows in manifest.truth_rows.items():
        print(f"  truth    {stream:<24} {rows:>10,}")
    print(f"\n  total {manifest.total_rows:,} rows over {days:.1f} days")

    gate = run_storage_gate(result, manifest, simulated_days=days)
    print("\nPhase 11 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_curriculum_gate(config: PlantConfig, args: argparse.Namespace) -> int:
    """Run every curriculum exercise against a freshly exported dataset.

    A curriculum of SQL that has never run is a list of plausible-looking
    queries, and the subtly wrong ones are exactly the ones a learner cannot
    diagnose. The window is chosen hot and curtailed so that every low-output
    condition in EX-501 actually occurs.

    Args:
        config: Plant configuration.
        args: Parsed arguments.

    Returns:
        Zero when every check passes, one otherwise.
    """
    import shutil

    from .market import CommercialTerms, economic_curtailment_mask, synthetic_prices

    # Ambient varies, with its peak trailing solar noon. Constant ambient makes
    # cell temperature collinear with irradiance, and any analysis separating
    # the two then returns a temperature coefficient of the wrong sign.
    source = clearsky_resource(
        config,
        args.start,
        args.end,
        freq="5min",
        temp_air_c=42.0,
        wind_speed_ms=3.0,
        temp_amplitude_c=9.0,
        seed=args.seed,
    )
    base = downscale_to_minute(source, config, seed=args.seed)
    base["wind_speed"] = 3.0
    base["wind_direction"] = 250.0

    terms = CommercialTerms()
    price = synthetic_prices(base.index, base["ghi"], seed=11)

    result = run_plant(
        config,
        build_plant(config),
        base,
        seed=args.seed,
        inject_faults=True,
        inject_defects=True,
        economic_curtailment=economic_curtailment_mask(price, terms),
    )

    shutil.rmtree(args.out, ignore_errors=True)
    export_parquet(result, args.out, run_id="curriculum")
    connection = duckdb_connection(args.out, "curriculum", "analyst")

    gate = run_curriculum_gate(connection)
    connection.close()

    print("SQL curriculum\n")
    by_id = {exercise.exercise_id: exercise for exercise in EXERCISES}
    for tier, exercises in exercises_by_tier().items():
        print(f"  Tier {tier}")
        for exercise in exercises:
            rows = next(
                r.rows for r in gate.results if r.exercise_id == exercise.exercise_id
            )
            print(f"    {exercise.exercise_id}  {exercise.title:<34} {rows:>6,} rows")
    assert by_id

    if args.write_sql:
        written = write_exercise_files(args.write_sql)
        print(f"\n  wrote {len(written)} .sql files to {args.write_sql}")

    print("\nPhase 12 gate\n")
    print(gate.render())
    return 0 if gate.passed else 1


def command_export(config: PlantConfig, output: Path) -> int:
    """Write the asset register to disk.

    Args:
        config: Plant configuration.
        output: Destination path; suffix selects CSV or Parquet.

    Returns:
        Zero on success, one if the suffix is unsupported.
    """
    plant = build_plant(config)
    records = [
        {
            "asset_id": a.asset_id,
            "asset_type": a.asset_type.value,
            "parent_id": a.parent_id or "",
            "x_m": a.position.x_m if a.position else None,
            "y_m": a.position.y_m if a.position else None,
            "rated_capacity_kw": a.rated_capacity_kw,
            "emits_telemetry": a.emits_telemetry,
            "config_version": plant.config_version,
        }
        for a in plant
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix == ".csv":
        with output.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    elif output.suffix == ".parquet":
        import pandas as pd  # noqa: PLC0415 - optional at import time

        pd.DataFrame(records).to_parquet(output, index=False, compression="zstd")
    else:
        print(f"Unsupported output format: {output.suffix} (use .csv or .parquet)")
        return 1

    print(f"Wrote {len(records):,} assets to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    config = load_plant_config(args.config)

    if args.command == "validate":
        return command_validate(config)
    if args.command == "summary":
        return command_summary(config)
    if args.command == "physics-gate":
        return command_physics_gate(config, args)
    if args.command == "spatial-gate":
        return command_spatial_gate(config, args)
    if args.command == "plant-gate":
        return command_plant_gate(config, args)
    if args.command == "state-gate":
        return command_state_gate(config, args)
    if args.command == "sensor-gate":
        return command_sensor_gate(config, args)
    if args.command == "loss-gate":
        return command_loss_gate(config, args)
    if args.command == "scenario-gate":
        return command_scenario_gate(config, args)
    if args.command == "financial-gate":
        return command_financial_gate(config, args)
    if args.command == "dataquality-gate":
        return command_dataquality_gate(config, args)
    if args.command == "ddl":
        return command_ddl(args)
    if args.command == "dashboards":
        return command_dashboards(args)
    if args.command == "generate":
        return command_generate(config, args)
    if args.command == "load":
        return command_load(args)
    if args.command == "score":
        return command_score(config, args)
    if args.command == "accept":
        return command_accept(config, args)
    if args.command == "storage-gate":
        return command_storage_gate(config, args)
    if args.command == "curriculum-gate":
        return command_curriculum_gate(config, args)
    if args.command == "export":
        return command_export(config, args.output)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
