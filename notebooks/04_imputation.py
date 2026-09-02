# %% [markdown]
# # 04 - Imputation as a Supervised Problem
#
# Missing data is where most analytics pipelines quietly go wrong. The usual
# fix is a forward fill, chosen because it is one line, and almost never
# measured.
#
# **This dataset lets you measure it.** The analyst tree carries gaps from
# injected communications outages; the truth tree has every value. So every
# missing point has a known correct answer, in physical units, requiring no
# annotation at all.
#
# That is unusual. Most public data gives you either labels or realism, and
# here you have both.

# %%

import pandas as pd
from northstar_analytics import find_dataset, open_dataset, score_imputation
from northstar_analytics.imputation import find_gaps, impute

DATASET = find_dataset("curriculum")
RUN_ID = "curriculum"

analyst = open_dataset(DATASET, RUN_ID, "analyst")
truth = open_dataset(DATASET, RUN_ID, "truth")

# %% [markdown]
# ## 1. Find the gaps
#
# Note what a gap is here: rows that are **present** with NULL fields, not
# missing timestamps.
#
# That distinction matters more than it sounds. TimescaleDB's `locf` and
# `time_bucket_gapfill` fill buckets that *gapfill created* — and these buckets
# already exist, so those functions cannot see them at all. Counting NULLs is
# the only thing that works.

# %%

gaps = find_gaps(analyst)
print(f"{len(gaps)} gaps found\n")
print(gaps.head(10).to_string(index=False))

# %% [markdown]
# ## 2. Look at one before modelling anything
#
# Always plot the thing before you model it. The shape tells you which methods
# have a chance.

# %%

worst = gaps.iloc[0]
window = analyst.execute(
    f"""
    SELECT time, asset_id, ac_power_kw, poa_global
    FROM inverter_telemetry
    WHERE asset_id = '{worst.asset_id}'
      AND time BETWEEN TIMESTAMP '{worst.start}' - INTERVAL '2 hours'
                   AND TIMESTAMP '{worst.end}' + INTERVAL '2 hours'
    ORDER BY time
    """
).df()

print(f"asset {worst.asset_id}, gap of {worst.minutes} minutes")
print(f"  rows in window        {len(window)}")
print(f"  non-null ac_power_kw  {window['ac_power_kw'].notna().sum()}")
print(f"  non-null poa_global   {window['poa_global'].notna().sum()}")

# %% [markdown]
# Every field is null together. A communications outage takes the whole
# telemetry stream, not one channel — so you cannot impute power from that
# inverter's own irradiance. The information has to come from somewhere else.

# %% [markdown]
# ## 3. Where the information actually is
#
# The other 39 inverters were reporting throughout. If they correlate with the
# gapped one, they carry what is missing.
#
# Check that before assuming it.

# %%

wide = (
    analyst.execute("SELECT time, asset_id, ac_power_kw FROM inverter_telemetry")
    .df()
    .pivot_table(index="time", columns="asset_id", values="ac_power_kw")
)

target = worst.asset_id
peers = wide.drop(columns=[target])
observed = wide[target].notna()

correlations = peers[observed].corrwith(wide.loc[observed, target])
print("correlation of the gapped inverter with its peers:")
print(f"  median {correlations.median():.4f}")
print(f"  min    {correlations.min():.4f}")
print(f"  max    {correlations.max():.4f}")

# %% [markdown]
# Correlations near 1.0. That is the whole reason peer methods work here, and
# it is a property of **this domain** rather than a general truth about
# imputation — every asset sees nearly the same sun at nearly the same moment.
#
# An interviewer asking "why did that work?" wants this answer, not the name of
# the algorithm.

# %% [markdown]
# ## 4. Score four methods against truth
#
# * **forward fill** — carry the last value. What most pipelines do.
# * **linear** — interpolate across the gap.
# * **peer median** — the fleet median, rescaled by this asset's usual ratio.
# * **peer regression** — least squares against the peers, fitted only on
#   intervals where the target was reporting.

# %%

result = score_imputation(DATASET, RUN_ID)
scores = result.frame()
print(scores.to_string(index=False))

# %% [markdown]
# ## 5. Read the bias column, not just the error
#
# MAE says how wrong. **Bias says which way**, and that is often the more
# actionable number.

# %%

for column in scores["column"].unique():
    best = result.best(column)
    worst_method = max(
        (s for s in result.scores if s.column == column), key=lambda s: s.mae
    )
    print(
        f"{column:<18} best {best.method:<16} MAE {best.mae:>8.3f}   "
        f"{worst_method.mae / best.mae:>6.1f}x better than {worst_method.method}"
    )

print()
ff = [s for s in result.scores if s.method == "forward_fill"]
for score in ff:
    print(f"forward fill on {score.column:<18} bias {score.bias:>+9.3f}")

# %% [markdown]
# Forward fill's bias is large and **negative** on the power columns. It
# carries whatever the inverter was doing before it went quiet — and outages
# that start near sunset carry the overnight standby draw straight through the
# following daylight.
#
# `sql/timeseries/02_gaps.sql` shows this concretely: −0.7 kW held across three
# hours of full sun.

# %% [markdown]
# ## 6. Does accuracy depend on gap length?
#
# A method that handles five minutes well may fail over three hours. Averaging
# across both hides it.

# %%

imputed, actual = impute(analyst, truth, "ac_power_kw")
errors = pd.DataFrame({name: (values - actual).abs() for name, values in imputed.items()})
errors["time"] = [index[0] for index in actual.index]
errors["asset"] = [index[1] for index in actual.index]

gap_lengths = {(row.asset_id): row.minutes for row in gaps.itertuples()}
errors["gap_minutes"] = errors["asset"].map(gap_lengths)
errors["bucket"] = pd.cut(
    errors["gap_minutes"],
    bins=[0, 30, 90, 180, 10_000],
    labels=["<30 min", "30-90", "90-180", ">180"],
)

print(
    errors.groupby("bucket", observed=True)[
        ["forward_fill", "linear", "peer_median", "peer_regression"]
    ]
    .mean()
    .round(2)
    .to_string()
)

# %% [markdown]
# Forward fill and linear degrade sharply with length — they extrapolate from
# the edges, and the edges get further away. The peer methods barely care,
# because they read the current interval rather than a past one.
#
# **That is the argument for them in production**, and it is stronger than the
# headline MAE.

# %% [markdown]
# ## 7. Where to take this
#
# 1. **Beat peer regression.** Gradient boosting over engineered features —
#    peer median, solar zenith, time of day, the target's historical ratio to
#    its block. Score it the same way. It may not win, and finding that out is
#    a legitimate result.
# 2. **Impute `operating_state`.** Categorical, so a classifier, and the metric
#    changes to accuracy and confusion rather than MAE.
# 3. **Try it with fewer peers.** How many reporting inverters do you need?
#    That question decides whether the method survives a site-wide outage.
# 4. **Then move to fault classification.** `scenario_instances` carries the
#    class label, and `northstar-sim score` gives you a baseline at 39.2%
#    recall and 81.7% precision. Same labelled-truth trick, harder problem.

# %%

analyst.close()
truth.close()
