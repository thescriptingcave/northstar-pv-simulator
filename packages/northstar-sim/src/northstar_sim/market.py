"""Market prices, settlement and economic curtailment.

The financial layer is what turns a physics dataset into an operating-asset
dataset. Design document ``18`` states the governing principle:

    Every kWh the plant fails to produce must carry a cause code and a dollar
    value at the price prevailing at the moment it was lost.

**The structural fact this module exists to reproduce:** in a high-solar market,
solar generates most when solar is worth least. Capture rate - generation-
weighted price divided by time-weighted price - is therefore below 100%, and it
is invisible in every physical metric. It emerges only from joining real prices
to real production shape.

Two settlement mechanics matter and are modelled separately:

* **Physical energy** settles at the real-time price at the plant's node.
* **A fixed-volume hedge** settles as a contract for differences, independent
  of what the plant actually generated. That independence creates volume risk:
  an outage during a scarcity hour costs the hedge buy-back as well as the
  lost energy.

Reference: design document ``18_financial_commercial_model`` sections 2-5.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Settlement interval. ERCOT real-time settlement point prices are produced
#: from SCED every 15 minutes, and design doc 18 section 2.2 adopts that grain.
SETTLEMENT_MINUTES = 15


@dataclass(frozen=True)
class CommercialTerms:
    """Offtake structure for the plant.

    Attributes:
        hedge_volume_mw: Fixed hedge volume, flat across all hours.
        hedge_strike_usd_mwh: Hedge strike price.
        ptc_usd_mwh: Production tax credit on generated energy.
        curtailment_hysteresis_usd: Band around the curtailment threshold,
            preventing the controller chattering at the boundary.
        curtailment_dwell_minutes: Minimum time curtailment is held once
            entered.
        fixed_om_usd_kw_year: Fixed operations and maintenance cost.
        truck_roll_usd: Cost of dispatching a crew.
        availability_guarantee: Contractual availability floor.
        pr_guarantee: Contractual performance ratio floor.
    """

    hedge_volume_mw: float = 70.0
    hedge_strike_usd_mwh: float = 34.0
    ptc_usd_mwh: float = 27.50
    curtailment_hysteresis_usd: float = 5.0
    curtailment_dwell_minutes: int = 15
    fixed_om_usd_kw_year: float = 21.50
    truck_roll_usd: float = 850.0
    availability_guarantee: float = 0.98
    pr_guarantee: float = 0.805


def synthetic_prices(
    index: pd.DatetimeIndex,
    solar_output: pd.Series,
    *,
    seed: int,
    base_usd_mwh: float = 38.0,
    suppression_usd_mwh: float = 30.0,
) -> pd.Series:
    """Generate a development price series with realistic structure.

    This is a **development stand-in**, not a dataset input. Production prices
    come from the cached ERCOT series in ``northstar_fetch``. It exists so the
    financial layer can be built and tested without live credentials, in the
    same way ``clearsky_resource`` supports the physics gate.

    The structure it must reproduce, because every financial conclusion depends
    on it:

    * prices fall when regional solar output is high, because supply is
      abundant precisely when this plant produces
    * they go **negative** in the middle of high-solar days, which is what makes
      economic curtailment a real decision rather than a hypothetical
    * evening scarcity pricing occurs after solar output has collapsed, which is
      what makes an outage's cost depend on *when* it happens

    Args:
        index: Time index to produce prices for.
        solar_output: Regional solar output as a fraction of its own peak.
            This must be an **output** shape, not a clearness measure. Using
            the clear-sky index instead put penetration near 1.0 from sunrise
            to sunset, suppressing prices across the whole day and driving
            27% of intervals negative with a generation-weighted price of
            -$3.77/MWh. Real penetration follows a diurnal bell.
        seed: Seed for the ``market_noise`` substream.
        base_usd_mwh: Off-peak baseline price.
        suppression_usd_mwh: Depth of the midday solar price depression.

    Returns:
        Signed prices in dollars per megawatt-hour.
    """
    rng = np.random.default_rng(seed)
    hour = index.hour + index.minute / 60.0

    # Solar suppression. The regional fleet tracks this plant closely enough
    # that its own normalised output is a serviceable proxy for penetration.
    peak = float(solar_output.max())
    penetration = (
        (solar_output.reindex(index).fillna(0.0) / peak)
        if peak > 0
        else pd.Series(0.0, index=index)
    )
    # Calibration matters. A suppression coefficient of 46 against a base of 32
    # drove 54% of intervals negative and made energy revenue negative overall,
    # which is not a market anyone operates in. ERCOT West runs roughly 10-15%
    # negative annually, concentrated in high-wind spring afternoons.
    suppression = suppression_usd_mwh * penetration.clip(lower=0.0)

    # Evening scarcity: load peaks after solar has collapsed. This is what makes
    # the cost of an outage depend on the hour rather than only on the energy.
    evening = np.exp(-0.5 * ((hour - 19.5) / 1.6) ** 2)
    scarcity = 55.0 * evening * (1.0 - penetration.to_numpy().clip(0.0, 1.0))

    noise = rng.normal(0.0, 8.0, size=len(index))
    spikes = np.where(
        rng.random(len(index)) < 0.0015, rng.gamma(3.0, 90.0, len(index)), 0.0
    )

    # Multi-day high-wind episodes deepen the depression enough to push price
    # below the negative-PTC threshold. Without them, economic curtailment
    # never fires and the most valuable scenario in the package is unreachable.
    day_number = (index - index[0]).days
    windy = pd.Series(
        rng.random(int(day_number.max()) + 1)[day_number] < 0.25, index=index
    )
    # Deep enough that the *settlement-grain* price falls below the negative
    # PTC threshold, not merely the noisy minute-level price. Curtailment is
    # decided against 15-minute settlement prices, so structural oversupply -
    # not noise - has to drive it. At 34 the minute series dipped below the
    # threshold while its 15-minute mean never did, and curtailment never fired.
    episode = np.where(windy.to_numpy(), 48.0 * penetration.to_numpy().clip(0.0), 0.0)

    price = base_usd_mwh - suppression.to_numpy() - episode + scarcity + noise + spikes

    # ERCOT administrative floor and cap. Verify current values before using
    # this for anything but development; the PUCT revises them.
    return pd.Series(np.clip(price, -251.0, 5000.0), index=index)


def to_settlement_grain(series: pd.Series, *, how: str = "mean") -> pd.Series:
    """Resample a 1-minute series to the settlement interval.

    Args:
        series: Minute-resolution series.
        how: ``"mean"`` for power and prices, ``"sum"`` for energy.

    Returns:
        The resampled series.
    """
    resampled = series.resample(f"{SETTLEMENT_MINUTES}min")
    return resampled.sum() if how == "sum" else resampled.mean()


def economic_curtailment_mask(
    price_usd_mwh: pd.Series, terms: CommercialTerms
) -> pd.Series:
    """Determine when curtailing is the economically correct decision.

    The rule is ``price + PTC < 0``: below that, generating destroys value even
    after the tax credit. The hedge does not enter the decision because its
    settlement is volumetrically fixed and therefore not marginal.

    **The decision is made at settlement grain, not at 1-minute resolution.**
    Curtailment is a commercial decision and settlement is 15-minute, so
    evaluating it against minute-level price noise is wrong twice over: no
    operator does it, and it over-curtails badly. Deciding per minute produced
    1,538 curtailed minutes of which only 294 had a genuinely negative
    marginal price - each brief noise excursion below the threshold triggered
    its own 15-minute dwell, and the plant sat idle through $25,844 of
    positive-margin generation.

    Hysteresis and a dwell timer then prevent oscillation at the threshold,
    which would produce an unrealistic sawtooth in the telemetry.

    Args:
        price_usd_mwh: Real-time price at the plant node.
        terms: Commercial terms.

    Returns:
        ``True`` where the plant should curtail, at the input resolution.
    """
    minute_index = price_usd_mwh.index
    settlement_price = to_settlement_grain(price_usd_mwh)

    mask = _dwelled_mask(settlement_price, terms)
    return mask.reindex(minute_index, method="ffill").fillna(False).astype(bool)


def _dwelled_mask(price_usd_mwh: pd.Series, terms: CommercialTerms) -> pd.Series:
    """Apply the entry threshold, hysteresis and dwell timer.

    Args:
        price_usd_mwh: Price at settlement grain.
        terms: Commercial terms.

    Returns:
        ``True`` where the plant should curtail.
    """
    enter = price_usd_mwh < -terms.ptc_usd_mwh
    exit_threshold = -terms.ptc_usd_mwh + terms.curtailment_hysteresis_usd

    # The dwell is expressed in minutes but applied at settlement grain.
    dwell_intervals = max(1, terms.curtailment_dwell_minutes // SETTLEMENT_MINUTES)

    active = np.zeros(len(price_usd_mwh), dtype=bool)
    dwell = 0
    state = False

    for position, (entering, price) in enumerate(
        zip(enter.to_numpy(), price_usd_mwh.to_numpy(), strict=True)
    ):
        if state:
            dwell += 1
            if dwell >= dwell_intervals and price > exit_threshold:
                state = False
        elif entering:
            state, dwell = True, 0
        active[position] = state

    return pd.Series(active, index=price_usd_mwh.index)


def settle(
    export_kw: pd.Series,
    node_price: pd.Series,
    hub_price: pd.Series,
    terms: CommercialTerms,
) -> pd.DataFrame:
    """Compute settlement at the 15-minute grain.

    Args:
        export_kw: Metered export power at 1-minute resolution.
        node_price: Real-time price at the plant node.
        hub_price: Real-time price at the hedge index hub.
        terms: Commercial terms.

    Returns:
        A settlement frame indexed at the settlement interval.
    """
    energy_mwh = to_settlement_grain(export_kw, how="sum") / 1000.0 * (1.0 / 60.0)
    node = to_settlement_grain(node_price)
    hub = to_settlement_grain(hub_price)

    hedge_volume_mwh = terms.hedge_volume_mw * SETTLEMENT_MINUTES / 60.0

    energy_revenue = energy_mwh * node
    # A contract for differences. Settlement is independent of generation, which
    # is what creates volume risk: an outage during a scarcity hour costs the
    # hedge buy-back on top of the lost energy.
    hedge_settlement = hedge_volume_mwh * (terms.hedge_strike_usd_mwh - hub)
    ptc_value = energy_mwh.clip(lower=0.0) * terms.ptc_usd_mwh
    basis = energy_mwh * (node - hub)

    return pd.DataFrame(
        {
            "export_energy_mwh": energy_mwh,
            "node_price_usd_mwh": node,
            "hub_price_usd_mwh": hub,
            "energy_revenue_usd": energy_revenue,
            "hedge_settlement_usd": hedge_settlement,
            "ptc_value_usd": ptc_value,
            "basis_usd": basis,
            "gross_margin_usd": energy_revenue + hedge_settlement + ptc_value,
        }
    )


def monetize_losses(
    stages: pd.DataFrame,
    node_price: pd.Series,
    terms: CommercialTerms,
    avoidable: dict[str, bool],
) -> pd.DataFrame:
    """Convert cause-coded energy losses into lost revenue.

    Lost revenue uses the **marginal** rate ``price + PTC``, not a blended
    average, because the hedge is volumetrically fixed: a lost MWh is monetized
    at what it would have earned at the margin.

    Lost revenue can be **negative**. During negative-price intervals an outage
    saved money. This is real, counterintuitive, and the single best check that
    an analyst understands the settlement model rather than pattern-matching
    "outage equals bad".

    Args:
        stages: Per-timestep loss power by cause code.
        node_price: Real-time price at the plant node.
        terms: Commercial terms.
        avoidable: Whether each cause code is treated as recoverable.

    Returns:
        One row per cause code with lost energy, lost revenue and the
        avoidable flag.
    """
    marginal = (node_price + terms.ptc_usd_mwh).reindex(stages.index).ffill()
    interval_hours = _interval_hours(stages.index)

    rows = []
    for code in stages.columns:
        energy = stages[code] * interval_hours / 1000.0
        rows.append(
            {
                "cause_code": code,
                "lost_energy_mwh": float(energy.sum()),
                "lost_revenue_usd": float((energy * marginal).sum()),
                "avoidable": bool(avoidable.get(code, False)),
            }
        )

    frame = pd.DataFrame(rows)
    return frame.sort_values("lost_revenue_usd", ascending=False)


def operating_costs(
    plant_ac_kw: float,
    days: float,
    terms: CommercialTerms,
    *,
    truck_rolls: int = 0,
) -> dict[str, float]:
    """Compute operating cost over a period.

    Args:
        plant_ac_kw: Plant AC nameplate.
        days: Days covered.
        terms: Commercial terms.
        truck_rolls: Number of crew dispatches, one per non-transient fault.

    Returns:
        Cost components in dollars.
    """
    fixed = plant_ac_kw * terms.fixed_om_usd_kw_year * days / 365.25
    events = truck_rolls * terms.truck_roll_usd
    return {"fixed_om_usd": fixed, "event_usd": events, "total_usd": fixed + events}


def capture_rate(export_kw: pd.Series, node_price: pd.Series) -> float:
    """Compute the generation-weighted price relative to the time-weighted price.

    Below 100% is the central economic fact of merchant solar: the plant
    produces most when its output is worth least. It is invisible in every
    physical metric and emerges only from joining prices to production shape.

    Args:
        export_kw: Metered export power.
        node_price: Real-time price at the plant node.

    Returns:
        Capture rate as a fraction. Above 1.0 means the join is wrong or the
        production shape is not solar.
    """
    aligned = node_price.reindex(export_kw.index).ffill()
    generation = export_kw.clip(lower=0.0)
    total = float(generation.sum())
    if total <= 0:
        return 0.0

    generation_weighted = float((generation * aligned).sum()) / total
    time_weighted = float(aligned.mean())
    return generation_weighted / time_weighted if time_weighted else 0.0


def _interval_hours(index: pd.DatetimeIndex) -> float:
    """Determine the sampling interval in hours.

    Args:
        index: A regular time index.

    Returns:
        Interval width in hours.
    """
    if len(index) < 2:
        return 1.0 / 60.0
    return (index[1] - index[0]).total_seconds() / 3600.0


@dataclass
class FinancialGateResult:
    """Outcome of the Phase 9 financial layer acceptance checks.

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


def run_financial_gate(
    result,
    settlement: pd.DataFrame,
    monetized: pd.DataFrame,
    node_price: pd.Series,
    curtailment: pd.Series,
    terms: CommercialTerms,
) -> FinancialGateResult:
    """Verify the financial layer meets its Phase 9 criteria.

    Args:
        result: A ``PlantRunResult``.
        settlement: Output of :func:`settle`.
        monetized: Output of :func:`monetize_losses`.
        node_price: Real-time price at the plant node.
        curtailment: Economic curtailment mask.
        terms: Commercial terms.

    Returns:
        A :class:`FinancialGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    export = result.plant["grid_export_power_kw"]

    # Below 100% is the central economic fact of merchant solar: the plant
    # produces most when its output is worth least. Above 100% means the join
    # is wrong or the production shape is not solar.
    rate = capture_rate(export, node_price)
    checks.append(
        (
            "capture_rate_below_unity",
            0.2 < rate < 1.0,
            f"{rate:.1%} (generation-weighted over time-weighted price)",
        )
    )

    negative_share = float((node_price < 0).mean())
    deep = float((to_settlement_grain(node_price) < -terms.ptc_usd_mwh).mean())
    checks.append(
        (
            "negative_prices_present",
            0.02 < negative_share < 0.35 and deep > 0.001,
            f"{negative_share:.1%} negative, {deep:.2%} of settlement "
            f"intervals below the negative-PTC threshold",
        )
    )

    checks.append(
        (
            "economic_curtailment_fires",
            0.0 < float(curtailment.mean()) < 0.25,
            f"{float(curtailment.sum()) / 60:.1f} h "
            f"({float(curtailment.mean()):.2%} of the record)",
        )
    )

    # The counterintuitive result the whole layer exists to produce.
    curtail_row = monetized[monetized["cause_code"] == "LOSS_CURTAILMENT"]
    curtail_revenue = (
        float(curtail_row["lost_revenue_usd"].iloc[0]) if len(curtail_row) else 0.0
    )
    checks.append(
        (
            "curtailment_saved_money",
            curtail_revenue < 0,
            f"lost revenue ${curtail_revenue:,.0f} - negative means curtailing "
            f"was correct",
        )
    )

    # The hedge settles independent of generation. That independence is what
    # creates volume risk, and it is testable.
    checks.append(
        (
            "hedge_independent_of_output",
            settlement["hedge_settlement_usd"].abs().sum() > 0,
            f"${settlement['hedge_settlement_usd'].sum():,.0f} settled on fixed volume",
        )
    )

    energy_revenue = float(settlement["energy_revenue_usd"].sum())
    checks.append(
        (
            "energy_revenue_positive",
            energy_revenue > 0,
            f"${energy_revenue:,.0f} - negative would mean a market no one operates in",
        )
    )

    avoidable = monetized[monetized["avoidable"]]
    structural = monetized[~monetized["avoidable"]]
    checks.append(
        (
            "losses_split_by_recoverability",
            len(avoidable) > 0 and len(structural) > 0,
            f"{len(avoidable)} avoidable, {len(structural)} structural cause codes",
        )
    )

    # Cost ranking must be able to differ from energy ranking. If they always
    # agreed, the financial layer would add nothing.
    by_energy = list(
        monetized.sort_values("lost_energy_mwh", ascending=False)["cause_code"]
    )
    by_revenue = list(
        monetized.sort_values("lost_revenue_usd", ascending=False)["cause_code"]
    )
    checks.append(
        (
            "cost_ranking_is_informative",
            by_energy != by_revenue,
            "energy and revenue rankings differ"
            if by_energy != by_revenue
            else "rankings identical - financial layer adds nothing",
        )
    )

    return FinancialGateResult(checks=checks)
