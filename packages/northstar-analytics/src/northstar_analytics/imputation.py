"""Impute missing telemetry, and score it against truth.

The analyst tree carries gaps: communications outages null every field for an
asset over a window, and the simulator writes those rows as NULL rather than
zero-filling them, because zero irradiance means night and a zero-filled
daytime gap fabricates an outage that did not happen.

The truth tree has no gaps. That makes imputation a **supervised problem with
free labels** - every missing value has a known correct answer, in physical
units, requiring no annotation.

**The baselines matter more than the model.** Forward-fill and interpolation
are what production systems actually do, and a learned method that does not
beat them is not worth deploying. `sql/timeseries/02_gaps.sql` shows
forward-fill dragging -0.7 kW of overnight standby across three hours of
daylight; that is the bar.

Reference: design documents ``10_data_quality_model`` and
``20_kpi_definitions`` section 11.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

#: Columns worth imputing. Power and irradiance drive every downstream metric;
#: state and code columns are categorical and need a different treatment.
IMPUTABLE = ("poa_global", "ac_power_kw", "dc_power_kw", "cell_temperature")


@dataclass
class ImputationScore:
    """Accuracy of one imputation method against withheld truth.

    Attributes:
        method: Name of the method.
        column: Column imputed.
        n: Values scored.
        mae: Mean absolute error, in the column's own units.
        rmse: Root mean squared error.
        bias: Mean signed error; positive means over-prediction.
    """

    method: str
    column: str
    n: int
    mae: float
    rmse: float
    bias: float

    def row(self) -> dict:
        """Return the score as a plain mapping.

        Returns:
            One row suitable for a DataFrame.
        """
        return {
            "method": self.method,
            "column": self.column,
            "n": self.n,
            "mae": round(self.mae, 3),
            "rmse": round(self.rmse, 3),
            "bias": round(self.bias, 3),
        }


@dataclass
class ImputationResult:
    """Scores for every method and column tried.

    Attributes:
        scores: Individual scores.
        gaps: Gap windows found in the analyst tree.
    """

    scores: list[ImputationScore] = field(default_factory=list)
    gaps: pd.DataFrame = field(default_factory=pd.DataFrame)

    def frame(self) -> pd.DataFrame:
        """Return every score as a table.

        Returns:
            One row per method and column, sorted by column then error.
        """
        if not self.scores:
            return pd.DataFrame()
        return (
            pd.DataFrame([s.row() for s in self.scores])
            .sort_values(["column", "mae"])
            .reset_index(drop=True)
        )

    def best(self, column: str) -> ImputationScore | None:
        """Return the lowest-error method for a column.

        Args:
            column: Column name.

        Returns:
            The best score, or ``None`` if the column was not scored.
        """
        candidates = [s for s in self.scores if s.column == column]
        return min(candidates, key=lambda s: s.mae) if candidates else None


def find_gaps(analyst, column: str = "ac_power_kw") -> pd.DataFrame:
    """Locate runs of missing values per asset.

    Rows are present with NULL fields rather than absent, which is why this
    counts NULLs rather than looking for missing timestamps. `locf` and
    `time_bucket_gapfill` do the opposite and therefore cannot see these.

    Args:
        analyst: DuckDB connection over the analyst tree.
        column: Column whose nullity defines a gap.

    Returns:
        One row per gap with its asset, bounds and length.
    """
    frame = analyst.execute(
        f"""
        SELECT time, asset_id, {column} IS NULL AS missing
        FROM inverter_telemetry
        ORDER BY asset_id, time
        """
    ).df()
    if frame.empty:
        return pd.DataFrame(columns=["asset_id", "start", "end", "minutes"])

    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    # Number consecutive runs: a running count of non-missing rows labels each
    # run of missing rows with the index of the last good value before it.
    frame["run"] = (~frame["missing"]).groupby(frame["asset_id"]).cumsum()

    # Keep asset_id: reset_index(drop=True) discards the groupby keys, which
    # leaves a table of windows with no indication of which asset they belong
    # to - and every downstream use needs that.
    gaps = (
        frame[frame["missing"]]
        .groupby(["asset_id", "run"])
        .agg(start=("time", "min"), end=("time", "max"), minutes=("time", "size"))
        .reset_index()
        .drop(columns=["run"])
    )
    return gaps.sort_values("minutes", ascending=False).reset_index(drop=True)


def _peer_frame(analyst, column: str) -> pd.DataFrame:
    """Build a wide frame of one column across all assets.

    Args:
        analyst: DuckDB connection.
        column: Column to pivot.

    Returns:
        Timestamps as the index, assets as columns.
    """
    long = analyst.execute(
        f"SELECT time, asset_id, {column} AS value FROM inverter_telemetry"
    ).df()
    long["time"] = pd.to_datetime(long["time"], utc=True)
    return long.pivot_table(
        index="time", columns="asset_id", values="value", aggfunc="first"
    ).sort_index()


def impute(
    analyst, truth, column: str = "ac_power_kw"
) -> tuple[dict[str, pd.Series], pd.Series]:
    """Impute a column by several methods and return them with the answer.

    Four methods, in increasing order of how much they use:

    * **forward fill** - carry the last observed value. What most pipelines do.
    * **linear** - interpolate between the values either side of the gap.
    * **peer median** - the median of the same interval across other assets,
      scaled by the gapped asset's own historical ratio to that median.
    * **peer regression** - a least-squares fit of the asset against its peers,
      trained only on intervals where it was reporting.

    The last two are the ones that should win, because a PV plant's assets are
    highly correlated at any instant: the information needed to reconstruct one
    inverter is sitting in the other thirty-nine.

    Args:
        analyst: Connection over the analyst tree.
        truth: Connection over the truth tree.
        column: Column to impute.

    Returns:
        A mapping of method name to imputed values, and the true values, both
        restricted to positions that were missing.
    """
    wide = _peer_frame(analyst, column)
    missing = wide.isna()
    if not missing.to_numpy().any():
        return {}, pd.Series(dtype=float)

    truth_long = truth.execute(
        f"SELECT time, asset_id, {column} AS value FROM inverter_truth"
    ).df()
    truth_long["time"] = pd.to_datetime(truth_long["time"], utc=True)
    truth_wide = truth_long.pivot_table(
        index="time", columns="asset_id", values="value", aggfunc="first"
    ).reindex(index=wide.index, columns=wide.columns)

    methods: dict[str, pd.DataFrame] = {
        "forward_fill": wide.ffill(),
        "linear": wide.interpolate(method="time", limit_direction="both"),
    }

    # Peer median, rescaled. The median over other assets is robust to a
    # simultaneous outage on a few of them; the ratio corrects for an asset
    # that is systematically above or below its peers.
    peer_median = wide.median(axis=1, skipna=True)
    scaled = pd.DataFrame(index=wide.index, columns=wide.columns, dtype=float)
    for asset in wide.columns:
        observed = wide[asset].notna() & (peer_median > 1.0)
        ratio = (
            (wide.loc[observed, asset] / peer_median[observed]).median()
            if observed.any()
            else 1.0
        )
        scaled[asset] = peer_median * (ratio if np.isfinite(ratio) else 1.0)
    methods["peer_median"] = wide.fillna(scaled)

    # Peer regression, fitted per asset on its own observed intervals.
    regressed = wide.copy()
    for asset in wide.columns:
        gap_rows = wide[asset].isna()
        if not gap_rows.any():
            continue
        peers = wide.drop(columns=[asset])
        train = wide[asset].notna() & peers.notna().all(axis=1)
        if train.sum() < 100 or not peers.loc[gap_rows].notna().all(axis=1).any():
            continue
        usable = gap_rows & peers.notna().all(axis=1)
        coefficients, *_ = np.linalg.lstsq(
            peers[train].to_numpy(), wide.loc[train, asset].to_numpy(), rcond=None
        )
        regressed.loc[usable, asset] = peers[usable].to_numpy() @ coefficients
    methods["peer_regression"] = wide.fillna(regressed)

    actual = truth_wide.where(missing).stack(future_stack=True).dropna()
    imputed = {
        name: frame.where(missing).stack(future_stack=True).reindex(actual.index)
        for name, frame in methods.items()
    }
    return imputed, actual


def score_imputation(
    dataset: Path,
    run_id: str,
    columns: tuple[str, ...] = IMPUTABLE,
) -> ImputationResult:
    """Impute every column by every method and score against truth.

    Args:
        dataset: Export root.
        run_id: Dataset identifier.
        columns: Columns to attempt.

    Returns:
        An :class:`ImputationResult`.
    """
    from northstar_sim.storage import duckdb_connection

    analyst = duckdb_connection(dataset, run_id, "analyst")
    truth = duckdb_connection(dataset, run_id, "truth")

    result = ImputationResult(gaps=find_gaps(analyst))

    for column in columns:
        try:
            imputed, actual = impute(analyst, truth, column)
        except Exception:  # noqa: BLE001 - a missing column is not fatal
            continue
        if actual.empty:
            continue

        for method, values in imputed.items():
            paired = pd.DataFrame({"a": actual, "p": values}).dropna()
            if paired.empty:
                continue
            error = paired["p"] - paired["a"]
            result.scores.append(
                ImputationScore(
                    method=method,
                    column=column,
                    n=len(paired),
                    mae=float(error.abs().mean()),
                    rmse=float(np.sqrt((error**2).mean())),
                    bias=float(error.mean()),
                )
            )

    analyst.close()
    truth.close()
    return result
