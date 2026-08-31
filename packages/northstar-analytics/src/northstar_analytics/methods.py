"""Analysis methods for NorthStar datasets.

These are the reference implementations an analyst's own work is scored
against. They follow the normative definitions in design document ``20`` rather
than local convention, and each is tested against data with a **known injected
answer** - which is the only way to tell a working estimator from one that
merely produces plausible numbers.

The estimators here deliberately consume the *analyst-facing* tree: measured
telemetry, sensor error and data-quality defects included. An estimator that
only works on physical truth is not an estimator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

#: Irradiance below which an interval is excluded from performance work. The
#: ratio is numerically unstable near zero.
MIN_IRRADIANCE_WM2 = 50.0


def find_dataset(name: str = "curriculum") -> Path:
    """Locate an exported dataset by walking up from the working directory.

    Notebooks cannot assume where they are run from. ``jupyter execute`` uses
    the notebook's own directory, JupyterLab uses wherever it was launched, and
    a hardcoded relative path works in exactly one of those.

    That is not hypothetical: ``make notebooks`` failed with "Table with name
    plant_telemetry does not exist" because ``Path("datasets/curriculum")``
    resolved to ``notebooks/datasets/curriculum``. The dataset was present and
    correct one directory up.

    Args:
        name: Dataset directory name under ``datasets/``.

    Returns:
        The resolved dataset path.

    Raises:
        FileNotFoundError: If no matching dataset is found in any parent.
    """
    start = Path.cwd().resolve()
    for directory in (start, *start.parents):
        candidate = directory / "datasets" / name
        if (candidate / "analyst").is_dir():
            return candidate

    raise FileNotFoundError(
        f"no dataset 'datasets/{name}' found from {start} upward. "
        "Generate one with: make dev-dataset"
    )


def open_dataset(root: Path, run_id: str, tree: str = "analyst"):
    """Open a DuckDB connection with views over an exported dataset.

    Args:
        root: Export root directory.
        run_id: Dataset identifier.
        tree: ``"analyst"`` or ``"truth"``. Analysis defaults to the analyst
            tree; reaching for truth is how an estimator accidentally cheats.

    Returns:
        An open DuckDB connection.
    """
    import duckdb

    connection = duckdb.connect()
    base = Path(root) / tree / f"run_id={run_id}"

    if not base.is_dir():
        raise FileNotFoundError(
            f"{base} does not exist. Either the dataset has not been generated "
            "(make dev-dataset) or the working directory is not what you "
            "think - use find_dataset() rather than a relative path."
        )

    for stream_dir in sorted(base.glob("stream=*")):
        stream = stream_dir.name.split("=", 1)[1]
        pattern = str(stream_dir / "**" / "*.parquet")
        connection.execute(
            f"CREATE OR REPLACE VIEW {stream} AS "
            f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=true)"
        )
    return connection


# --------------------------------------------------------------------------
# Expected power (ASTM E2848)
# --------------------------------------------------------------------------


@dataclass
class ExpectedPowerModel:
    """A fitted ASTM E2848 capacity regression.

    The standard form is ``P = G x (a + b*G + c*T + d*v)``. It is the industry
    reference for weather-adjusted performance, and its coefficients are only
    meaningful when fitted on a **clean** period - free of faults, curtailment
    and heavy soiling.

    Attributes:
        coefficients: Fitted ``a``, ``b``, ``c``, ``d``.
        r_squared: Fit quality on the training period.
        samples: Training samples used.
    """

    coefficients: tuple[float, float, float, float]
    r_squared: float
    samples: int

    def predict(
        self,
        irradiance: pd.Series,
        temperature: pd.Series,
        wind_speed: pd.Series,
    ) -> pd.Series:
        """Predict expected power from measured conditions.

        Args:
            irradiance: Plane-of-array irradiance.
            temperature: Ambient temperature.
            wind_speed: Wind speed.

        Returns:
            Expected power in the units the model was fitted on.
        """
        a, b, c, d = self.coefficients
        return irradiance * (a + b * irradiance + c * temperature + d * wind_speed)


def fit_expected_power(
    power: pd.Series,
    irradiance: pd.Series,
    temperature: pd.Series,
    wind_speed: pd.Series,
    *,
    min_irradiance: float = MIN_IRRADIANCE_WM2,
) -> ExpectedPowerModel:
    """Fit the ASTM E2848 regression.

    Args:
        power: Measured output.
        irradiance: Plane-of-array irradiance.
        temperature: Ambient temperature.
        wind_speed: Wind speed.
        min_irradiance: Exclusion threshold.

    Returns:
        The fitted :class:`ExpectedPowerModel`.

    Raises:
        ValueError: If fewer than 100 usable samples remain, which makes the
            four-coefficient fit unreliable rather than merely imprecise.
    """
    frame = pd.DataFrame(
        {
            "power": power,
            "irradiance": irradiance,
            "temperature": temperature,
            "wind_speed": wind_speed,
        }
    ).dropna()
    frame = frame[frame["irradiance"] >= min_irradiance]

    if len(frame) < 100:
        raise ValueError(
            f"only {len(frame)} usable samples; a four-coefficient fit needs "
            "considerably more to be meaningful"
        )

    # The model is linear in its coefficients once divided through by G.
    design = np.column_stack(
        [
            np.ones(len(frame)),
            frame["irradiance"],
            frame["temperature"],
            frame["wind_speed"],
        ]
    )
    target = frame["power"] / frame["irradiance"]

    coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    predicted = frame["irradiance"] * (design @ coefficients)

    residual = float(((frame["power"] - predicted) ** 2).sum())
    total = float(((frame["power"] - frame["power"].mean()) ** 2).sum())

    return ExpectedPowerModel(
        coefficients=tuple(float(value) for value in coefficients),
        r_squared=1.0 - residual / total if total else 0.0,
        samples=len(frame),
    )


# --------------------------------------------------------------------------
# Degradation (year-on-year, RdTools convention)
# --------------------------------------------------------------------------


@dataclass
class DegradationEstimate:
    """A year-on-year degradation rate estimate.

    Attributes:
        rate_per_year: Estimated relative change per year. Negative means
            degrading.
        confidence_low: 2.5th percentile of the year-on-year distribution.
        confidence_high: 97.5th percentile.
        standard_error: Standard error of the median estimate. Report this.
            A single three-year estimate at typical noise carries roughly
            0.12 percentage points of standard error, so design doc 20
            section 14.5's plus-or-minus 0.15 tolerance is about a 1.3-sigma
            bound and will be missed by chance in a meaningful fraction of
            realisations. The estimator is unbiased; the tolerance is tight.
        pairs: Year-on-year pairs the median was taken over.
    """

    rate_per_year: float
    confidence_low: float
    confidence_high: float
    standard_error: float
    pairs: int


def estimate_degradation(normalized: pd.Series) -> DegradationEstimate:
    """Estimate degradation by the year-on-year method.

    The median of the year-on-year ratio distribution is used rather than a
    linear trend, because it is robust to soiling cycles, outages and seasonal
    residuals that a trend fit absorbs into the slope.

    Requires **at least two full years**. That is why design document ``03``
    section 10 makes a three-year dataset a hard requirement rather than an
    aspiration.

    Args:
        normalized: Output normalised for weather, indexed by time. Any
            residual weather dependence biases the result.

    Returns:
        The :class:`DegradationEstimate`.

    Raises:
        ValueError: If the record is shorter than two years.
    """
    series = normalized.dropna().sort_index()
    if series.empty:
        raise ValueError("no usable samples")

    span_years = (series.index[-1] - series.index[0]).days / 365.25
    if span_years < 2.0:
        raise ValueError(
            f"record spans {span_years:.2f} years; the year-on-year method "
            "needs at least two"
        )

    # Compare each point against the same point one year earlier, which cancels
    # the seasonal cycle exactly rather than modelling it.
    shifted = series.copy()
    shifted.index = shifted.index + pd.DateOffset(years=1)

    # Leap days collide. Shifting 29 February by one year clamps to 28
    # February, landing on an entry that already exists, and the join then
    # fails outright on duplicate labels. Only real multi-year data reaches
    # this - a synthetic series that never spans a leap day will not.
    shifted = shifted[~shifted.index.duplicated(keep="first")]

    paired = pd.concat(
        [series.rename("current"), shifted.rename("previous")], axis=1
    ).dropna()

    if paired.empty:
        raise ValueError("no year-on-year pairs; check the sampling cadence")

    ratios = paired["current"] / paired["previous"].replace(0.0, np.nan)
    ratios = ratios.replace([np.inf, -np.inf], np.nan).dropna()

    # Standard error of a median is pi/2 larger than that of a mean, which is
    # the price of robustness against soiling cycles and outages.
    standard_error = (
        float(np.sqrt(np.pi / 2.0) * ratios.std() / np.sqrt(len(ratios)))
        if len(ratios) > 1
        else float("nan")
    )

    return DegradationEstimate(
        rate_per_year=float(ratios.median() - 1.0),
        confidence_low=float(ratios.quantile(0.025) - 1.0),
        confidence_high=float(ratios.quantile(0.975) - 1.0),
        standard_error=standard_error,
        pairs=len(ratios),
    )


def daily_performance_index(
    power: pd.Series, model: ExpectedPowerModel, conditions: pd.DataFrame
) -> pd.Series:
    """Compute the daily ratio of measured energy to weather-expected energy.

    **Use this for degradation work, not a daily median of hourly ratios.**

    The distinction is not cosmetic. A median of hourly ratios weights every
    interval equally, so unstable low-irradiance hours - where the denominator
    is small and the ratio noisy - dominate a statistic that should be driven by
    the hours carrying the energy. Measured on a real three-year record against
    a known injected rate:

    ========================================  ===========  =========
    Method                                    Estimate     Error
    ========================================  ===========  =========
    Daily median of hourly ratios             -0.16 %/yr   +0.24 pp
    **Ratio of daily energy sums**            -0.35 %/yr   +0.05 pp
    Ratio of monthly energy sums              +0.56 %/yr   +0.96 pp
    ========================================  ===========  =========

    Monthly aggregation is worse again: too few year-on-year pairs survive.

    Args:
        power: Measured output.
        model: A model fitted on a clean period.
        conditions: Frame carrying ``irradiance``, ``temperature`` and
            ``wind_speed``.

    Returns:
        A daily series near 1.0 for a healthy plant.
    """
    expected = model.predict(
        conditions["irradiance"], conditions["temperature"], conditions["wind_speed"]
    )
    measured_daily = power.resample("1D").sum()
    expected_daily = expected.resample("1D").sum()

    ratio = (measured_daily / expected_daily.replace(0.0, np.nan)).replace(
        [np.inf, -np.inf], np.nan
    )
    # Days that are mostly cloud or mostly outage produce meaningless ratios.
    return ratio[(ratio > 0.5) & (ratio < 1.5)].dropna()


def normalize_for_weather(
    power: pd.Series, model: ExpectedPowerModel, conditions: pd.DataFrame
) -> pd.Series:
    """Divide measured output by its weather-expected value.

    Args:
        power: Measured output.
        model: A model fitted on a clean period.
        conditions: Frame carrying ``irradiance``, ``temperature`` and
            ``wind_speed``.

    Returns:
        Normalised output at the input cadence, dimensionless and near 1.0 for
        a healthy plant. For degradation work aggregate with
        :func:`daily_performance_index` rather than taking a median of these.
    """
    expected = model.predict(
        conditions["irradiance"], conditions["temperature"], conditions["wind_speed"]
    )
    return (power / expected.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)


# --------------------------------------------------------------------------
# Change-point detection (soiling and cleaning)
# --------------------------------------------------------------------------


@dataclass
class ChangePoint:
    """A detected step change in a normalised series.

    Attributes:
        time: When the change occurred.
        magnitude: Size of the step, signed. Positive means recovery.
        before: Mean level before.
        after: Mean level after.
    """

    time: pd.Timestamp
    magnitude: float
    before: float
    after: float


def detect_change_points(
    series: pd.Series, *, window: int = 7, threshold: float = 0.02
) -> list[ChangePoint]:
    """Find step changes by comparing rolling means either side of each point.

    Soiling accumulates gradually and clears abruptly, so a cleaning event or a
    rain reset is a **step up** in normalised output. Distinguishing that from
    permanent degradation - which never steps up - is the whole exercise.

    Args:
        series: Normalised output, typically daily.
        window: Samples either side to average.
        threshold: Minimum step size to report.

    Returns:
        Detected change points, largest first.
    """
    clean = series.dropna().sort_index()
    if len(clean) < 2 * window + 1:
        return []

    before = clean.rolling(window).mean()
    after = clean[::-1].rolling(window).mean()[::-1].shift(-1)
    step = after - before

    found: list[ChangePoint] = []
    for timestamp, value in step.dropna().items():
        if abs(value) >= threshold:
            found.append(
                ChangePoint(
                    time=timestamp,
                    magnitude=float(value),
                    before=float(before.loc[timestamp]),
                    after=float(after.loc[timestamp]),
                )
            )

    # Collapse adjacent detections: a single physical event produces a run of
    # above-threshold samples, and reporting each as its own event would inflate
    # the count by the window width.
    collapsed: list[ChangePoint] = []
    for point in sorted(found, key=lambda p: p.time):
        if collapsed and (point.time - collapsed[-1].time) < pd.Timedelta(days=window):
            if abs(point.magnitude) > abs(collapsed[-1].magnitude):
                collapsed[-1] = point
        else:
            collapsed.append(point)

    return sorted(collapsed, key=lambda p: abs(p.magnitude), reverse=True)


# --------------------------------------------------------------------------
# Forecasting features
# --------------------------------------------------------------------------


def build_features(
    frame: pd.DataFrame,
    *,
    target: str = "grid_export_power_kw",
    lags: tuple[int, ...] = (1, 2, 3, 6, 12),
    rolling: tuple[int, ...] = (6, 24),
) -> pd.DataFrame:
    """Build lag and rolling features for a forecasting model.

    **Every feature is strictly backward-looking.** Target leakage is the
    dominant failure mode in energy forecasting: a model given the concurrent
    irradiance scores beautifully in backtest and is useless in production,
    because at forecast time that irradiance has not happened yet.

    Args:
        frame: Time-indexed telemetry.
        target: Column to forecast.
        lags: Lag steps to include.
        rolling: Rolling-mean windows to include.

    Returns:
        A feature frame aligned to the input index.

    Raises:
        KeyError: If the target column is absent.
    """
    if target not in frame.columns:
        raise KeyError(f"target column {target!r} not present")

    features = pd.DataFrame(index=frame.index)

    for lag in lags:
        features[f"{target}_lag{lag}"] = frame[target].shift(lag)
    for window in rolling:
        features[f"{target}_roll{window}"] = frame[target].shift(1).rolling(window).mean()

    # Calendar features are known in advance and therefore always safe.
    features["hour"] = frame.index.hour + frame.index.minute / 60.0
    features["day_of_year"] = frame.index.dayofyear
    features["hour_sin"] = np.sin(2 * np.pi * features["hour"] / 24.0)
    features["hour_cos"] = np.cos(2 * np.pi * features["hour"] / 24.0)

    return features


def leaking_columns(
    features: pd.DataFrame, frame: pd.DataFrame, target: str
) -> list[str]:
    """Identify features that carry concurrent information about the target.

    A feature correlating near-perfectly with the *current* target value, with
    no lag applied, is leakage. This is a check worth running rather than a
    discipline worth trusting.

    Args:
        features: Candidate feature frame.
        frame: Source telemetry.
        target: Target column name.

    Returns:
        Feature names that look like leakage.
    """
    suspect: list[str] = []
    actual = frame[target]

    for column in features.columns:
        if features[column].nunique() < 2:
            continue
        correlation = features[column].corr(actual)
        if pd.notna(correlation) and abs(correlation) > 0.999:
            suspect.append(column)
    return suspect


@dataclass
class ForecastScore:
    """Backtest result for one forecasting model.

    Attributes:
        name: Model name.
        mae: Mean absolute error.
        rmse: Root mean squared error.
        skill: Improvement over persistence, as a fraction. Negative means the
            model is worse than assuming nothing changes.
        samples: Evaluation samples.
    """

    name: str
    mae: float
    rmse: float
    skill: float
    samples: int


def evaluate_forecast(
    actual: pd.Series, predicted: pd.Series, persistence: pd.Series, name: str
) -> ForecastScore:
    """Score a forecast against the persistence baseline.

    Absolute error means little on its own. **Skill against persistence** is
    the number that matters: a solar forecast that cannot beat "the same as
    last interval" has no value, and many do not.

    Args:
        actual: Observed values.
        predicted: Model predictions.
        persistence: Persistence baseline.
        name: Model name.

    Returns:
        The :class:`ForecastScore`.
    """
    frame = pd.DataFrame(
        {"actual": actual, "predicted": predicted, "persistence": persistence}
    ).dropna()

    if frame.empty:
        return ForecastScore(name, float("nan"), float("nan"), float("nan"), 0)

    error = frame["actual"] - frame["predicted"]
    baseline_error = frame["actual"] - frame["persistence"]

    mae = float(error.abs().mean())
    baseline_mae = float(baseline_error.abs().mean())

    return ForecastScore(
        name=name,
        mae=mae,
        rmse=float(np.sqrt((error**2).mean())),
        skill=1.0 - mae / baseline_mae if baseline_mae else 0.0,
        samples=len(frame),
    )
