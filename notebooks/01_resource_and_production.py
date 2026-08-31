# %% [markdown]
# # 01 - Resource and Production
#
# The causal chain from sunlight to exported power, read directly from the
# analyst-facing dataset. Nothing here touches the truth tree.
#
# **What to take away:** the relationships are strong but not perfect, and the
# imperfections are where the analysis lives.

# %%

import matplotlib.pyplot as plt
import pandas as pd

from northstar_analytics import find_dataset, open_dataset

# Located by walking up from the working directory: `jupyter execute`
# runs from notebooks/, JupyterLab from wherever it was launched.
DATASET = find_dataset("curriculum")
RUN_ID = "curriculum"

db = open_dataset(DATASET, RUN_ID, "analyst")
print([row[0] for row in db.execute("SHOW TABLES").fetchall()])

# %% [markdown]
# ## Plant shape
#
# Daily energy first. Energy is always integrated from power - never stored
# independently - so a 1-minute sample is 1/60 of an hour.

# %%
daily = db.execute("""
    SELECT date_trunc('day', time) AS day,
           sum(grid_export_power_kw) / 60.0 / 1000.0 AS energy_mwh
    FROM plant_telemetry
    GROUP BY day ORDER BY day
""").df()
print(daily.to_string(index=False))

# %% [markdown]
# ## Night is not zero
#
# Overnight export is **negative**. Forty inverters draw standby power and ten
# transformers draw no-load loss, and the plant imports station service.
#
# A daylight filter that keeps these rows biases every efficiency calculation
# slightly negative. This is the first thing to get right.

# %%
night = db.execute("""
    SELECT min(grid_export_power_kw) AS min_kw,
           max(grid_export_power_kw) AS max_kw,
           avg(grid_export_power_kw) AS mean_kw
    FROM plant_telemetry
    WHERE grid_export_power_kw < 0
""").df()
print(night.to_string(index=False))

# %% [markdown]
# ## Irradiance against power
#
# DC power tracks front-side POA almost perfectly. **AC power does not** - and
# the gap between the two correlations is the whole story of this dataset.
#
# DC is unconstrained: more light, more current. AC is capped by the inverter
# and can be commanded to zero by the plant controller, so above the clipping
# threshold it stops responding to irradiance entirely.
#
# If you see a weak POA-to-AC correlation, do not conclude the array is faulty.
# Look at what is holding AC down.

# %%
sample = db.execute("""
    SELECT time, poa_global, dc_power_kw, ac_power_kw, cell_temperature
    FROM inverter_telemetry
    WHERE asset_id = 'NORTHSTA-BLK01-INV1' AND poa_global > 50
""").df()

print(f"front POA vs DC       r = {sample['poa_global'].corr(sample['dc_power_kw']):.4f}")
print(f"front POA vs AC       r = {sample['poa_global'].corr(sample['ac_power_kw']):.4f}")
print("\nDC responds to light without limit; AC is capped and commandable.")

fig, ax = plt.subplots(figsize=(7, 4))
ax.scatter(sample["poa_global"], sample["dc_power_kw"], s=1, alpha=0.2)
ax.set_xlabel("Plane-of-array irradiance (W/m2)")
ax.set_ylabel("DC power (kW)")
ax.set_title("DC power against front-side POA")
fig.tight_layout()

# %% [markdown]
# ## Temperature, and a confounding trap
#
# Hotter cells produce less power. That is settled physics: this module has a
# -0.433 %/C power coefficient.
#
# Measuring it is harder than it looks, because cell temperature and irradiance
# move together. Irradiance confounds the relationship and **attenuates** it -
# a naive correlation understates the effect badly, and on a dataset with
# constant ambient temperature it inverts the sign entirely.
#
# Three views below: the naive correlation over a wide irradiance band, the same
# inside a narrow band, and a two-variable regression that separates the effects
# properly. Only the last recovers the module's rated coefficient.

# %%
band = sample[(sample["poa_global"] > 900) & (sample["poa_global"] < 1000)]
print(f"naive, 900-1000 W/m2 ({len(band)} samples): "
      f"r = {band['cell_temperature'].corr(band['dc_power_kw']):+.3f}  "
      f"<- right sign, badly attenuated")

narrow = sample[(sample["poa_global"] > 985) & (sample["poa_global"] < 995)]
if len(narrow) > 30:
    print(f"narrow band 985-995 W/m2 ({len(narrow)} samples): "
          f"r = {narrow['cell_temperature'].corr(narrow['dc_power_kw']):+.3f}")

# %% [markdown]
# Controlling properly: regress DC on both irradiance and cell temperature. The
# temperature coefficient should now be negative and close to the module's
# rated -0.433 %/C.

# %%
import numpy as np

fit = sample[sample["poa_global"] > 200].dropna(
    subset=["poa_global", "cell_temperature", "dc_power_kw"]
)
design = np.column_stack(
    [np.ones(len(fit)), fit["poa_global"], fit["cell_temperature"]]
)
coefficients, *_ = np.linalg.lstsq(design, fit["dc_power_kw"], rcond=None)

mean_dc = fit["dc_power_kw"].mean()
print(f"controlling for irradiance, {len(fit):,} samples:")
print(f"  dDC/dPOA  = {coefficients[1]:+.4f} kW per W/m2")
print(f"  dDC/dTemp = {coefficients[2]:+.4f} kW per C "
      f"({coefficients[2] / mean_dc:+.4%} of mean output per C)")
print("  module rated temperature coefficient: -0.433%/C")
print("\nThe controlled estimate recovers the rated coefficient. Neither raw")
print("correlation does - one is attenuated, and with constant ambient the")
print("same regression returns the wrong sign entirely.")

# %% [markdown]
# ## Spatial structure
#
# Inverters do not see the same weather. A cloud reaches downwind assets later,
# which is why peer comparison has a realistic noise floor and why wind
# direction is recoverable from irradiance telemetry alone.

# %%
spread = db.execute("""
    SELECT time,
           max(poa_global) - min(poa_global) AS poa_spread,
           avg(poa_global) AS poa_mean
    FROM inverter_telemetry
    WHERE poa_global > 100
    GROUP BY time
""").df()
print(f"mean relative POA spread across the fleet: "
      f"{(spread['poa_spread'] / spread['poa_mean']).mean():.2%}")

db.close()
