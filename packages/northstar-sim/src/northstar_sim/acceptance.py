"""Dataset acceptance report.

Design document ``15 §12`` specifies a fourteen-section report that every
canonical dataset must produce, and ``15 §13`` states the principle it enforces:

    A dataset is not accepted because it looks realistic on a graph. It is
    accepted because it satisfies documented physical, temporal, relational,
    statistical, financial, and scenario-specific checks.

The report is the artefact that makes that decision **reviewable**. A verdict
with no itemised evidence is an opinion; one that lists every check with its
measured value can be argued with, which is the point.

Two properties matter more than the section list:

* **Every number is measured from the dataset**, not carried forward from the
  run that produced it. A report generated from in-memory state would validate
  the simulator rather than the data anyone actually receives.
* **A failing check is named, not summarised.** "3 checks failed" tells a
  reader nothing they can act on.

Reference: design document ``15_validation_acceptance_specification``
sections 12 and 13.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class Finding:
    """One line of the report.

    Attributes:
        section: Which report section it belongs to.
        name: Short identifier.
        value: The measured value, formatted for reading.
        passed: ``None`` for informational lines that carry no verdict.
        detail: Why it failed, or context worth keeping.
    """

    section: str
    name: str
    value: str
    passed: bool | None = None
    detail: str = ""


@dataclass
class AcceptanceReport:
    """A complete dataset acceptance report.

    Attributes:
        run_id: Dataset identifier.
        generated_utc: When the report was produced.
        findings: Every line, in section order.
    """

    run_id: str
    generated_utc: str
    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        section: str,
        name: str,
        value: Any,
        passed: bool | None = None,
        detail: str = "",
    ) -> None:
        """Record one finding.

        Args:
            section: Report section.
            name: Short identifier.
            value: Measured value; formatted to a string.
            passed: Verdict, or ``None`` for informational lines.
            detail: Failure reason or context.
        """
        self.findings.append(Finding(section, name, str(value), passed, detail))

    @property
    def failures(self) -> list[Finding]:
        """Checks that failed.

        Returns:
            Every finding whose verdict is ``False``.
        """
        return [f for f in self.findings if f.passed is False]

    @property
    def checks(self) -> list[Finding]:
        """Findings that carry a verdict.

        Returns:
            Every finding that is not purely informational.
        """
        return [f for f in self.findings if f.passed is not None]

    @property
    def accepted(self) -> bool:
        """Whether the dataset may be used.

        Returns:
            ``True`` when no check failed.
        """
        return not self.failures

    def render(self) -> str:
        """Format the report for reading.

        Returns:
            A multi-line report, sections in order, verdict last.
        """
        lines = [
            "=" * 74,
            f"DATASET ACCEPTANCE REPORT - {self.run_id}",
            f"generated {self.generated_utc}",
            "=" * 74,
        ]

        current = None
        for finding in self.findings:
            if finding.section != current:
                current = finding.section
                lines.append(f"\n{current.upper()}")
            mark = (
                "     "
                if finding.passed is None
                else ("[PASS]" if finding.passed else "[FAIL]")
            )
            lines.append(f"  {mark} {finding.name:<34} {finding.value}")
            if finding.detail:
                lines.append(f"           {finding.detail}")

        lines.append("\n" + "=" * 74)
        if self.accepted:
            lines.append(f"VERDICT: ACCEPTED  ({len(self.checks)} checks, 0 failures)")
        else:
            lines.append(
                f"VERDICT: REJECTED  ({len(self.checks)} checks, "
                f"{len(self.failures)} failure(s))"
            )
            # Itemised, never summarised. A count is not actionable.
            for failure in self.failures:
                lines.append(
                    f"  - {failure.section}/{failure.name}: {failure.value}"
                    + (f" - {failure.detail}" if failure.detail else "")
                )
        lines.append("=" * 74)
        return "\n".join(lines)

    def to_frame(self) -> pd.DataFrame:
        """Render the report as a table for storage alongside the dataset.

        Returns:
            One row per finding.
        """
        return pd.DataFrame(
            [
                {
                    "section": f.section,
                    "name": f.name,
                    "value": f.value,
                    "passed": f.passed,
                    "detail": f.detail,
                }
                for f in self.findings
            ]
        )


def build_report(
    root: Path,
    run_id: str,
    *,
    config,
    prices: pd.Series | None = None,
) -> AcceptanceReport:
    """Generate an acceptance report from an exported dataset.

    Everything is measured from the Parquet trees, not from the run that
    produced them. That distinction matters: a report built from in-memory state
    validates the simulator, while this validates the artefact a recipient
    actually gets - including anything the export or the round trip damaged.

    Args:
        root: Export root directory.
        run_id: Dataset identifier.
        config: Plant configuration, for nameplate comparisons.
        prices: Optional price series enabling the financial section.

    Returns:
        The completed :class:`AcceptanceReport`.
    """
    import pvlib

    from .storage import duckdb_connection

    report = AcceptanceReport(
        run_id=run_id,
        generated_utc=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )

    analyst = duckdb_connection(root, run_id, "analyst")
    truth = duckdb_connection(root, run_id, "truth")

    _provenance(report, config, pvlib.__version__)
    streams = _volume(report, analyst, root)
    _completeness(report, analyst, streams)
    _distributions(report, analyst)
    _states(report, analyst)
    _scenarios_and_events(report, truth)
    _physics(report, analyst, config, truth)
    _statistics(report, analyst)
    _energy(report, analyst)
    _financial(report, analyst, config, prices)
    _kpis(report, analyst, config)
    _truth_separation(report, analyst, truth)

    analyst.close()
    truth.close()
    return report


def _provenance(report: AcceptanceReport, config, pvlib_version: str) -> None:
    """Record what produced the dataset.

    Args:
        report: Report under construction.
        config: Plant configuration.
        pvlib_version: Version of pvlib in use.
    """
    report.add("provenance", "config_version", config.config_version)
    report.add("provenance", "site", config.site.name)
    report.add("provenance", "pvlib", pvlib_version)
    report.add("provenance", "python", platform.python_version())
    report.add(
        "provenance",
        "plant_nameplate",
        f"{config.plant_ac_kw / 1000:.2f} MW AC / {config.plant_dc_kw / 1000:.2f} MWp DC",
    )
    report.add(
        "provenance",
        "equipment",
        f"{config.module.cec_database_key} / {config.inverter.cec_database_key}",
    )


def _volume(report: AcceptanceReport, db, root: Path) -> list[str]:
    """Record record counts, time range and storage size.

    Args:
        report: Report under construction.
        db: DuckDB connection over the analyst tree.
        root: Export root, for measuring size on disk.

    Returns:
        The stream names found.
    """
    streams = sorted(row[0] for row in db.execute("SHOW TABLES").fetchall())
    total = 0

    for stream in streams:
        rows = db.execute(f"SELECT count(*) FROM {stream}").fetchone()[0]
        total += rows
        report.add("volume", stream, f"{rows:,} rows")

    report.add("volume", "total_rows", f"{total:,}")

    span = db.execute("SELECT min(time), max(time) FROM plant_telemetry").fetchone()
    days = (span[1] - span[0]).total_seconds() / 86400.0
    report.add("volume", "time_range", f"{span[0]} to {span[1]}")
    report.add("volume", "simulated_days", f"{days:.2f}")

    size = sum(f.stat().st_size for f in Path(root).rglob("*.parquet"))
    report.add("volume", "size_on_disk", f"{size / 1e6:,.1f} MB")
    report.add(
        "volume",
        "projected_annual",
        f"{size / 1e9 * 365 / max(days, 1e-9):.2f} GB",
        passed=size / 1e9 * 365 / max(days, 1e-9) < 25.0,
        detail="budget 25 GB per simulated year",
    )
    return streams


def _completeness(report: AcceptanceReport, db, streams: list[str]) -> None:
    """Record missingness and duplicate keys.

    Args:
        report: Report under construction.
        db: DuckDB connection.
        streams: Stream names.
    """
    frame = (
        db.execute(
            """
        SELECT count(*) AS total,
               sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS missing
        FROM inverter_telemetry
        """
        )
        .df()
        .iloc[0]
    )

    availability = 1.0 - float(frame["missing"]) / max(float(frame["total"]), 1)
    report.add(
        "completeness",
        "inverter_availability",
        f"{availability:.4%}",
        passed=availability > 0.98,
        detail="doc 20 section 11 target: above 99%",
    )

    duplicates = db.execute(
        """
        SELECT count(*) FROM (
            SELECT time, asset_id FROM inverter_telemetry
            GROUP BY time, asset_id HAVING count(*) > 1
        )
        """
    ).fetchone()[0]
    report.add(
        "completeness",
        "duplicate_keys",
        f"{duplicates:,}",
        passed=duplicates == 0,
        detail="uniqueness is (run_id, asset_id, time)",
    )

    monotonic = db.execute(
        """
        SELECT count(*) FROM (
            SELECT time, lag(time) OVER (PARTITION BY asset_id ORDER BY time)
                     AS previous
            FROM inverter_telemetry
        ) WHERE previous IS NOT NULL AND time <= previous
        """
    ).fetchone()[0]
    report.add(
        "completeness",
        "non_monotonic_timestamps",
        f"{monotonic:,}",
        passed=monotonic == 0,
    )


def _distributions(report: AcceptanceReport, db) -> None:
    """Record the range of key signals.

    Args:
        report: Report under construction.
        db: DuckDB connection.
    """
    frame = (
        db.execute(
            """
        SELECT min(poa_global) AS poa_min, max(poa_global) AS poa_max,
               min(ac_power_kw) AS ac_min, max(ac_power_kw) AS ac_max,
               min(cell_temperature) AS t_min, max(cell_temperature) AS t_max
        FROM inverter_telemetry
        """
        )
        .df()
        .iloc[0]
    )

    report.add(
        "distributions",
        "poa_global",
        f"{frame['poa_min']:.1f} to {frame['poa_max']:.1f} W/m2",
        passed=frame["poa_min"] >= 0 and frame["poa_max"] < 1500,
    )
    report.add(
        "distributions",
        "cell_temperature",
        f"{frame['t_min']:.1f} to {frame['t_max']:.1f} C",
        passed=frame["t_min"] > -30 and frame["t_max"] < 95,
    )
    report.add(
        "distributions",
        "inverter_ac_power",
        f"{frame['ac_min']:.2f} to {frame['ac_max']:.1f} kW",
        passed=frame["ac_min"] < 0,
        detail="minimum must be negative: standby draw",
    )


def _states(report: AcceptanceReport, db) -> None:
    """Record the operating state distribution and its consistency.

    Args:
        report: Report under construction.
        db: DuckDB connection.
    """
    frame = db.execute(
        """
        SELECT operating_state, count(*) AS n
        FROM inverter_telemetry GROUP BY 1 ORDER BY n DESC
        """
    ).df()
    for _, row in frame.iterrows():
        report.add("states", str(row.operating_state), f"{int(row.n):,} minutes")

    inconsistent = db.execute(
        """
        SELECT count(*) FROM inverter_telemetry
        WHERE operating_state IN ('STANDBY', 'OFF', 'FAULT')
          AND ac_power_kw > 1.0
        """
    ).fetchone()[0]
    report.add(
        "states",
        "state_telemetry_consistent",
        f"{inconsistent:,} violations",
        passed=inconsistent == 0,
        detail="a sleeping or faulted inverter must not report generation",
    )


def _scenarios_and_events(report: AcceptanceReport, truth) -> None:
    """Record injected scenarios and defects from the truth tree.

    Args:
        report: Report under construction.
        truth: DuckDB connection over the truth tree.
    """
    tables = {row[0] for row in truth.execute("SHOW TABLES").fetchall()}

    if "scenario_instances" in tables:
        frame = truth.execute(
            "SELECT scenario_id, count(*) AS n FROM scenario_instances "
            "GROUP BY 1 ORDER BY 1"
        ).df()
        for _, row in frame.iterrows():
            report.add("scenarios", str(row.scenario_id), f"{int(row.n)} instances")
        report.add(
            "scenarios",
            "total_injected",
            f"{int(frame['n'].sum())}",
            passed=int(frame["n"].sum()) > 0,
        )
    else:
        report.add("scenarios", "total_injected", "0", detail="none injected")

    if "defect_schedule" in tables:
        frame = truth.execute(
            "SELECT kind, count(*) AS n, avg(CASE WHEN flagged THEN 1.0 ELSE 0.0 END)"
            " AS flagged FROM defect_schedule GROUP BY 1 ORDER BY 1"
        ).df()
        for _, row in frame.iterrows():
            report.add(
                "events",
                str(row.kind),
                f"{int(row.n)} instances, {row.flagged:.0%} flagged",
            )
        unflagged = 1.0 - float(
            (frame["n"] * frame["flagged"]).sum() / max(frame["n"].sum(), 1)
        )
        report.add(
            "events",
            "unflagged_share",
            f"{unflagged:.0%}",
            passed=unflagged > 0.05,
            detail="the quality column must not be a complete oracle",
        )


def _physics(report: AcceptanceReport, db, config, truth=None) -> None:
    """Record physical invariants, each against the tree where it holds.

    **An invariant belongs to one tree.** Physical truth must respect
    nameplate; measured telemetry need not, because a power sensor with a
    calibration gain reading a clipped inverter legitimately reports above it.

    Applying the truth invariant to measured data rejected a perfectly good
    dataset over 15,562 "exceedances" reaching 2,531 kW - entirely the sensor
    layer doing its job. The same dataset had **zero** exceedances in truth.

    Args:
        report: Report under construction.
        db: DuckDB connection over the analyst tree.
        config: Plant configuration.
        truth: Connection over the truth tree, where available.
    """
    night = db.execute(
        "SELECT max(ac_power_kw) FROM inverter_telemetry WHERE poa_global < 1.0"
    ).fetchone()[0]
    report.add(
        "physics",
        "night_generation",
        f"max {night:.3f} kW",
        passed=night is None or night <= 0.0,
        detail="must be negative or zero: standby draw only",
    )

    cap = config.inverter.rated_ac_kw

    if truth is not None:
        over = truth.execute(
            f"SELECT count(*) FROM inverter_truth WHERE ac_power_kw > {cap * 1.001}"
        ).fetchone()[0]
        report.add(
            "physics",
            "ac_within_nameplate_truth",
            f"{over:,} exceedances",
            passed=over == 0,
            detail="physical truth must respect the AC cap exactly",
        )

    # Measured output may exceed nameplate: calibration gain on a clipped
    # inverter. The bound is what the sensor model permits, not the cap.
    peak = db.execute("SELECT max(ac_power_kw) FROM inverter_telemetry").fetchone()[0]
    excess = (peak - cap) / cap if peak else 0.0
    report.add(
        "physics",
        "measured_ac_within_sensor_error",
        f"peak {peak:,.1f} kW, {excess:+.2%} of cap",
        passed=excess < 0.05,
        detail="measurement may exceed nameplate by calibration error, not more",
    )

    peak_dc = db.execute("SELECT max(total_dc_power_kw) FROM plant_telemetry").fetchone()[
        0
    ]
    fraction = peak_dc / config.plant_dc_kw
    report.add(
        "physics",
        "peak_dc_fraction",
        f"{fraction:.1%} of nameplate",
        passed=fraction <= 1.12,
        detail="bifacial gain permits above 100%, not 155%",
    )


def _statistics(report: AcceptanceReport, db) -> None:
    """Record required correlations.

    Args:
        report: Report under construction.
        db: DuckDB connection.
    """
    frame = db.execute(
        """
        SELECT poa_global, dc_power_kw, effective_irradiance
        FROM inverter_telemetry WHERE poa_global > 50
        """
    ).df()

    poa_dc = float(frame["poa_global"].corr(frame["dc_power_kw"]))
    report.add(
        "statistics",
        "poa_to_dc_correlation",
        f"{poa_dc:.4f}",
        passed=poa_dc > 0.9,
    )

    if "effective_irradiance" in frame.columns:
        effective = float(frame["effective_irradiance"].corr(frame["dc_power_kw"]))
        # No ordering is asserted against the POA correlation. It was, and it
        # was wrong: effective irradiance carries rear-side gain that varies
        # with tracker geometry, adding variance that does not map linearly
        # onto DC. Measured in truth, POA correlates at 0.9927 and effective at
        # 0.9921 - the "must be higher" assumption fails on clean data.
        report.add(
            "statistics",
            "effective_to_dc_correlation",
            f"{effective:.4f}",
            passed=effective > 0.9,
        )

    spread = db.execute(
        """
        SELECT avg((mx - mn) / nullif(av, 0)) FROM (
            SELECT max(poa_global) AS mx, min(poa_global) AS mn,
                   avg(poa_global) AS av
            FROM inverter_telemetry WHERE poa_global > 100 GROUP BY time
        )
        """
    ).fetchone()[0]
    report.add(
        "statistics",
        "fleet_poa_spread",
        f"{spread:.2%}",
        passed=0.001 < spread < 0.60,
        detail="assets must see different weather, but the same day; relative "
        "spread inflates at low winter irradiance because the mean is small",
    )


def _energy(report: AcceptanceReport, db) -> None:
    """Record energy reconciliation between raw and aggregated data.

    Args:
        report: Report under construction.
        db: DuckDB connection.
    """
    raw = db.execute(
        "SELECT sum(grid_export_power_kw) / 60.0 / 1000.0 FROM plant_telemetry"
    ).fetchone()[0]
    aggregated = db.execute(
        """
        SELECT sum(mean_kw) * 5.0 / 60.0 / 1000.0 FROM (
            SELECT avg(grid_export_power_kw) AS mean_kw
            FROM plant_telemetry
            GROUP BY time_bucket(INTERVAL '5 minutes', time)
        )
        """
    ).fetchone()[0]

    error = abs(raw - aggregated) / abs(raw) if raw else 0.0
    report.add("energy", "exported_energy", f"{raw:,.1f} MWh")
    report.add(
        "energy",
        "raw_vs_aggregate",
        f"{error:.2e} relative",
        passed=error < 1e-3,
        detail="doc 15 section 11; the TimescaleDB leg remains outstanding",
    )


def _financial(report: AcceptanceReport, db, config, prices) -> None:
    """Record settlement figures where prices are available.

    Args:
        report: Report under construction.
        db: DuckDB connection.
        config: Plant configuration.
        prices: Price series, or ``None`` to skip.
    """
    if prices is None:
        report.add("financial", "prices", "not supplied", detail="section skipped")
        return

    from .market import CommercialTerms, capture_rate, settle

    frame = db.execute(
        "SELECT time, grid_export_power_kw FROM plant_telemetry ORDER BY time"
    ).df()
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    export = frame.set_index("time")["grid_export_power_kw"]

    aligned = prices.reindex(export.index).ffill().bfill()
    terms = CommercialTerms()
    settlement = settle(export, aligned, aligned, terms)

    rate = capture_rate(export, aligned)
    report.add(
        "financial",
        "capture_rate",
        f"{rate:.1%}",
        passed=0.0 < rate < 1.0,
        detail="solar produces most when its output is worth least; a "
        "cloudless week drives penetration to maximum every midday and can "
        "push capture into single digits. Above 1.0 means the join is wrong",
    )
    report.add(
        "financial",
        "energy_revenue",
        f"${settlement['energy_revenue_usd'].sum():,.0f}",
        passed=settlement["energy_revenue_usd"].sum() > 0,
    )
    report.add(
        "financial",
        "gross_margin",
        f"${settlement['gross_margin_usd'].sum():,.0f}",
    )


def _kpis(report: AcceptanceReport, db, config) -> None:
    """Record performance and availability figures.

    Args:
        report: Report under construction.
        db: DuckDB connection.
        config: Plant configuration.
    """
    from .kpis import T_REF_C, performance_metrics

    plant = db.execute(
        "SELECT time, grid_export_power_kw, curtailed_power_kw "
        "FROM plant_telemetry ORDER BY time"
    ).df()
    conditions = db.execute(
        """
        SELECT time, avg(poa_global) AS poa, avg(cell_temperature) AS cell,
               sum(dc_power_kw) AS dc
        FROM inverter_telemetry GROUP BY time ORDER BY time
        """
    ).df()

    merged = plant.merge(conditions, on="time").set_index("time")
    merged.index = pd.to_datetime(merged.index, utc=True)

    # Solar zenith is not stored on plant telemetry, so daylight is inferred
    # from irradiance. The distinction is immaterial for the filter.
    zenith = pd.Series(np.where(merged["poa"] > 5.0, 30.0, 100.0), index=merged.index)

    metrics = performance_metrics(
        config,
        merged["poa"],
        merged["cell"],
        merged["dc"],
        merged["grid_export_power_kw"],
        zenith,
        curtailed=merged["curtailed_power_kw"],
    )

    # PR above 1.0 is not an error. A cold, clear winter week on a 1.25 DC/AC
    # plant genuinely exceeds unity - cells below 25 C outperform their STC
    # rating. Measured at 1.0087 on a December record, which a 0.95 ceiling
    # rejected.
    report.add(
        "kpis",
        "performance_ratio",
        f"{metrics.performance_ratio:.4f}",
        passed=0.55 < metrics.performance_ratio < 1.15,
        detail="above 1.0 is valid in cold conditions",
    )
    # Corrected PR answers "what would PR be at 25 C cell temperature", so its
    # direction relative to raw PR follows the cells, not the hemisphere:
    #   cells above 25 C -> correction < 1 -> corrected PR HIGHER
    #   cells below 25 C -> correction > 1 -> corrected PR LOWER
    #
    # Hardcoding the hot-climate direction rejected a December dataset whose
    # mean cell temperature was 18.9 C. The check now derives the expected
    # direction from the data.
    mean_cell = float(merged["cell"][merged["poa"] > 50].mean())
    corrected_should_exceed = mean_cell > T_REF_C
    direction_holds = (
        metrics.performance_ratio_corrected > metrics.performance_ratio
    ) == corrected_should_exceed
    report.add(
        "kpis",
        "performance_ratio_corrected",
        f"{metrics.performance_ratio_corrected:.4f}",
        passed=direction_holds,
        detail=f"mean cell {mean_cell:.1f} C, so corrected PR should be "
        f"{'above' if corrected_should_exceed else 'below'} raw",
    )
    # The band must span the seasons this simulator can produce. A 0.15 floor
    # was calibrated on summer data and rejected a perfectly valid December
    # week at 0.1426 - a check that only accepts the conditions it was written
    # against is testing the fixture, not the dataset.
    #
    # A short cloudy winter record legitimately reaches 0.05; anything above
    # 0.60 for a 1.25 DC/AC plant is not physical.
    report.add(
        "kpis",
        "capacity_factor_ac",
        f"{metrics.capacity_factor_ac:.4f}",
        passed=0.03 < metrics.capacity_factor_ac < 0.60,
        detail="band spans winter to summer; not a performance judgement",
    )
    report.add(
        "kpis",
        "intervals_filtered",
        f"{metrics.filtered_intervals:,} of {metrics.total_intervals:,}",
        detail="doc 20 section 4.4 filter set applied",
    )


def _truth_separation(report: AcceptanceReport, analyst, truth) -> None:
    """Verify the analyst tree carries measured data and no truth.

    Args:
        report: Report under construction.
        analyst: Connection over the analyst tree.
        truth: Connection over the truth tree.
    """
    analyst_tables = {row[0] for row in analyst.execute("SHOW TABLES").fetchall()}
    truth_tables = {row[0] for row in truth.execute("SHOW TABLES").fetchall()}

    leaked = analyst_tables & truth_tables
    report.add(
        "truth separation",
        "no_truth_in_analyst_tree",
        f"{len(analyst_tables)} analyst streams",
        passed=not leaked,
        detail="" if not leaked else f"leaked: {sorted(leaked)}",
    )

    if "inverter_truth" in truth_tables:
        asset = analyst.execute(
            "SELECT asset_id FROM inverter_telemetry LIMIT 1"
        ).fetchone()[0]
        measured = analyst.execute(
            "SELECT avg(ac_power_kw) FROM inverter_telemetry WHERE asset_id = ?",
            [asset],
        ).fetchone()[0]
        actual = truth.execute(
            "SELECT avg(ac_power_kw) FROM inverter_truth WHERE asset_id = ?",
            [asset],
        ).fetchone()[0]
        report.add(
            "truth separation",
            "analyst_tree_is_measured",
            f"{measured:.4f} vs truth {actual:.4f} kW",
            passed=measured != actual,
            detail="identical values mean the sensor layer was bypassed",
        )
