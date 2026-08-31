# %% [markdown]
# # 02 - Expected Power and Loss Attribution
#
# Fitting the ASTM E2848 capacity regression, then using it to separate
# performance from resource.
#
# **The hard part is choosing the training period.** The coefficients are only
# meaningful when fitted on a clean stretch - free of faults, curtailment and
# heavy soiling. Selecting one is part of the exercise.

# %%

import pandas as pd

from northstar_analytics import (
    find_dataset,
    fit_expected_power,
    normalize_for_weather,
    open_dataset,
)

# Located by walking up from the working directory: `jupyter execute`
# runs from notebooks/, JupyterLab from wherever it was launched.
DATASET = find_dataset("curriculum")
RUN_ID = "curriculum"
db = open_dataset(DATASET, RUN_ID, "analyst")

# %% [markdown]
# ## Assemble plant-level conditions
#
# Irradiance and temperature are averaged across inverters; power is the meter.

# %%
frame = db.execute("""
    SELECT i.time,
           avg(i.poa_global) AS irradiance,
           avg(i.cell_temperature) AS temperature,
           avg(i.ac_power_kw) AS mean_inverter_kw
    FROM inverter_telemetry i
    GROUP BY i.time ORDER BY i.time
""").df()
plant = db.execute("""
    SELECT time, grid_export_power_kw, curtailed_power_kw
    FROM plant_telemetry ORDER BY time
""").df()

merged = frame.merge(plant, on="time").set_index("time")
merged.index = pd.to_datetime(merged.index, utc=True)
merged["wind_speed"] = 3.0
print(f"{len(merged):,} intervals")

# %% [markdown]
# ## Select a clean training period
#
# Exclude curtailed intervals and low irradiance. In blind mode you cannot use
# the truth schema to find a clean stretch, so this filtering is the exercise.

# %%
clean = merged[
    (merged["curtailed_power_kw"] <= 0)
    & (merged["irradiance"] > 50)
    & (merged["grid_export_power_kw"] > 0)
]
print(f"{len(clean):,} intervals survive filtering "
      f"({len(clean) / len(merged):.1%})")

split = int(len(clean) * 0.5)
train, test = clean.iloc[:split], clean.iloc[split:]

model = fit_expected_power(
    train["grid_export_power_kw"],
    train["irradiance"],
    train["temperature"],
    train["wind_speed"],
)
print(f"\nfitted on {model.samples:,} samples, R2 = {model.r_squared:.4f}")
print(f"coefficients a,b,c,d = "
      f"{', '.join(f'{c:.6g}' for c in model.coefficients)}")

# %% [markdown]
# ## Hold-out performance
#
# The residual is the signal. Systematic drift in it means the plant changed,
# not that the model is wrong.

# %%
predicted = model.predict(
    test["irradiance"], test["temperature"], test["wind_speed"]
)
residual = test["grid_export_power_kw"] - predicted
print(f"hold-out mean absolute error {residual.abs().mean():,.0f} kW "
      f"({residual.abs().mean() / test['grid_export_power_kw'].mean():.2%} "
      f"of mean output)")
print(f"mean residual {residual.mean():+,.0f} kW  "
      f"(a large signed value means the plant changed between periods)")

# %% [markdown]
# ## Normalised output
#
# Dividing measured output by weather-expected output removes the dominant
# signal, leaving plant behaviour. This is the series every degradation and
# soiling method operates on.

# %%
normalized = normalize_for_weather(
    clean["grid_export_power_kw"],
    model,
    clean[["irradiance", "temperature", "wind_speed"]],
)
daily_normalized = normalized.resample("1D").median().dropna()
print(daily_normalized.round(4).to_string())

db.close()
