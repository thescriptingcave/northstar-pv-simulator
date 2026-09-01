"""Score blind analysis against injected truth.

`16 §16` names three criteria as the point of the exercise. One - degradation
recovery - is demonstrated in `35` at 0.055 pp error. The other two have never
been scored:

* **an analyst working blind can identify injected faults from telemetry**
* **cost ranking of faults differs from energy ranking, and the difference is
  explainable**

Both need the same thing: a detector that sees only the analyst tree, and a
scorer that opens the truth tree afterwards. That separation is what makes the
result a measurement rather than an assertion - the detector cannot consult
the answer, because it is handed a connection that does not contain it.

Reference: design documents ``01`` section 8 and ``16`` section 16.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class DetectionScore:
    """Detection performance against injected truth.

    Attributes:
        injected: Fault instances present in the truth tree.
        detected: Windows the detector flagged.
        matched: Injected instances a detection overlapped.
        false_positives: Detections matching no injected fault.
    """

    injected: int
    detected: int
    matched: int
    false_positives: int

    @property
    def recall(self) -> float:
        """Share of injected faults that were found.

        Returns:
            Recall in the range zero to one.
        """
        return self.matched / self.injected if self.injected else 0.0

    @property
    def precision(self) -> float:
        """Share of detections that correspond to a real fault.

        Returns:
            Precision in the range zero to one.
        """
        return (
            (self.detected - self.false_positives) / self.detected
            if self.detected
            else 0.0
        )


@dataclass
class RankingComparison:
    """Energy and cost rankings of the same fault population.

    Attributes:
        rows: One row per scenario with both rankings.
        rank_changes: Scenarios whose position differs between rankings.
    """

    rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    rank_changes: int = 0

    @property
    def rankings_differ(self) -> bool:
        """Whether cost ordering differs from energy ordering.

        Returns:
            ``True`` when at least one scenario changes position.
        """
        return self.rank_changes > 0


def detect_underperformance(
    analyst,
    *,
    threshold: float = 0.75,
    min_minutes: int = 15,
) -> pd.DataFrame:
    """Find sustained underperformance from the analyst tree alone.

    Normalises each inverter against its **block** mean at every interval, which
    removes the weather. Normalising against the plant mean instead
    manufactures underperformers out of the spatial cloud field: an inverter
    under a cloud is not a faulty inverter.

    Args:
        analyst: DuckDB connection over the analyst tree.
        threshold: Peer ratio below which an interval counts as degraded.
        min_minutes: Consecutive degraded intervals required to report.

    Returns:
        One row per detected window with its asset, start, end and depth.
    """
    frame = analyst.execute(
        """
        WITH daylight AS (
            SELECT time, asset_id, substr(asset_id, 1, 14) AS block_id,
                   ac_power_kw
            FROM inverter_telemetry
            WHERE poa_global > 200 AND ac_power_kw IS NOT NULL
        ),
        peers AS (
            SELECT time, asset_id, ac_power_kw,
                   avg(ac_power_kw) OVER (PARTITION BY time, block_id) AS peer
            FROM daylight
        )
        SELECT time, asset_id, ac_power_kw / nullif(peer, 0) AS ratio
        FROM peers
        WHERE peer > 50
        ORDER BY asset_id, time
        """
    ).df()

    if frame.empty:
        return pd.DataFrame(columns=["asset_id", "start", "end", "minutes", "mean_ratio"])

    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame["degraded"] = frame["ratio"] < threshold

    # Number consecutive runs of degraded intervals per asset.
    frame["run"] = frame.groupby("asset_id")["degraded"].transform(
        lambda s: (s != s.shift()).cumsum()
    )

    runs = (
        frame[frame["degraded"]]
        .groupby(["asset_id", "run"])
        .agg(
            start=("time", "min"),
            end=("time", "max"),
            minutes=("time", "size"),
            mean_ratio=("ratio", "mean"),
        )
        .reset_index(drop=False)
    )
    return runs[runs["minutes"] >= min_minutes].drop(columns=["run"])


def score_detection(
    detections: pd.DataFrame, truth, *, tolerance_minutes: int = 60
) -> DetectionScore:
    """Score detections against the injected scenario schedule.

    A detection counts as matched when it overlaps an injected instance on the
    same asset within a tolerance. Exact boundary agreement is not required and
    would not be meaningful: a fault becomes visible in telemetry some minutes
    after it starts.

    Args:
        detections: Output of :func:`detect_underperformance`.
        truth: DuckDB connection over the truth tree.
        tolerance_minutes: Slack allowed at each end of an injected window.

    Returns:
        A :class:`DetectionScore`.
    """
    injected = truth.execute(
        'SELECT asset_id, time AS start_time, "end" AS end_time FROM scenario_instances'
    ).df()

    if injected.empty:
        return DetectionScore(0, len(detections), 0, len(detections))

    for column in ("start_time", "end_time"):
        injected[column] = pd.to_datetime(injected[column], utc=True)

    slack = pd.Timedelta(minutes=tolerance_minutes)
    matched_injected: set[int] = set()
    matched_detections: set[int] = set()

    for d_index, detection in detections.iterrows():
        candidates = injected[injected["asset_id"] == detection["asset_id"]]
        for i_index, instance in candidates.iterrows():
            overlaps = (
                detection["start"] <= instance["end_time"] + slack
                and detection["end"] >= instance["start_time"] - slack
            )
            if overlaps:
                matched_injected.add(i_index)
                matched_detections.add(d_index)

    return DetectionScore(
        injected=len(injected),
        detected=len(detections),
        matched=len(matched_injected),
        false_positives=len(detections) - len(matched_detections),
    )


def compare_rankings(truth, prices: pd.Series | None = None) -> RankingComparison:
    """Rank scenarios by energy lost and by revenue lost, and compare.

    `01 §8` criterion 3 asserts the two orderings differ. They do so because a
    megawatt-hour lost at midday in July is not worth a megawatt-hour lost at
    dawn in January - and a maintenance budget allocated by energy therefore
    misallocates.

    Args:
        truth: DuckDB connection over the truth tree.
        prices: Settlement prices indexed by time. Without them only the energy
            ranking is produced, because a cost ranking from synthetic prices
            would not support the claim.

    Returns:
        A :class:`RankingComparison`.
    """
    instances = truth.execute(
        'SELECT scenario_id, asset_id, time AS start_time, "end" AS end_time '
        "FROM scenario_instances"
    ).df()

    if instances.empty or prices is None:
        return RankingComparison(rows=instances, rank_changes=0)

    for column in ("start_time", "end_time"):
        instances[column] = pd.to_datetime(instances[column], utc=True)

    # There is no stored fault-loss column. Loss is what the plant could have
    # produced minus what it did: available_power_kw is the pre-command
    # capability, ac_power_kw the outcome. Curtailment is excluded, because
    # curtailed energy is a commercial decision rather than a fault.
    lost = truth.execute(
        """
        SELECT time, asset_id,
               available_power_kw - ac_power_kw AS fault_loss_kw
        FROM inverter_truth
        WHERE available_power_kw - ac_power_kw > 1.0
          AND coalesce(curtailed_power_kw, 0) < 1.0
        """
    ).df()

    if lost.empty:
        return RankingComparison(rows=instances, rank_changes=0)

    lost["time"] = pd.to_datetime(lost["time"], utc=True)
    lost["price"] = prices.reindex(lost["time"], method="ffill").to_numpy()
    lost["energy_mwh"] = lost["fault_loss_kw"] / 60 / 1000
    lost["revenue_usd"] = lost["energy_mwh"] * lost["price"]

    # Attribute each lost interval to the scenario covering it.
    tagged = lost.merge(instances, on="asset_id", how="inner")
    tagged = tagged[
        (tagged["time"] >= tagged["start_time"]) & (tagged["time"] <= tagged["end_time"])
    ]

    if tagged.empty:
        return RankingComparison(rows=instances, rank_changes=0)

    summary = (
        tagged.groupby("scenario_id")
        .agg(energy_mwh=("energy_mwh", "sum"), revenue_usd=("revenue_usd", "sum"))
        .reset_index()
    )
    summary["energy_rank"] = summary["energy_mwh"].rank(ascending=False).astype(int)
    summary["cost_rank"] = summary["revenue_usd"].rank(ascending=False).astype(int)
    summary["moved"] = summary["energy_rank"] != summary["cost_rank"]

    return RankingComparison(
        rows=summary.sort_values("energy_rank"),
        rank_changes=int(summary["moved"].sum()),
    )


def run_blind_scoring(
    dataset: Path, run_id: str, prices: pd.Series | None = None
) -> tuple[DetectionScore, RankingComparison]:
    """Detect from the analyst tree, then score against truth.

    The detector receives a connection to the analyst tree only. It cannot
    consult the answer, which is what makes the result a measurement.

    Args:
        dataset: Export root.
        run_id: Dataset identifier.
        prices: Settlement prices, for the cost ranking.

    Returns:
        The detection score and the ranking comparison.
    """
    from .storage import duckdb_connection

    analyst = duckdb_connection(dataset, run_id, "analyst")
    detections = detect_underperformance(analyst)
    analyst.close()

    truth = duckdb_connection(dataset, run_id, "truth")
    score = score_detection(detections, truth)
    rankings = compare_rankings(truth, prices)
    truth.close()

    return score, rankings
