"""Tests for Grafana dashboard generation.

Dashboards are generated from the same constants the rest of the package uses.
Hand-maintained JSON drifts from the schema silently: a renamed column leaves a
panel showing an empty graph rather than an error.

**These tests do not verify that any dashboard renders.** No panel here has been
run against a live datasource. They check structure and that panel SQL parses,
which is the same limitation the TimescaleDB DDL carries.
"""

from __future__ import annotations

import json
import warnings

import pytest

warnings.filterwarnings("ignore")

from northstar_sim.dashboards import (  # noqa: E402
    DASHBOARDS,
    Dashboard,
    Panel,
    validate_dashboards,
    write_dashboards,
)
from northstar_sim.storage import generate_table_ddl, validate_ddl  # noqa: E402


def test_every_dashboard_validates() -> None:
    """Structure is sound and every panel query parses as PostgreSQL."""
    assert validate_dashboards() == []


def test_every_panel_has_a_description() -> None:
    """A panel without one is unreadable to anyone who did not build it."""
    for builder in DASHBOARDS:
        dashboard = builder()
        for panel in dashboard.panels:
            assert panel.description.strip(), f"{dashboard.uid}/{panel.title}"


def test_panels_do_not_overlap() -> None:
    """A starting layout that stacks panels on top of each other is unusable."""
    for builder in DASHBOARDS:
        payload = builder().to_json()
        occupied: set[tuple[int, int]] = set()
        for panel in payload["panels"]:
            grid = panel["gridPos"]
            cells = {
                (x, y)
                for x in range(grid["x"], grid["x"] + grid["w"])
                for y in range(grid["y"], grid["y"] + grid["h"])
            }
            assert not (cells & occupied), panel["title"]
            occupied |= cells


def test_panels_fit_the_grid() -> None:
    """Grafana's grid is 24 columns wide."""
    for builder in DASHBOARDS:
        for panel in builder().to_json()["panels"]:
            grid = panel["gridPos"]
            assert grid["x"] + grid["w"] <= 24, panel["title"]


def test_dashboard_uids_are_unique_and_stable() -> None:
    """Links must survive regeneration."""
    uids = [builder().uid for builder in DASHBOARDS]
    assert len(set(uids)) == len(uids)
    assert all(uid.startswith("northstar-") for uid in uids)


def test_written_dashboards_are_valid_json(tmp_path) -> None:
    """Grafana refuses to provision a malformed file, silently on some versions.

    The provider file is deliberately excluded: it is YAML, not JSON.
    """
    written = write_dashboards(tmp_path)
    assert len(written) == len(DASHBOARDS) + 1  # dashboards plus the provider

    for path in written:
        if path.suffix != ".json":
            continue
        payload = json.loads(path.read_text())
        assert isinstance(payload, dict)


def test_time_series_panels_alias_a_time_column() -> None:
    """Grafana needs a column named ``time`` or the panel renders nothing."""
    for builder in DASHBOARDS:
        for panel in builder().panels:
            if panel.panel_type != "timeseries":
                continue
            assert "AS time" in panel.sql, f"{builder().uid}/{panel.title}"


def test_hypertable_ddl_requires_table_definitions() -> None:
    """create_hypertable fails unless the table already exists.

    The first version of the schema generator emitted only hypertable calls.
    It parsed cleanly - ``SELECT create_hypertable(...)`` is a function call -
    and would have failed on the first statement against a live server. Parsing
    validates syntax, not sense.
    """
    import pandas as pd

    frame = pd.DataFrame(
        {
            "time": pd.date_range("2023-06-01", periods=3, freq="1min", tz="UTC"),
            "asset_id": ["A", "B", "C"],
            "ac_power_kw": [1.0, 2.0, 3.0],
            "active_inverters": [4, 4, 4],
        }
    )
    sql = generate_table_ddl({"inverter_telemetry": frame})

    assert "CREATE TABLE IF NOT EXISTS telemetry.inverter_telemetry" in sql
    assert validate_ddl(sql) == []


def test_timestamps_are_declared_timezone_aware() -> None:
    """A naive column drops the offset and shifts every series silently."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "time": pd.date_range("2023-06-01", periods=3, freq="1min", tz="UTC"),
            "value": [1.0, 2.0, 3.0],
        }
    )
    assert '"time" TIMESTAMPTZ' in generate_table_ddl({"plant_telemetry": frame})


def test_a_panel_with_broken_sql_is_rejected() -> None:
    """The validator must actually fire, not merely be present."""
    import sqlglot

    broken = Panel(title="Broken", description="test", sql="SELECT FROM WHERE ORDER")
    dashboard = Dashboard(title="t", uid="northstar-t", panels=[broken])

    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(
            dashboard.to_json()["panels"][0]["targets"][0]["rawSql"],
            dialect="postgres",
        )


# --------------------------------------------------------------------------
# The three causes of "No data"
# --------------------------------------------------------------------------


def test_a_datasource_provisioning_file_is_written(tmp_path) -> None:
    """Without it every panel's uid resolves to nothing.

    Grafana renders "No data" and logs no error, so the dashboards look correct
    and are simply blank. This file was missing entirely: the compose stack
    mounted dashboards but never a datasource.
    """
    from northstar_sim.dashboards import DATASOURCE, write_datasource

    path = write_datasource(tmp_path)
    text = path.read_text()

    assert path.suffix == ".yaml"
    assert f"uid: {DATASOURCE['uid']}" in text
    assert "type: postgres" in text


def test_the_datasource_uid_matches_every_panel(tmp_path) -> None:
    """A mismatch is silent. The two must be generated from one constant."""
    from northstar_sim.dashboards import DATASOURCE, write_datasource

    text = write_datasource(tmp_path).read_text()
    for builder in DASHBOARDS:
        for panel in builder().to_json()["panels"]:
            assert panel["datasource"]["uid"] == DATASOURCE["uid"]
    assert f"uid: {DATASOURCE['uid']}" in text


def test_the_default_time_window_can_point_at_the_data(tmp_path) -> None:
    """A default window of now-7d shows nothing for a historical dataset.

    Three correctly-generated dashboards displayed "No data" on every panel for
    exactly this reason, with the database fully loaded.
    """
    written = write_dashboards(
        tmp_path,
        time_from="2023-06-21T05:00:00+00:00",
        time_to="2023-06-28T05:00:00+00:00",
    )
    payload = json.loads(next(p for p in written if p.suffix == ".json").read_text())

    assert payload["time"]["from"].startswith("2023-06-21")
    assert payload["time"]["to"].startswith("2023-06-28")


def test_the_provider_file_is_yaml_not_json(tmp_path) -> None:
    """Grafana reads YAML here. The file was named .yaml and contained JSON.

    YAML is a JSON superset so it happened to load, but nothing guaranteed it.
    """
    written = write_dashboards(tmp_path)
    provider = next(p for p in written if p.name == "provider.yaml")
    text = provider.read_text().lstrip()

    assert not text.startswith("{")
    assert "apiVersion: 1" in text


def test_the_loader_dependency_is_declared() -> None:
    """The loader imports psycopg2, so the manifest must require it.

    It was installed by hand during development and omitted from
    pyproject.toml, so `make db-load` failed on a clean checkout with
    ModuleNotFoundError - after five minutes of dataset generation.
    """
    import tomllib
    from pathlib import Path

    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(manifest.read_text())["project"]["dependencies"]
    assert any("psycopg2" in requirement for requirement in declared)


def test_the_physics_chain_emits_no_numerical_warnings() -> None:
    """Forty lines of scipy noise per run trains people to ignore output.

    At night effective irradiance is zero and the single-diode solver divides
    zero by zero. The NaN result is correct and zero-filled, but the warning is
    emitted per call. Suppressed narrowly at that one call.

    **Scoped to numerical warnings from our own numerics.** An earlier version
    asserted zero warnings of any kind and duly failed on a `DeprecationWarning`
    raised inside pvlib on a newer pandas - a dependency's deprecation timetable
    breaking a test about our arithmetic. A test that fails for reasons outside
    the thing it describes is worse than no test: it gets muted, and takes the
    real assertion with it.
    """
    import warnings
    from pathlib import Path

    from northstar_sim.physics import run_inverter_chain
    from northstar_sim.plant_config import load_plant_config
    from northstar_sim.resource import clearsky_resource, downscale_to_minute

    root = Path(__file__).resolve().parents[3]
    config = load_plant_config(root / "config" / "northstar.toml")
    source = clearsky_resource(config, "2023-06-21 00:00", "2023-06-22 00:00")
    weather = downscale_to_minute(source, config, seed=1)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run_inverter_chain(config, weather)

    # RuntimeWarning is the class scipy raises for divide-by-zero and invalid
    # values - the thing the suppression exists for. Deprecations from
    # dependencies are somebody else's release schedule.
    numerical = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert not numerical, [str(w.message)[:60] for w in numerical]


def test_datasource_credentials_use_grafana_interpolation(tmp_path) -> None:
    """Grafana does not implement the shell `${VAR:-default}` form.

    That string is passed through verbatim and arrives as a literal username,
    so the datasource looks provisioned and fails to authenticate. It gets
    fixed by hand in the UI and lost on the next restart.

    Grafana supports `$VAR`; compose supplies the values with its own defaults,
    where the `:-` form does work.
    """
    from northstar_sim.dashboards import write_datasource

    text = write_datasource(tmp_path).read_text()

    # Check the credential lines only. The file documents the broken form in a
    # comment, so scanning the whole text matches its own explanation - the
    # same false positive the diagnostic script first had.
    credentials = [
        line
        for line in text.splitlines()
        if line.strip().startswith(("user:", "password:"))
    ]
    assert credentials, "no credential lines found"
    assert not any(":-" in line for line in credentials)

    assert "user: $POSTGRES_USER" in text
    assert "password: $POSTGRES_PASSWORD" in text


def test_datasource_url_is_the_compose_service_not_localhost(tmp_path) -> None:
    """Grafana resolves this from inside its own container."""
    from northstar_sim.dashboards import write_datasource

    text = write_datasource(tmp_path).read_text()
    assert "url: timescaledb:5432" in text
    assert "localhost" not in text
