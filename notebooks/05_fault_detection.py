# %% [markdown]
# # 05 - Fault Detection as a Supervised Problem
#
# Notebook 04 imputed missing values, where the label was simply the value that
# had been withheld. This is harder: the label is an **event**, and events have
# boundaries you have to decide on.
#
# `northstar-sim score` runs a deliberately naive detector — a fixed peer-ratio
# threshold — and gets **39.2% recall at 81.7% precision** on a full year. That
# is the number to beat, and the point of this notebook is to understand *why*
# it is only 39%, before trying to improve it.
#
# **A caution worth stating up front.** It is easy to raise recall by lowering
# the threshold, and precision collapses. Any change has to be scored on both.

# %%

import numpy as np
import pandas as pd
from northstar_analytics import find_dataset, open_dataset

DATASET = find_dataset("curriculum")
RUN_ID = "curriculum"

analyst = open_dataset(DATASET, RUN_ID, "analyst")
truth = open_dataset(DATASET, RUN_ID, "truth")

# %% [markdown]
# ## 1. What was actually injected
#
# Look at the labels before building anything. The class balance and the
# duration distribution both constrain what is achievable.

# %%

instances = truth.execute(
    'SELECT scenario_id, asset_id, time AS start_time, "end" AS end_time, '
    "duration_minutes, is_transient FROM scenario_instances"
).df()

print(f"{len(instances)} injected instances\n")
print(
    instances.groupby("scenario_id")
    .agg(
        n=("scenario_id", "size"),
        median_minutes=("duration_minutes", "median"),
        transient=("is_transient", "sum"),
    )
    .to_string()
)

# %% [markdown]
# Note the transients. A fault lasting under 15 minutes is excluded from
# reliability statistics by design (`doc 09`), and it is also close to
# undetectable — there is barely enough signal to distinguish it from a passing
# cloud.
#
# **Any recall number should state whether transients are in the denominator.**
# Ours includes them, which is the honest choice and also the harsher one.

# %% [markdown]
# ## 2. Build the feature the naive detector uses
#
# Peer ratio: each inverter against its own block mean, at every interval.
# Block rather than plant, because assets in a block share weather closely and
# the plant does not — normalising against the plant manufactures
# underperformers out of the cloud field.

# %%

features = analyst.execute(
    """
    WITH daylight AS (
        SELECT time, asset_id, substr(asset_id, 1, 14) AS block_id,
               ac_power_kw, poa_global, cell_temperature,
               tracker_angle_deg, operating_state
        FROM inverter_telemetry
        WHERE poa_global > 200 AND ac_power_kw IS NOT NULL
    )
    SELECT time, asset_id, block_id, ac_power_kw, poa_global,
           cell_temperature, tracker_angle_deg, operating_state,
           avg(ac_power_kw) OVER (PARTITION BY time, block_id) AS block_mean
    FROM daylight
    """
).df()
features["time"] = pd.to_datetime(features["time"], utc=True)
features["peer_ratio"] = features["ac_power_kw"] / features["block_mean"].replace(
    0, np.nan
)

print(f"{len(features):,} daylight intervals")
print(features["peer_ratio"].describe().round(4).to_string())

# %% [markdown]
# ## 3. Label each interval
#
# Join intervals to injected windows. This is the step where an event-detection
# problem becomes a per-row classification problem, and where you quietly make
# a modelling decision: **an interval inside any injected window is positive.**

# %%

labelled = features.merge(instances, on="asset_id", how="left")
inside = (labelled["time"] >= labelled["start_time"]) & (
    labelled["time"] <= labelled["end_time"]
)
labelled["is_fault"] = inside.fillna(False)

per_interval = labelled.groupby(["time", "asset_id"], as_index=False).agg(
    peer_ratio=("peer_ratio", "first"),
    poa_global=("poa_global", "first"),
    tracker_angle_deg=("tracker_angle_deg", "first"),
    operating_state=("operating_state", "first"),
    is_fault=("is_fault", "max"),
)

positives = int(per_interval["is_fault"].sum())
print(
    f"positive intervals {positives:,} of {len(per_interval):,} "
    f"({100 * positives / len(per_interval):.2f}%)"
)

# %% [markdown]
# **Severe class imbalance**, and that is realistic — faults are rare. It also
# means accuracy is a useless metric here: predicting "never a fault" scores
# over 99%.
#
# Precision and recall, always. Or precision-recall AUC if you want one number.

# %% [markdown]
# ## 4. How separable is the naive feature?
#
# Before reaching for a model, check whether the feature carries the signal.

# %%

print(
    per_interval.groupby("is_fault")["peer_ratio"]
    .describe()[["count", "mean", "25%", "50%", "75%"]]
    .round(4)
    .to_string()
)

# %% [markdown]
# If the two distributions overlap heavily, no threshold on this feature alone
# will do well — which is the honest explanation for 39% recall, and it is not
# fixed by tuning.

# %% [markdown]
# ## 5. Sweep the threshold
#
# See the precision-recall trade-off directly rather than assuming it.

# %%

rows = []
for threshold in (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
    predicted = per_interval["peer_ratio"] < threshold
    actual = per_interval["is_fault"].astype(bool)
    tp = int((predicted & actual).sum())
    fp = int((predicted & ~actual).sum())
    fn = int((~predicted & actual).sum())
    rows.append(
        {
            "threshold": threshold,
            "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
            "flagged": int(predicted.sum()),
        }
    )

print(pd.DataFrame(rows).to_string(index=False))

# %% [markdown]
# **Look carefully at that table — it is not the smooth trade-off you expect.**
#
# Recall sits flat at 0.348 from 0.50 all the way to 0.90, then jumps to 0.939
# at 0.95. Precision falls steadily and then *rises* again.
#
# The reason is in the distribution above: injected faults are **bimodal**. The
# 25th percentile of faulted intervals is a peer ratio of 0.0 — total loss —
# while the median is 0.935, barely below healthy. Roughly a third of faults
# stop the inverter; the rest degrade it slightly.
#
# So one threshold cannot work. Anything below 0.9 catches only the total
# losses; only a threshold close to 1.0 reaches the mild ones, and by then it
# is brushing the healthy distribution.
#
# **This is why the naive detector scores 39%, and it is a property of the
# fault population rather than of the threshold.** Two detectors — one for
# outright stoppage, one for sustained mild deviation — will beat any single
# threshold, and that is the design insight worth taking from this notebook.

# %% [markdown]
# ## 6. The faults this feature cannot see
#
# A stuck tracker reduces the irradiance reaching the modules. Output falls —
# but so does POA, so power-per-POA looks normal and the peer ratio barely
# moves.
#
# **No amount of threshold tuning finds these.** They need a different feature.

# %%

tracker_stats = analyst.execute(
    """
    SELECT asset_id,
           stddev(tracker_angle_deg) AS angle_stddev,
           avg(tracker_angle_deg)    AS mean_angle
    FROM inverter_telemetry
    WHERE poa_global > 200
    GROUP BY asset_id
    ORDER BY angle_stddev
    """
).df()

print(tracker_stats.head(8).round(3).to_string(index=False))

# %% [markdown]
# A tracker sweeping ±60° across a day has a large angle standard deviation. A
# small one means it stopped moving.
#
# **This is the lesson of the notebook.** Detection is not one model, it is a
# set of detectors each sensitive to a different failure mode, and knowing
# which mode each one misses is more valuable than any single score.

# %% [markdown]
# ## 7. Where to take this
#
# 1. **Add features and re-score.** Angle variance, temperature residual
#    against block peers, ramp rate, time since sunrise. Then a gradient-boosted
#    classifier, scored the same way.
# 2. **Score events, not intervals.** An operator cares about "did you find the
#    outage", not per-minute accuracy. `northstar-sim score` matches on
#    overlap — mirror that or your numbers will not be comparable.
# 3. **Handle the imbalance deliberately.** Class weights, or threshold moved
#    on a validation split. Never on the test set.
# 4. **Split by time, not at random.** A random split leaks: adjacent minutes
#    of the same outage land on both sides, and the model memorises the event
#    rather than learning the signature.
#
# That last one is the mistake that most often makes a fault detector look
# excellent in backtest and fail in production.

# %%

analyst.close()
truth.close()
