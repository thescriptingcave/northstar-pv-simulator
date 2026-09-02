"""Load an exported dataset into TimescaleDB.

Creating the schema is not the same as having data, and nothing in this project
bridged the two. ``make db-up`` ran the init scripts, which create schemas,
tables, hypertables and aggregates - and left every table empty. Grafana then
correctly reported "No data" for every panel.

The loader uses ``COPY``, not row-by-row ``INSERT``. At roughly 89 million rows
per simulated year the difference is hours.

**Only the analyst tree is loaded by default.** The truth tree is withheld
unless explicitly requested, because a database holding truth alongside
telemetry cannot support blind analysis no matter what the role grants say.

Reference: design documents ``13_time_series_data_model`` section 9 and
``15_validation_acceptance_specification`` section 11.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from .storage import ANALYST_STREAMS, TRUTH_STREAMS, duckdb_connection


@dataclass
class LoadResult:
    """Outcome of loading a dataset.

    Attributes:
        rows_by_stream: Rows loaded per stream.
        skipped: Streams present in the export but absent from the database.
    """

    rows_by_stream: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        """Total rows loaded.

        Returns:
            Sum across every stream.
        """
        return sum(self.rows_by_stream.values())


def _target_columns(cursor, schema: str, table: str) -> list[str]:
    """Read a table's column names from the database.

    The export carries hive partition columns such as ``date`` that the table
    may not have, and column order is not guaranteed to match. Loading by
    explicit intersection avoids both problems.

    Args:
        cursor: An open database cursor.
        schema: Schema name.
        table: Table name.

    Returns:
        Column names in the table's own order, empty if the table is absent.
    """
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return [row[0] for row in cursor.fetchall()]


def load_dataset(
    dsn: str,
    root: Path,
    run_id: str,
    *,
    include_truth: bool = False,
    truncate: bool = True,
) -> LoadResult:
    """Load an exported Parquet dataset into TimescaleDB.

    Args:
        dsn: PostgreSQL connection string.
        root: Export root directory.
        run_id: Dataset identifier.
        include_truth: Also load the truth tree into the ``truth`` schema.
            Off by default - a database holding truth cannot support blind
            analysis regardless of role grants.
        truncate: Empty each target table before loading, so re-running is
            idempotent rather than duplicating rows.

    Returns:
        A :class:`LoadResult`.

    Raises:
        RuntimeError: If the export contains no readable streams.
    """
    import psycopg2

    result = LoadResult()
    connection = psycopg2.connect(dsn)

    trees = [("analyst", "telemetry", ANALYST_STREAMS)]
    if include_truth:
        trees.append(("truth", "truth", TRUTH_STREAMS))

    loaded_any = False
    for tree, schema, streams in trees:
        duck = duckdb_connection(root, run_id, tree)
        available = {row[0] for row in duck.execute("SHOW TABLES").fetchall()}

        for stream in streams:
            if stream not in available:
                continue
            loaded_any = True

            with connection.cursor() as cursor:
                columns = _target_columns(cursor, schema, stream)
                if not columns:
                    result.skipped.append(f"{schema}.{stream}")
                    continue

                source_columns = [
                    description[0]
                    for description in duck.execute(
                        f"SELECT * FROM {stream} LIMIT 0"
                    ).description
                ]
                shared = [c for c in columns if c in source_columns]

                frame = duck.execute(
                    f"SELECT {', '.join(f'"{c}"' for c in shared)} FROM {stream}"
                ).df()

                if truncate:
                    cursor.execute(
                        f"TRUNCATE telemetry.{stream}"
                        if schema == "telemetry"
                        else f"TRUNCATE {schema}.{stream}"
                    )

                buffer = io.StringIO()
                frame.to_csv(
                    buffer,
                    index=False,
                    header=False,
                    na_rep="",
                    quoting=csv.QUOTE_MINIMAL,
                )
                buffer.seek(0)

                cursor.copy_expert(
                    f"COPY {schema}.{stream} ({', '.join(shared)}) "
                    "FROM STDIN WITH (FORMAT csv, NULL '')",
                    buffer,
                )
                result.rows_by_stream[f"{schema}.{stream}"] = len(frame)

            connection.commit()
        duck.close()

    connection.close()

    if not loaded_any:
        raise RuntimeError(
            f"no readable streams under {root}/analyst/run_id={run_id} - "
            "generate a dataset first"
        )
    return result


def refresh_aggregates(dsn: str) -> list[str]:
    """Refresh every continuous aggregate, parents before children.

    A hierarchical aggregate reads another aggregate, so refreshing in the
    wrong order leaves the rollups empty even though the raw data is present.
    Ordering by bucket width refreshes parents first.

    Args:
        dsn: PostgreSQL connection string.

    Returns:
        The aggregates refreshed, in the order applied.
    """
    import psycopg2

    connection = psycopg2.connect(dsn)
    connection.autocommit = True  # refresh cannot run inside a transaction

    refreshed: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT view_schema, view_name
            FROM timescaledb_information.continuous_aggregates
            ORDER BY length(view_name), view_name
            """
        )
        for schema, view in cursor.fetchall():
            cursor.execute(
                f"CALL refresh_continuous_aggregate('{schema}.{view}', NULL, NULL)"
            )
            refreshed.append(f"{schema}.{view}")

    connection.close()
    return refreshed


def dataset_time_range(root: Path, run_id: str) -> tuple[str, str] | None:
    """Read the time span an export covers.

    Dashboards default to a window around "now", which shows nothing for a
    historical dataset. Knowing the real span lets the generator point them at
    the data instead.

    Args:
        root: Export root directory.
        run_id: Dataset identifier.

    Returns:
        ISO start and end timestamps, or ``None`` if unreadable.
    """
    duck = duckdb_connection(root, run_id, "analyst")
    try:
        row = duck.execute("SELECT min(time), max(time) FROM plant_telemetry").fetchone()
    except Exception:  # noqa: BLE001 - absence is an expected outcome here
        return None
    finally:
        duck.close()

    if not row or row[0] is None:
        return None

    # Emit UTC with a "Z" suffix, not a local offset.
    #
    # DuckDB returns these in the session's local timezone, so isoformat()
    # produced strings like "2023-06-20T22:00:00-07:00". Grafana stores that
    # verbatim and re-interprets it against the dashboard timezone, which
    # defaults to browser time - shifting the default window off the data and
    # rendering every panel empty while the connection and the SQL are both
    # fine.
    import pandas as pd

    start = pd.Timestamp(row[0]).tz_convert("UTC")
    end = pd.Timestamp(row[1]).tz_convert("UTC")
    return (
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
