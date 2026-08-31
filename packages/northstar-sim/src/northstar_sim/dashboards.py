"""Grafana dashboard generation.

Dashboards are **generated from the same constants the rest of the package
uses**, not hand-edited JSON. Hand-maintained dashboards drift from the schema
silently: a renamed column leaves a panel showing an empty graph rather than an
error, and nobody notices until someone needs the number.

**These dashboards are not verified.** They are structurally valid Grafana JSON
whose panel queries parse as PostgreSQL, but no dashboard here has been rendered
against a live datasource. Until ``make db-up`` runs and the schema is applied,
treat them as a starting point rather than a working artefact - the same caveat
that applies to the TimescaleDB DDL.

Reference: design document ``02_time_series_analytics_requirements`` section 13.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Datasource the panels query. Provisioned separately; the uid must match.
DATASOURCE = {"type": "postgres", "uid": "northstar-timescaledb"}

#: Grid width Grafana uses. Panel widths are fractions of this.
GRID_WIDTH = 24


@dataclass
class Panel:
    """One dashboard panel.

    Attributes:
        title: Panel heading.
        sql: The query. Must return a time column named ``time`` for time
            series panels.
        panel_type: Grafana panel type.
        unit: Grafana unit string, such as ``kwatt`` or ``percent``.
        width: Width in grid columns out of 24.
        height: Height in grid rows.
        description: What the panel is for, shown on hover. Panels without one
            become unreadable to anyone who did not build them.
    """

    title: str
    sql: str
    panel_type: str = "timeseries"
    unit: str = "kwatt"
    width: int = 12
    height: int = 8
    description: str = ""


@dataclass
class Dashboard:
    """A generated dashboard.

    Attributes:
        title: Dashboard name.
        uid: Stable identifier, so links survive regeneration.
        panels: Panels in layout order.
        tags: Grafana tags.
        description: What the dashboard answers.
        time_from: Default window start. **Must cover the data.** The default
            "now-7d" shows nothing for a historical dataset, which is exactly
            how three correctly-generated dashboards came to display "No data"
            on every panel.
        time_to: Default window end.
    """

    title: str
    uid: str
    panels: list[Panel]
    tags: list[str] = field(default_factory=list)
    description: str = ""

    time_from: str = "now-7d"
    time_to: str = "now"

    def to_json(self) -> dict[str, Any]:
        """Render the dashboard as Grafana JSON.

        Panels are laid out left to right, wrapping when a row fills. Grafana
        will re-flow them, but a sensible starting layout means the dashboard is
        usable the first time it is opened.

        Returns:
            A Grafana dashboard definition.
        """
        rendered: list[dict[str, Any]] = []
        x = y = 0

        for index, panel in enumerate(self.panels):
            if x + panel.width > GRID_WIDTH:
                x = 0
                y += panel.height

            rendered.append(
                {
                    "id": index + 1,
                    "title": panel.title,
                    "description": panel.description,
                    "type": panel.panel_type,
                    "datasource": DATASOURCE,
                    "gridPos": {
                        "x": x,
                        "y": y,
                        "w": panel.width,
                        "h": panel.height,
                    },
                    "fieldConfig": {
                        "defaults": {
                            "unit": panel.unit,
                            "custom": {"lineWidth": 1, "fillOpacity": 8},
                        },
                        "overrides": [],
                    },
                    "targets": [
                        {
                            "refId": "A",
                            "format": "time_series"
                            if panel.panel_type == "timeseries"
                            else "table",
                            "rawQuery": True,
                            "rawSql": panel.sql.strip(),
                            "datasource": DATASOURCE,
                        }
                    ],
                }
            )
            x += panel.width

        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": ["northstar", *self.tags],
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "refresh": "",
            "time": {"from": self.time_from, "to": self.time_to},
            "panels": rendered,
            "templating": {"list": []},
        }


def plant_overview() -> Dashboard:
    """Build the plant overview dashboard.

    Returns:
        The dashboard definition.
    """
    return Dashboard(
        title="NorthStar - Plant Overview",
        uid="northstar-overview",
        tags=["overview"],
        description="Export, resource and availability at plant level.",
        panels=[
            Panel(
                title="Grid export",
                description=(
                    "Metered export. Goes negative overnight: forty inverters "
                    "at standby plus ten transformers at no-load. That is "
                    "station service, not a fault."
                ),
                sql="""
SELECT time_bucket('5 minutes', time) AS time,
       avg(grid_export_power_kw) AS "Grid export"
FROM telemetry.plant_telemetry
WHERE $__timeFilter(time)
GROUP BY 1 ORDER BY 1
""",
            ),
            Panel(
                title="Daily energy",
                description="Energy is integrated from power, never stored "
                "independently.",
                unit="megwatth",
                sql="""
SELECT time_bucket('1 day', time) AS time,
       sum(grid_export_power_kw) / 60.0 / 1000.0 AS "Energy"
FROM telemetry.plant_telemetry
WHERE $__timeFilter(time)
GROUP BY 1 ORDER BY 1
""",
            ),
            Panel(
                title="DC, inverter AC and export",
                description=(
                    "The three diverge by exactly the loss chain. A widening "
                    "gap with no matching loss category is an unattributed "
                    "loss path."
                ),
                sql="""
SELECT time_bucket('5 minutes', time) AS time,
       avg(total_dc_power_kw) AS "DC",
       avg(total_inverter_ac_power_kw) AS "Inverter AC",
       avg(grid_export_power_kw) AS "Export"
FROM telemetry.plant_telemetry
WHERE $__timeFilter(time)
GROUP BY 1 ORDER BY 1
""",
            ),
            Panel(
                title="Curtailed power",
                description=(
                    "Curtailment at high irradiance with no fault code is "
                    "economic, not equipment. Join the price series before "
                    "dispatching anyone."
                ),
                sql="""
SELECT time_bucket('5 minutes', time) AS time,
       avg(curtailed_power_kw) AS "Curtailed"
FROM telemetry.plant_telemetry
WHERE $__timeFilter(time)
GROUP BY 1 ORDER BY 1
""",
            ),
        ],
    )


def inverter_comparison() -> Dashboard:
    """Build the inverter peer comparison dashboard.

    Returns:
        The dashboard definition.
    """
    return Dashboard(
        title="NorthStar - Inverter Comparison",
        uid="northstar-inverters",
        tags=["assets", "reliability"],
        description="Peer comparison, the primary fault detection method.",
        panels=[
            Panel(
                title="Normalised output by inverter",
                description=(
                    "Output per unit of the irradiance each inverter actually "
                    "saw. Normalising by plant-average irradiance instead "
                    "manufactures underperformers out of the cloud field."
                ),
                panel_type="table",
                unit="none",
                width=24,
                sql="""
SELECT asset_id AS "Inverter",
       sum(ac_power_kw) / 60.0 / 1000.0 AS "Energy (MWh)",
       sum(ac_power_kw) / nullif(sum(poa_global), 0) AS "Normalised output"
FROM telemetry.inverter_telemetry
WHERE $__timeFilter(time) AND poa_global > 50
GROUP BY asset_id
ORDER BY "Normalised output"
""",
            ),
            Panel(
                title="Peer ratio over time",
                description=(
                    "Each inverter against its block mean. Removes the weather, "
                    "which is the dominant signal, leaving equipment behaviour."
                ),
                unit="percentunit",
                width=24,
                sql="""
WITH peers AS (
    SELECT time, asset_id, ac_power_kw,
           avg(ac_power_kw) OVER (
               PARTITION BY time, substr(asset_id, 1, 14)
           ) AS peer_mean
    FROM telemetry.inverter_telemetry
    WHERE $__timeFilter(time) AND ac_power_kw > 50
)
SELECT time_bucket('15 minutes', time) AS time,
       asset_id,
       avg(ac_power_kw / nullif(peer_mean, 0)) AS "Peer ratio"
FROM peers
GROUP BY 1, 2 ORDER BY 1
""",
            ),
            Panel(
                title="Operating state distribution",
                description="A FAULT inverter reporting output is a "
                "state/telemetry inconsistency, not a curiosity.",
                panel_type="table",
                unit="none",
                sql="""
SELECT operating_state AS "State",
       count(*) AS "Sample minutes",
       round(avg(ac_power_kw)::numeric, 1) AS "Mean AC (kW)"
FROM telemetry.inverter_telemetry
WHERE $__timeFilter(time)
GROUP BY operating_state
ORDER BY "Sample minutes" DESC
""",
            ),
            Panel(
                title="Inverter internal temperature",
                description=(
                    "Derating begins above the ambient onset plus full-load "
                    "rise. A derating inverter clips less, so the two losses "
                    "are not additive."
                ),
                unit="celsius",
                sql="""
SELECT time_bucket('15 minutes', time) AS time,
       max(internal_temp_c) AS "Hottest inverter",
       avg(internal_temp_c) AS "Fleet mean"
FROM telemetry.inverter_telemetry
WHERE $__timeFilter(time)
GROUP BY 1 ORDER BY 1
""",
            ),
        ],
    )


def data_quality() -> Dashboard:
    """Build the data quality dashboard.

    Returns:
        The dashboard definition.
    """
    return Dashboard(
        title="NorthStar - Data Quality",
        uid="northstar-data-quality",
        tags=["data quality"],
        description=(
            "Completeness and plausibility. Report these alongside every "
            "performance figure: a performance ratio of 84% from 91% "
            "availability is not the same claim as 84% from 99.8%."
        ),
        panels=[
            Panel(
                title="Data availability by inverter",
                description="Missing means NULL. Zeros where you expect gaps "
                "mean something upstream zero-filled them.",
                panel_type="table",
                unit="percentunit",
                width=24,
                sql="""
SELECT asset_id AS "Inverter",
       count(*) AS "Expected",
       sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS "Missing",
       1.0 - sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END)::float
             / count(*) AS "Availability"
FROM telemetry.inverter_telemetry
WHERE $__timeFilter(time)
GROUP BY asset_id
ORDER BY "Availability"
LIMIT 20
""",
            ),
            Panel(
                title="Missing samples over time",
                description="Contiguous blocks are communications outages; "
                "scattered singles are dropped samples.",
                unit="none",
                sql="""
SELECT time_bucket('15 minutes', time) AS time,
       sum(CASE WHEN ac_power_kw IS NULL THEN 1 ELSE 0 END) AS "Missing"
FROM telemetry.inverter_telemetry
WHERE $__timeFilter(time)
GROUP BY 1 ORDER BY 1
""",
            ),
            Panel(
                title="Weather station disagreement",
                description=(
                    "Two causes: a cloud between them, or a drifting "
                    "instrument. Spatial disagreement moves with the wind; "
                    "calibration disagreement persists. Above 8% the interval "
                    "is unusable for performance ratio."
                ),
                unit="percentunit",
                sql="""
SELECT time_bucket('15 minutes', time) AS time,
       (max(ghi) - min(ghi)) / nullif(avg(ghi), 0) AS "Relative spread"
FROM telemetry.weather_telemetry
WHERE $__timeFilter(time) AND ghi > 50
GROUP BY 1 ORDER BY 1
""",
            ),
        ],
    )


#: Every dashboard the generator produces.
DASHBOARDS = (plant_overview, inverter_comparison, data_quality)


def write_datasource(
    target: Path,
    *,
    url: str = "timescaledb:5432",
    database: str = "northstar",
) -> Path:
    """Write the datasource provisioning file.

    **Without this, every panel resolves its datasource uid to nothing and
    renders "No data" regardless of what the database holds.** The dashboards
    reference uid ``northstar-timescaledb``; something has to define it.

    Args:
        target: Destination directory, mounted at
            ``/etc/grafana/provisioning/datasources``.
        url: Host and port Grafana connects to. The compose service name, not
            localhost - Grafana resolves this from inside its own container.
        database: Database name.

    Returns:
        The written path.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)

    path = target / "northstar.yaml"
    path.write_text(
        f"""# Generated by northstar_sim.dashboards.write_datasource.
#
# The uid must match the datasource reference in every dashboard panel
# ({DATASOURCE["uid"]}). A mismatch renders as "No data" with no error.
#
# Credentials use Grafana's `$VAR` interpolation. **Not** the shell
# `${{VAR:-default}}` form: Grafana does not implement bash default expansion,
# so that string is passed through verbatim and arrives as a literal username.
# The symptom is a datasource that looks provisioned and fails to authenticate,
# which is fixed by hand in the UI and then lost on the next restart.
#
# db/docker-compose.yml supplies POSTGRES_USER and POSTGRES_PASSWORD to the
# Grafana container, applying its own defaults there, where they do work.
apiVersion: 1

datasources:
  - name: NorthStar TimescaleDB
    uid: {DATASOURCE["uid"]}
    type: postgres
    access: proxy
    url: {url}
    database: {database}
    user: $POSTGRES_USER
    secureJsonData:
      password: $POSTGRES_PASSWORD
    jsonData:
      sslmode: disable
      postgresVersion: 1600
      timescaledb: true
    isDefault: true
    editable: true
"""
    )
    return path


def write_dashboards(
    target: Path,
    *,
    datasource_target: Path | None = None,
    time_from: str = "now-7d",
    time_to: str = "now",
) -> list[Path]:
    """Write every dashboard to provisionable JSON.

    Args:
        target: Destination directory, mounted into Grafana by the compose
            file.
        datasource_target: Where to write the datasource provisioning file.
            Omitting it produces dashboards that cannot resolve their
            datasource.
        time_from: Default window start. Point this at the data, not at "now".
        time_to: Default window end.

    Returns:
        The written paths.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for builder in DASHBOARDS:
        dashboard = builder()
        dashboard.time_from = time_from
        dashboard.time_to = time_to
        path = target / f"{dashboard.uid}.json"
        path.write_text(json.dumps(dashboard.to_json(), indent=2))
        written.append(path)

    # Grafana requires YAML here. A .json provider file is silently ignored,
    # and the dashboards then never load at all.
    provider_path = target / "provider.yaml"
    provider_path.write_text(
        """# Generated by northstar_sim.dashboards.write_dashboards.
apiVersion: 1

providers:
  - name: northstar
    type: file
    allowUiUpdates: true
    options:
      path: /etc/grafana/provisioning/dashboards
      foldersFromFilesStructure: false
"""
    )
    written.append(provider_path)

    if datasource_target is not None:
        written.append(write_datasource(datasource_target))

    return written


def validate_dashboards() -> list[str]:
    """Check dashboard structure and panel SQL.

    Verifies the JSON is structurally sound and every panel query parses as
    PostgreSQL. This does **not** confirm a panel renders: Grafana macros such
    as ``$__timeFilter`` are substituted at query time and are not valid SQL on
    their own, so they are stripped before parsing.

    Returns:
        Problem descriptions, empty when every dashboard checks out.
    """
    import sqlglot

    problems: list[str] = []

    for builder in DASHBOARDS:
        dashboard = builder()
        payload = dashboard.to_json()

        if not payload["panels"]:
            problems.append(f"{dashboard.uid}: no panels")
        for panel, definition in zip(dashboard.panels, payload["panels"], strict=True):
            if not panel.description.strip():
                problems.append(f"{dashboard.uid}/{panel.title}: no description")
            if not definition["targets"][0]["rawSql"].strip():
                problems.append(f"{dashboard.uid}/{panel.title}: empty query")

            # Grafana macros are substituted server-side and are not SQL.
            sql = (
                definition["targets"][0]["rawSql"]
                .replace("$__timeFilter(time)", "TRUE")
                .replace("$__timeFilter(bucket)", "TRUE")
            )
            try:
                sqlglot.parse_one(sql, dialect="postgres")
            except Exception as error:  # noqa: BLE001 - reporting, not handling
                problems.append(
                    f"{dashboard.uid}/{panel.title}: {str(error).splitlines()[0][:80]}"
                )

    return problems
