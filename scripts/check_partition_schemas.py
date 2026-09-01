"""Compare Parquet schemas across a dataset's date partitions.

    uv run python scripts/check_partition_schemas.py datasets/observed poa_global

A dataset is written one partition per date. If a single partition types a
column differently - most often as `null`, because every value in that
partition was missing - a `union_by_name` read can coerce the column to null
across the whole dataset. The symptom is a column that is almost entirely NULL
on read while the underlying values are fine.

This reports the type each partition assigned, so a single disagreeing
partition is visible immediately.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    """Report per-partition types for a column.

    Returns:
        Zero when every partition agrees, one otherwise.
    """
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    root = Path(sys.argv[1])
    column = sys.argv[2] if len(sys.argv) > 2 else "poa_global"

    import pyarrow.parquet as pq

    files = sorted(root.rglob("*.parquet"))
    if not files:
        print(f"no parquet files under {root}")
        return 1

    by_type: dict[str, list[Path]] = defaultdict(list)
    absent: list[Path] = []

    for path in files:
        schema = pq.read_schema(path)
        if column not in schema.names:
            absent.append(path)
            continue
        by_type[str(schema.field(column).type)].append(path)

    print(f"{root}  column={column}")
    print(f"  partitions scanned : {len(files):,}")
    print(f"  column absent from : {len(absent):,}")
    print()

    for dtype, paths in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        print(f"  {dtype:<12} {len(paths):>6,} partition(s)")
        # Name the minority so a single odd partition is findable.
        if len(paths) <= 5:
            for p in paths:
                print(f"      {p.relative_to(root)}")

    print()
    if len(by_type) > 1:
        print("  DISAGREEMENT: partitions type this column differently.")
        print("  A `null`-typed partition can coerce the column to null across")
        print("  the whole dataset on a union_by_name read.")
        return 1

    print("  All partitions agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
