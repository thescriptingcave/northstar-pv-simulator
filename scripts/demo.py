"""Show what the dataset contains, in one command.

    uv run python scripts/demo.py

Three questions a newcomer actually has: does it work, what does the data look
like, and is there anything interesting in it. Answers all three in under a
minute, with no server and no configuration.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Print a short tour of the generated dataset.

    Returns:
        Zero on success, one when no dataset is present.
    """
    from northstar_analytics import find_dataset, open_dataset

    try:
        dataset = find_dataset("curriculum")
    except FileNotFoundError:
        print("No dataset found. Run `make dev-dataset` first.")
        return 1

    db = open_dataset(dataset, "curriculum", "analyst")
    line = "-" * 64

    print(f"\n{line}\n  NorthStar PV — 100 MW plant, 40 inverters, 1-minute data\n{line}")

    print("\n1. Daily energy\n")
    print(
        db.execute(
            """
            SELECT CAST(time AS DATE) AS day,
                   round(sum(grid_export_power_kw) / 60 / 1000, 1) AS energy_mwh,
                   round(max(grid_export_power_kw) / 1000, 1)      AS peak_mw
            FROM plant_telemetry
            GROUP BY 1 ORDER BY 1
            """
        )
        .df()
        .to_string(index=False)
    )

    print("\n2. Night output is negative — the plant draws power when idle\n")
    print(
        db.execute(
            """
            SELECT round(min(grid_export_power_kw), 1) AS min_kw,
                   round(max(grid_export_power_kw) / 1000, 1) AS max_mw
            FROM plant_telemetry
            """
        )
        .df()
        .to_string(index=False)
    )
    print("\n   Forty inverters on standby plus ten transformers at no load.")
    print("   Any pipeline that clips negatives to zero loses this.")

    print("\n3. An inverter that is underperforming its neighbours\n")
    print(
        db.execute(
            """
            WITH peers AS (
                SELECT asset_id, ac_power_kw,
                       avg(ac_power_kw) OVER (
                           PARTITION BY time, substr(asset_id, 1, 14)
                       ) AS block_mean
                FROM inverter_telemetry
                WHERE poa_global > 300 AND ac_power_kw IS NOT NULL
            )
            SELECT asset_id,
                   round(avg(ac_power_kw / nullif(block_mean, 0)), 4) AS peer_ratio
            FROM peers GROUP BY asset_id
            ORDER BY peer_ratio LIMIT 5
            """
        )
        .df()
        .to_string(index=False)
    )
    print("\n   Healthy inverters sit near 1.00. Compared against its own block,")
    print("   not the plant — assets in a block share weather, the plant does not.")

    print("\n4. Curtailment looks exactly like a fault\n")
    print(
        db.execute(
            """
            SELECT operating_state,
                   count(*) AS minutes,
                   round(avg(poa_global), 0)  AS mean_irradiance,
                   round(avg(ac_power_kw), 0) AS mean_kw
            FROM inverter_telemetry
            WHERE poa_global > 100
            GROUP BY 1 ORDER BY minutes DESC
            """
        )
        .df()
        .to_string(index=False)
    )
    print("\n   CURTAILED has full sun and no output. Without the state column")
    print("   you would report a fleet-wide equipment failure that never happened.")

    print(f"\n{line}")
    print("  The answers are known but not visible: faults and sensor errors")
    print("  are injected from a truth tree the analyst copy does not contain.")
    print()
    print("  Next:  LEARNING.md          six-week PV analytics track")
    print("         sql/drills/          SQL interview practice")
    print("         make impute          ML with free labels")
    print(f"{line}\n")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
