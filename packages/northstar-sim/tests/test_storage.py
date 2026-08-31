"""Tests for Phase 11 storage.

Two storage paths exist deliberately: TimescaleDB for hypertables and
continuous aggregates, DuckDB over Parquet for portable SQL with no containers
running. Every exercise should run against both.

The tests that matter most check **separation**, not throughput. Outside the
database only directory separation enforces the truth boundary, so the truth
tree has to be independently withholdable and the analyst tree has to carry
measured telemetry rather than truth.

No PostgreSQL server is available here, so the TimescaleDB leg is verified by
parsing rather than execution. That is stated plainly rather than implied.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pandas as pd
import pytest

warnings.filterwarnings("ignore")

from northstar_sim.builder import build_plant  # noqa: E402
from northstar_sim.plant_run import run_plant  # noqa: E402
from northstar_sim.resource import clearsky_resource, downscale_to_minute  # noqa: E402
from northstar_sim.storage import (  # noqa: E402
    ANALYST_STREAMS,
    CONTINUOUS_AGGREGATES,
    TRUTH_STREAMS,
    duckdb_connection,
    export_parquet,
    generate_ddl,
    reconcile_aggregates,
    run_storage_gate,
    validate_ddl,
)

from .test_physics import real_config  # noqa: E402


@pytest.fixture(scope="module")
def config():
    """Provide the real-equipment configuration.

    Returns:
        A plant configuration whose CEC keys resolve.
    """
    return real_config()


@pytest.fixture(scope="module")
def result(config):
    """Provide a run with faults and defects, ready to export.

    Args:
        config: Plant configuration.

    Returns:
        The run result.
    """
    source = clearsky_resource(
        config,
        "2023-06-21 05:00",
        "2023-06-23 05:00",
        freq="5min",
        temp_air_c=38.0,
        wind_speed_ms=5.0,
    )
    base = downscale_to_minute(source, config, seed=12345)
    base["wind_speed"] = 5.0
    base["wind_direction"] = 250.0
    return run_plant(
        config,
        build_plant(config),
        base,
        seed=999,
        inject_faults=True,
        inject_defects=True,
    )


@pytest.fixture(scope="module")
def exported(result, tmp_path_factory):
    """Export the run to a temporary Parquet tree.

    Args:
        result: The run result.
        tmp_path_factory: pytest temporary directory factory.

    Returns:
        A tuple of manifest and export root.
    """
    root = tmp_path_factory.mktemp("export")
    shutil.rmtree(root, ignore_errors=True)
    manifest = export_parquet(result, root, run_id="test-001")
    return manifest, Path(root)


# --------------------------------------------------------------------------
# Truth separation
# --------------------------------------------------------------------------


def test_truth_tree_is_separately_withholdable(exported) -> None:
    """Handing over the analyst tree must not hand over truth.

    Inside the database, schemas and roles enforce this. Outside it, only
    directory separation is available, so the trees must be independent.
    """
    _, root = exported
    connection = duckdb_connection(root, "test-001", "analyst")
    tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    connection.close()

    assert tables
    assert not (tables & set(TRUTH_STREAMS))


def test_truth_tree_contains_the_ground_truth(exported) -> None:
    """The validator needs it even though the analyst must not have it."""
    _, root = exported
    connection = duckdb_connection(root, "test-001", "truth")
    tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    connection.close()

    assert "inverter_truth" in tables


def test_analyst_tree_carries_measured_not_truth(exported, result) -> None:
    """Exporting truth frames here would defeat the sensor and defect layers.

    Silently, and with no error anywhere: the numbers would simply be too
    clean.
    """
    _, root = exported
    asset = next(iter(result.inverters))

    analyst = duckdb_connection(root, "test-001", "analyst")
    truth = duckdb_connection(root, "test-001", "truth")

    measured_avg = analyst.execute(
        "SELECT avg(ac_power_kw) FROM inverter_telemetry WHERE asset_id = ?",
        [asset],
    ).fetchone()[0]
    truth_avg = truth.execute(
        "SELECT avg(ac_power_kw) FROM inverter_truth WHERE asset_id = ?", [asset]
    ).fetchone()[0]

    analyst.close()
    truth.close()
    assert measured_avg != truth_avg


# --------------------------------------------------------------------------
# Export layout
# --------------------------------------------------------------------------


def test_export_is_hive_partitioned(exported) -> None:
    """Partitioning by run, stream and date enables predicate pushdown."""
    _, root = exported
    assert any(root.rglob("run_id=test-001"))
    assert any(root.rglob("stream=inverter_telemetry"))
    assert any(root.rglob("date=*"))


def test_every_analyst_stream_is_written(exported) -> None:
    """A missing stream is a silently incomplete dataset."""
    manifest, _ = exported
    for stream in ANALYST_STREAMS:
        assert manifest.analyst_rows.get(stream, 0) > 0, stream


def test_timestamps_survive_the_round_trip(exported) -> None:
    """Timezone-aware UTC must be preserved, not coerced to naive or local."""
    _, root = exported
    connection = duckdb_connection(root, "test-001", "analyst")
    frame = connection.execute(
        "SELECT time FROM inverter_telemetry ORDER BY time LIMIT 5"
    ).df()
    connection.close()

    times = pd.to_datetime(frame["time"], utc=True)
    assert times.dt.tz is not None


def test_asset_ids_survive_as_strings(exported) -> None:
    """Longitudinal analysis joins on asset id; coercion would break it."""
    _, root = exported
    connection = duckdb_connection(root, "test-001", "analyst")
    value = connection.execute(
        "SELECT asset_id FROM inverter_telemetry LIMIT 1"
    ).fetchone()[0]
    connection.close()
    assert isinstance(value, str)
    assert value.startswith("NORTHSTA")


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_pandas_and_duckdb_aggregates_agree(exported) -> None:
    """Two independent aggregation paths over the same data.

    Doc 15 section 11 requires three-way agreement between raw SQL, a
    continuous aggregate and DuckDB. Two legs are checked here; the
    TimescaleDB leg needs a running server.
    """
    _, root = exported
    reconciliation = reconcile_aggregates(root, "test-001")

    assert reconciliation.raw_rows > 0
    assert reconciliation.bucket_count > 0
    assert reconciliation.max_relative_error < 1e-9
    assert reconciliation.energy_error < 1e-9


def test_aggregation_reduces_row_count_as_expected(exported) -> None:
    """Five-minute buckets over 1-minute data reduce by roughly five."""
    _, root = exported
    reconciliation = reconcile_aggregates(root, "test-001")
    ratio = reconciliation.raw_rows / reconciliation.bucket_count
    assert 4.5 < ratio < 5.5


# --------------------------------------------------------------------------
# TimescaleDB schema
# --------------------------------------------------------------------------


def test_generated_ddl_parses_as_postgres() -> None:
    """No server is available, so parsing is the strongest check there is.

    It distinguishes syntactically valid SQL from merely plausible-looking SQL,
    which is the failure mode of hand-written DDL.
    """
    assert validate_ddl(generate_ddl()) == []


def test_ddl_creates_hypertables_and_compression() -> None:
    """Chunk intervals and compression are generated, not hand-maintained."""
    sql = generate_ddl()
    assert "create_hypertable" in sql
    assert "timescaledb.compress" in sql
    assert "add_compression_policy" in sql


def test_ddl_includes_hierarchical_continuous_aggregates() -> None:
    """Aggregate-on-aggregate is a real performance win.

    It is also a specific Timescale feature worth exercising deliberately
    rather than stumbling into.
    """
    sql = generate_ddl()
    hierarchical = [a for a in CONTINUOUS_AGGREGATES if a["hierarchical"]]

    assert hierarchical
    for aggregate in hierarchical:
        assert f"telemetry.{aggregate['name']}" in sql
    assert "FROM telemetry.inverter_5min" in sql


def test_combiner_telemetry_has_a_wider_chunk_interval() -> None:
    """It is the largest stream; a 7-day chunk would multiply chunk count."""
    from northstar_sim.storage import CHUNK_INTERVALS

    assert CHUNK_INTERVALS["combiner_telemetry"] != CHUNK_INTERVALS["inverter_telemetry"]


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------


def test_storage_gate_passes(result, exported) -> None:
    """The Phase 11 acceptance gate."""
    manifest, _ = exported
    gate = run_storage_gate(result, manifest, simulated_days=2.0)
    assert gate.passed, gate.render()


def test_annual_size_projection_is_within_budget(exported) -> None:
    """Doc 14 section 4.1 budgets 25 GB per simulated year, compressed."""
    manifest, root = exported
    size_bytes = sum(f.stat().st_size for f in root.rglob("*.parquet"))
    annual_gb = size_bytes / 1e9 * 365.0 / 2.0
    assert annual_gb < 25.0
