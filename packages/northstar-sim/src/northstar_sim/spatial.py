"""Layer 3: the advected spatial cloud field.

This layer is load-bearing, not a realism refinement. Without it every asset
sees identical irradiance, and under that assumption:

* inverter peer comparison degrades to comparing identical inputs
* plant aggregate is exactly proportional to any single asset
* weather-station comparison has nothing to compare
* cloud-passage ramp analysis has no spatial signature

Roughly half the analyses in design document ``02`` section 5 become trivial or
meaningless. The requirement comes from the analytical contract, not from
aesthetics.

**Mechanism.** A stationary, spatially correlated cloud transmissivity field is
generated once and *advected* across the site at the prevailing wind. An asset
at position ``r`` samples the field at a cloud-frame coordinate that depends on
its position projected onto the wind direction, so a cloud edge reaches
downwind assets later than upwind ones.

Two coordinates are used. The along-wind coordinate carries the advection and
therefore the lag; the cross-wind coordinate carries decorrelation without lag.
Both matter: pure time-shifting of one series would make every asset see an
*identical* series at a different time, giving a correlation of exactly 1.0 at
the right lag, which is not what a real plant looks like.

Reference: design document ``06_environmental_model`` section 5.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .assets import Asset, Position
from .plant_config import PlantConfig
from .resource import KT_CEILING, variability_sigma

#: Cloud structure is multi-scale, and a single correlation length cannot
#: represent it. A field built at one scale of 1.5 km leaves inverters 155 m
#: apart correlated at r > 0.999 - technically "not identical", but with no
#: usable structure for peer comparison, which is the point of having the layer.
#:
#: Each entry is (correlation length in metres, share of variance). Small scales
#: decorrelate neighbouring inverters; large scales carry the coherent cloud
#: edges that produce measurable advection lag across the site.
CORRELATION_SCALES_M: tuple[tuple[float, float], ...] = (
    (150.0, 0.30),
    (700.0, 0.35),
    (2500.0, 0.35),
)

#: Grid resolution, metres. Must be well below the shortest correlation length
#: or the smallest scale is undersampled and degenerates into white noise.
GRID_RESOLUTION_M = 50.0

#: Largest along-wind extent the field is materialised over, metres.
#:
#: The field is advected past the plant, so a naive implementation sizes the
#: grid to the total distance travelled: at 4 m/s that is 126,000 km over a
#: year, which is 2.5 million columns and 5.3 GiB. Beyond this cap the
#: coordinate **wraps** rather than clipping. Clipping would be far worse than
#: a repeat - every sample past the edge would return an identical value, so a
#: multi-year run would show one frozen cloud pattern for most of its length.
#:
#: The default corresponds to roughly a month of advection at typical wind. A
#: repeat at that period is undetectable in any analysis the dataset supports.
MAX_ALONG_EXTENT_M = 10_000_000.0

#: Wind speeds below this are treated as this value when computing advection
#: lag. At genuinely calm speeds the lag diverges, and a cloud field that takes
#: an hour to cross the site is neither realistic nor useful.
MIN_ADVECTION_WIND_MS = 1.5

#: Scale factor applied to the variability envelope when generating spatial
#: perturbations. The field modulates around the plant-average resource rather
#: than replacing it, so its amplitude is a fraction of temporal variability.
SPATIAL_AMPLITUDE = 0.55


@dataclass
class CloudField:
    """A frozen, spatially correlated transmissivity field.

    The field is generated in a cloud-fixed frame and advected past the plant,
    which is the standard "frozen turbulence" idealisation. It is a good
    approximation over the minutes a cloud takes to cross a site.

    Attributes:
        values: Field samples on an ``(along, across)`` grid, zero-mean.
        along_origin_m: Along-wind coordinate of the first grid column.
        across_origin_m: Cross-wind coordinate of the first grid row.
        resolution_m: Grid spacing.
    """

    values: np.ndarray
    along_origin_m: float
    across_origin_m: float
    resolution_m: float

    def sample(self, along_m: np.ndarray, across_m: np.ndarray) -> np.ndarray:
        """Sample the field at arbitrary cloud-frame coordinates.

        Uses nearest-neighbour lookup with edge clamping. At the grid resolution
        used here that is well below the correlation length, so the smoothing
        error is small compared with the structure being represented.

        Args:
            along_m: Along-wind coordinates.
            across_m: Cross-wind coordinates.

        Returns:
            Field values at the requested coordinates.
        """
        rows, columns = self.values.shape

        # Along-wind wraps. The field is stationary, so a repeat is
        # statistically indistinguishable from fresh structure - whereas
        # clipping returns one frozen pattern for every sample past the edge.
        along_index = np.mod(
            np.round((along_m - self.along_origin_m) / self.resolution_m).astype(int),
            columns,
        )
        # Cross-wind clips. Assets never leave the site, so the grid is sized to
        # contain them and clipping cannot bind in practice.
        across_index = np.clip(
            np.round((across_m - self.across_origin_m) / self.resolution_m).astype(int),
            0,
            rows - 1,
        )
        return self.values[across_index, along_index]


def _gaussian_kernel(sigma_cells: float, *, max_radius: int | None = None) -> np.ndarray:
    """Build a normalised 1-D Gaussian smoothing kernel.

    The radius is capped so the kernel can never exceed the array it smooths.
    ``numpy.convolve`` in ``same`` mode returns ``max(len(a), len(kernel))``
    rather than ``len(a)``, so an over-long kernel silently changes the array
    shape instead of raising.

    Args:
        sigma_cells: Standard deviation in grid cells.
        max_radius: Largest permitted half-width, in cells.

    Returns:
        A kernel spanning at most three standard deviations either side.
    """
    radius = max(1, int(np.ceil(3.0 * sigma_cells)))
    if max_radius is not None:
        radius = max(1, min(radius, max_radius))
    offsets = np.arange(-radius, radius + 1)
    kernel = np.exp(-0.5 * (offsets / sigma_cells) ** 2)
    return kernel / kernel.sum()


def generate_cloud_field(
    *,
    along_min_m: float,
    along_max_m: float,
    across_half_width_m: float,
    seed: int,
    scales: tuple[tuple[float, float], ...] = CORRELATION_SCALES_M,
    resolution_m: float = GRID_RESOLUTION_M,
) -> CloudField:
    """Generate a multi-scale, spatially correlated, zero-mean cloud field.

    Each scale is white noise smoothed with a Gaussian kernel, giving an
    approximately Gaussian covariance at that correlation length. Scales are
    combined by variance share, producing a field with structure across the
    full range from individual cumulus to synoptic cloud banks.

    The result is rescaled to unit variance so downstream amplitude control is
    independent of grid size and of the scale mixture.

    Args:
        along_min_m: Lowest along-wind coordinate any asset will sample.
        along_max_m: Highest along-wind coordinate any asset will sample. This
            spans the total distance the field travels past the plant over the
            window, not the plant's own size.
        across_half_width_m: Half-width needed perpendicular to the wind,
            measured from the site origin.
        seed: Seed for the ``cloud_field`` substream.
        scales: Correlation lengths and their variance shares.
        resolution_m: Grid spacing.

    Returns:
        The generated :class:`CloudField`.
    """
    extent = min(along_max_m - along_min_m, MAX_ALONG_EXTENT_M)
    columns = max(8, int(np.ceil(extent / resolution_m)) + 1)
    rows = max(8, int(np.ceil(2.0 * across_half_width_m / resolution_m)) + 1)

    rng = np.random.default_rng(seed)
    combined = np.zeros((rows, columns))

    for correlation_length_m, weight in scales:
        noise = rng.standard_normal((rows, columns))
        sigma_cells = max(0.6, correlation_length_m / resolution_m)

        kernel = _gaussian_kernel(sigma_cells, max_radius=(columns - 1) // 2)
        smoothed = np.apply_along_axis(
            lambda row, k=kernel: np.convolve(row, k, mode="same"), axis=1, arr=noise
        )
        # Cross-wind structure is coarser than along-wind: cloud streets
        # elongate with the flow, so the perpendicular scale is larger.
        cross_kernel = _gaussian_kernel(
            max(0.6, sigma_cells * 1.5), max_radius=(rows - 1) // 2
        )
        smoothed = np.apply_along_axis(
            lambda column, k=cross_kernel: np.convolve(column, k, mode="same"),
            axis=0,
            arr=smoothed,
        )

        spread = smoothed.std()
        if spread > 0:
            combined += np.sqrt(weight) * smoothed / spread

    spread = combined.std()
    smoothed = combined / spread if spread > 0 else combined

    return CloudField(
        values=smoothed,
        along_origin_m=along_min_m,
        across_origin_m=-across_half_width_m,
        resolution_m=resolution_m,
    )


def cloud_travel_distance(
    wind_speed: pd.Series, *, min_speed_ms: float = MIN_ADVECTION_WIND_MS
) -> pd.Series:
    """Integrate wind speed to a cumulative cloud displacement.

    This is the coordinate that carries the field past the plant. Using
    cumulative displacement rather than a fixed lag means a change in wind speed
    correctly changes how fast structure arrives.

    Args:
        wind_speed: Wind speed in metres per second, indexed by time.
        min_speed_ms: Floor applied before integration.

    Returns:
        Cumulative displacement in metres, starting at zero.
    """
    speed = wind_speed.clip(lower=min_speed_ms)
    seconds = speed.index.to_series().diff().dt.total_seconds().fillna(0.0)
    return (speed * seconds).cumsum()


def asset_coordinates(
    position: Position, wind_direction_deg: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """Project an asset position onto the wind and cross-wind axes.

    Args:
        position: Asset location in the site frame, metres east and north.
        wind_direction_deg: Meteorological wind direction, the compass bearing
            the wind blows *from*.

    Returns:
        A tuple of along-wind and cross-wind projections in metres. The
        along-wind value is negative for assets the wind reaches first, so
        subtracting it from cloud displacement yields an earlier field sample.
    """
    # Meteorological convention: 270 degrees means wind from the west, blowing
    # toward the east. The unit vector points in the direction of travel.
    radians = np.radians(wind_direction_deg.to_numpy())
    east = -np.sin(radians)
    north = -np.cos(radians)

    along = position.x_m * east + position.y_m * north
    across = -position.x_m * north + position.y_m * east

    index = wind_direction_deg.index
    return pd.Series(along, index=index), pd.Series(across, index=index)


def apply_spatial_field(
    base: pd.DataFrame,
    asset: Asset,
    field: CloudField,
    travel: pd.Series,
    *,
    amplitude: float = SPATIAL_AMPLITUDE,
    smoothing_minutes: float = 0.0,
) -> pd.DataFrame:
    """Produce one asset's resource series from the plant-average series.

    The field modulates the clear-sky index multiplicatively, with amplitude
    conditioned on the same variability envelope used for temporal downscaling.
    Clear skies stay clear everywhere; broken cloud is where assets diverge.

    Args:
        base: Plant-average 1-minute resource, carrying ``clearsky_index``,
            ``clearsky_ghi``, ``solar_zenith`` and optionally ``wind_speed``.
        asset: The asset to produce a series for. Must carry a position.
        field: The generated cloud field.
        travel: Cumulative cloud displacement from
            :func:`cloud_travel_distance`.
        amplitude: Scale factor on the spatial perturbation.
        smoothing_minutes: Temporal smoothing applied afterwards, representing
            the asset's own footprint. A power block averages over more ground
            than a pyranometer and therefore sees a smoother series.

    Returns:
        A resource frame for this asset with the same columns as ``base``.

    Raises:
        ValueError: If the asset has no position.
    """
    if asset.position is None:
        raise ValueError(f"{asset.asset_id} has no position; cannot place it")

    direction = base.get("wind_direction", pd.Series(225.0, index=base.index))
    along, across = asset_coordinates(asset.position, direction)

    perturbation = field.sample((travel - along).to_numpy(), across.to_numpy())

    # The perturbation replaces the plant-average one rather than adding to it.
    #
    # An earlier version modulated the already-perturbed plant-average series.
    # That series is identical at every asset, so it dominated every
    # correlation and no advection lag could appear no matter how the field was
    # built: measured lag was 0 minutes where geometry predicted 6.8. The
    # temporal variability an asset sees must come *from* the advected field,
    # which is what makes a cloud edge arrive at different assets at different
    # times.
    envelope = base["kt_envelope"] if "kt_envelope" in base else base["clearsky_index"]
    sigma = variability_sigma(envelope.to_numpy())
    factor = 1.0 + amplitude * sigma * perturbation

    kt = np.clip(envelope.to_numpy() * factor, 0.0, KT_CEILING)
    kt_series = pd.Series(kt, index=base.index)

    if smoothing_minutes > 0:
        window = max(1, int(round(smoothing_minutes)))
        kt_series = kt_series.rolling(window, center=True, min_periods=1).mean()

    return _rebuild_irradiance(base, kt_series)


def _rebuild_irradiance(base: pd.DataFrame, kt: pd.Series) -> pd.DataFrame:
    """Reconstruct irradiance components from a modified clear-sky index.

    Components are re-derived rather than scaled, so the closure relationship
    between global, direct and diffuse continues to hold at every asset.

    Args:
        base: Plant-average resource frame.
        kt: Modified clear-sky index for this asset.

    Returns:
        The asset's resource frame.
    """
    import pvlib

    ghi = kt * base["clearsky_ghi"]
    components = pvlib.irradiance.erbs(ghi, base["solar_zenith"], base.index)

    result = base.copy()
    result["ghi"] = ghi
    result["dni"] = components["dni"]
    result["dhi"] = components["dhi"]
    result["clearsky_index"] = kt
    return result


def build_asset_resources(
    config: PlantConfig,
    base: pd.DataFrame,
    assets: list[Asset],
    *,
    seed: int,
    footprint_smoothing: dict[str, float] | None = None,
) -> dict[str, pd.DataFrame]:
    """Generate per-asset resource series for a set of positioned assets.

    Args:
        config: Plant configuration, used for the site extent.
        base: Plant-average 1-minute resource series.
        assets: Positioned assets to produce series for.
        seed: Seed for the ``cloud_field`` substream.
        footprint_smoothing: Optional map of asset type name to smoothing
            window in minutes.

    Returns:
        A mapping of asset identifier to resource frame.
    """
    from .builder import site_extent

    width, height = site_extent(config)
    wind = base.get("wind_speed", pd.Series(8.0, index=base.index))
    travel = cloud_travel_distance(wind)

    # The field must cover every coordinate any asset will sample: the whole
    # window's advection, offset either way by the site diagonal, with margin.
    diagonal = float(np.hypot(width, height))
    margin = diagonal + 4.0 * GRID_RESOLUTION_M

    field = generate_cloud_field(
        along_min_m=-margin,
        along_max_m=float(travel.iloc[-1]) + margin,
        across_half_width_m=diagonal + margin,
        seed=seed,
    )

    smoothing = footprint_smoothing or {}
    return {
        asset.asset_id: apply_spatial_field(
            base,
            asset,
            field,
            travel,
            smoothing_minutes=smoothing.get(asset.asset_type.value, 0.0),
        )
        for asset in assets
        if asset.position is not None
    }
