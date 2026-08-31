"""The Phase 2 physics oracle gate.

Design document ``16`` section 5 makes this a hard gate: nothing expands beyond
one inverter until the simulator's production chain agrees with an independent
pvlib reference.

The gate matters because a physics error is invisible to every downstream check.
Correlations still look right, ramps still look right, energy still integrates
from power - the numbers are simply wrong by a systematic factor that no
later test is designed to notice. This is the only check that examines the
production chain itself rather than its consequences.

For the comparison to mean anything the two paths must be genuinely
independent. :mod:`northstar_sim.physics` composes explicit pvlib calls;
:func:`run_reference_chain` here delegates to ``pvlib.modelchain.ModelChain``,
which resolves and orders the same models through entirely different code. Same
physics, different implementation.

Reference: design documents ``07_solar_production_model`` section 4 and
``15_validation_acceptance_specification`` section 4.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pvlib

from .physics import build_system, run_inverter_chain
from .plant_config import PlantConfig
from .resource import location_from_config

#: Relative tolerance for the gate, applied to daylight samples. Both paths
#: solve the same single-diode equation with the same parameters, so agreement
#: should be limited only by floating point and by minor ordering differences in
#: how the two implementations sequence identical operations.
GATE_RELATIVE_TOLERANCE = 1e-6

#: Irradiance below which a sample is excluded from the comparison. Near zero
#: the relative error denominator collapses and reports enormous relative
#: differences for absolute differences of microwatts.
DAYLIGHT_THRESHOLD_WM2 = 50.0


@dataclass
class GateResult:
    """Outcome of the physics oracle comparison.

    Attributes:
        samples: Daylight samples compared.
        max_relative_error_dc: Largest relative DC power difference.
        max_relative_error_ac: Largest relative AC power difference.
        max_absolute_error_dc_kw: Largest absolute DC difference.
        max_absolute_error_ac_kw: Largest absolute AC difference.
        energy_error_fraction: Relative difference in total AC energy.
        passed: Whether every tolerance was met.
    """

    samples: int
    max_relative_error_dc: float
    max_relative_error_ac: float
    max_absolute_error_dc_kw: float
    max_absolute_error_ac_kw: float
    energy_error_fraction: float
    passed: bool

    def render(self) -> str:
        """Format the gate result for terminal output.

        Returns:
            A multi-line report.
        """
        verdict = "PASS" if self.passed else "FAIL"
        return "\n".join(
            [
                f"  samples compared        {self.samples:,}",
                f"  max relative error DC   {self.max_relative_error_dc:.3e}",
                f"  max relative error AC   {self.max_relative_error_ac:.3e}",
                f"  max absolute error DC   {self.max_absolute_error_dc_kw:.6f} kW",
                f"  max absolute error AC   {self.max_absolute_error_ac_kw:.6f} kW",
                f"  AC energy difference    {self.energy_error_fraction:.3e}",
                f"  tolerance               {GATE_RELATIVE_TOLERANCE:.1e}",
                f"\n  {verdict}",
            ]
        )


def run_reference_chain(config: PlantConfig, weather: pd.DataFrame) -> pd.DataFrame:
    """Compute production through pvlib's own ``ModelChain``.

    ``ModelChain`` resolves model choices, orders the computation and handles
    array bookkeeping internally. None of that code is shared with
    :func:`northstar_sim.physics.run_inverter_chain`, which is what makes the
    comparison a real check rather than a tautology.

    Args:
        config: Plant configuration.
        weather: 1-minute frame with ``ghi``, ``dni``, ``dhi``, ``temp_air`` and
            ``wind_speed``.

    Returns:
        A frame with ``dc_power_kw``, ``ac_power_kw``, ``cell_temperature`` and
        ``poa_global``.
    """
    system = build_system(config)
    # Every model is named explicitly. ModelChain's defaults are not the locked
    # chain - it transposes with Hay-Davies, while DR-003 specifies Perez. The
    # first run of this gate failed at 15% relative error for exactly that
    # reason, which is the gate doing its job: it compares implementations of
    # the same physics, so the physics must be pinned on both sides.
    chain = pvlib.modelchain.ModelChain(
        system,
        location_from_config(config),
        dc_model="cec",
        ac_model="sandia",
        aoi_model="physical",
        spectral_model="no_loss",
        temperature_model="faiman",
        transposition_model="perez",
        losses_model="no_loss",
    )
    chain.run_model(weather)

    return pd.DataFrame(
        {
            "dc_power_kw": chain.results.dc["p_mp"] / 1000.0,
            "ac_power_kw": chain.results.ac / 1000.0,
            "cell_temperature": chain.results.cell_temperature,
            "poa_global": chain.results.total_irrad["poa_global"],
        },
        index=weather.index,
    )


def _relative_error(left: pd.Series, right: pd.Series) -> float:
    """Compute the largest relative difference between two series.

    Args:
        left: First series.
        right: Second series.

    Returns:
        Maximum relative difference, or 0.0 when there is nothing to compare.
    """
    scale = np.maximum(left.abs(), right.abs())
    mask = scale > 1e-9
    if not mask.any():
        return 0.0
    return float(((left - right).abs()[mask] / scale[mask]).max())


def run_physics_gate(
    config: PlantConfig,
    weather: pd.DataFrame,
    *,
    tolerance: float = GATE_RELATIVE_TOLERANCE,
) -> GateResult:
    """Run both chains over the same inputs and compare them.

    Bifacial rear irradiance and plant losses are both disabled in the
    simulator chain for this comparison. ``ModelChain`` has no equivalent step
    for either, so leaving them enabled would compare two different physical
    models and report a difference that means nothing about implementation
    correctness.

    Args:
        config: Plant configuration.
        weather: Resource frame covering the comparison window.
        tolerance: Maximum permitted relative difference.

    Returns:
        A :class:`GateResult`.
    """
    # Plant losses are disabled on both sides. ModelChain models module and
    # inverter physics; degradation, mismatch, DC wiring and thermal derating
    # are plant characteristics with no ModelChain equivalent. Leaving them on
    # compares a plant against a module and blames the difference on the
    # implementation.
    simulator = run_inverter_chain(
        config, weather, include_rear=False, apply_plant_losses=False
    )
    reference = run_reference_chain(config, weather)

    daylight = simulator["poa_global"] > DAYLIGHT_THRESHOLD_WM2
    sim_day = simulator[daylight]
    ref_day = reference[daylight]

    dc_relative = _relative_error(sim_day["dc_power_kw"], ref_day["dc_power_kw"])
    ac_relative = _relative_error(sim_day["ac_power_kw"], ref_day["ac_power_kw"])

    dc_absolute = float((sim_day["dc_power_kw"] - ref_day["dc_power_kw"]).abs().max())
    ac_absolute = float((sim_day["ac_power_kw"] - ref_day["ac_power_kw"]).abs().max())

    sim_energy = float(simulator["ac_power_kw"].sum())
    ref_energy = float(reference["ac_power_kw"].sum())
    energy_error = abs(sim_energy - ref_energy) / abs(ref_energy) if ref_energy else 0.0

    return GateResult(
        samples=int(daylight.sum()),
        max_relative_error_dc=dc_relative,
        max_relative_error_ac=ac_relative,
        max_absolute_error_dc_kw=dc_absolute,
        max_absolute_error_ac_kw=ac_absolute,
        energy_error_fraction=energy_error,
        passed=(
            dc_relative <= tolerance
            and ac_relative <= tolerance
            and energy_error <= tolerance
        ),
    )
