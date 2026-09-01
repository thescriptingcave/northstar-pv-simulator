"""Load fetched observations into the simulator.

Phase 0.5 acquires real NSRDB irradiance and real ERCOT prices into a
checksummed cache. Until this module existed nothing read them: every gate,
every dataset and every published figure came from
:func:`~northstar_sim.resource.clearsky_resource` and
:func:`~northstar_sim.market.synthetic_prices`.

Both stand-ins document themselves as development substitutes for exactly this
data. This module is the seam they were written against.

**What changes when real data is used.** Clear-sky is smooth by construction:
one sinusoid a day, no cloud transients, no gaps, and a variability structure
the downscaler itself imposed. Real irradiance has none of those guarantees, so
figures derived from clear-sky runs - capture rate, curtailment value, ramp
attribution - are all provisional until recomputed here.

Reference: design documents ``19_external_data_acquisition`` and
``05_resource_model``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

#: Columns the simulator's physics chain requires from a resource frame. The
#: fetch layer harmonizes provider fields to these names already, so the
#: contract is checked rather than translated.
REQUIRED_COLUMNS = ("ghi", "dni", "dhi", "temp_air", "wind_speed")

#: Columns used when present and defaulted when absent. A provider that omits
#: pressure is usable; one that omits GHI is not.
#: Extraterrestrial irradiance. No surface measurement legitimately exceeds it,
#: so it is the ceiling for any repaired component.
SOLAR_CONSTANT = 1367.0

#: Below this cosine of zenith - about 84 degrees - the closure relation is
#: numerically useless: GHI divided by a near-zero cosine implies an enormous
#: DNI. Twilight carries no energy, so these intervals are left alone.
MIN_COS_ZENITH = 0.10

OPTIONAL_DEFAULTS: dict[str, float] = {
    "pressure": 101325.0,
    "wind_direction": 180.0,
    "albedo": 0.25,
}


@dataclass
class ResourceLoad:
    """A resource series loaded from the fetch cache, with its provenance.

    Attributes:
        frame: The resource series, indexed by UTC timestamp.
        source_slug: Which dataset it came from.
        partitions: Partition labels that contributed.
        gaps_filled: Rows that were interpolated across short gaps.
        rows_dropped: Rows discarded as unusable.
    """

    frame: pd.DataFrame
    source_slug: str
    partitions: list[str]
    gaps_filled: int
    rows_dropped: int

    def describe(self) -> str:
        """Summarise the load for logging.

        Returns:
            A one-line description.
        """
        return (
            f"{self.source_slug}: {len(self.frame):,} rows from "
            f"{len(self.partitions)} partition(s), {self.gaps_filled:,} "
            f"interpolated, {self.rows_dropped:,} dropped"
        )


def _cache_for(config, cache_root: Path | None = None):
    """Open the fetch cache described by a plant configuration.

    Args:
        config: Plant configuration, read for its site block.
        cache_root: Override for the cache directory.

    Returns:
        An open cache instance.

    Raises:
        FileNotFoundError: If the cache directory does not exist.
    """
    from northstar_fetch.cache import PartitionCache

    root = Path(cache_root or "resource_cache").expanduser()
    if not root.is_dir():
        raise FileNotFoundError(
            f"no fetch cache at {root}. Run `make fetch` first, or pass "
            "cache_root explicitly if it lives outside the checkout."
        )

    site = {
        "latitude": config.site.latitude,
        "longitude": config.site.longitude,
        "elevation_m": getattr(config.site, "elevation_m", 0.0),
    }
    return PartitionCache(root, "v1", site)


def available_years(config, *, cache_root: Path | None = None) -> list[int]:
    """Report which years have cached weather.

    Asking before loading is cheaper than a traceback, and the answer is not
    obvious: ERCOT retains about a year while NSRDB reaches back to 2018, so
    weather and price coverage differ.

    Args:
        config: Plant configuration.
        cache_root: Override for the cache directory.

    Returns:
        Years present in the cache, ascending.
    """
    root = Path(cache_root or "resource_cache").expanduser()
    weather = root / "weather"
    if not weather.is_dir():
        return []

    years: set[int] = set()
    for path in weather.rglob("year=*"):
        try:
            years.add(int(path.name.split("=", 1)[1]))
        except (IndexError, ValueError):
            continue
    return sorted(years)


def real_resource(
    config,
    year: int,
    *,
    cache_root: Path | None = None,
    source_slug: str = "nsrdb_goes_conus_v4",
    max_gap_minutes: int = 60,
) -> ResourceLoad:
    """Load a year of fetched irradiance as a simulator resource frame.

    Returns the same shape :func:`clearsky_resource` does, so it drops into
    :func:`~northstar_sim.resource.downscale_to_minute` unchanged.

    **Gaps are interpolated only across short outages and never zero-filled.**
    Zero irradiance is a physically meaningful value - it means night - so
    filling a daytime gap with zero fabricates an outage that did not happen.
    Longer gaps are left as NaN for the caller to decide about.

    Args:
        config: Plant configuration.
        year: Calendar year to load.
        cache_root: Override for the cache directory.
        source_slug: Which cached weather dataset to read.
        max_gap_minutes: Longest run of missing values to interpolate across.

    Returns:
        A :class:`ResourceLoad`.

    Raises:
        FileNotFoundError: If no partition for the year is cached.
        ValueError: If the loaded frame lacks a required column.
    """
    root = Path(cache_root or "resource_cache").expanduser()
    pattern = f"weather/source={source_slug}/**/year={year}/**/*.parquet"
    files = sorted(root.glob(pattern))

    if not files:
        # Fall back to a looser search: the cache layout has changed once and
        # a hard-coded path would fail confusingly.
        files = sorted(
            p
            for p in root.rglob("*.parquet")
            if f"year={year}" in str(p) and source_slug in str(p)
        )

    if not files:
        have = available_years(config, cache_root=cache_root)
        raise FileNotFoundError(
            f"no cached {source_slug} for {year} under {root}. "
            f"Years available: {have or 'none - run `make fetch`'}"
        )

    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame = _to_resource_frame(frame, config, year)

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(
            f"cached {source_slug} lacks required column(s) {missing}; "
            f"present: {sorted(frame.columns)}"
        )

    before = int(frame[list(REQUIRED_COLUMNS)].isna().any(axis=1).sum())
    frame, dropped = _fill_short_gaps(frame, max_gap_minutes)
    after = int(frame[list(REQUIRED_COLUMNS)].isna().any(axis=1).sum())

    return ResourceLoad(
        frame=frame,
        source_slug=source_slug,
        partitions=[f.parent.name for f in files],
        gaps_filled=max(before - after - dropped, 0),
        rows_dropped=dropped,
    )


def _to_resource_frame(raw: pd.DataFrame, config, year: int) -> pd.DataFrame:
    """Shape a harmonized weather frame into the simulator's resource contract.

    The fetch layer already emits canonical column names, so this sets the
    index, restricts to the requested year, applies defaults for optional
    fields, and derives the clear-sky reference columns the downscaler needs.

    Args:
        raw: Harmonized weather rows.
        config: Plant configuration.
        year: Year to restrict to.

    Returns:
        A resource frame indexed by UTC timestamp.
    """
    frame = _canonicalize(raw.copy())
    time_column = "time" if "time" in frame.columns else frame.columns[0]
    frame[time_column] = pd.to_datetime(frame[time_column], utc=True)
    frame = frame.set_index(time_column).sort_index().loc[lambda f: f.index.year == year]
    frame = frame[~frame.index.duplicated(keep="first")]

    for column, default in OPTIONAL_DEFAULTS.items():
        if column not in frame.columns:
            frame[column] = default
        else:
            frame[column] = frame[column].fillna(default)

    frame = _enforce_closure(frame, location_for(config))

    # The downscaler needs a clear-sky reference to compute a clearness index.
    # It is derived here rather than read, because no provider supplies the
    # clear-sky GHI for this exact site and the site is what defines it.
    clearsky = location_for(config).get_clearsky(frame.index, model="ineichen")
    frame["clearsky_ghi"] = clearsky["ghi"].to_numpy()

    # Clearness index is undefined when the clear-sky reference is zero, which
    # is every night. Zero there rather than inf or NaN: a night with no sun is
    # perfectly clear by any useful definition, and NaN would propagate.
    reference = frame["clearsky_ghi"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        index = np.where(reference > 1.0, frame["ghi"].to_numpy() / reference, 0.0)
    frame["clearsky_index"] = np.clip(index, 0.0, 1.2)

    # The envelope is the ceiling the downscaler renormalizes against.
    frame["kt_envelope"] = (
        frame["clearsky_index"].rolling(window=12, min_periods=1, center=True).max()
    )

    return frame


def _canonicalize(frame: pd.DataFrame) -> pd.DataFrame:
    """Map any remaining provider column names to canonical ones.

    The fetch layer harmonizes on write, but partitions cached before that
    logic handled space-separated names - "Wind Speed" rather than
    "wind_speed" - carry the raw headers. Re-canonicalizing on read means an
    existing cache stays usable instead of costing a 197-request refetch.

    Canonical columns already present always win; this only fills gaps.

    Args:
        frame: Rows as read from the cache.

    Returns:
        The frame with provider names mapped where a canonical column is absent.
    """
    try:
        from northstar_fetch.harmonize import CANONICAL_COLUMNS, _canonical_key
    except ImportError:  # pragma: no cover - fetch package is optional
        return frame

    renames = {}
    for column in frame.columns:
        target = CANONICAL_COLUMNS.get(_canonical_key(column))
        if target and target != column and target not in frame.columns:
            renames[column] = target
    return frame.rename(columns=renames) if renames else frame


def location_for(config):
    """Build a pvlib Location from the plant configuration.

    Args:
        config: Plant configuration.

    Returns:
        A configured :class:`pvlib.location.Location`.
    """
    import pvlib

    return pvlib.location.Location(
        latitude=config.site.latitude,
        longitude=config.site.longitude,
        altitude=getattr(config.site, "elevation_m", 0.0),
        tz="UTC",
    )


def _enforce_closure(frame: pd.DataFrame, location) -> pd.DataFrame:
    """Repair irradiance components that violate the closure relation.

    Transposition requires ``GHI ~= DHI + DNI * cos(zenith)``. Components that
    contradict it drive the Perez model to NaN, and a single NaN then poisons
    the sensor layer's cumulative state for the **entire** series - one bad
    interval nulls a whole year of measured irradiance.

    Real NSRDB satisfies closure, and the fetch layer validates it. This exists
    so a provider that does not cannot silently destroy a run: DHI is recomputed
    from GHI and DNI where they disagree, which preserves the two components
    measured most directly.

    Args:
        frame: Resource frame with ghi, dni and dhi.
        location: Site location, for solar position.

    Returns:
        The frame with consistent components.
    """
    if not {"ghi", "dni", "dhi"} <= set(frame.columns):
        return frame

    position = location.get_solarposition(frame.index)
    cos_zenith = np.cos(np.radians(position["apparent_zenith"].to_numpy()))
    cos_zenith = np.where(cos_zenith > 0.0, cos_zenith, 0.0)

    ghi = frame["ghi"].to_numpy(dtype=float)
    dni = frame["dni"].to_numpy(dtype=float)
    dhi = frame["dhi"].to_numpy(dtype=float)

    implied = dhi + dni * cos_zenith
    # 5% of GHI, with a floor so near-zero night values are not flagged.
    tolerance = np.maximum(0.05 * np.abs(ghi), 10.0)
    broken = np.abs(implied - ghi) > tolerance

    if broken.any():
        # Only repair where the sun is meaningfully up. Dividing GHI by
        # cos(zenith) near the horizon implies an enormous DNI - at 89.5
        # degrees a GHI of 50 implies 5,000 W/m2 - which drove POA and AC to
        # five times nameplate on real NSRDB data. Twilight contributes no
        # energy, so leaving those intervals alone costs nothing.
        repairable = broken & (cos_zenith > MIN_COS_ZENITH)

        implied = np.divide(
            ghi,
            cos_zenith,
            out=np.zeros_like(ghi),
            where=cos_zenith > MIN_COS_ZENITH,
        )
        # Nothing reaching the surface exceeds the solar constant.
        dni = np.where(
            repairable, np.minimum(dni, np.minimum(implied, SOLAR_CONSTANT)), dni
        )
        dhi = np.where(repairable, np.clip(ghi - dni * cos_zenith, 0.0, None), dhi)

        frame["dni"] = np.clip(dni, 0.0, SOLAR_CONSTANT)
        frame["dhi"] = np.clip(dhi, 0.0, SOLAR_CONSTANT)
        frame.attrs["closure_repaired"] = int(repairable.sum())
        frame.attrs["closure_skipped_low_sun"] = int((broken & ~repairable).sum())

    return frame


def _fill_short_gaps(
    frame: pd.DataFrame, max_gap_minutes: int
) -> tuple[pd.DataFrame, int]:
    """Interpolate short gaps and drop rows still unusable afterwards.

    Args:
        frame: Resource frame indexed by time.
        max_gap_minutes: Longest run of missing values to interpolate across.

    Returns:
        The repaired frame and the count of rows dropped.
    """
    if frame.empty:
        return frame, 0

    cadence = frame.index.to_series().diff().median()
    limit = None
    if pd.notna(cadence) and cadence.total_seconds() > 0:
        limit = max(int(max_gap_minutes * 60 / cadence.total_seconds()), 1)

    numeric = frame.select_dtypes(include="number").columns
    frame[numeric] = frame[numeric].interpolate(
        method="time", limit=limit, limit_direction="both"
    )

    before = len(frame)
    frame = frame.dropna(subset=list(REQUIRED_COLUMNS))
    return frame, before - len(frame)


def real_prices(
    config,
    year: int,
    *,
    settlement_point: str | None = None,
    cache_root: Path | None = None,
    source_slug: str = "ercot_rt_spp",
) -> pd.Series:
    """Load a year of fetched settlement prices.

    Replaces :func:`~northstar_sim.market.synthetic_prices`, which documents
    itself as a development stand-in for exactly this series.

    Args:
        config: Plant configuration.
        year: Calendar year to load.
        settlement_point: Point to load; defaults to the plant's own node.
        cache_root: Override for the cache directory.
        source_slug: Which cached price dataset to read.

    Returns:
        Prices in USD/MWh, indexed by UTC timestamp.

    Raises:
        FileNotFoundError: If no partition for the year and point is cached.
    """
    point = settlement_point or getattr(
        config.site, "node_settlement_point", "HRNT_SLR_RN"
    )
    root = Path(cache_root or "resource_cache").expanduser()

    files = sorted(
        p
        for p in root.rglob("*.parquet")
        if f"year={year}" in str(p)
        and source_slug in str(p)
        and f"point={point}" in str(p)
    )
    if not files:
        raise FileNotFoundError(
            f"no cached {source_slug} for {point} in {year} under {root}. "
            "Run `make fetch`, and check the price years in "
            "config/northstar.toml - ERCOT retains roughly one year."
        )

    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)

    series = (
        frame.set_index("time")["price_usd_mwh"]
        .sort_index()
        .loc[lambda s: s.index.year == year]
    )
    return series[~series.index.duplicated(keep="first")]


def align_prices(prices: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Align a settlement price series onto a finer simulation index.

    Prices are held constant across their settlement interval rather than
    interpolated. A price is the clearing outcome for a whole interval, not a
    sample of a continuous quantity, so interpolating invents intermediate
    values that never cleared.

    Args:
        prices: Settlement prices, indexed by UTC timestamp.
        index: Target index, typically 1-minute.

    Returns:
        Prices on the target index.
    """
    return prices.reindex(index, method="ffill").bfill()
