"""Execute a .sql file against the dev dataset and print each result.

    uv run python scripts/run_sql_file.py sql/learning/week1_resource.sql
    uv run python scripts/run_sql_file.py sql/drills/01_window_functions.sql

Splits on semicolons, skips comment-only blocks, and prints the first rows of
each statement. Exists because the same logic inlined into a Makefile recipe
needs four levels of quoting and does not survive contact with make.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def statements(text: str) -> list[str]:
    """Split a SQL file into executable statements.

    Args:
        text: File contents.

    Returns:
        Statements with comment-only blocks removed.
    """
    found = []
    for chunk in text.split(";"):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body and re.match(r"(?is)^\s*(select|with)", body):
            found.append(body)
    return found


def main() -> int:
    """Run every statement in the given file.

    Returns:
        Zero when all statements succeed.
    """
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    from northstar_analytics import find_dataset, open_dataset

    path = Path(sys.argv[1])
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    db = open_dataset(find_dataset("curriculum"), "curriculum", "analyst")
    failures = 0

    for index, sql in enumerate(statements(path.read_text()), start=1):
        print(f"\n--- {path.name} [{index}] " + "-" * 40)
        try:
            frame = db.execute(sql).df()
        except Exception as error:  # noqa: BLE001 - reporting
            failures += 1
            print(f"  FAILED: {str(error).splitlines()[0][:80]}")
            continue
        if frame.empty:
            print("  (no rows)")
        else:
            print(frame.head(rows).to_string(index=False))

    db.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
