"""Execute every Grafana panel query against a live database.

A dashboard fails in practice because its query errors or returns nothing - not
because the JSON is malformed. Structural validation cannot catch either.

This runs the panel SQL directly, substituting the Grafana macros the server
would normally expand. It verifies everything about a dashboard except the
rendering layer.

    python db/tests/test_dashboard_panels.py --dsn "postgresql://user@host/db"
"""

from __future__ import annotations

import argparse
import re
import sys

#: Grafana expands this server-side; it is not valid SQL on its own.
TIME_FILTER = re.compile(r"\$__timeFilter\((\w+)\)")


def main() -> int:
    """Run every panel query and report which return data.

    Returns:
        Zero when every panel returns at least one row.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="PostgreSQL connection string")
    parser.add_argument("--from", dest="start", default="2023-06-21")
    parser.add_argument("--to", dest="end", default="2023-06-25")
    args = parser.parse_args()

    import psycopg2  # imported here so the module loads without it installed
    from northstar_sim.dashboards import DASHBOARDS

    replacement = rf"\1 BETWEEN '{args.start}'::timestamptz AND '{args.end}'::timestamptz"

    connection = psycopg2.connect(args.dsn)
    total = passed = 0
    failures: list[str] = []

    for builder in DASHBOARDS:
        dashboard = builder()
        print(f"\n{dashboard.uid}")
        for panel in dashboard.panels:
            total += 1
            sql = TIME_FILTER.sub(replacement, panel.sql).strip().rstrip(";")
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f"SELECT count(*) FROM ({sql}) q")
                    rows = cursor.fetchone()[0]
                except Exception as error:  # noqa: BLE001 - reporting
                    connection.rollback()
                    message = str(error).splitlines()[0][:70]
                    print(f"  [FAIL ] {panel.title:<34} {message}")
                    failures.append(f"{dashboard.uid}/{panel.title}: {message}")
                    continue
            connection.rollback()
            if rows > 0:
                passed += 1
                print(f"  [ok   ] {panel.title:<34} {rows:,} rows")
            else:
                # An empty panel is a silent failure: it renders as a blank
                # graph rather than an error, and nobody notices.
                print(f"  [EMPTY] {panel.title:<34} 0 rows")
                failures.append(f"{dashboard.uid}/{panel.title}: no rows")

    connection.close()
    print(f"\n{passed}/{total} panels return data")
    for failure in failures:
        print(f"  - {failure}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
