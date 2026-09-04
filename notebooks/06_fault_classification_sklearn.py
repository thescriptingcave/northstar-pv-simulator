# %% [markdown]
# # 06 - Fault Classification with scikit-learn
#
# Notebook 05 established *why* a single peer-ratio threshold caps at 39%
# recall: injected faults are **bimodal**. About a third stop the inverter
# outright; the rest degrade it to roughly 0.935 of its block peers. No single
# cut-point spans both.
#
# A tree-based model splits differently in different regions of feature space,
# so it should handle that natively. This notebook tests whether it does.
#
# **The discipline that makes the result meaningful:**
#
# 1. **Split by time, never at random.** Adjacent minutes of one outage would
#    otherwise land on both sides, and the model memorises the event rather
#    than learning its signature.
# 2. **Never tune on the test set.** Threshold selection happens on validation.
# 3. **Report precision and recall, never accuracy.** Positives are 0.17% of
#    rows — predicting "no fault" scores 99.8%.

# %%

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, precision_recall_curve

from northstar_analytics import find_dataset, open_dataset

DATASET = find_dataset("curriculum")
RUN_ID = "curriculum"

analyst = open_dataset(DATASET, RUN_ID, "analyst")
truth = open_dataset(DATASET, RUN_ID, "truth")

# %% [markdown]
# ## 1. Features
#
# Every feature is computable from the analyst tree at the interval in
# question. Nothing here requires knowing the future, and nothing consults the
# truth tree — that constraint is what makes the score honest.

# %%

frame = analyst.execute(
    """
    WITH daylight AS (
        SELECT time, asset_id, substr(asset_id, 1, 14) AS block_id,
               ac_power_kw, dc_power_kw, poa_global, cell_temperature,
               tracker_angle_deg, internal_temp_c
        FROM inverter_telemetry
        WHERE poa_global > 200 AND ac_power_kw IS NOT NULL
    )
    SELECT time, asset_id, ac_power_kw, dc_power_kw, poa_global,
           cell_temperature, tracker_angle_deg, internal_temp_c,
           avg(ac_power_kw)      OVER (PARTITION BY time, block_id) AS block_power,
           avg(cell_temperature) OVER (PARTITION BY time, block_id) AS block_cell,
           avg(tracker_angle_deg) OVER (PARTITION BY time, block_id) AS block_angle
    FROM daylight
    """
).df()
frame["time"] = pd.to_datetime(frame["time"], utc=True)
frame = frame.sort_values(["asset_id", "time"]).reset_index(drop=True)

# Peer-relative features. Absolute power tells you the weather; power relative
# to identical neighbours tells you about the asset.
frame["peer_ratio"] = frame["ac_power_kw"] / frame["block_power"].replace(0, np.nan)
frame["temp_residual"] = frame["cell_temperature"] - frame["block_cell"]
frame["angle_residual"] = frame["tracker_angle_deg"] - frame["block_angle"]
frame["dc_ac_ratio"] = frame["dc_power_kw"] / frame["ac_power_kw"].replace(0, np.nan)
frame["power_per_poa"] = frame["ac_power_kw"] / frame["poa_global"].replace(0, np.nan)

# Short-window behaviour, per asset. A fault has a shape in time as well as a
# level, and a ratio that is low AND falling differs from one that is just low.
grouped = frame.groupby("asset_id")["peer_ratio"]
frame["ratio_change"] = grouped.diff()
frame["ratio_rolling"] = grouped.transform(
    lambda s: s.rolling(15, min_periods=1).mean()
)
frame["ratio_volatility"] = grouped.transform(
    lambda s: s.rolling(15, min_periods=1).std()
)

FEATURES = [
    "peer_ratio",
    "temp_residual",
    "angle_residual",
    "dc_ac_ratio",
    "power_per_poa",
    "ratio_change",
    "ratio_rolling",
    "ratio_volatility",
    "poa_global",
]
print(f"{len(frame):,} intervals, {len(FEATURES)} features")

# %% [markdown]
# ## 2. Labels

# %%

instances = truth.execute(
    'SELECT asset_id, time AS start_time, "end" AS end_time '
    "FROM scenario_instances"
).df()
for column in ("start_time", "end_time"):
    instances[column] = pd.to_datetime(instances[column], utc=True)

frame["is_fault"] = False
for row in instances.itertuples():
    hit = (
        (frame["asset_id"] == row.asset_id)
        & (frame["time"] >= row.start_time)
        & (frame["time"] <= row.end_time)
    )
    frame.loc[hit, "is_fault"] = True

positives = int(frame["is_fault"].sum())
print(f"positives {positives:,} of {len(frame):,} "
      f"({100 * positives / len(frame):.3f}%)")

# %% [markdown]
# ## 3. Split by time
#
# The first 70% of the record trains, the next 15% validates, the last 15%
# tests. A random split would leak: minute *t* and minute *t+1* of the same
# outage are nearly identical rows.
#
# **This is the single most common way a fault detector looks excellent in
# backtest and fails in production.**

# %%

frame = frame.sort_values("time").reset_index(drop=True)
cut_train = frame["time"].quantile(0.70)
cut_valid = frame["time"].quantile(0.85)

train = frame[frame["time"] <= cut_train]
valid = frame[(frame["time"] > cut_train) & (frame["time"] <= cut_valid)]
test = frame[frame["time"] > cut_valid]

for name, part in (("train", train), ("valid", valid), ("test", test)):
    print(f"  {name:<6} {len(part):>8,} rows, {int(part['is_fault'].sum()):>4} positives")

# %% [markdown]
# **Check every split has positives.** On a short record this often fails:
# faults are injected sparsely and cluster in time, so the last 15% of a
# seven-day dataset can easily contain none. Recall is then undefined and every
# metric reads zero.
#
# When that happens the fix is not to reshuffle at random — that reintroduces
# the leak. **Split by asset instead.** Train on some inverters, test on
# others. Events stay whole, no minute appears on both sides, and the model is
# forced to generalise across equipment rather than memorise one outage.
#
# Splitting by entity rather than by time is the right call whenever events are
# rare and time-clustered, and it is worth knowing as a technique in its own
# right.

# %%

if int(test["is_fault"].sum()) == 0 or int(valid["is_fault"].sum()) == 0:
    print("A split has no positives - switching to a split by asset.\n")
    assets = sorted(frame["asset_id"].unique())
    rng = np.random.default_rng(7)
    shuffled = list(rng.permutation(assets))
    n = len(shuffled)
    train_assets = set(shuffled[: int(0.70 * n)])
    valid_assets = set(shuffled[int(0.70 * n) : int(0.85 * n)])
    test_assets = set(shuffled[int(0.85 * n) :])

    train = frame[frame["asset_id"].isin(train_assets)]
    valid = frame[frame["asset_id"].isin(valid_assets)]
    test = frame[frame["asset_id"].isin(test_assets)]

    for name, part in (("train", train), ("valid", valid), ("test", test)):
        print(
            f"  {name:<6} {len(part):>8,} rows, "
            f"{int(part['is_fault'].sum()):>4} positives, "
            f"{part['asset_id'].nunique():>2} assets"
        )

# %% [markdown]
# ## 4. Train
#
# `class_weight="balanced"` matters at 0.17% positives: without it the model
# minimises loss by predicting the majority class everywhere.

# %%

model = HistGradientBoostingClassifier(
    max_iter=200,
    learning_rate=0.1,
    max_depth=6,
    class_weight="balanced",
    random_state=7,
)
model.fit(train[FEATURES], train["is_fault"])

valid_scores = model.predict_proba(valid[FEATURES])[:, 1]
test_scores = model.predict_proba(test[FEATURES])[:, 1]

print(f"validation average precision {average_precision_score(valid['is_fault'], valid_scores):.4f}")
print(f"test       average precision {average_precision_score(test['is_fault'], test_scores):.4f}")

# %% [markdown]
# **Average precision, not ROC AUC.** With 0.17% positives, ROC AUC looks
# impressive for a model that is barely useful — the false-positive rate has a
# huge denominator. Precision-recall is the honest curve under heavy imbalance.

# %% [markdown]
# ## 5. Choose the threshold on validation, apply it to test

# %%

precision, recall, thresholds = precision_recall_curve(
    valid["is_fault"], valid_scores
)
f1 = 2 * precision * recall / np.where(precision + recall == 0, 1, precision + recall)
chosen = thresholds[int(np.nanargmax(f1[:-1]))]
print(f"threshold chosen on validation: {chosen:.4f}")

predicted = test_scores >= chosen
actual = test["is_fault"].to_numpy()
tp = int((predicted & actual).sum())
fp = int((predicted & ~actual).sum())
fn = int((~predicted & actual).sum())

print()
print(f"  test precision {tp / (tp + fp):.1%}" if tp + fp else "  no positives predicted")
print(f"  test recall    {tp / (tp + fn):.1%}" if tp + fn else "  no positives present")
print(f"  tp {tp}   fp {fp}   fn {fn}")

# %% [markdown]
# ## 6. Compare against the naive baseline
#
# The number that matters. `northstar-sim score` gets 39.2% recall at 81.7%
# precision on a full year with a fixed threshold.
#
# Run the same peer-ratio rule on this test set so the comparison is like for
# like.

# %%

naive = test["peer_ratio"] < 0.75
tp_n = int((naive & actual).sum())
fp_n = int((naive & ~actual).sum())
fn_n = int((~naive & actual).sum())

print("naive peer-ratio threshold on the same test set:")
print(f"  precision {tp_n / (tp_n + fp_n):.1%}" if tp_n + fp_n else "  none flagged")
print(f"  recall    {tp_n / (tp_n + fn_n):.1%}" if tp_n + fn_n else "  no positives")

# %% [markdown]
# ### Read this honestly
#
# On this seven-day dataset **the naive threshold beats the model**: 100%
# precision and 95.2% recall against 53.8% and 100%.
#
# That is the correct result to report, and the temptation to bury it is worth
# resisting. Three things are true at once:
#
# 1. **21 positives in the test set is far too few to conclude anything.** The
#    difference between these two numbers is a handful of rows.
# 2. **The model traded precision for recall** — it found every fault and
#    flagged 18 healthy intervals doing it. Whether that is better depends
#    entirely on the cost of a false alarm, which is an operations question,
#    not a modelling one.
# 3. **A gradient-boosted model on 154 training positives will overfit.** It
#    has more capacity than the data supports.
#
# The honest conclusion is *"insufficient evidence"*, not *"the model is
# worse"*. Re-run on a full year — `generate --real --year 2025` gives roughly
# 400 injected instances instead of 7 — and the comparison becomes meaningful.
#
# **An interviewer will trust you more for saying this than for a number that
# happens to be higher.** Being able to recognise when your own result does not
# support a claim is the skill being tested.

# %% [markdown]
# ## 7. What the model learned

# %%

from sklearn.inspection import permutation_importance  # noqa: E402

importance = permutation_importance(
    model, test[FEATURES], test["is_fault"], n_repeats=5, random_state=7,
    scoring="average_precision",
)
ranked = (
    pd.DataFrame({"feature": FEATURES, "importance": importance.importances_mean})
    .sort_values("importance", ascending=False)
)
print(ranked.round(4).to_string(index=False))

# %% [markdown]
# **Permutation importance on the test set, not the training set.** Training
# importance rewards features the model overfit to.
#
# If `angle_residual` ranks highly, the model found the stuck trackers that
# peer-ratio alone cannot see — which was the whole argument for adding more
# features.

# %% [markdown]
# ## 8. Honest limitations
#
# - **A seven-day dataset is thin for this.** Few injected instances, and the
#   time split may leave a class absent from test. Re-run on a full year via
#   `generate --real` before believing any number here.
# - **Interval-level scoring is not event-level.** An operator cares whether
#   the outage was found, not per-minute accuracy. `northstar-sim score`
#   matches on overlap; mirror that before comparing.
# - **The model can only find what was injected.** Recall is measured against
#   nine scenario classes, not against every way a real plant fails.
#
# ## Where to take it
#
# 1. Re-run on a full simulated year and compare against the 39.2% baseline
#    properly.
# 2. Aggregate interval predictions into events, then score with
#    `score_detection` from `northstar_sim.scoring`.
# 3. Add a second model specialised for mild degradation, and combine — the
#    bimodality in notebook 05 argues for two detectors rather than one.

# %%

analyst.close()
truth.close()
