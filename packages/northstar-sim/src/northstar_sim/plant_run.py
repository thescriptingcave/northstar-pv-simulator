"""Run the complete plant: every inverter, transformer, block and the meter.

Phase 4 expands from one block to the full reference plant. The expansion is
mostly bookkeeping plus two new physical stages - transformer and AC collection
losses - but it is also where throughput starts to matter, because everything
downstream generates data at this scale.

**Shared work is computed once.** Solar position, tracker orientation, airmass
and the bifacial ground view factors depend only on location, time and tracker
geometry, not on the irradiance an individual asset receives. Computing them per
inverter cost 0.373 s per inverter-day; sharing them costs 0.022 s, a 17-fold
reduction that moves a simulated year from 91 minutes to under 7.

Reference: design documents ``04_physical_architecture`` and
``16_implementation_roadmap`` section 7.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .assets import Asset, AssetType, Plant
from .block import FOOTPRINT_SMOOTHING_MINUTES
from .control import (
    PlantController,
    apply_setpoint,
    apply_state_to_output,
    resolve_inverter_states,
    resolve_plant_state,
)
from .dataquality import DefectSchedule, apply_defects, schedule_defects
from .physics import build_site_geometry, compute_rear_irradiance, run_inverter_chain
from .plant_config import PlantConfig
from .scenarios import (
    ScenarioKind,
    ScenarioSchedule,
    apply_inverter_scenarios,
    apply_tracker_scenario,
    scenario_events,
    schedule_scenarios,
)
from .sensors import (
    FIELD_CLASSES,
    SensorFleet,
    build_sensor_fleet,
    measure_frame,
)
from .spatial import build_asset_resources
from .states import (
    INVERTER_TRANSITIONS,
    extract_transitions,
    transitions_to_frame,
    validate_transitions,
)

LOGGER = logging.getLogger(__name__)

#: Resistive loss in the medium-voltage collection system at full plant output,
#: as a fraction. Load-dependent: it scales with the square of loading, so a
#: plant at half output loses a quarter as much.
AC_COLLECTION_LOSS_AT_RATED = 0.008

#: Ambient temperature assumed for transformer thermal rise when the weather
#: series carries none, degrees Celsius.
DEFAULT_AMBIENT_C = 25.0

#: Transformer winding temperature rise above ambient at rated load, degrees
#: Celsius. Combined with the configured thermal time constant this produces
#: the lag that scenario SCN-045 depends on.
TRANSFORMER_RISE_AT_RATED_C = 65.0


@dataclass
class PlantRunResult:
    """Output of a full-plant simulation.

    Attributes:
        inverters: Per-inverter production frames.
        transformers: Per-transformer loading and thermal frames.
        blocks: Per-block aggregate frames.
        weather: Per-weather-station resource frames.
        plant: Plant-level totals including grid export and plant state.
        timings: Wall-clock seconds by stage, for the throughput check.
        events: State transition records, one row per change.
        measured: Analyst-facing frames after the sensor layer. Distinct
            objects from the truth frames, never derived by mutation.
        sensors: The sensor fleet, itself ground truth about instrument error.
        schedule: Injected scenarios - ground truth for blind analysis scoring.
        defects: Injected data-quality defects - also ground truth.
        quality: Per-asset quality flag frames, themselves fallible.
        fault_events: Onset and clearance records for every scenario.
        fault_loss_kw: Energy lost to faults, per inverter.
    """

    inverters: dict[str, pd.DataFrame]
    transformers: dict[str, pd.DataFrame]
    blocks: dict[str, pd.DataFrame]
    weather: dict[str, pd.DataFrame]
    plant: pd.DataFrame
    timings: dict[str, float] = field(default_factory=dict)
    events: pd.DataFrame = field(default_factory=pd.DataFrame)
    measured: dict[str, pd.DataFrame] = field(default_factory=dict)
    sensors: SensorFleet | None = None
    schedule: ScenarioSchedule | None = None
    defects: DefectSchedule | None = None
    quality: dict[str, pd.DataFrame] = field(default_factory=dict)
    fault_events: pd.DataFrame = field(default_factory=pd.DataFrame)
    fault_loss_kw: dict[str, pd.Series] = field(default_factory=dict)

    def state_matrix(self) -> pd.DataFrame:
        """Assemble per-inverter operating state.

        Returns:
            One column per inverter, indexed by time.
        """
        return pd.DataFrame(
            {key: frame["operating_state"] for key, frame in self.inverters.items()}
        )

    def ac_matrix(self) -> pd.DataFrame:
        """Assemble per-inverter AC power.

        Returns:
            One column per inverter, indexed by time.
        """
        return pd.DataFrame(
            {key: frame["ac_power_kw"] for key, frame in self.inverters.items()}
        )

    def block_matrix(self) -> pd.DataFrame:
        """Assemble per-block AC power.

        Returns:
            One column per block, indexed by time.
        """
        return pd.DataFrame(
            {key: frame["ac_power_kw"] for key, frame in self.blocks.items()}
        )


def transformer_response(
    input_power_kw: pd.Series,
    config: PlantConfig,
    ambient_c: pd.Series,
) -> pd.DataFrame:
    """Model one block transformer's losses and thermal behaviour.

    Losses are load-dependent, not a fixed percentage. A constant-efficiency
    transformer would produce a flat efficiency curve, which eliminates the
    efficiency-variation analysis in ``02 §5`` and makes transformer health
    monitoring impossible.

    Winding temperature follows loading with a first-order lag. That lag is the
    mechanism behind the overheating scenario, and it is why a transformer can
    still be hot after load has fallen.

    Args:
        input_power_kw: Aggregate inverter AC power entering the transformer.
        config: Plant configuration.
        ambient_c: Ambient temperature series.

    Returns:
        A frame with output power, loading, losses and winding temperature.
    """
    transformer = config.transformer
    rated_kw = transformer.rated_mva * 1000.0
    loading = (input_power_kw / rated_kw).clip(lower=0.0)

    # No-load loss is constant whenever energised; load loss scales with the
    # square of current, and therefore of loading.
    no_load = transformer.no_load_loss_kw
    load_loss = transformer.load_loss_kw_at_rated * loading**2

    # Output is deliberately NOT clipped at zero. No-load loss is present
    # whenever the transformer is energised, and utility PV plants keep their
    # transformers energised overnight, so output is genuinely negative in
    # darkness - the plant imports station service.
    #
    # Clipping instead discards that energy, and the loss chain then fails to
    # close by about 0.14% of daily export. Small enough to look like rounding,
    # large enough to corrupt any loss attribution built on it.
    output = input_power_kw - no_load - load_loss

    # First-order thermal lag: an exponential moving average of the steady-state
    # rise the present loading would eventually produce.
    steady_state = ambient_c + TRANSFORMER_RISE_AT_RATED_C * loading**2
    interval_minutes = _interval_minutes(input_power_kw.index)
    alpha = 1.0 - np.exp(-interval_minutes / transformer.thermal_time_constant_min)
    winding = steady_state.ewm(alpha=alpha, adjust=False).mean()

    return pd.DataFrame(
        {
            "input_power_kw": input_power_kw,
            "output_power_kw": output,
            "loading_pct": loading * 100.0,
            "no_load_loss_kw": no_load,
            "load_loss_kw": load_loss,
            "winding_temp_c": winding,
            "efficiency_pct": (output / input_power_kw.where(input_power_kw > 0)) * 100.0,
        }
    )


def collection_loss_kw(power_kw: pd.Series, rated_kw: float) -> pd.Series:
    """Compute resistive loss in the AC collection system.

    Args:
        power_kw: Power flowing through the collection system.
        rated_kw: Plant AC nameplate, the reference for the loss fraction.

    Returns:
        Loss in kilowatts, scaling with the square of loading.
    """
    loading = (power_kw / rated_kw).clip(lower=0.0)
    return AC_COLLECTION_LOSS_AT_RATED * rated_kw * loading**2


def _interval_minutes(index: pd.DatetimeIndex) -> float:
    """Determine the sampling interval in minutes.

    Args:
        index: A regular time index.

    Returns:
        Interval width in minutes, defaulting to 1.0 for a degenerate index.
    """
    if len(index) < 2:
        return 1.0
    return (index[1] - index[0]).total_seconds() / 60.0


def run_plant(
    config: PlantConfig,
    plant: Plant,
    base_weather: pd.DataFrame,
    *,
    seed: int,
    schedule: ScenarioSchedule | None = None,
    inject_faults: bool = False,
    inject_defects: bool = False,
    economic_curtailment: pd.Series | None = None,
) -> PlantRunResult:
    """Simulate the complete plant over a time window.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.
        base_weather: Plant-average 1-minute resource series.
        seed: Seed for the ``cloud_field`` substream.
        schedule: Scenarios to inject. Generated automatically when
            ``inject_faults`` is set and none is supplied.
        inject_faults: Whether to run the scenario engine at all. Off by
            default so the earlier gates keep testing a fault-free plant.
        inject_defects: Whether to corrupt measured telemetry with
            data-quality defects. Off by default for the same reason.
        economic_curtailment: Intervals where curtailing is the economically
            correct decision, from ``market.economic_curtailment_mask``.

    Returns:
        The :class:`PlantRunResult`.
    """
    timings: dict[str, float] = {}

    started = time.perf_counter()
    inverter_assets = plant.of_type(AssetType.INVERTER)
    block_assets = plant.of_type(AssetType.POWER_BLOCK)
    station_assets = plant.of_type(AssetType.WEATHER_STATION)

    resources = build_asset_resources(
        config,
        base_weather,
        [*inverter_assets, *block_assets, *station_assets],
        seed=seed,
        footprint_smoothing=FOOTPRINT_SMOOTHING_MINUTES,
    )
    timings["resource"] = time.perf_counter() - started

    started = time.perf_counter()
    geometry = build_site_geometry(config, base_weather)
    rear = compute_rear_irradiance(
        config, base_weather, geometry.solar_position, geometry.surface_tilt
    )
    timings["geometry"] = time.perf_counter() - started

    started = time.perf_counter()
    unconstrained = {
        asset.asset_id: run_inverter_chain(
            config,
            resources[asset.asset_id],
            geometry=geometry,
            rear_irradiance=rear,
        )
        for asset in inverter_assets
    }
    timings["inverters"] = time.perf_counter() - started

    started = time.perf_counter()
    inverters, events = _apply_states_and_control(
        config, unconstrained, economic_curtailment
    )
    timings["states_and_control"] = time.perf_counter() - started

    started = time.perf_counter()
    inverters, schedule, fault_loss = _apply_scenarios(
        plant,
        inverters,
        base_weather.index,
        seed=seed,
        schedule=schedule,
        enabled=inject_faults,
        daylight=base_weather.get(
            "solar_zenith", pd.Series(0.0, index=base_weather.index)
        )
        < 85.0,
    )
    timings["scenarios"] = time.perf_counter() - started

    started = time.perf_counter()
    ambient = base_weather.get(
        "temp_air", pd.Series(DEFAULT_AMBIENT_C, index=base_weather.index)
    )
    blocks, transformers = _run_blocks(config, plant, block_assets, inverters, ambient)
    plant_frame = _run_plant_level(config, blocks)
    timings["balance_of_plant"] = time.perf_counter() - started

    started = time.perf_counter()
    measured, fleet = _apply_sensor_layer(
        inverters,
        {a.asset_id: resources[a.asset_id] for a in station_assets},
        seed=seed,
    )
    timings["sensors"] = time.perf_counter() - started

    started = time.perf_counter()
    measured, quality, defects = _apply_data_quality(
        measured, seed=seed, enabled=inject_defects
    )
    timings["data_quality"] = time.perf_counter() - started

    plant_frame["plant_state"] = resolve_plant_state(
        plant_frame,
        pd.DataFrame({key: frame["operating_state"] for key, frame in inverters.items()}),
        total_inverters=config.total_inverters,
    )

    return PlantRunResult(
        inverters=inverters,
        transformers=transformers,
        blocks=blocks,
        weather={a.asset_id: resources[a.asset_id] for a in station_assets},
        plant=plant_frame,
        timings=timings,
        events=events,
        measured=measured,
        sensors=fleet,
        schedule=schedule,
        defects=defects,
        quality=quality,
        fault_events=scenario_events(schedule) if schedule else pd.DataFrame(),
        fault_loss_kw=fault_loss,
    )


def _apply_data_quality(
    measured: dict[str, pd.DataFrame],
    *,
    seed: int,
    enabled: bool,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], DefectSchedule | None]:
    """Corrupt measured telemetry, leaving truth untouched.

    This runs **last**, after faults and after the sensor layer. The ordering is
    the phase: faults change what the plant produced, sensors change how
    accurately it was measured, and defects change what was reported. Collapsing
    any two of those makes an equipment fault indistinguishable from a data
    fault.

    Args:
        measured: Per-asset measured frames.
        seed: Seed for the ``dataquality_injection`` substream.
        enabled: Whether to inject at all.

    Returns:
        Corrupted frames, quality flags and the applied schedule.
    """
    if not enabled:
        return measured, {}, None

    pairs = [
        (asset_id, column)
        for asset_id, frame in measured.items()
        for column in frame.columns
        if pd.api.types.is_numeric_dtype(frame[column])
    ]
    schedule = schedule_defects(pairs, next(iter(measured.values())).index, seed=seed)
    corrupted, quality = apply_defects(measured, schedule, seed=seed)
    return corrupted, quality, schedule


def _apply_scenarios(
    plant: Plant,
    inverters: dict[str, pd.DataFrame],
    index: pd.DatetimeIndex,
    *,
    seed: int,
    schedule: ScenarioSchedule | None,
    enabled: bool,
    daylight: pd.Series | None = None,
) -> tuple[dict[str, pd.DataFrame], ScenarioSchedule | None, dict[str, pd.Series]]:
    """Inject faults into physical truth, before the sensor layer.

    Ordering matters more than it looks. Faults applied here change what the
    plant produced; the sensor layer downstream changes only what was reported.
    Reversing the order would make an equipment fault indistinguishable from a
    measurement fault, which is the one distinction design doc 09 section 7
    exists to preserve.

    Args:
        plant: Instantiated plant.
        inverters: Per-inverter truth frames.
        index: Simulation time index.
        seed: Seed for the ``fault_schedule`` substream.
        schedule: Pre-built schedule, or ``None`` to generate one.
        enabled: Whether to inject at all.
        daylight: Mask selecting operating hours for fault scheduling.

    Returns:
        Modified frames, the applied schedule, and per-inverter fault losses.
    """
    if not enabled:
        return inverters, schedule, {}

    inverter_ids = sorted(inverters)
    block_ids = [a.asset_id for a in plant.of_type(AssetType.POWER_BLOCK)]
    tracker_ids = [a.asset_id for a in plant.of_type(AssetType.TRACKER_ROW_BLOCK)]

    if schedule is None:
        schedule = schedule_scenarios(
            inverter_ids,
            block_ids,
            tracker_ids,
            index,
            seed=seed,
            daylight=daylight,
        )

    # A tracker row-block serves a share of its power block's inverters, so a
    # stuck tracker degrades those inverters rather than an asset of its own.
    tracker_targets: dict[str, list[str]] = {}
    for tracker_id in tracker_ids:
        block_id = plant.get(tracker_id).parent_id
        siblings = [
            a.asset_id
            for a in plant.children(block_id)
            if a.asset_type is AssetType.INVERTER
        ]
        tracker_targets[tracker_id] = siblings[:1]

    result = dict(inverters)
    losses: dict[str, pd.Series] = {}

    for inverter_id in inverter_ids:
        direct = [
            i
            for i in schedule.for_asset(inverter_id)
            if i.scenario_id != ScenarioKind.STUCK_TRACKER.value
        ]
        frame, lost = apply_inverter_scenarios(result[inverter_id], direct)
        result[inverter_id] = frame
        losses[inverter_id] = lost

    for instance in schedule.instances:
        if instance.scenario_id != ScenarioKind.STUCK_TRACKER.value:
            continue
        for target in tracker_targets.get(instance.asset_id, []):
            frame, lost = apply_tracker_scenario(result[target], instance)
            result[target] = frame
            losses[target] = losses.get(target, pd.Series(0.0, index=index)).add(
                lost, fill_value=0.0
            )

    # A transformer outage takes its whole block down: every inverter behind it
    # stops exporting regardless of its own health.
    for instance in schedule.instances:
        if instance.scenario_id != ScenarioKind.TRANSFORMER_TRIP.value:
            continue
        siblings = [
            a.asset_id
            for a in plant.children(instance.asset_id)
            if a.asset_type is AssetType.INVERTER
        ]
        for target in siblings:
            frame, lost = apply_inverter_scenarios(result[target], [instance])
            result[target] = frame
            losses[target] = losses.get(target, pd.Series(0.0, index=index)).add(
                lost, fill_value=0.0
            )

    return result, schedule, losses


def _apply_sensor_layer(
    inverters: dict[str, pd.DataFrame],
    stations: dict[str, pd.DataFrame],
    *,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], SensorFleet]:
    """Produce analyst-facing frames from physical truth.

    The truth frames are left untouched. Measurement is a pure function applied
    *to* truth, never a modification *of* it - which is what guarantees that a
    sensor fault cannot change what the plant actually produced.

    Args:
        inverters: Per-inverter truth frames.
        stations: Per-weather-station truth frames.
        seed: Seed for the sensor substreams.

    Returns:
        A tuple of measured frames and the sensor fleet that produced them.
    """
    sources = {**stations, **inverters}
    pairs = [
        (asset_id, column)
        for asset_id, frame in sources.items()
        for column in frame.columns
        if column in FIELD_CLASSES
    ]
    fleet = build_sensor_fleet(pairs, seed=seed)

    measured = {
        asset_id: measure_frame(frame, asset_id, fleet, seed=seed)
        for asset_id, frame in sources.items()
    }
    return measured, fleet


def _apply_states_and_control(
    config: PlantConfig,
    unconstrained: dict[str, pd.DataFrame],
    economic_curtailment: pd.Series | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Resolve inverter states, dispatch setpoints and record transitions.

    Order matters. States are resolved from resource availability first, so an
    inverter's sleep and startup behaviour does not depend on what the
    controller happens to be doing. The controller then binds on top, and any
    inverter it holds below its available power is relabelled ``CURTAILED``.

    Args:
        config: Plant configuration.
        unconstrained: Physics-chain output per inverter, before states.
        economic_curtailment: Price-driven curtailment intervals.

    Returns:
        A tuple of per-inverter frames and the state transition records.
    """
    keys = sorted(unconstrained)

    states: dict[str, pd.Series] = {}
    reasons: dict[str, pd.Series] = {}
    delivered: dict[str, pd.Series] = {}

    for key in keys:
        frame = unconstrained[key]
        state, reason = resolve_inverter_states(config, frame["poa_global"])
        states[key] = state
        reasons[key] = reason
        delivered[key] = apply_state_to_output(config, state, frame["ac_power_kw"])

    # Delivery efficiency converts inverter AC into expected export, so the
    # meter-side limit can be turned into inverter-side setpoints.
    efficiency = 1.0 - AC_COLLECTION_LOSS_AT_RATED
    controller = PlantController(config)
    commanded = controller.dispatch(
        {key: unconstrained[key]["ac_power_kw"].clip(lower=0.0) for key in keys},
        delivery_efficiency=pd.Series(efficiency, index=unconstrained[keys[0]].index),
        economic_curtailment=economic_curtailment,
    )

    results: dict[str, pd.DataFrame] = {}
    transitions = []

    for key in keys:
        operation = apply_setpoint(
            states[key],
            reasons[key],
            unconstrained[key]["ac_power_kw"].clip(lower=0.0),
            commanded[key],
            delivered[key],
        )
        frame = unconstrained[key].copy()
        frame["available_power_kw"] = operation.available_power_kw
        frame["commanded_power_kw"] = operation.commanded_power_kw
        frame["curtailed_power_kw"] = operation.curtailed_power_kw
        frame["ac_power_kw"] = operation.ac_power_kw
        frame["operating_state"] = operation.state
        frame["state_reason"] = operation.reason
        results[key] = frame

        transitions.extend(extract_transitions(operation.state, key, operation.reason))

    illegal = validate_transitions(transitions, INVERTER_TRANSITIONS)
    if illegal:
        LOGGER.error(
            "%d illegal inverter transitions, first: %s -> %s",
            len(illegal),
            illegal[0].from_state,
            illegal[0].to_state,
        )

    return results, transitions_to_frame(transitions)


def _run_blocks(
    config: PlantConfig,
    plant: Plant,
    block_assets: list[Asset],
    inverters: dict[str, pd.DataFrame],
    ambient: pd.Series,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Aggregate inverters to blocks and apply transformer losses.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.
        block_assets: Power blocks to process.
        inverters: Per-inverter production frames.
        ambient: Ambient temperature series.

    Returns:
        A tuple of per-block frames and per-transformer frames.
    """
    blocks: dict[str, pd.DataFrame] = {}
    transformers: dict[str, pd.DataFrame] = {}

    for block in block_assets:
        children = plant.children(block.asset_id)
        block_inverters = [
            child.asset_id for child in children if child.asset_type is AssetType.INVERTER
        ]
        transformer_id = next(
            child.asset_id
            for child in children
            if child.asset_type is AssetType.TRANSFORMER
        )

        inverter_ac = sum(inverters[key]["ac_power_kw"] for key in block_inverters)
        inverter_dc = sum(inverters[key]["dc_power_kw"] for key in block_inverters)

        response = transformer_response(inverter_ac, config, ambient)
        transformers[transformer_id] = response

        curtailed = sum(
            inverters[key].get(
                "curtailed_power_kw", pd.Series(0.0, index=inverter_ac.index)
            )
            for key in block_inverters
        )

        blocks[block.asset_id] = pd.DataFrame(
            {
                "dc_power_kw": inverter_dc,
                "curtailed_power_kw": curtailed,
                "inverter_ac_power_kw": inverter_ac,
                "ac_power_kw": response["output_power_kw"],
                "transformer_loss_kw": (
                    response["no_load_loss_kw"] + response["load_loss_kw"]
                ),
                "transformer_loading_pct": response["loading_pct"],
                "active_inverters": len(block_inverters),
            }
        )

    return blocks, transformers


def _run_plant_level(
    config: PlantConfig, blocks: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    """Aggregate blocks through AC collection to the revenue meter.

    Args:
        config: Plant configuration.
        blocks: Per-block frames.

    Returns:
        A plant-level frame including grid export.
    """
    collected = sum(frame["ac_power_kw"] for frame in blocks.values())
    dc = sum(frame["dc_power_kw"] for frame in blocks.values())
    inverter_ac = sum(frame["inverter_ac_power_kw"] for frame in blocks.values())
    transformer_loss = sum(frame["transformer_loss_kw"] for frame in blocks.values())
    curtailed = sum(frame["curtailed_power_kw"] for frame in blocks.values())

    loss = collection_loss_kw(collected, config.plant_ac_kw)
    substation_output = collected - loss

    # The point of interconnection limit binds regardless of what the plant
    # could produce. Phase 9 turns the difference into curtailed energy with a
    # cause code; here it is recorded so the constraint is visible.
    export = substation_output.clip(upper=config.grid.poi_export_limit_kw)

    return pd.DataFrame(
        {
            "total_dc_power_kw": dc,
            "total_inverter_ac_power_kw": inverter_ac,
            "collected_ac_power_kw": collected,
            "transformer_loss_kw": transformer_loss,
            "collection_loss_kw": loss,
            "substation_power_kw": substation_output,
            "grid_export_power_kw": export,
            "poi_limited_kw": (substation_output - export).clip(lower=0.0),
            "curtailed_power_kw": curtailed,
            "active_blocks": len(blocks),
        }
    )


def energy_mwh(power_kw: pd.Series) -> float:
    """Integrate a power series to energy.

    Energy is always integrated from power, never generated independently.
    Independent generation is the most common way simulated datasets fail
    reconciliation.

    Args:
        power_kw: Power in kilowatts, on a regular index.

    Returns:
        Energy in megawatt-hours.
    """
    hours = _interval_minutes(power_kw.index) / 60.0
    return float(power_kw.sum() * hours / 1000.0)


@dataclass
class PlantGateResult:
    """Outcome of the Phase 4 full-plant acceptance checks.

    Attributes:
        checks: Named outcomes, each a pass flag and a detail string.
        seconds_per_day: Wall-clock cost of one simulated day.
    """

    checks: list[tuple[str, bool, str]]
    seconds_per_day: float

    @property
    def passed(self) -> bool:
        """Whether every check succeeded.

        Returns:
            ``True`` when no check failed.
        """
        return all(ok for _, ok, _ in self.checks)

    def render(self) -> str:
        """Format the result for terminal output.

        Returns:
            A multi-line report.
        """
        lines = [
            f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {detail}"
            for name, ok, detail in self.checks
        ]
        lines.append(f"\n  {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def run_plant_gate(
    config: PlantConfig,
    plant: Plant,
    result: PlantRunResult,
    *,
    seconds_per_day: float,
    throughput_target_minutes: float = 60.0,
) -> PlantGateResult:
    """Verify a full-plant run meets its Phase 4 acceptance criteria.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.
        result: Output of :func:`run_plant`.
        seconds_per_day: Wall-clock cost of the run, per simulated day.
        throughput_target_minutes: Budget for one simulated year.

    Returns:
        A :class:`PlantGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    frame = result.plant

    # Hierarchy reconciliation: block totals must be the sum of their inverters,
    # and plant totals the sum of their blocks. A mismatch means an asset was
    # double-counted or dropped, which no downstream check would notice.
    inverter_sum = result.ac_matrix().sum(axis=1)
    block_inverter_sum = sum(f["inverter_ac_power_kw"] for f in result.blocks.values())
    hierarchy_error = float((inverter_sum - block_inverter_sum).abs().max())
    checks.append(
        (
            "hierarchy_reconciles",
            hierarchy_error < 1e-6,
            f"inverter sum vs block sum, max error {hierarchy_error:.2e} kW",
        )
    )

    # Energy must close through the loss chain, not merely look plausible.
    closure = energy_mwh(
        frame["total_inverter_ac_power_kw"]
        - frame["transformer_loss_kw"]
        - frame["collection_loss_kw"]
        - frame["poi_limited_kw"]
        - frame["grid_export_power_kw"]
    )
    exported = energy_mwh(frame["grid_export_power_kw"])
    residual = abs(closure) / exported if exported else 1.0
    checks.append(
        (
            "energy_chain_closes",
            residual < 1e-6,
            f"residual {residual:.2e} of exported energy",
        )
    )

    # Bifacial gain lets DC exceed nameplate briefly; 155% does not. That was
    # the symptom of passing front-surface geometry to the rear irradiance
    # model, which returned front irradiance and added 70% of it back.
    # An upper bound only. The lower bound depends on the irradiance the gate
    # happens to run at, so requiring DC to approach nameplate would make the
    # check a statement about the test weather rather than about the model.
    # The clear-sky case is pinned separately in the unit tests.
    dc_fraction = float(frame["total_dc_power_kw"].max()) / config.plant_dc_kw
    checks.append(
        (
            "dc_within_physical_bound",
            dc_fraction <= 1.12,
            f"peak DC {dc_fraction:.1%} of nameplate (bifacial gain permits >100%)",
        )
    )

    ac_fraction = float(frame["total_inverter_ac_power_kw"].max()) / config.plant_ac_kw
    checks.append(
        (
            "ac_respects_nameplate",
            ac_fraction <= 1.001,
            f"peak inverter AC {ac_fraction:.1%} of nameplate",
        )
    )

    transformer_fraction = energy_mwh(frame["transformer_loss_kw"]) / max(
        energy_mwh(frame["total_inverter_ac_power_kw"]), 1e-9
    )
    collection_fraction = energy_mwh(frame["collection_loss_kw"]) / max(
        energy_mwh(frame["total_inverter_ac_power_kw"]), 1e-9
    )
    checks.append(
        (
            "losses_in_expected_range",
            0.001 < transformer_fraction < 0.02 and 0.001 < collection_fraction < 0.02,
            f"transformer {transformer_fraction:.2%}, collection "
            f"{collection_fraction:.2%}",
        )
    )

    # Block peer comparison must be non-trivial: identical blocks make
    # underperformance detection meaningless.
    blocks = result.block_matrix()
    daylight = blocks.mean(axis=1) > 0.02 * config.plant_ac_kw
    matrix = blocks[daylight].corr().to_numpy()
    off_diagonal = matrix[np.triu_indices_from(matrix, 1)]
    checks.append(
        (
            "blocks_vary_realistically",
            off_diagonal.min() > 0.85 and off_diagonal.max() < 0.99999,
            f"block AC correlation {off_diagonal.min():.4f} .. {off_diagonal.max():.4f}",
        )
    )

    # Fleet smoothing: 40 inverters spread over 3.26 km must ramp less sharply
    # in aggregate than any one of them does alone.
    per_inverter = result.ac_matrix()
    individual = float(
        np.mean([per_inverter[c].diff().dropna().std() for c in per_inverter.columns])
    )
    aggregate = float(
        (frame["grid_export_power_kw"] / len(per_inverter.columns)).diff().dropna().std()
    )
    ratio = aggregate / individual if individual else 1.0
    checks.append(
        (
            "fleet_aggregate_smoother",
            ratio < 0.9,
            f"ramp std ratio {ratio:.4f}",
        )
    )

    projected = seconds_per_day * 365.0 / 60.0
    checks.append(
        (
            "throughput_within_budget",
            projected <= throughput_target_minutes,
            f"{seconds_per_day:.2f} s/day -> {projected:.1f} min/year "
            f"(budget {throughput_target_minutes:.0f})",
        )
    )

    assets = len(plant.telemetry_assets())
    checks.append(
        (
            "full_plant_simulated",
            len(result.inverters) == config.total_inverters
            and len(result.blocks) == config.topology.power_blocks,
            f"{len(result.inverters)} inverters, {len(result.blocks)} blocks, "
            f"{assets} telemetry assets",
        )
    )

    return PlantGateResult(checks=checks, seconds_per_day=seconds_per_day)
