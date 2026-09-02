# %% [markdown]
# # 03 - Forecasting Plant Output
#
# A short-horizon forecast, evaluated honestly.
#
# **Two things sink energy forecasting projects, and both are avoidable:**
#
# 1. **Target leakage.** Give the model concurrent irradiance and it scores
#    beautifully in backtest and is useless in production, because at forecast
#    time that irradiance has not happened yet.
# 2. **No baseline.** Absolute error means nothing on its own. A solar forecast
#    that cannot beat "the same as last interval" has no value, and many do not.

# %%

import pandas as pd
from northstar_analytics import (
    build_features,
    evaluate_forecast,
    find_dataset,
    leaking_columns,
    open_dataset,
)

# Located by walking up from the working directory: `jupyter execute`
# runs from notebooks/, JupyterLab from wherever it was launched.
DATASET = find_dataset("curriculum")
RUN_ID = "curriculum"
db = open_dataset(DATASET, RUN_ID, "analyst")

# %% [markdown]
# ## Resample to a forecast horizon
#
# Fifteen minutes, matching the settlement interval - so a forecast error maps
# directly onto a settlement consequence.

# %%
plant = db.execute("""
    SELECT time, grid_export_power_kw FROM plant_telemetry ORDER BY time
""").df()
plant["time"] = pd.to_datetime(plant["time"], utc=True)
series = plant.set_index("time")["grid_export_power_kw"].resample("15min").mean()
frame = series.to_frame("grid_export_power_kw").dropna()
print(f"{len(frame):,} intervals at 15 minutes")

# %% [markdown]
# ## Build strictly backward-looking features
#
# Every feature is a lag, a rolling mean of lags, or a calendar term known in
# advance. Nothing concurrent.

# %%
features = build_features(frame, target="grid_export_power_kw")
leaks = leaking_columns(features, frame, "grid_export_power_kw")
print(f"features: {list(features.columns)}")
print(f"leakage check: {leaks if leaks else 'none detected'}")

# %% [markdown]
# ## Deliberate leakage, for contrast
#
# Add the concurrent target and watch the check catch it. This is what a
# too-good backtest looks like from the inside.

# %%
leaky = features.copy()
leaky["concurrent_output"] = frame["grid_export_power_kw"]
print(
    f"with concurrent target added: "
    f"{leaking_columns(leaky, frame, 'grid_export_power_kw')}"
)

# %% [markdown]
# ## Train and evaluate against persistence
#
# A chronological split, never a random one: a random split lets the model see
# the future.

# %%
from sklearn.ensemble import HistGradientBoostingRegressor

data = features.join(frame).dropna()
split = int(len(data) * 0.7)
train, test = data.iloc[:split], data.iloc[split:]

columns = list(features.columns)
model = HistGradientBoostingRegressor(max_iter=200, random_state=0)
model.fit(train[columns], train["grid_export_power_kw"])

predicted = pd.Series(model.predict(test[columns]), index=test.index)
persistence = test["grid_export_power_kw_lag1"]

score = evaluate_forecast(
    test["grid_export_power_kw"], predicted, persistence, "gradient boosting"
)
baseline = evaluate_forecast(
    test["grid_export_power_kw"], persistence, persistence, "persistence"
)

print(f"{'model':<20} {'MAE (kW)':>12} {'RMSE (kW)':>12} {'skill':>10}")
for result in (baseline, score):
    print(
        f"{result.name:<20} {result.mae:>12,.0f} {result.rmse:>12,.0f} "
        f"{result.skill:>9.1%}"
    )

# %% [markdown]
# ## Reading the result
#
# Positive skill means the model beats persistence. At a 15-minute horizon
# persistence is a genuinely strong baseline for solar, because irradiance is
# highly autocorrelated over short intervals - so modest skill is a real result,
# not a disappointing one.
#
# Where the model should win is on **ramps**: persistence is worst exactly when
# a cloud arrives, which is when the forecast matters most.

# %%
ramps = test["grid_export_power_kw"].diff().abs()
steep = ramps > ramps.quantile(0.9)

steep_score = evaluate_forecast(
    test["grid_export_power_kw"][steep],
    predicted[steep],
    persistence[steep],
    "gradient boosting, steep ramps",
)
print(
    f"on the steepest 10% of ramps: MAE {steep_score.mae:,.0f} kW, "
    f"skill {steep_score.skill:.1%}"
)

db.close()
