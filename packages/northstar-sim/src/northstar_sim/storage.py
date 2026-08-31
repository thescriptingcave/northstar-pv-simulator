"""Storage: Parquet export, TimescaleDB schema, and reconciliation.

Two storage paths exist deliberately, and every SQL exercise should run against
both. TimescaleDB gives hypertables, ``time_bucket`` and continuous aggregates;
DuckDB over Parquet gives portable SQL with no containers running. Doing each
exercise twice teaches the difference between time-series-specific features and
standard SQL - and it keeps the dataset usable on a plane.

**Truth is exported to a separate directory tree.** The database enforces the
truth/measurement boundary with schemas and roles; outside the database only
directory separation is available, so the truth tree must be independently
withholdable. Handing someone the analyst tree and keeping the truth tree is
what makes blind analysis possible without a running server.

Reference: design document ``13_time_series_data_model`` sections 9 and 13.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

#: Streams written to the analyst-facing tree.
ANALYST_STREAMS = (
    "inverter_telemetry",
    "weather_telemetry",
    "block_telemetry",
    "transformer_telemetry",
    "plant_telemetry",
)

#: Streams written to the restricted truth tree. These must be withholdable as
#: a unit; a dataset shipped with them is not a blind dataset.
TRUTH_STREAMS = (
    "inverter_truth",
    "weather_truth",
    "scenario_instances",
    "defect_schedule",
    "sensor_fleet",
)

#: Continuous aggregate definitions: name, source, bucket, and whether it is
#: built on another aggregate rather than on raw data.
CONTINUOUS_AGGREGATES: tuple[dict[str, object], ...] = (
    {
        "name": "inverter_5min",
        "source": "telemetry.inverter_telemetry",
        "bucket": "5 minutes",
        "hierarchical": False,
    },
    {
        "name": "inverter_hourly",
        "source": "telemetry.inverter_5min",
        "bucket": "1 hour",
        "hierarchical": True,
    },
    {
        "name": "plant_5min",
        "source": "telemetry.plant_telemetry",
        "bucket": "5 minutes",
        "hierarchical": False,
    },
    {
        "name": "plant_hourly",
        "source": "telemetry.plant_5min",
        "bucket": "1 hour",
        "hierarchical": True,
    },
    {
        "name": "plant_daily",
        "source": "telemetry.plant_hourly",
        "bucket": "1 day",
        "hierarchical": True,
    },
    {
        "name": "weather_hourly",
        "source": "telemetry.weather_telemetry",
        "bucket": "1 hour",
        "hierarchical": False,
    },
)

#: Chunk intervals by stream class. Combiner telemetry is the largest stream, so
#: it gets a wider chunk to keep chunk count manageable.
CHUNK_INTERVALS: dict[str, str] = {
    "inverter_telemetry": "7 days",
    "weather_telemetry": "7 days",
    "block_telemetry": "7 days",
    "transformer_telemetry": "7 days",
    "plant_telemetry": "7 days",
    "combiner_telemetry": "14 days",
    "prices": "90 days",
    "settlement": "90 days",
}


@dataclass
class ExportManifest:
    """Record of what an export wrote.

    Attributes:
        run_id: Identifier for this dataset.
        root: Directory the export was written to.
        analyst_rows: Rows written per analyst-facing stream.
        truth_rows: Rows written per truth stream.
    """

    run_id: str
    root: Path
    analyst_rows: dict[str, int]
    truth_rows: dict[str, int]

    @property
    def total_rows(self) -> int:
        """Total rows across both trees.

        Returns:
            Combined row count.
        """
        return sum(self.analyst_rows.values()) + sum(self.truth_rows.values())


def _write_stream(
    root: Path, tree: str, run_id: str, stream: str, frame: pd.DataFrame
) -> int:
    """Write one stream, hive-partitioned by date.

    Args:
        root: Export root directory.
        tree: ``"analyst"`` or ``"truth"``.
        run_id: Dataset identifier.
        stream: Stream name.
        frame: Data carrying a ``time`` column.

    Returns:
        Rows written.
    """
    if frame.empty:
        return 0

    frame = frame.copy()
    if "time" not in frame.columns:
        frame = frame.reset_index().rename(columns={"index": "time"})

    times = pd.to_datetime(frame["time"], utc=True)
    frame["date"] = times.dt.strftime("%Y-%m-%d")

    written = 0
    for date, group in frame.groupby("date", sort=True):
        target = root / tree / f"run_id={run_id}" / f"stream={stream}" / f"date={date}"
        target.mkdir(parents=True, exist_ok=True)
        payload = group.drop(columns=["date"])
        payload.to_parquet(target / "part-0.parquet", index=False, compression="zstd")
        written += len(payload)
    return written


def _stack(frames: dict[str, pd.DataFrame], key: str = "asset_id") -> pd.DataFrame:
    """Combine per-asset frames into one long frame.

    Args:
        frames: Per-asset frames indexed by time.
        key: Column name to hold the asset identifier.

    Returns:
        A long frame with a ``time`` column, or empty if there is nothing.
    """
    if not frames:
        return pd.DataFrame()

    parts = []
    for asset_id, frame in frames.items():
        part = frame.reset_index().rename(columns={"index": "time"})
        if "time" not in part.columns:
            part = part.rename(columns={part.columns[0]: "time"})
        part.insert(1, key, asset_id)
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def export_parquet(result, root: Path, *, run_id: str) -> ExportManifest:
    """Export a simulation run to a hive-partitioned Parquet tree.

    Args:
        result: A ``PlantRunResult``.
        root: Destination directory.
        run_id: Dataset identifier, used as the top partition key.

    Returns:
        An :class:`ExportManifest` describing what was written.
    """
    root = Path(root)
    analyst: dict[str, int] = {}
    truth: dict[str, int] = {}

    # Analyst-facing telemetry is the *measured* frames where they exist,
    # falling back to truth only for assets with no sensors. Exporting the
    # truth frames here would silently defeat the whole separation.
    measured = result.measured or {}
    inverter_measured = {
        key: measured.get(key, frame) for key, frame in result.inverters.items()
    }
    weather_measured = {
        key: measured.get(key, frame) for key, frame in result.weather.items()
    }

    analyst["inverter_telemetry"] = _write_stream(
        root, "analyst", run_id, "inverter_telemetry", _stack(inverter_measured)
    )
    analyst["weather_telemetry"] = _write_stream(
        root, "analyst", run_id, "weather_telemetry", _stack(weather_measured)
    )
    analyst["block_telemetry"] = _write_stream(
        root, "analyst", run_id, "block_telemetry", _stack(result.blocks)
    )
    analyst["transformer_telemetry"] = _write_stream(
        root, "analyst", run_id, "transformer_telemetry", _stack(result.transformers)
    )
    analyst["plant_telemetry"] = _write_stream(
        root,
        "analyst",
        run_id,
        "plant_telemetry",
        result.plant.reset_index().rename(columns={"index": "time"}),
    )

    truth["inverter_truth"] = _write_stream(
        root, "truth", run_id, "inverter_truth", _stack(result.inverters)
    )
    truth["weather_truth"] = _write_stream(
        root, "truth", run_id, "weather_truth", _stack(result.weather)
    )
    if result.schedule is not None:
        truth["scenario_instances"] = _write_stream(
            root,
            "truth",
            run_id,
            "scenario_instances",
            result.schedule.to_frame().rename(columns={"start": "time"}),
        )
    if result.defects is not None:
        truth["defect_schedule"] = _write_stream(
            root,
            "truth",
            run_id,
            "defect_schedule",
            result.defects.to_frame().rename(columns={"start": "time"}),
        )

    return ExportManifest(
        run_id=run_id, root=root, analyst_rows=analyst, truth_rows=truth
    )


#: Mapping from pandas dtypes to PostgreSQL column types. Timestamps must be
#: ``timestamptz``: a naive column silently drops the offset and shifts every
#: series by the server's timezone.
_PG_TYPES: dict[str, str] = {
    "datetime64[ns, UTC]": "TIMESTAMPTZ",
    "float64": "DOUBLE PRECISION",
    "float32": "REAL",
    "int64": "BIGINT",
    "int32": "INTEGER",
    "bool": "BOOLEAN",
    "object": "TEXT",
}


def _column_type(dtype) -> str:
    """Map a pandas dtype to a PostgreSQL column type.

    Args:
        dtype: The pandas dtype.

    Returns:
        A PostgreSQL type name, defaulting to ``TEXT`` for anything unmapped.
    """
    name = str(dtype)
    if name.startswith("datetime64") and "UTC" in name:
        return "TIMESTAMPTZ"
    return _PG_TYPES.get(name, "TEXT")


def generate_table_ddl(schemas: dict[str, pd.DataFrame]) -> str:
    """Generate CREATE TABLE statements from actual exported frames.

    Types are derived from the data rather than declared by hand, so the schema
    cannot drift from what the simulator writes.

    This exists because the first version of :func:`generate_ddl` emitted
    ``create_hypertable`` calls against tables that were never created. It
    parsed cleanly - ``SELECT create_hypertable(...)`` is just a function call -
    and would have failed on the first statement against a live server. Parsing
    validates syntax, not sense.

    Args:
        schemas: Sample frames keyed by stream name.

    Returns:
        A SQL script creating each table in the ``telemetry`` schema.
    """
    lines = ["-- Table definitions, generated from the exported data.", ""]

    for stream, frame in sorted(schemas.items()):
        columns = []
        for name, dtype in frame.dtypes.items():
            columns.append(f'    "{name}" {_column_type(dtype)}')

        lines.append(f"CREATE TABLE IF NOT EXISTS telemetry.{stream} (")
        lines.append(",\n".join(columns))
        lines.append(");")
        lines.append("")

        # Uniqueness is (run_id, asset_id, time) per doc 13 section 11, but the
        # index is created rather than a primary key: hypertables require any
        # unique index to include the partitioning column, and a plain index
        # keeps the duplicate-detection exercise possible in staging.
        if "asset_id" in frame.columns:
            lines.append(
                f"CREATE INDEX IF NOT EXISTS {stream}_asset_time_idx"
                f" ON telemetry.{stream} (asset_id, time DESC);"
            )
        else:
            lines.append(
                f"CREATE INDEX IF NOT EXISTS {stream}_time_idx"
                f" ON telemetry.{stream} (time DESC);"
            )
        lines.append("")

    return "\n".join(lines)


def generate_ddl(
    streams: Iterable[str] = ANALYST_STREAMS,
    schemas: dict[str, pd.DataFrame] | None = None,
) -> str:
    """Generate the TimescaleDB schema for the exported streams.

    Emits hypertable creation, chunk intervals, compression and continuous
    aggregates. The statements are generated rather than hand-written so the
    chunk intervals and aggregate definitions cannot drift from the constants
    the rest of the package uses.

    **Statements are shaped to the columns each stream actually has.** An
    earlier version emitted ``compress_segmentby = 'asset_id'`` and aggregates
    over ``ac_power_kw`` for every stream unconditionally. ``plant_telemetry``
    is plant-level - one row per timestamp - and has neither column, so
    execution failed with ``column "asset_id" does not exist``. Parsing cannot
    catch this: the SQL is well-formed, it just refers to columns that are not
    there.

    Args:
        streams: Streams to emit hypertable statements for.
        schemas: Sample frames keyed by stream, used to shape each statement.
            Without them the generator assumes the per-asset shape, which is
            wrong for plant-level streams.

    Returns:
        A SQL script.
    """
    schemas = schemas or {}

    def columns_of(stream: str) -> set[str]:
        """Return the columns a stream carries, if known.

        Args:
            stream: Stream name.

        Returns:
            Column names, or an empty set when the schema is unknown.
        """
        frame = schemas.get(stream)
        return set(frame.columns) if frame is not None else set()

    def value_column(stream: str) -> str:
        """Pick the stream's headline power column.

        Args:
            stream: Stream name.

        Returns:
            A column name suitable for aggregation.
        """
        available = columns_of(stream)
        for candidate in ("ac_power_kw", "grid_export_power_kw", "ghi"):
            if candidate in available:
                return candidate
        return "ac_power_kw"

    lines = [
        "-- Generated by northstar_sim.storage.generate_ddl.",
        "-- Schemas and roles are created by db/init/01_schemas.sql.",
        "-- Table definitions come from generate_table_ddl and must be applied",
        "-- first: create_hypertable requires the table to already exist.",
        "",
    ]

    for stream in streams:
        interval = CHUNK_INTERVALS.get(stream, "7 days")
        has_asset = "asset_id" in columns_of(stream) or not schemas

        lines += [
            f"SELECT create_hypertable('telemetry.{stream}', 'time',",
            f"    chunk_time_interval => INTERVAL '{interval}',",
            "    if_not_exists => TRUE);",
            "",
            f"ALTER TABLE telemetry.{stream} SET (",
            "    timescaledb.compress,",
        ]
        # Segmenting by a column the stream does not have is a hard error.
        if has_asset:
            lines.append("    timescaledb.compress_segmentby = 'asset_id',")
        lines += [
            "    timescaledb.compress_orderby = 'time DESC');",
            "",
            f"SELECT add_compression_policy('telemetry.{stream}',",
            "    INTERVAL '30 days', if_not_exists => TRUE);",
            "",
        ]

    # A hierarchical aggregate inherits its shape from its parent, which may
    # itself be plant-level. Assuming every hierarchical view has asset_id
    # failed on plant_hourly, whose parent plant_5min has no such column.
    aggregate_has_asset: dict[str, bool] = {}

    for aggregate in CONTINUOUS_AGGREGATES:
        name = str(aggregate["name"])
        source = str(aggregate["source"])
        bucket = aggregate["bucket"]
        hierarchical = bool(aggregate["hierarchical"])

        base = source.split(".")[-1]
        column = value_column(base) if not hierarchical else "value"
        has_asset = (
            aggregate_has_asset.get(base, False)
            if hierarchical
            else "asset_id" in columns_of(base)
        )
        aggregate_has_asset[name] = has_asset
        # A hierarchical aggregate reads the previous one, whose time column is
        # named `bucket`, not `time`.
        time_column = "bucket" if hierarchical else "time"

        note = (
            "-- Hierarchical: built on another continuous aggregate."
            if hierarchical
            else "-- Built directly on raw telemetry."
        )
        select = [
            note,
            f"CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.{name}",
            "WITH (timescaledb.continuous) AS",
            f"SELECT time_bucket(INTERVAL '{bucket}', {time_column}) AS bucket,",
        ]
        # Group by ordinal, not by name. In a hierarchical aggregate the output
        # alias `bucket` collides with the source column of the same name, and
        # PostgreSQL resolves GROUP BY to the source column - so TimescaleDB
        # sees no time_bucket() in the grouping and rejects the view with
        # "must include a valid time bucket function". Ordinals are unambiguous.
        group = ["1"]
        if has_asset:
            select.append("    asset_id,")
            group.append("2")

        if hierarchical:
            select += [
                "    avg(value) AS value,",
                "    max(value_max) AS value_max,",
                "    sum(samples) AS samples",
            ]
        else:
            select += [
                f"    avg({column}) AS value,",
                f"    max({column}) AS value_max,",
                "    count(*) AS samples",
            ]

        select += [f"FROM {source}", f"GROUP BY {', '.join(group)};", ""]
        lines += select

    return "\n".join(lines)


def duckdb_connection(root: Path, run_id: str, tree: str = "analyst"):
    """Open a DuckDB connection with views over the exported tree.

    Views are created only over the requested tree. Pointing this at the
    analyst tree is what gives an analyst a queryable dataset with no access to
    truth and no server running.

    Args:
        root: Export root directory.
        run_id: Dataset identifier.
        tree: ``"analyst"`` or ``"truth"``.

    Returns:
        A DuckDB connection with one view per available stream.
    """
    import duckdb

    connection = duckdb.connect()
    base = Path(root) / tree / f"run_id={run_id}"
    if not base.exists():
        return connection

    for stream_dir in sorted(base.glob("stream=*")):
        stream = stream_dir.name.split("=", 1)[1]
        pattern = str(stream_dir / "**" / "*.parquet")
        connection.execute(
            f"CREATE OR REPLACE VIEW {stream} AS "
            f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=true)"
        )
    return connection


@dataclass
class ReconciliationResult:
    """Outcome of comparing aggregation paths.

    Attributes:
        raw_rows: Rows in the raw stream.
        bucket_count: Buckets produced by aggregation.
        max_relative_error: Largest relative disagreement between paths.
        energy_error: Relative disagreement in total energy.
    """

    raw_rows: int
    bucket_count: int
    max_relative_error: float
    energy_error: float


def reconcile_aggregates(
    root: Path,
    run_id: str,
    *,
    stream: str = "inverter_telemetry",
    column: str = "ac_power_kw",
    bucket: str = "5 minutes",
) -> ReconciliationResult:
    """Compare in-memory aggregation against DuckDB over Parquet.

    Design document ``15 §11`` requires three-way agreement between raw SQL, a
    continuous aggregate, and DuckDB over Parquet. Two of the three legs are
    checked here. The TimescaleDB leg needs a running server and is exercised
    by the same SQL that :func:`generate_ddl` emits.

    Args:
        root: Export root directory.
        run_id: Dataset identifier.
        stream: Stream to reconcile.
        column: Column to aggregate.
        bucket: Bucket width.

    Returns:
        A :class:`ReconciliationResult`.
    """
    connection = duckdb_connection(root, run_id)

    raw = connection.execute(f"SELECT * FROM {stream}").df()
    raw["time"] = pd.to_datetime(raw["time"], utc=True)

    minutes = int(bucket.split()[0]) if bucket.split()[0].isdigit() else 5
    pandas_side = (
        raw.set_index("time")
        .groupby("asset_id")[column]
        .resample(f"{minutes}min")
        .mean()
        .rename("value")
        .reset_index()
    )

    duck_side = connection.execute(
        f"""
        SELECT time_bucket(INTERVAL '{minutes} minutes', time) AS bucket,
               asset_id,
               avg({column}) AS value
        FROM {stream}
        GROUP BY bucket, asset_id
        ORDER BY bucket, asset_id
        """
    ).df()
    duck_side["bucket"] = pd.to_datetime(duck_side["bucket"], utc=True)

    merged = pandas_side.merge(
        duck_side,
        left_on=["time", "asset_id"],
        right_on=["bucket", "asset_id"],
        suffixes=("_pandas", "_duckdb"),
    ).dropna(subset=["value_pandas", "value_duckdb"])

    scale = merged[["value_pandas", "value_duckdb"]].abs().max(axis=1)
    usable = scale > 1e-9
    relative = (merged["value_pandas"] - merged["value_duckdb"]).abs()[usable] / scale[
        usable
    ]

    pandas_total = float(pandas_side["value"].sum(skipna=True))
    duck_total = float(duck_side["value"].sum(skipna=True))
    energy_error = (
        abs(pandas_total - duck_total) / abs(pandas_total) if pandas_total else 0.0
    )

    connection.close()
    return ReconciliationResult(
        raw_rows=len(raw),
        bucket_count=len(duck_side),
        max_relative_error=float(relative.max()) if len(relative) else 0.0,
        energy_error=energy_error,
    )


def validate_ddl(sql: str) -> list[str]:
    """Parse generated DDL and report syntax errors.

    Without a PostgreSQL server available, parsing is the strongest available
    check that the emitted schema is syntactically valid rather than merely
    plausible-looking.

    Args:
        sql: The SQL script.

    Returns:
        Error messages, empty when every statement parses.
    """
    import sqlglot

    errors: list[str] = []
    for statement in sql.split(";"):
        text = statement.strip()
        if not text or text.startswith("--"):
            continue
        try:
            sqlglot.parse_one(text, dialect="postgres")
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            errors.append(f"{text.splitlines()[0][:60]}...: {error}")
    return errors


@dataclass
class StorageGateResult:
    """Outcome of the Phase 11 storage acceptance checks.

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


def run_storage_gate(
    result, manifest: ExportManifest, *, simulated_days: float
) -> StorageGateResult:
    """Verify the storage layer meets its Phase 11 criteria.

    Args:
        result: The exported ``PlantRunResult``.
        manifest: Output of :func:`export_parquet`.
        simulated_days: Days covered, for the annual size projection.

    Returns:
        A :class:`StorageGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    root, run_id = manifest.root, manifest.run_id

    checks.append(
        (
            "both_trees_written",
            bool(manifest.analyst_rows) and bool(manifest.truth_rows),
            f"{sum(manifest.analyst_rows.values()):,} analyst rows, "
            f"{sum(manifest.truth_rows.values()):,} truth rows",
        )
    )

    # Outside the database only directory separation is available, so the truth
    # tree must be independently withholdable. Handing over the analyst tree and
    # keeping the truth tree is what makes blind analysis possible with no
    # server running.
    analyst = duckdb_connection(root, run_id, "analyst")
    analyst_tables = {row[0] for row in analyst.execute("SHOW TABLES").fetchall()}
    leaked = analyst_tables & set(TRUTH_STREAMS)
    checks.append(
        (
            "truth_tree_separable",
            not leaked,
            f"{len(analyst_tables)} analyst streams, no truth stream present"
            if not leaked
            else f"leaked: {sorted(leaked)}",
        )
    )

    # The analyst tree must carry *measured* telemetry. Exporting truth frames
    # here would silently defeat the entire sensor and defect layer.
    truth = duckdb_connection(root, run_id, "truth")
    sample_asset = next(iter(result.inverters))
    measured_avg = analyst.execute(
        "SELECT avg(ac_power_kw) FROM inverter_telemetry WHERE asset_id = ?",
        [sample_asset],
    ).fetchone()[0]
    truth_avg = truth.execute(
        "SELECT avg(ac_power_kw) FROM inverter_truth WHERE asset_id = ?",
        [sample_asset],
    ).fetchone()[0]
    checks.append(
        (
            "analyst_tree_is_measured",
            measured_avg is not None
            and truth_avg is not None
            and measured_avg != truth_avg,
            f"{sample_asset}: measured {measured_avg:.4f} vs truth {truth_avg:.4f} kW",
        )
    )
    analyst.close()
    truth.close()

    errors = validate_ddl(generate_ddl())
    checks.append(
        (
            "ddl_parses",
            not errors,
            "every generated statement parses as PostgreSQL"
            if not errors
            else f"{len(errors)} parse errors",
        )
    )

    reconciliation = reconcile_aggregates(root, run_id)
    checks.append(
        (
            "aggregates_reconcile",
            reconciliation.max_relative_error < 1e-9
            and reconciliation.energy_error < 1e-9,
            f"{reconciliation.raw_rows:,} raw rows -> "
            f"{reconciliation.bucket_count:,} buckets, max relative error "
            f"{reconciliation.max_relative_error:.2e}",
        )
    )

    size_bytes = sum(f.stat().st_size for f in Path(root).rglob("*.parquet"))
    annual_gb = size_bytes / 1e9 * 365.0 / max(simulated_days, 1e-9)
    checks.append(
        (
            "annual_size_within_budget",
            annual_gb < 25.0,
            f"{size_bytes / 1e6:.1f} MB for {simulated_days:.1f} days -> "
            f"{annual_gb:.2f} GB per year (budget 25)",
        )
    )

    checks.append(
        (
            "hive_partitioning_by_date",
            any(Path(root).rglob("date=*")),
            "partitioned by run, stream and date for predicate pushdown",
        )
    )

    return StorageGateResult(checks=checks)
