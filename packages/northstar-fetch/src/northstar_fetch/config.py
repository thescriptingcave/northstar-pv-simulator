"""Configuration models for the NorthStar PV resource fetch client.

Configuration is declarative and versioned. Every field that affects what is
fetched, or how it is harmonized, appears here and is recorded in the cache
manifest so a dataset can be regenerated exactly.

Reference: design document ``19_external_data_acquisition`` sections 3-6.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Public identifiers for each external source, matching the source registry in
# design document 19 section 3.
SourceId = Literal[
    "SRC-WX-01",  # NSRDB GOES CONUS v4 (PSM4), 5-minute
    "SRC-WX-02",  # NSRDB GOES TMY v4, hourly
    "SRC-WX-03",  # Open-Meteo ERA5 archive, hourly
    "SRC-PX-01",  # ERCOT real-time settlement point prices, 15-minute
    "SRC-PX-02",  # ERCOT day-ahead settlement point prices, hourly
    "SRC-GR-01",  # EIA API v2, hourly grid context
]


class SiteConfig(BaseModel):
    """Physical and market identity of the simulated plant.

    These values are locked by design decision DR-001 and must not drift
    between cache versions, because every fetched series is keyed to them.

    Attributes:
        name: Short slug used in cache paths.
        latitude: Decimal degrees north, positive.
        longitude: Decimal degrees east, negative for the western hemisphere.
        elevation_m: Site elevation in metres above sea level.
        timezone: IANA timezone name, used for local-solar-day analysis only.
            All stored timestamps remain UTC.
        hub_settlement_point: ERCOT trading hub used as the hedge index.
        zone_settlement_point: ERCOT load zone, for regional context.
        node_settlement_point: Plant resource node, used for basis calculation.
    """

    name: str
    latitude: float
    longitude: float
    elevation_m: float
    timezone: str
    hub_settlement_point: str
    zone_settlement_point: str
    node_settlement_point: str

    @field_validator("latitude")
    @classmethod
    def _check_latitude(cls, value: float) -> float:
        """Reject latitudes outside the valid range.

        Args:
            value: Candidate latitude in decimal degrees.

        Returns:
            The validated latitude.

        Raises:
            ValueError: If the latitude is not between -90 and 90.
        """
        if not -90.0 <= value <= 90.0:
            raise ValueError(f"latitude {value} outside [-90, 90]")
        return value

    @field_validator("longitude")
    @classmethod
    def _check_longitude(cls, value: float) -> float:
        """Reject longitudes outside the valid range.

        Args:
            value: Candidate longitude in decimal degrees.

        Returns:
            The validated longitude.

        Raises:
            ValueError: If the longitude is not between -180 and 180.
        """
        if not -180.0 <= value <= 180.0:
            raise ValueError(f"longitude {value} outside [-180, 180]")
        return value


class HarmonizationConfig(BaseModel):
    """Rules applied to every acquired series before it is cached.

    Harmonization happens at cache-write time rather than at simulation time so
    that the simulator consumes one consistent schema regardless of provider
    conventions.

    Attributes:
        wind_measurement_height_m: Height at which the provider reports wind.
        wind_target_height_m: Height expected by the module temperature model.
        roughness_length_m: Surface roughness used by the log wind profile.
        interval_convention: Whether stored timestamps label the beginning or
            the end of their interval. NSRDB and ERCOT disagree; this resolves
            it once, here.
    """

    wind_measurement_height_m: float = 10.0
    wind_target_height_m: float = 3.0
    roughness_length_m: float = 0.03
    interval_convention: Literal["beginning", "ending"] = "beginning"


class SourceConfig(BaseModel):
    """Per-source acquisition settings.

    Attributes:
        source_id: Registry identifier from design document 19 section 3.
        enabled: Whether this source participates in a fetch run.
        years: Calendar years to acquire. Ignored by typical-year products.
        time_step_min: Requested temporal resolution in minutes, where the
            provider supports a choice.
        fields: Provider field names to request. Empty means provider default.
        max_requests_per_day: Client-side ceiling, kept below the provider
            limit so a run cannot exhaust the key.
        min_request_interval_s: Minimum spacing between requests.
    """

    source_id: SourceId
    enabled: bool = True
    years: list[int] = Field(default_factory=list)
    time_step_min: int | None = None
    fields: list[str] = Field(default_factory=list)
    max_requests_per_day: int = 400
    min_request_interval_s: float = 1.0


class FetchConfig(BaseModel):
    """Top-level configuration for a fetch run.

    Attributes:
        cache_version: Version stamp written into the manifest. Increment on
            any change to data content, harmonization, or source product.
            Never mutate a published version in place.
        cache_root: Directory that holds the cache tree and manifest.
        site: Plant identity.
        harmonization: Normalization rules.
        sources: Per-source settings.
        trailing_refetch_days: Window of recent market data re-fetched on every
            run to capture provider price corrections.
        request_timeout_s: Per-request network timeout.
        max_retries: Attempts per request before the partition is abandoned.
    """

    cache_version: str
    cache_root: Path
    site: SiteConfig
    harmonization: HarmonizationConfig = Field(default_factory=HarmonizationConfig)
    sources: list[SourceConfig]
    trailing_refetch_days: int = 30
    request_timeout_s: int = 60
    max_retries: int = 5

    def source(self, source_id: str) -> SourceConfig | None:
        """Look up the settings for one source.

        Args:
            source_id: Registry identifier to find.

        Returns:
            The matching :class:`SourceConfig`, or ``None`` if the source is
            not present in this configuration.
        """
        for source in self.sources:
            if source.source_id == source_id:
                return source
        return None

    def enabled_sources(self) -> list[SourceConfig]:
        """List the sources that should run.

        Returns:
            Every source whose ``enabled`` flag is set, in declaration order.
        """
        return [source for source in self.sources if source.enabled]


def load_config(path: Path) -> FetchConfig:
    """Read and validate a TOML configuration file.

    Args:
        path: Path to the configuration file.

    Returns:
        A validated :class:`FetchConfig`.

    Raises:
        FileNotFoundError: If the file does not exist.
        pydantic.ValidationError: If the file contents are malformed.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    raw.setdefault("cache_root", "resource_cache")
    # Expand "~" so a cache outside the checkout can be written the obvious
    # way. Without this, "~/.cache/northstar" creates a directory literally
    # named "~" in the working directory.
    raw["cache_root"] = str(Path(str(raw["cache_root"])).expanduser())
    raw["cache_root"] = Path(raw["cache_root"])
    return FetchConfig.model_validate(raw)


def _load_env_file(env_file: Path | str | None = None) -> Path | None:
    """Load a ``.env`` file into the process environment.

    **This has to happen explicitly.** Reading ``os.environ`` alone silently
    ignores the file: the values are correct on disk, and every credentialed
    source reports "missing credentials". Only the unauthenticated provider
    succeeds, which makes it look like a credential problem rather than a
    loading problem.

    Searches upward from the working directory, so the command works from any
    subdirectory in the same way ``git`` finds its repository root.

    Existing environment variables are **not** overwritten - an explicitly
    exported value should beat a file on disk.

    Args:
        env_file: Explicit path, or ``None`` to search for ``.env``.

    Returns:
        The file that was loaded, or ``None`` if none was found.
    """
    if env_file is not None:
        candidate = Path(env_file)
        candidates = [candidate] if candidate.is_file() else []
    else:
        start = Path.cwd().resolve()
        candidates = [
            directory / ".env"
            for directory in (start, *start.parents)
            if (directory / ".env").is_file()
        ]

    if not candidates:
        return None

    path = candidates[0]
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if name and name not in os.environ:
            os.environ[name] = value

    return path


class Credentials(BaseModel):
    """API credentials read from the process environment.

    Credentials are never read from the configuration file and never written to
    the manifest, so that configuration and manifest are both safe to commit.

    Attributes:
        nrel_api_key: NLR Developer Network key for NSRDB products.
        nrel_email: Contact address required by the NSRDB API.
        ercot_username: ERCOT Public API account username.
        ercot_password: ERCOT Public API account password.
        ercot_subscription_key: ERCOT API subscription key header value.
        eia_api_key: EIA API v2 key.
    """

    nrel_api_key: str | None = None
    nrel_email: str | None = None
    ercot_username: str | None = None
    ercot_password: str | None = None
    ercot_subscription_key: str | None = None
    eia_api_key: str | None = None

    @classmethod
    def from_env(cls, env_file: Path | str | None = None) -> Credentials:
        """Build credentials from the environment, loading ``.env`` first.

        Recognised variables are ``NREL_API_KEY``, ``NREL_EMAIL``,
        ``ERCOT_USERNAME``, ``ERCOT_PASSWORD``, ``ERCOT_SUBSCRIPTION_KEY`` and
        ``EIA_API_KEY``. Missing variables yield ``None`` rather than an error,
        so a partial fetch of the sources that are configured can still run.

        **The .env file is loaded here, explicitly.** Reading ``os.environ``
        alone ignores it entirely: the values sit correct on disk while every
        credentialed source reports "missing credentials", and only the
        unauthenticated provider succeeds - which reads as a credential problem
        rather than a loading problem.

        Args:
            env_file: Explicit path to an env file, or ``None`` to search
                upward from the working directory.

        Returns:
            A :class:`Credentials` instance populated from the environment.
        """
        _load_env_file(env_file)
        return cls(
            nrel_api_key=os.environ.get("NREL_API_KEY"),
            nrel_email=os.environ.get("NREL_EMAIL"),
            ercot_username=os.environ.get("ERCOT_USERNAME"),
            ercot_password=os.environ.get("ERCOT_PASSWORD"),
            ercot_subscription_key=os.environ.get("ERCOT_SUBSCRIPTION_KEY"),
            eia_api_key=os.environ.get("EIA_API_KEY"),
        )

    def require(self, *names: str) -> None:
        """Assert that the named credential fields are populated.

        Args:
            *names: Attribute names that must not be ``None``.

        Raises:
            RuntimeError: If any named field is missing or blank, naming every
                field at once rather than failing on the first.
        """
        # A variable present in .env but left blank loads as "", not None.
        # Checking only for None let an empty ERCOT_SUBSCRIPTION_KEY pass
        # validation and fail later as an opaque 401 from the API, several
        # minutes into a fetch. Blank is missing.
        missing = [name for name in names if not str(getattr(self, name) or "").strip()]
        if missing:
            env_names = ", ".join(name.upper() for name in missing)
            raise RuntimeError(f"missing credentials: set {env_names} in the environment")
