"""Recompute the README's headline figures from a real dataset.

    uv run python scripts/headline_figures.py datasets/year year

Every published figure came from `clearsky_resource` and `synthetic_prices`.
This recomputes them from a dataset built with `--real`, so the README can
state measured results rather than modelled ones.

Prices are read from the fetch cache; a figure that needs them is skipped
rather than silently computed against a synthetic stand-in.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Print the headline figures.

    Returns:
        Zero on success.
    """
    dataset = Path(sys.argv[1] if len(sys.argv) > 1 else "datasets/year")
    run_id = sys.argv[2] if len(sys.argv) > 2 else "year"

    import pandas as pd
    from northstar_sim.market import capture_rate
    from northstar_sim.observed import align_prices, real_prices
    from northstar_sim.plant_config import load_plant_config
    from northstar_sim.storage import duckdb_connection

    config = load_plant_config(Path("config/northstar.toml"))
    db = duckdb_connection(dataset, run_id, "analyst")

    plant = db.execute(
        "SELECT time, grid_export_power_kw, curtailed_power_kw "
        "FROM plant_telemetry ORDER BY time"
    ).df()
    plant["time"] = pd.to_datetime(plant["time"], utc=True)
    plant = plant.set_index("time")

    export = plant["grid_export_power_kw"]
    energy = export.sum() / 60 / 1000

    print(f"Dataset: {dataset} ({len(plant):,} intervals)\n")
    print(f"  exported energy          {energy:>14,.1f} MWh")
    print(
        f"  capacity factor (AC)     "
        f"{energy / (config.plant_ac_kw / 1000 * len(plant) / 60):>14.4f}"
    )
    print(f"  peak export              {export.max() / 1000:>14,.1f} MW")
    print(f"  night export (min)       {export.min() / 1000:>14,.3f} MW")

    curtailed = plant["curtailed_power_kw"].sum() / 60 / 1000
    print(f"  curtailed energy         {curtailed:>14,.1f} MWh")

    years = sorted({int(y) for y in plant.index.year.unique()})
    try:
        frames = [real_prices(config, y) for y in years]
    except FileNotFoundError as error:
        print(f"\n  prices unavailable: {error}")
        db.close()
        return 0

    prices = pd.concat(frames).sort_index()
    prices = prices[~prices.index.duplicated(keep="first")]
    aligned = align_prices(prices, plant.index)

    rate = capture_rate(export, aligned)
    generation_weighted = (export.clip(lower=0) * aligned).sum() / export.clip(
        lower=0
    ).sum()

    print()
    print(f"  time-weighted price      ${aligned.mean():>13,.2f}/MWh")
    print(f"  generation-weighted      ${generation_weighted:>13,.2f}/MWh")
    print(f"  CAPTURE RATE             {rate:>14.1%}")
    revenue = (export.clip(lower=0) * aligned).sum() / 60 / 1000
    curtailed_value = (plant["curtailed_power_kw"] * aligned).sum() / 60 / 1000

    print()
    print(f"  energy revenue            ${revenue:>13,.0f}")
    print(f"  value of curtailed energy ${curtailed_value:>13,.0f}")
    print()
    print("  Negative curtailment value means curtailing MADE money - the")
    print("  intervals avoided were priced below the PTC floor.")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
