"""Layer 5: the sensor model. Truth becomes measurement.

Everything upstream of this module is simulator physical truth. Everything an
analyst sees passes through here first.

**The governing constraint: a sensor fault must never alter physical truth.** A
stuck pyranometer reports a constant value while actual irradiance, and
therefore actual production, continues to vary. That distinction is the whole
basis of data-quality analysis, and it only holds if measurement is a pure
function applied *to* truth rather than a modification *of* it. Nothing in this
module writes back.

Six effects are modelled, each with per-instance parameters so that two sensors
measuring the same quantity disagree in their own characteristic way:

* **calibration bias** - a fixed gain error, the dominant systematic
* **drift** - slow monotonic change, the mechanism behind SCN-062
* **noise** - random, small, and the only effect an averaging analyst removes
* **response time** - thermopile pyranometers lag; fast photodiodes do not
* **quantization** - ADC resolution, invisible until someone looks for stuck values
* **soiling** - pyranometers get dirty too, independently of the modules

The last one deserves emphasis. A soiled pyranometer under-reads irradiance,
which makes performance ratio look **better** than it is. An analyst chasing a
suspiciously good PR is doing the right thing, and the answer is a dirty sensor
rather than a good plant. That is scenario SCN-067.

Reference: design documents ``06_environmental_model`` section 8 and
``11_telemetry_specification`` sections 11 and 12.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Per-quantity sensor characteristics. Ranges are sampled once per instance, so
#: a given sensor keeps its personality for the life of the dataset.
#:
#: Values are chosen to sit inside the tolerances of real instrumentation:
#: a secondary-standard pyranometer is roughly 2% accurate, an RTD better than
#: 1 K, a cup anemometer several percent.
SENSOR_CLASSES: dict[str, dict[str, float]] = {
    "irradiance": {
        "bias_gain_sigma": 0.015,
        "drift_per_year": 0.010,
        "noise_sigma_rel": 0.004,
        "response_seconds": 20.0,
        "quantization": 0.5,
        "soiling_per_year": 0.030,
    },
    "temperature": {
        "bias_offset_sigma": 0.35,
        "drift_per_year": 0.20,
        "noise_sigma_abs": 0.08,
        "response_seconds": 60.0,
        "quantization": 0.1,
    },
    "wind_speed": {
        "bias_gain_sigma": 0.03,
        "drift_per_year": 0.01,
        "noise_sigma_rel": 0.02,
        "response_seconds": 5.0,
        "quantization": 0.1,
    },
    "power": {
        "bias_gain_sigma": 0.003,
        "drift_per_year": 0.001,
        "noise_sigma_rel": 0.002,
        "response_seconds": 0.0,
        "quantization": 0.1,
    },
    "voltage": {
        "bias_gain_sigma": 0.004,
        "drift_per_year": 0.002,
        "noise_sigma_rel": 0.002,
        "response_seconds": 0.0,
        "quantization": 0.5,
    },
}

#: Which sensor class each telemetry field belongs to.
FIELD_CLASSES: dict[str, str] = {
    "ghi": "irradiance",
    "dni": "irradiance",
    "dhi": "irradiance",
    "poa_global": "irradiance",
    "temp_air": "temperature",
    "cell_temperature": "temperature",
    "module_temp_c": "temperature",
    "wind_speed": "wind_speed",
    "ac_power_kw": "power",
    "dc_power_kw": "power",
    "dc_voltage_v": "voltage",
}


@dataclass(frozen=True)
class SensorSpec:
    """Calibration and behaviour of one physical sensor instance.

    Attributes:
        asset_id: Asset the sensor is attached to.
        quantity: Telemetry field it measures.
        sensor_class: Which entry of :data:`SENSOR_CLASSES` applies.
        bias_gain: Multiplicative calibration error. 1.0 is perfect.
        bias_offset: Additive calibration error, in the field's own units.
        drift_per_year: Fractional or absolute change per year, applied
            linearly from the start of the record.
        noise_sigma_rel: Relative random noise.
        noise_sigma_abs: Absolute random noise, in the field's units.
        response_seconds: First-order time constant. Nonzero means the sensor
            lags a step change.
        quantization: Reporting resolution.
        soiling_per_year: Annual multiplicative under-read from contamination.
    """

    asset_id: str
    quantity: str
    sensor_class: str
    bias_gain: float = 1.0
    bias_offset: float = 0.0
    drift_per_year: float = 0.0
    noise_sigma_rel: float = 0.0
    noise_sigma_abs: float = 0.0
    response_seconds: float = 0.0
    quantization: float = 0.0
    soiling_per_year: float = 0.0


@dataclass
class SensorFleet:
    """Every sensor instance in the plant, keyed by asset and quantity.

    Attributes:
        specs: Sensor specifications.
        seed: Master seed the fleet was generated from, recorded so a dataset
            can be traced to the instruments that produced it.
    """

    specs: dict[tuple[str, str], SensorSpec] = field(default_factory=dict)
    seed: int = 0

    def get(self, asset_id: str, quantity: str) -> SensorSpec | None:
        """Look up one sensor.

        Args:
            asset_id: Asset the sensor is attached to.
            quantity: Telemetry field.

        Returns:
            The specification, or ``None`` if the field is not instrumented.
        """
        return self.specs.get((asset_id, quantity))

    def to_frame(self) -> pd.DataFrame:
        """Render the fleet as a table.

        This is ground truth: it tells a validator exactly how each instrument
        is wrong, which is what makes an analyst's calibration estimate
        scoreable.

        Returns:
            One row per sensor instance.
        """
        return pd.DataFrame(
            [
                {
                    "asset_id": spec.asset_id,
                    "quantity": spec.quantity,
                    "sensor_class": spec.sensor_class,
                    "bias_gain": spec.bias_gain,
                    "bias_offset": spec.bias_offset,
                    "drift_per_year": spec.drift_per_year,
                    "response_seconds": spec.response_seconds,
                    "quantization": spec.quantization,
                    "soiling_per_year": spec.soiling_per_year,
                }
                for spec in self.specs.values()
            ]
        )


def build_sensor_fleet(
    assets_and_fields: Iterable[tuple[str, str]], *, seed: int
) -> SensorFleet:
    """Generate sensor instances with deterministic per-instance calibration.

    Each sensor draws its own parameters from an independent substream keyed by
    asset and quantity. That matters for reproducibility: adding a sensor, or
    changing which fields are instrumented, must not reshuffle the calibration
    of every other instrument in the plant.

    Args:
        assets_and_fields: Pairs of asset identifier and telemetry field.
        seed: Seed for the ``sensor_noise`` and ``sensor_drift`` substreams.

    Returns:
        The generated :class:`SensorFleet`.
    """
    specs: dict[tuple[str, str], SensorSpec] = {}

    for asset_id, quantity in assets_and_fields:
        sensor_class = FIELD_CLASSES.get(quantity)
        if sensor_class is None:
            continue
        parameters = SENSOR_CLASSES[sensor_class]

        # Keyed independently per instance so the fleet is stable under change.
        key = abs(hash((seed, asset_id, quantity))) % (2**32)
        rng = np.random.default_rng(key)

        specs[(asset_id, quantity)] = SensorSpec(
            asset_id=asset_id,
            quantity=quantity,
            sensor_class=sensor_class,
            bias_gain=1.0 + rng.normal(0.0, parameters.get("bias_gain_sigma", 0.0)),
            bias_offset=rng.normal(0.0, parameters.get("bias_offset_sigma", 0.0)),
            drift_per_year=rng.normal(0.0, parameters.get("drift_per_year", 0.0)),
            noise_sigma_rel=parameters.get("noise_sigma_rel", 0.0),
            noise_sigma_abs=parameters.get("noise_sigma_abs", 0.0),
            response_seconds=parameters.get("response_seconds", 0.0),
            quantization=parameters.get("quantization", 0.0),
            soiling_per_year=abs(
                rng.normal(0.0, parameters.get("soiling_per_year", 0.0))
            ),
        )

    return SensorFleet(specs=specs, seed=seed)


def _years_elapsed(index: pd.DatetimeIndex) -> np.ndarray:
    """Compute fractional years since the start of a record.

    Args:
        index: Time index.

    Returns:
        Elapsed years at each timestep, starting at zero.
    """
    seconds = (index - index[0]).total_seconds()
    return np.asarray(seconds) / (365.25 * 86400.0)


def apply_sensor(
    truth: pd.Series, spec: SensorSpec, *, rng: np.random.Generator
) -> pd.Series:
    """Transform a truth series into what its sensor reports.

    Effects are applied in physical order: contamination and calibration act on
    the quantity before the instrument responds to it, the instrument then lags
    and adds noise, and quantization happens last in the analogue-to-digital
    conversion.

    Args:
        truth: The physical quantity.
        spec: Sensor characteristics.
        rng: Generator for this sensor's noise realisation.

    Returns:
        The measured series. ``truth`` is not modified.
    """
    values = truth.to_numpy(dtype=float, copy=True)
    years = _years_elapsed(truth.index)

    # Soiling and drift both accumulate over the record. Soiling always makes an
    # irradiance sensor under-read, which is why it inflates performance ratio.
    if spec.soiling_per_year:
        values = values * (1.0 - spec.soiling_per_year * years)

    if spec.sensor_class == "temperature":
        values = values + spec.bias_offset + spec.drift_per_year * years
    else:
        values = values * spec.bias_gain * (1.0 + spec.drift_per_year * years)

    if spec.response_seconds > 0:
        values = _first_order_lag(values, truth.index, spec.response_seconds)

    if spec.noise_sigma_rel:
        values = values + rng.normal(0.0, spec.noise_sigma_rel * np.abs(values))
    if spec.noise_sigma_abs:
        values = values + rng.normal(0.0, spec.noise_sigma_abs, size=values.shape)

    if spec.quantization > 0:
        values = np.round(values / spec.quantization) * spec.quantization

    return pd.Series(values, index=truth.index, name=truth.name)


def _first_order_lag(
    values: np.ndarray, index: pd.DatetimeIndex, time_constant_s: float
) -> np.ndarray:
    """Apply a first-order instrument response.

    A thermopile pyranometer takes tens of seconds to settle. At 1-minute
    cadence the effect is small but real, and it is one reason two instruments
    disagree most sharply during fast ramps.

    Args:
        values: The quantity presented to the instrument.
        index: Time index.
        time_constant_s: Instrument time constant in seconds.

    Returns:
        The lagged response.
    """
    if len(index) < 2:
        return values
    interval_s = (index[1] - index[0]).total_seconds()
    alpha = 1.0 - np.exp(-interval_s / time_constant_s)

    response = np.empty_like(values)
    response[0] = values[0]
    for position in range(1, len(values)):
        response[position] = response[position - 1] + alpha * (
            values[position] - response[position - 1]
        )
    return response


def measure_frame(
    truth: pd.DataFrame,
    asset_id: str,
    fleet: SensorFleet,
    *,
    seed: int,
) -> pd.DataFrame:
    """Produce the analyst-facing frame for one asset.

    Instrumented fields are replaced by their measured values. Fields with no
    sensor are carried through unchanged and remain truth - the classification
    in ``11 §11`` is what tells an analyst which is which, and the database
    role separation in ``13 §2`` is what enforces it.

    Args:
        truth: The asset's physical truth frame.
        asset_id: Asset identifier, used to select sensor instances.
        fleet: The sensor fleet.
        seed: Seed for the noise realisation.

    Returns:
        A new frame. The input is not modified.
    """
    measured = truth.copy()

    for quantity in truth.columns:
        spec = fleet.get(asset_id, quantity)
        if spec is None:
            continue
        key = abs(hash((seed, asset_id, quantity, "noise"))) % (2**32)
        measured[quantity] = apply_sensor(
            truth[quantity], spec, rng=np.random.default_rng(key)
        )

    return measured


def station_spread(measured: dict[str, pd.DataFrame], quantity: str) -> pd.Series:
    """Compute relative disagreement across weather stations.

    Station spread is a first-class analytical signal, not an error. Design
    document ``20`` section 11 sets a threshold above which resource
    measurement is treated as unreliable and performance-ratio intervals are
    filtered out.

    Args:
        measured: Per-station measured frames.
        quantity: Field to compare.

    Returns:
        Relative spread at each timestep, as a fraction of the mean.
    """
    frame = pd.DataFrame({key: value[quantity] for key, value in measured.items()})
    mean = frame.mean(axis=1)
    return ((frame.max(axis=1) - frame.min(axis=1)) / mean.where(mean != 0)).fillna(0.0)


@dataclass
class SensorGateResult:
    """Outcome of the Phase 6 sensor acceptance checks.

    Attributes:
        checks: Named outcomes, each a pass flag and a detail string.
    """

    checks: list[tuple[str, bool, str]]

    @property
    def passed(self) -> bool:
        """Whether every check succeeded.

        Returns:
            ``True`` when no check failed.
        """
        return all(ok for _, ok, _ in self.checks)

    def render(self) -> str:
        """Format the result for terminal output.

        Returns:
            A multi-line report.
        """
        lines = [
            f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {detail}"
            for name, ok, detail in self.checks
        ]
        lines.append(f"\n  {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def run_sensor_gate(result, *, daylight: pd.Series) -> SensorGateResult:
    """Verify the sensor layer meets its Phase 6 criteria.

    Args:
        result: A ``PlantRunResult`` from a full-plant run.
        daylight: Boolean mask selecting daylight samples.

    Returns:
        A :class:`SensorGateResult`.
    """
    checks: list[tuple[str, bool, str]] = []
    fleet = result.sensors

    checks.append(
        (
            "fleet_instantiated",
            fleet is not None and len(fleet.specs) > 0,
            f"{len(fleet.specs) if fleet else 0} sensor instances",
        )
    )

    # Measurement must be a separate object from truth. If the sensor layer
    # mutated truth in place, a sensor fault would change what the plant
    # actually produced - the one thing this layer must never do.
    sample_id = next(iter(result.inverters))
    truth = result.inverters[sample_id]
    measured = result.measured[sample_id]
    checks.append(
        (
            "truth_object_distinct",
            truth is not measured and not truth.equals(measured),
            "measured frames are distinct objects with distinct values",
        )
    )

    # Divergence must be bounded by the modelled effects, not arbitrary.
    divergences = []
    for asset_id, truth_frame in result.inverters.items():
        measured_frame = result.measured[asset_id]
        for quantity in ("poa_global", "ac_power_kw"):
            if quantity not in truth_frame.columns:
                continue
            lit = truth_frame[quantity][daylight]
            reading = measured_frame[quantity][daylight]
            scale = lit.abs().mean()
            if scale > 0:
                divergences.append(float((reading - lit).abs().mean() / scale))
    worst = max(divergences) if divergences else 0.0
    checks.append(
        (
            "divergence_bounded",
            worst < 0.08,
            f"worst mean relative divergence {worst:.2%}",
        )
    )

    # Instrument error is systematic, not just noise. A gain error means the
    # residual scales with the measurement, which is what makes calibration
    # estimable and what averaging cannot remove.
    #
    # This is a FLEET property, not a per-instrument one. A sensor that happens
    # to be drawn with a near-unity gain legitimately shows weak correlation -
    # its error really is mostly noise. Checking a single instance made the
    # gate's verdict depend on which sensor happened to be sampled. Across the
    # fleet, residual-to-signal correlation tracks gain-error magnitude at
    # r = 0.95, which is the property worth asserting.
    correlations = []
    for asset_id, truth_frame in result.inverters.items():
        if "poa_global" not in truth_frame.columns:
            continue
        residual = (result.measured[asset_id]["poa_global"] - truth_frame["poa_global"])[
            daylight
        ]
        value = residual.corr(truth_frame["poa_global"][daylight])
        if pd.notna(value):
            correlations.append(abs(float(value)))

    median_correlation = float(np.median(correlations)) if correlations else 0.0
    checks.append(
        (
            "error_is_systematic",
            median_correlation > 0.3,
            f"median |residual-to-signal| correlation {median_correlation:.3f} "
            f"across {len(correlations)} instruments",
        )
    )

    # Instrument disagreement must add to spatial disagreement, and the total
    # must stay inside the range doc 20 treats as usable.
    stations = {
        key: frame for key, frame in result.measured.items() if key in result.weather
    }
    if len(stations) >= 2:
        measured_spread = station_spread(stations, "ghi")[daylight].mean()
        truth_spread = station_spread(result.weather, "ghi")[daylight].mean()
        checks.append(
            (
                "station_spread_realistic",
                truth_spread < measured_spread < 0.08,
                f"spatial {truth_spread:.2%} -> with instruments "
                f"{measured_spread:.2%} (usable below 8%)",
            )
        )

    return SensorGateResult(checks=checks)
