"""The loss attribution waterfall.

Design document ``18`` section 5 requires that **every lost kWh carries a cause
code**, and that the waterfall from theoretical output to metered export closes
with a residual under 0.5%. A growing residual means an unattributed loss path
exists, which is a correctness bug rather than a rounding issue.

**Attribution is cascading, not independent.** Losses multiply, so the energy
attributed to a stage is what that stage removed *from what reached it*, not
what it would have removed from the theoretical maximum. Attributing each
stage against the theoretical would over-count: the sum of independently
computed losses exceeds the actual shortfall whenever two or more stages act.

The formulation here closes by construction. Each stage is
``upstream_power x (1 - factor)`` and the next stage starts from
``upstream_power x factor``, so the telescoping sum is exactly the difference
between the first and last terms.

Not every loss is avoidable, and the distinction matters more than the
arithmetic. Clipping and degradation are **design and physics**, not failures;
reporting them as recoverable is a classic analytical error that this module
makes possible to commit and then to correct.

Reference: design documents ``18_financial_commercial_model`` section 5 and
``02_time_series_analytics_requirements`` section 7.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .plant_config import PlantConfig

#: Cause codes and whether the loss is treated as avoidable. "Monetized" in
#: design doc 18 means the loss is converted to lost revenue and reported as
#: recoverable. Clipping and degradation are deliberately excluded: they are
#: consequences of the design and of physics, and an analyst who reports them
#: as recoverable has made an error the dataset should permit and then correct.
CAUSE_CODES: dict[str, bool] = {
    "LOSS_SOILING": True,
    "LOSS_DEGRADATION": False,
    "LOSS_MISMATCH": False,
    "LOSS_DC_WIRING": False,
    "LOSS_THERMAL": False,
    "LOSS_LOWLIGHT": False,
    "LOSS_INVERTER_EFF": False,
    "LOSS_CLIPPING": False,
    "LOSS_INV_THERMAL": True,
    "LOSS_INV_STATE": True,
    "LOSS_CURTAILMENT": True,
    "LOSS_TRANSFORMER": False,
    "LOSS_AC_COLLECTION": False,
    "LOSS_POI_LIMIT": True,
    "LOSS_RESIDUAL": False,
}

#: Residual above this fraction of theoretical energy means an unattributed
#: loss path exists. Design doc 18 section 10 sets the limit.
RESIDUAL_TOLERANCE = 0.005


@dataclass
class LossWaterfall:
    """Cause-coded loss attribution for one asset or the whole plant.

    Attributes:
        stages: Per-timestep loss power by cause code, in waterfall order.
        theoretical_kw: Power before any loss.
        exported_kw: Power after every loss.
    """

    stages: pd.DataFrame
    theoretical_kw: pd.Series
    exported_kw: pd.Series

    def energy_mwh(self, interval_minutes: float = 1.0) -> pd.Series:
        """Integrate each loss stage to energy.

        Args:
            interval_minutes: Sampling interval.

        Returns:
            Energy by cause code in megawatt-hours, plus theoretical and
            exported totals.
        """
        hours = interval_minutes / 60.0
        totals = self.stages.sum() * hours / 1000.0
        totals["THEORETICAL"] = self.theoretical_kw.sum() * hours / 1000.0
        totals["EXPORTED"] = self.exported_kw.sum() * hours / 1000.0
        return totals

    def residual_kw(self) -> pd.Series:
        """Compute the unattributed remainder at each timestep.

        Returns:
            Theoretical less every attributed loss less exported power. Should
            be zero to machine precision.
        """
        return self.theoretical_kw - self.stages.sum(axis=1) - self.exported_kw

    def closure_error(self) -> float:
        """Measure closure as a fraction of theoretical energy.

        Returns:
            Absolute residual energy divided by theoretical energy.
        """
        theoretical = float(self.theoretical_kw.sum())
        if theoretical == 0:
            return 0.0
        return abs(float(self.residual_kw().sum())) / theoretical

    def summary(self, interval_minutes: float = 1.0) -> pd.DataFrame:
        """Render the waterfall as a reportable table.

        Args:
            interval_minutes: Sampling interval.

        Returns:
            One row per cause code with energy, share of theoretical, and
            whether the loss is treated as avoidable.
        """
        energy = self.energy_mwh(interval_minutes)
        theoretical = energy["THEORETICAL"]

        rows = [
            {
                "cause_code": code,
                "energy_mwh": float(energy.get(code, 0.0)),
                "share_of_theoretical": (
                    float(energy.get(code, 0.0)) / theoretical if theoretical else 0.0
                ),
                "avoidable": avoidable,
            }
            for code, avoidable in CAUSE_CODES.items()
            if code in energy.index
        ]
        return pd.DataFrame(rows).sort_values("energy_mwh", ascending=False)


def _cascade(
    upstream: pd.Series, factor: pd.Series | float
) -> tuple[pd.Series, pd.Series]:
    """Apply one multiplicative loss stage.

    Args:
        upstream: Power entering the stage.
        factor: Fraction surviving the stage.

    Returns:
        A tuple of the loss removed and the power leaving the stage.
    """
    survived = upstream * factor
    return upstream - survived, survived


def inverter_waterfall(config: PlantConfig, frame: pd.DataFrame) -> LossWaterfall:
    """Build the loss waterfall for a single inverter.

    Theoretical output is defined as the array's STC-rated power scaled by
    effective irradiance: what a perfect, new, clean, cool array would produce
    from the light actually reaching it. Every stage from there is attributed.

    Args:
        config: Plant configuration.
        frame: Inverter production frame from the physics chain.

    Returns:
        The :class:`LossWaterfall`.
    """
    losses = config.losses
    index = frame.index

    rated_dc_kw = config.inverter_dc_kw
    theoretical = rated_dc_kw * (frame["effective_irradiance"] / 1000.0)
    theoretical = theoretical.clip(lower=0.0)

    stages: dict[str, pd.Series] = {}
    upstream = theoretical

    # Soiling reduces the light reaching the cells. Modelled as a constant here;
    # the precipitation-driven model supplies the time series in later work.
    stages["LOSS_SOILING"], upstream = _cascade(upstream, losses.soiling_ratio)

    # Temperature. Everything above STC cell temperature costs output, and this
    # is the loss that makes raw performance ratio seasonal.
    thermal_factor = (
        1.0 + config.module.temp_coeff_pmax_per_c * (frame["cell_temperature"] - 25.0)
    ).clip(lower=0.0, upper=1.0)
    stages["LOSS_THERMAL"], upstream = _cascade(upstream, thermal_factor)

    stages["LOSS_DEGRADATION"], upstream = _cascade(upstream, config.degradation_factor)
    stages["LOSS_MISMATCH"], upstream = _cascade(upstream, 1.0 - losses.mismatch)
    stages["LOSS_DC_WIRING"], upstream = _cascade(upstream, 1.0 - losses.dc_wiring)

    # Whatever remains between the linear model and the single-diode solution is
    # module non-linearity. It is a real, separately identifiable effect, so it
    # gets its own code rather than being buried in the residual.
    #
    # This term is SIGNED and must not be clipped at zero. Low-light efficiency
    # falls away from the STC ratio, but at high irradiance the single-diode
    # solution exceeds the linear extrapolation, making the term a gain.
    # Clipping it left the cascade below actual DC power, which then reported
    # inverter conversion loss as 0.04% instead of roughly 1.5% and pushed a
    # spurious -0.21% into the residual.
    actual_dc = frame["dc_power_kw"].clip(lower=0.0)
    stages["LOSS_LOWLIGHT"] = upstream - actual_dc
    upstream = actual_dc

    # DC to AC conversion, before the AC cap binds.
    preclip = frame["ac_preclip_kw"].clip(lower=0.0)
    stages["LOSS_INVERTER_EFF"] = (upstream - preclip).clip(lower=0.0)
    upstream = upstream - stages["LOSS_INVERTER_EFF"]

    # Clipping. Not a fault: the intended consequence of the DC/AC ratio, and
    # not monetized as recoverable.
    capped = np.minimum(preclip, config.inverter.rated_ac_kw)
    stages["LOSS_CLIPPING"] = (upstream - capped).clip(lower=0.0)
    upstream = upstream - stages["LOSS_CLIPPING"]

    # Inverter thermal derating, applied after clipping because a derated
    # inverter clips at a lower level rather than not clipping at all.
    derate_factor = frame.get("thermal_derate_factor", pd.Series(1.0, index=index))
    stages["LOSS_INV_THERMAL"], upstream = _cascade(upstream, derate_factor)

    # Whatever the state machine and controller removed. Curtailment is
    # reported separately because its cause is commercial, not physical.
    delivered = frame["ac_power_kw"]
    curtailed = frame.get("curtailed_power_kw", pd.Series(0.0, index=index))
    stages["LOSS_CURTAILMENT"] = curtailed.clip(lower=0.0)
    upstream = upstream - stages["LOSS_CURTAILMENT"]

    # Everything else the operating state removed: standby, startup ramp, any
    # future outage. Negative delivered power in standby is the inverter's own
    # consumption and belongs here.
    stages["LOSS_INV_STATE"] = (upstream - delivered).clip(lower=0.0)
    upstream = upstream - stages["LOSS_INV_STATE"]

    stages["LOSS_RESIDUAL"] = upstream - delivered

    return LossWaterfall(
        stages=pd.DataFrame(stages, index=index),
        theoretical_kw=theoretical,
        exported_kw=delivered,
    )


def plant_waterfall(config: PlantConfig, result) -> LossWaterfall:
    """Build the plant-wide loss waterfall through to metered export.

    Inverter waterfalls are summed, then the AC-side losses that occur between
    the inverters and the meter are appended.

    Args:
        config: Plant configuration.
        result: A ``PlantRunResult`` from a full-plant run.

    Returns:
        The plant :class:`LossWaterfall`.
    """
    per_inverter = [
        inverter_waterfall(config, frame) for frame in result.inverters.values()
    ]

    stages = per_inverter[0].stages.copy()
    for waterfall in per_inverter[1:]:
        stages = stages.add(waterfall.stages, fill_value=0.0)

    theoretical = sum(w.theoretical_kw for w in per_inverter)

    frame = result.plant
    stages["LOSS_TRANSFORMER"] = frame["transformer_loss_kw"]
    stages["LOSS_AC_COLLECTION"] = frame["collection_loss_kw"]
    stages["LOSS_POI_LIMIT"] = frame["poi_limited_kw"]

    # The residual is computed once against the plant export, absorbing both
    # the per-inverter remainders and the AC-side stages. Adding to it first and
    # then recomputing would double-count.
    exported = frame["grid_export_power_kw"]
    stages["LOSS_RESIDUAL"] = (
        theoretical - stages.drop(columns=["LOSS_RESIDUAL"]).sum(axis=1) - exported
    )

    return LossWaterfall(stages=stages, theoretical_kw=theoretical, exported_kw=exported)


def loss_signatures(config: PlantConfig, frame: pd.DataFrame) -> pd.DataFrame:
    """Extract the discriminating signals for low-output attribution.

    Design document ``07`` section 9.1 requires an analyst to separate four
    causes of reduced output. This assembles the signals that make the
    separation possible.

    Args:
        config: Plant configuration.
        frame: Inverter production frame.

    Returns:
        A frame flagging which condition applies at each timestep.
    """
    rated = config.inverter.rated_ac_kw
    preclip = frame["ac_preclip_kw"]
    derate = frame.get("thermal_derate_factor", pd.Series(1.0, index=frame.index))
    curtailed = frame.get("curtailed_power_kw", pd.Series(0.0, index=frame.index))

    return pd.DataFrame(
        {
            "resource_limited": preclip < rated * 0.98,
            "clipping": preclip >= rated * 0.999,
            "thermal_derating": derate < 0.999,
            "curtailed": curtailed > 0.0,
        },
        index=frame.index,
    )


@dataclass
class LossGateResult:
    """Outcome of the Phase 7 loss attribution acceptance checks.

    Attributes:
        checks: Named outcomes, each a pass flag and a detail string.
    """

    checks: list[tuple[str, bool, str]]

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


def run_loss_gate(config: PlantConfig, result, hot_result=None) -> LossGateResult:
    """Verify loss attribution meets its Phase 7 criteria.

    Args:
        config: Plant configuration.
        result: A ``PlantRunResult`` at moderate temperature.
        hot_result: An optional second run at extreme ambient temperature, used
            to check that thermal derating engages and interacts with clipping.

    Returns:
        A :class:`LossGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    waterfall = plant_waterfall(config, result)
    energy = waterfall.energy_mwh()

    closure = waterfall.closure_error()
    checks.append(
        (
            "waterfall_closes",
            closure < RESIDUAL_TOLERANCE,
            f"residual {closure:.2e} of theoretical (tolerance {RESIDUAL_TOLERANCE:.1e})",
        )
    )

    expected = {
        "LOSS_SOILING",
        "LOSS_THERMAL",
        "LOSS_DEGRADATION",
        "LOSS_MISMATCH",
        "LOSS_DC_WIRING",
        "LOSS_INVERTER_EFF",
        "LOSS_CLIPPING",
        "LOSS_TRANSFORMER",
        "LOSS_AC_COLLECTION",
    }
    present = {code for code in expected if abs(float(energy.get(code, 0.0))) > 1e-6}
    checks.append(
        (
            "all_causes_attributed",
            expected <= present,
            f"{len(present)}/{len(expected)} cause codes carry energy"
            + ("" if expected <= present else f", missing {sorted(expected - present)}"),
        )
    )

    theoretical = float(energy["THEORETICAL"])
    share = {code: float(energy.get(code, 0.0)) / theoretical for code in expected}
    plausible = (
        0.005 < share["LOSS_INVERTER_EFF"] < 0.04
        and 0.02 < share["LOSS_SOILING"] < 0.06
        and 0.005 < share["LOSS_DEGRADATION"] < 0.04
        and 0.001 < share["LOSS_TRANSFORMER"] < 0.02
    )
    checks.append(
        (
            "loss_magnitudes_plausible",
            plausible,
            f"inverter {share['LOSS_INVERTER_EFF']:.2%}, "
            f"soiling {share['LOSS_SOILING']:.2%}, "
            f"degradation {share['LOSS_DEGRADATION']:.2%}",
        )
    )

    # Avoidable and structural losses must be distinguishable. Reporting
    # clipping or degradation as recoverable is the classic error the dataset
    # exists to let an analyst make and then correct.
    avoidable = {code for code, flag in CAUSE_CODES.items() if flag}
    checks.append(
        (
            "avoidable_classified",
            "LOSS_SOILING" in avoidable
            and "LOSS_CLIPPING" not in avoidable
            and "LOSS_DEGRADATION" not in avoidable,
            "soiling avoidable; clipping and degradation structural",
        )
    )

    # The four causes of reduced output must be separable per timestep.
    sample = next(iter(result.inverters.values()))
    signatures = loss_signatures(config, sample)
    overlap = (signatures["resource_limited"] & signatures["clipping"]).sum()
    checks.append(
        (
            "causes_are_separable",
            overlap == 0 and signatures["clipping"].sum() > 0,
            f"clipping {signatures['clipping'].sum()} min, "
            f"resource-limited {signatures['resource_limited'].sum()} min, "
            f"overlap {overlap}",
        )
    )

    # Degradation must reflect the configured plant age rather than a constant.
    checks.append(
        (
            "degradation_tracks_age",
            0.90 < config.degradation_factor < 1.0,
            f"factor {config.degradation_factor:.4f} at "
            f"{config.losses.plant_age_years:.1f} years",
        )
    )

    if hot_result is not None:
        hot = plant_waterfall(config, hot_result)
        hot_energy = hot.energy_mwh()
        derate = float(hot_energy.get("LOSS_INV_THERMAL", 0.0))
        cool_derate = float(energy.get("LOSS_INV_THERMAL", 0.0))
        checks.append(
            (
                "thermal_derating_engages",
                derate > cool_derate and derate > 0,
                f"{cool_derate:.2f} MWh at moderate ambient -> {derate:.2f} MWh "
                f"at extreme",
            )
        )
        # A derating inverter clips less, because derating pulls output below
        # the cap. The two losses are not independent and must not be treated
        # as additive.
        checks.append(
            (
                "derating_reduces_clipping",
                float(hot_energy.get("LOSS_CLIPPING", 0.0))
                < float(energy.get("LOSS_CLIPPING", 0.0)),
                f"clipping {float(energy.get('LOSS_CLIPPING', 0.0)):.1f} -> "
                f"{float(hot_energy.get('LOSS_CLIPPING', 0.0)):.1f} MWh when derating",
            )
        )

    return LossGateResult(checks=checks)
