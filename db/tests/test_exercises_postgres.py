"""Execute every curriculum exercise against a live PostgreSQL database.

The exercises are authored for DuckDB over Parquet. Three of them were not
valid PostgreSQL at all - `INTERVAL 60 MINUTE`, `UNPIVOT`, and a `::DOUBLE`
cast - and the generated files papered over it with a comment saying the
TimescaleDB form "is identical apart from the schema prefix".

That comment was accurate for eleven exercises and wrong for three, and useless
for all fourteen: it told a reader a difference existed without showing it.

    python db/tests/test_exercises_postgres.py --dsn "postgresql://user@host/db"
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    """Run every exercise's TimescaleDB form and report failures.

    Returns:
        Zero when all fourteen execute.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()

    import psycopg2
    from northstar_sim.curriculum import EXERCISES, timescale_form

    connection = psycopg2.connect(args.dsn)
    failures: list[str] = []

    for exercise in EXERCISES:
        sql = timescale_form(exercise).rstrip(";")
        with connection.cursor() as cursor:
            try:
                cursor.execute(f"SELECT count(*) FROM ({sql}) q")
                rows = cursor.fetchone()[0]
            except Exception as error:  # noqa: BLE001 - reporting
                connection.rollback()
                message = str(error).splitlines()[0][:70]
                print(f"  [FAIL] {exercise.exercise_id} {message}")
                failures.append(f"{exercise.exercise_id}: {message}")
                continue
        connection.rollback()
        print(f"  [ok  ] {exercise.exercise_id} {exercise.title:<34} {rows:>6} rows")

    connection.close()
    print(f"\n{len(EXERCISES) - len(failures)}/{len(EXERCISES)} run against PostgreSQL")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
