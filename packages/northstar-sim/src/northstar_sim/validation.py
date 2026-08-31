"""Phase 1 configuration and plant validation.

This is the gate described in ``16_implementation_roadmap`` section 4: a
configuration must instantiate a complete plant whose capacity totals reconcile
and whose electrical limits are respected, before any physics is written.

The most valuable check here is the cold-temperature open-circuit voltage limit
in :func:`check_string_voltage`. It caught a genuine error in the baseline
design: ``03_reference_solar_farm`` v2.0 specified 28 modules per string and
claimed roughly 1,494 V at the design minimum temperature. The correct figure is
1,586 V, which exceeds the 1,500 V inverter ceiling. That error survived review
in prose and was found the moment the arithmetic became executable.

Reference: design documents ``15_validation_acceptance_specification`` and
``05_equipment_catalog``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .assets import AssetType, Plant
from .plant_config import PlantConfig


@dataclass
class CheckResult:
    """Outcome of a single validation check.

    Attributes:
        name: Short identifier.
        passed: Whether the check succeeded.
        blocking: Whether a failure prevents the plant being used.
        detail: Explanation, populated whether or not the check passed, since
            the measured value is useful in either case.
    """

    name: str
    passed: bool
    blocking: bool
    detail: str = ""


@dataclass
class ValidationReport:
    """Aggregated Phase 1 validation results.

    Attributes:
        results: Check outcomes in execution order.
    """

    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        """Record one check outcome.

        Args:
            result: Outcome to append.
        """
        self.results.append(result)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        """Failures that prevent the configuration being used.

        Returns:
            Failed blocking checks.
        """
        return [r for r in self.results if not r.passed and r.blocking]

    @property
    def warnings(self) -> list[CheckResult]:
        """Failures recorded without blocking.

        Returns:
            Failed non-blocking checks.
        """
        return [r for r in self.results if not r.passed and not r.blocking]

    @property
    def ok(self) -> bool:
        """Whether the configuration passes.

        Returns:
            ``True`` when there are no blocking failures.
        """
        return not self.blocking_failures

    def render(self) -> str:
        """Format the full report for terminal output.

        Returns:
            A multi-line report with one line per check.
        """
        lines = []
        for result in self.results:
            mark = "PASS" if result.passed else ("FAIL" if result.blocking else "WARN")
            lines.append(f"  [{mark}] {result.name:<28} {result.detail}")
        verdict = "PASS" if self.ok else "FAIL"
        lines.append(
            f"\n  {verdict}: {len(self.results)} checks, "
            f"{len(self.warnings)} warning(s), "
            f"{len(self.blocking_failures)} blocking failure(s)"
        )
        return "\n".join(lines)


def check_string_voltage(config: PlantConfig) -> CheckResult:
    """Verify string open-circuit voltage against the inverter ceiling.

    Open-circuit voltage rises as cell temperature falls, so the worst case is a
    cold clear sunrise: the array is illuminated and at ambient temperature while
    the inverter has not yet begun drawing current. Exceeding the DC voltage
    ceiling in that condition damages equipment, so it constrains string length
    directly.

    Args:
        config: Plant configuration.

    Returns:
        A blocking :class:`CheckResult`.
    """
    voc = config.string_voc_cold_v
    limit = config.inverter.max_dc_voltage_v
    margin = config.voc_margin_v
    passed = voc < limit

    detail = (
        f"{config.topology.modules_per_string} modules -> {voc:.1f} V at "
        f"{config.site.design_min_temp_c:.0f} C, limit {limit:.0f} V, "
        f"margin {margin:+.1f} V"
    )
    if not passed:
        detail += f" - maximum is {config.max_modules_per_string} modules"
    return CheckResult("string_voc_cold", passed, blocking=True, detail=detail)


def check_voc_margin_reasonable(config: PlantConfig) -> CheckResult:
    """Warn when the voltage headroom is implausibly generous.

    A margin above roughly 10% means the string is shorter than it needs to be,
    which raises balance-of-system cost per watt for no benefit. Real designs sit
    close to the ceiling. This is a modelling-realism warning, not an error.

    Args:
        config: Plant configuration.

    Returns:
        A non-blocking :class:`CheckResult`.
    """
    limit = config.inverter.max_dc_voltage_v
    fraction = config.voc_margin_v / limit
    passed = fraction <= 0.10
    return CheckResult(
        "voc_margin_realistic",
        passed,
        blocking=False,
        detail=(
            f"{fraction:.1%} headroom"
            + ("" if passed else " - string could be longer; check realism")
        ),
    )


def check_dc_input_limit(config: PlantConfig) -> CheckResult:
    """Verify connected DC does not exceed the inverter's DC input rating.

    Args:
        config: Plant configuration.

    Returns:
        A blocking :class:`CheckResult`.
    """
    connected = config.inverter_dc_kw
    limit = config.inverter.max_dc_input_kw
    passed = connected <= limit
    return CheckResult(
        "inverter_dc_input",
        passed,
        blocking=True,
        detail=(
            f"{connected:,.1f} kW connected, {limit:,.1f} kW rated "
            f"({connected / limit:.1%})"
        ),
    )


def check_dc_ac_ratio(
    config: PlantConfig, *, low: float = 1.15, high: float = 1.45
) -> CheckResult:
    """Verify the DC to AC ratio sits in a range that produces useful clipping.

    Below the lower bound the plant rarely clips, removing a whole loss category
    from the dataset. Above the upper bound clipping saturates the middle of the
    day, which destroys the irradiance-to-AC-power correlation the validation
    specification requires.

    Args:
        config: Plant configuration.
        low: Minimum acceptable ratio.
        high: Maximum acceptable ratio.

    Returns:
        A blocking :class:`CheckResult`.
    """
    ratio = config.dc_ac_ratio
    passed = low <= ratio <= high
    return CheckResult(
        "dc_ac_ratio",
        passed,
        blocking=True,
        detail=f"{ratio:.4f} (acceptable {low}-{high})",
    )


def check_transformer_rating(config: PlantConfig) -> CheckResult:
    """Verify each block transformer can carry its inverters at full output.

    Args:
        config: Plant configuration.

    Returns:
        A blocking :class:`CheckResult`.
    """
    block_ac_kw = config.inverter.rated_ac_kw * config.topology.inverters_per_block
    rating_kw = config.transformer.rated_mva * 1000.0
    loading = block_ac_kw / rating_kw
    passed = loading <= 1.0
    return CheckResult(
        "transformer_rating",
        passed,
        blocking=True,
        detail=(
            f"block peak {block_ac_kw:,.0f} kW against {rating_kw:,.0f} kVA "
            f"({loading:.1%} loading)"
        ),
    )


def check_poi_limit(config: PlantConfig) -> CheckResult:
    """Verify the interconnection limit is consistent with AC nameplate.

    An export limit far below nameplate would make curtailment permanent rather
    than event-driven, which is a different plant from the one being modelled.

    Args:
        config: Plant configuration.

    Returns:
        A non-blocking :class:`CheckResult`.
    """
    ratio = config.grid.poi_export_limit_kw / config.plant_ac_kw
    passed = 0.95 <= ratio <= 1.05
    return CheckResult(
        "poi_export_limit",
        passed,
        blocking=False,
        detail=(
            f"{config.grid.poi_export_limit_kw:,.0f} kW against "
            f"{config.plant_ac_kw:,.0f} kW AC nameplate ({ratio:.1%})"
        ),
    )


def check_capacity_reconciliation(config: PlantConfig, plant: Plant) -> CheckResult:
    """Verify instantiated assets reproduce the configuration's capacity.

    Capacity is summed independently from the built asset tree and compared with
    the figure derived from module count and rating. Agreement proves the
    builder and the configuration describe the same plant.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.

    Returns:
        A blocking :class:`CheckResult`.
    """
    inverter_ac = sum(
        a.rated_capacity_kw or 0.0 for a in plant.of_type(AssetType.INVERTER)
    )
    combiner_dc = sum(
        a.rated_capacity_kw or 0.0 for a in plant.of_type(AssetType.COMBINER)
    )
    ac_error = abs(inverter_ac - config.plant_ac_kw)
    dc_error = abs(combiner_dc - config.plant_dc_kw)
    passed = ac_error < 1.0 and dc_error < 1.0
    return CheckResult(
        "capacity_reconciliation",
        passed,
        blocking=True,
        detail=(
            f"AC {inverter_ac:,.1f} kW (error {ac_error:.3f}), "
            f"DC {combiner_dc:,.1f} kW (error {dc_error:.3f})"
        ),
    )


def check_hierarchy_integrity(plant: Plant) -> CheckResult:
    """Verify every asset except the site has a resolvable parent.

    Args:
        plant: Instantiated plant.

    Returns:
        A blocking :class:`CheckResult`.
    """
    orphans = [
        a.asset_id for a in plant if a.parent_id is not None and a.parent_id not in plant
    ]
    roots = [a for a in plant if a.parent_id is None]
    passed = not orphans and len(roots) == 1
    detail = f"{len(plant):,} assets, {len(roots)} root"
    if orphans:
        detail += f", orphans: {orphans[:5]}"
    return CheckResult("hierarchy_integrity", passed, blocking=True, detail=detail)


def check_telemetry_positions(plant: Plant) -> CheckResult:
    """Verify every telemetry-bearing asset carries a position.

    Positions drive the advected cloud field. An asset without one cannot be
    given a spatially coherent irradiance series, so it would silently see the
    plant-average resource and stop being useful for peer comparison.

    Args:
        plant: Instantiated plant.

    Returns:
        A blocking :class:`CheckResult`.
    """
    missing = [a.asset_id for a in plant.telemetry_assets() if a.position is None]
    telemetry_count = len(plant.telemetry_assets())
    return CheckResult(
        "telemetry_positions",
        not missing,
        blocking=True,
        detail=(
            f"{telemetry_count:,} telemetry assets positioned"
            if not missing
            else f"{len(missing)} without position: {missing[:5]}"
        ),
    )


def check_asset_id_uniqueness(plant: Plant) -> CheckResult:
    """Verify asset identifiers are unique.

    Duplicate identifiers would silently merge two assets' telemetry, which is
    undetectable downstream. :class:`~northstar_sim.assets.Plant` rejects
    duplicates at construction, so this check confirms that guarantee held.

    Args:
        plant: Instantiated plant.

    Returns:
        A blocking :class:`CheckResult`.
    """
    identifiers = [a.asset_id for a in plant]
    unique = len(set(identifiers))
    passed = unique == len(identifiers)
    return CheckResult(
        "asset_id_uniqueness",
        passed,
        blocking=True,
        detail=f"{unique:,} unique of {len(identifiers):,}",
    )


def check_spatial_spread(config: PlantConfig, plant: Plant) -> CheckResult:
    """Verify assets span enough ground for cloud advection to be observable.

    At a typical West Texas wind speed a cloud edge must take appreciably longer
    than the telemetry interval to cross the site, or every asset sees the same
    irradiance at the same time and spatial analysis is impossible.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.

    Returns:
        A non-blocking :class:`CheckResult`.
    """
    positioned = [a.position for a in plant.telemetry_assets() if a.position]
    if not positioned:
        return CheckResult("spatial_spread", False, blocking=False, detail="no positions")

    span_x = max(p.x_m for p in positioned) - min(p.x_m for p in positioned)
    span_y = max(p.y_m for p in positioned) - min(p.y_m for p in positioned)
    longest = max(span_x, span_y)

    # 8 m/s is a representative West Texas daytime wind speed.
    crossing_minutes = longest / 8.0 / 60.0
    passed = crossing_minutes >= 2.0
    return CheckResult(
        "spatial_spread",
        passed,
        blocking=False,
        detail=(
            f"{span_x:,.0f} x {span_y:,.0f} m, cloud crossing "
            f"{crossing_minutes:.1f} min at 8 m/s"
        ),
    )


def check_area_plausible(config: PlantConfig, plant: Plant) -> CheckResult:
    """Compare the laid-out footprint with the configured site area.

    Args:
        config: Plant configuration.
        plant: Instantiated plant, unused but accepted for signature symmetry.

    Returns:
        A non-blocking :class:`CheckResult`.
    """
    from .builder import site_extent

    width, height = site_extent(config)
    acres = width * height / 4046.86
    ratio = acres / config.site.area_acres
    passed = 0.8 <= ratio <= 1.2
    return CheckResult(
        "footprint_area",
        passed,
        blocking=False,
        detail=(
            f"layout {acres:,.0f} acres against configured "
            f"{config.site.area_acres:,.0f} ({ratio:.0%})"
        ),
    )


def validate_plant(config: PlantConfig, plant: Plant) -> ValidationReport:
    """Run the full Phase 1 check set.

    Args:
        config: Plant configuration.
        plant: Instantiated plant.

    Returns:
        A report whose blocking failures mean the configuration must not be
        used to generate data.
    """
    report = ValidationReport()
    report.add(check_string_voltage(config))
    report.add(check_voc_margin_reasonable(config))
    report.add(check_dc_input_limit(config))
    report.add(check_dc_ac_ratio(config))
    report.add(check_transformer_rating(config))
    report.add(check_poi_limit(config))
    report.add(check_capacity_reconciliation(config, plant))
    report.add(check_hierarchy_integrity(plant))
    report.add(check_asset_id_uniqueness(plant))
    report.add(check_telemetry_positions(plant))
    report.add(check_spatial_spread(config, plant))
    report.add(check_area_plausible(config, plant))
    return report
