"""Analysis methods for NorthStar datasets.

Reference implementations of the normative definitions in design document
``20``, against which an analyst's own work is scored. Each is tested on data
with a **known injected answer**, which is the only way to distinguish a working
estimator from one producing plausible numbers.
"""

from .methods import (
    ChangePoint,
    DegradationEstimate,
    ExpectedPowerModel,
    ForecastScore,
    build_features,
    daily_performance_index,
    detect_change_points,
    estimate_degradation,
    evaluate_forecast,
    find_dataset,
    fit_expected_power,
    leaking_columns,
    normalize_for_weather,
    open_dataset,
)

__version__ = "0.1.0"

__all__ = [
    "ChangePoint",
    "DegradationEstimate",
    "ExpectedPowerModel",
    "ForecastScore",
    "build_features",
    "daily_performance_index",
    "detect_change_points",
    "estimate_degradation",
    "evaluate_forecast",
    "find_dataset",
    "fit_expected_power",
    "leaking_columns",
    "normalize_for_weather",
    "open_dataset",
    "__version__",
]
