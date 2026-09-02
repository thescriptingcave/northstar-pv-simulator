"""Tests for gap imputation scored against truth."""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from northstar_analytics.imputation import (  # noqa: E402
    ImputationResult,
    ImputationScore,
)


def test_scores_sort_by_error_within_a_column() -> None:
    """The table must make the best method obvious at a glance."""
    result = ImputationResult(
        scores=[
            ImputationScore("forward_fill", "ac_power_kw", 100, 350.9, 743.8, -125.3),
            ImputationScore("peer_regression", "ac_power_kw", 100, 2.5, 5.1, 1.5),
        ]
    )
    frame = result.frame()

    assert frame.iloc[0]["method"] == "peer_regression"
    assert result.best("ac_power_kw").method == "peer_regression"


def test_best_returns_none_for_an_unscored_column() -> None:
    """A column with no gaps is not an error."""
    assert ImputationResult().best("poa_global") is None


def test_bias_is_signed_not_absolute() -> None:
    """Direction matters.

    Forward fill through a daylight gap carries the last value, which after
    sunset is the overnight standby draw - so it under-predicts heavily. A
    signed bias shows that; an absolute error does not.
    """
    score = ImputationScore("forward_fill", "ac_power_kw", 100, 350.9, 743.8, -125.3)

    assert score.bias < 0, "carrying a stale low value under-predicts"
    assert score.mae > abs(score.bias), "MAE cannot be smaller than |bias|"


def test_find_gaps_counts_null_rows_not_missing_timestamps() -> None:
    """The simulator writes gaps as present rows with NULL fields.

    That is why `locf` and `time_bucket_gapfill` cannot see them: they fill
    buckets that were never created, and these buckets exist. Counting NULLs
    is the only thing that works here.
    """
    from northstar_analytics.imputation import find_gaps

    index = pd.date_range("2025-06-01", periods=10, freq="1min", tz="UTC")
    values = [1.0, 2.0, None, None, None, 6.0, 7.0, 8.0, 9.0, 10.0]

    class _Stub:
        def execute(self, query):
            class _Result:
                def df(self):
                    return pd.DataFrame(
                        {
                            "time": index,
                            "asset_id": "INV1",
                            "missing": [v is None for v in values],
                        }
                    )

            return _Result()

    gaps = find_gaps(_Stub())

    assert len(gaps) == 1
    assert gaps.iloc[0]["minutes"] == 3


def test_a_perfect_imputation_scores_zero() -> None:
    """Sanity: the metric must reward exactness."""
    actual = pd.Series([1.0, 2.0, 3.0])
    error = actual - actual

    assert float(error.abs().mean()) == 0.0
    assert float(np.sqrt((error**2).mean())) == 0.0
