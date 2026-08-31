"""Fetch run orchestration.

The orchestrator owns everything that is common across sources: which partitions
to skip, what order to fetch in, when to validate, and when to update the
manifest. Adapters stay ignorant of resumption and idempotency.

Two behaviours matter more than they look:

* A partition is skipped only when the manifest records it *and* its checksum
  still matches. A partial or corrupted write is treated as absent, so an
  interrupted run self-heals instead of silently carrying bad data forward.
* The manifest is written after every successful partition, not at the end. A
  run killed halfway leaves a valid manifest describing exactly what completed.

Reference: design document ``19_external_data_acquisition`` section 7.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from .cache import PartitionKey, ResourceCache
from .config import Credentials, FetchConfig
from .http import FetchError, HttpClient, RateLimiter, RateLimitError, RetryPolicy
from .sources.base import Source
from .sources.market import (
    ErcotDayAheadPriceSource,
    ErcotPriceSource,
    OpenMeteoSource,
)
from .sources.nsrdb import NsrdbConusSource, NsrdbTmySource
from .validate import (
    check_negative_prices_present,
    cross_source_temperature_correlation,
)

LOGGER = logging.getLogger(__name__)

# Registry mapping registry identifiers to adapter classes. Adding a provider or
# a market means adding one entry here and one subclass.
SOURCE_REGISTRY: dict[str, type[Source]] = {
    "SRC-WX-01": NsrdbConusSource,
    "SRC-WX-02": NsrdbTmySource,
    "SRC-WX-03": OpenMeteoSource,
    "SRC-PX-01": ErcotPriceSource,
    "SRC-PX-02": ErcotDayAheadPriceSource,
}


@dataclass
class RunSummary:
    """Outcome of one fetch run.

    Attributes:
        fetched: Partitions successfully acquired and written.
        skipped: Partitions already present and checksum-valid.
        failed: Partition labels paired with the reason they failed.
        warnings: Non-blocking validation findings, for the run report.
        requests: Total HTTP requests issued across all sources.
    """

    fetched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requests: int = 0

    @property
    def ok(self) -> bool:
        """Whether the run completed without failures.

        Returns:
            ``True`` when no partition failed.
        """
        return not self.failed

    def render(self) -> str:
        """Format a human-readable run report.

        Returns:
            A multi-line summary suitable for terminal output or a log file.
        """
        lines = [
            "Fetch run summary",
            f"  fetched : {len(self.fetched)}",
            f"  skipped : {len(self.skipped)}",
            f"  failed  : {len(self.failed)}",
            f"  requests: {self.requests}",
        ]
        if self.warnings:
            lines.append("  warnings:")
            lines.extend(f"    - {warning}" for warning in self.warnings)
        if self.failed:
            lines.append("  failures:")
            lines.extend(f"    - {label}: {reason}" for label, reason in self.failed)
        return "\n".join(lines)


class FetchOrchestrator:
    """Runs a fetch across every enabled source.

    Args:
        config: Whole-run configuration.
        credentials: Environment-supplied API credentials.
        cache: Target cache. Injectable so tests can supply a temporary tree.
        source_overrides: Pre-built adapters keyed by source identifier, used by
            tests to substitute stubs without touching the registry.
    """

    def __init__(
        self,
        config: FetchConfig,
        credentials: Credentials,
        cache: ResourceCache | None = None,
        source_overrides: dict[str, Source] | None = None,
    ) -> None:
        """Initialise the orchestrator and its cache."""
        self.config = config
        self.credentials = credentials
        self.cache = cache or ResourceCache(
            root=config.cache_root,
            cache_version=config.cache_version,
            site=config.site.model_dump(),
        )
        self.source_overrides = source_overrides or {}

    def build_sources(self) -> list[Source]:
        """Instantiate an adapter for every enabled source.

        Each adapter receives its own HTTP client and rate limiter, so one
        source exhausting its provider budget does not throttle the others.

        Returns:
            Adapters in configuration order.

        Raises:
            KeyError: If a configured source has no registered adapter.
        """
        sources: list[Source] = []
        for source_config in self.config.enabled_sources():
            source_id = source_config.source_id
            if source_id in self.source_overrides:
                sources.append(self.source_overrides[source_id])
                continue
            if source_id not in SOURCE_REGISTRY:
                raise KeyError(f"no adapter registered for source {source_id}")
            client = HttpClient(
                rate_limiter=RateLimiter(
                    min_interval_s=source_config.min_request_interval_s,
                    max_requests_per_day=source_config.max_requests_per_day,
                ),
                retry_policy=RetryPolicy(max_attempts=self.config.max_retries),
                timeout_s=self.config.request_timeout_s,
            )
            sources.append(
                SOURCE_REGISTRY[source_id](
                    self.config, source_config, self.credentials, client
                )
            )
        return sources

    def run(self, *, force: bool = False, limit: int | None = None) -> RunSummary:
        """Execute the fetch.

        Args:
            force: Refetch every partition even when a valid cached copy
                exists. Used after a harmonization change that does not warrant
                a cache version bump.
            limit: Stop after this many partitions **per source**. The full plan
                is 445 partitions across five providers; a limit of 1 confirms
                every provider authenticates and returns parseable data in a few
                requests rather than a few hours. Credentials that are merely
                *present* are not credentials that *work*, and that distinction
                is expensive to discover at partition 300.

        Returns:
            A :class:`RunSummary` describing what happened.
        """
        summary = RunSummary()
        sources = self.build_sources()

        for source in sources:
            try:
                source.check_credentials()
            except RuntimeError as error:
                LOGGER.error("%s: %s", source.source_id, error)
                summary.failed.append((source.source_id, str(error)))
                continue

            self._run_source(source, summary, force=force, limit=limit)
            summary.requests += source.client.rate_limiter.request_count

        self.cache.write_manifest(harmonization=self.config.harmonization.model_dump())
        self._run_cross_source_checks(summary)
        self._run_annual_price_checks(summary)
        return summary

    def _run_annual_price_checks(self, summary: RunSummary) -> None:
        """Assert that each cached price year contains negative intervals.

        Applied at year level rather than month level because a single month can
        legitimately contain no negative prices, while a West Texas year cannot.
        A year without any means the price field was clipped, coerced or
        mis-parsed - not that the market behaved unusually.

        Args:
            summary: Run summary to update in place.
        """
        price_source = self.config.source("SRC-PX-01")
        if not price_source or not price_source.enabled:
            return

        hub = self.config.site.hub_settlement_point
        for year in sorted(price_source.years):
            months = [
                PartitionKey(
                    "prices",
                    "ercot_rt_spp",
                    (("point", hub), ("year", str(year)), ("month", f"{month:02d}")),
                )
                for month in range(1, 13)
            ]
            if not all(self.cache.has_valid(key) for key in months):
                continue

            frames = [self.cache.read_partition(key) for key in months]
            result = check_negative_prices_present(pd.concat(frames, ignore_index=True))
            if result.passed:
                LOGGER.info("negative prices present for %s %d", hub, year)
            else:
                message = f"{hub} {year}: {result.detail}"
                LOGGER.error(message)
                summary.failed.append(("negative_prices_present", message))

    def _run_source(
        self,
        source: Source,
        summary: RunSummary,
        *,
        force: bool,
        limit: int | None = None,
    ) -> None:
        """Fetch every partition for one source.

        A client-side rate limit stops that source cleanly and leaves the rest
        of the run to continue, because the correct response is to resume in the
        next window rather than to abandon the whole fetch.

        Args:
            source: Adapter to run.
            summary: Run summary to update in place.
            force: Whether to ignore existing valid partitions.
            limit: Stop after this many partitions are actually fetched.
                Cached partitions do not count against it, so a smoke test
                after a partial run still exercises the network.
        """
        fetched = 0
        for key in source.partitions():
            if limit is not None and fetched >= limit:
                LOGGER.info(
                    "%s: stopping at limit of %d partition(s)",
                    source.source_id,
                    limit,
                )
                return

            if not force and self.cache.has_valid(key):
                LOGGER.debug("skip %s (cached)", key.label())
                summary.skipped.append(key.label())
                continue

            try:
                result = source.fetch(key)
            except RateLimitError as error:
                LOGGER.warning("%s: %s", source.source_id, error)
                summary.failed.append((key.label(), str(error)))
                return
            except (FetchError, ValueError, KeyError) as error:
                LOGGER.error("%s: fetch failed: %s", key.label(), error)
                summary.failed.append((key.label(), str(error)))
                continue

            fetched += 1
            report = source.validate(key, result)
            for warning in report.warnings:
                message = f"{key.label()}: {warning.name}: {warning.detail}"
                LOGGER.warning(message)
                summary.warnings.append(message)

            if not report.ok:
                detail = "; ".join(
                    f"{check.name}: {check.detail}" for check in report.blocking_failures
                )
                LOGGER.error("%s: validation failed: %s", key.label(), detail)
                summary.failed.append((key.label(), f"validation: {detail}"))
                continue

            self.cache.write_partition(
                key,
                result.frame,
                source_id=source.source_id,
                provider=source.provider,
                endpoint=source.endpoint,
                client=source.client_name,
                transformations=result.transformations,
                license_note=source.license_note,
                notes=source.notes,
            )
            # Written per partition so an interrupted run leaves a manifest that
            # accurately describes what completed.
            self.cache.write_manifest(
                harmonization=self.config.harmonization.model_dump()
            )
            LOGGER.info(
                "fetched %s (%d rows, %s)",
                key.label(),
                len(result.frame),
                report.summary(),
            )
            summary.fetched.append(key.label())

    def _run_cross_source_checks(self, summary: RunSummary) -> None:
        """Compare independent providers for the same coordinates.

        Runs after all partitions are cached because it needs both providers
        present. A failure here is reported rather than blocking, since the data
        is already validated individually and the operator needs to see the
        correlation figure to diagnose the cause.

        Args:
            summary: Run summary to update in place.
        """
        nsrdb = self.config.source("SRC-WX-01")
        meteo = self.config.source("SRC-WX-03")
        if not nsrdb or not meteo or not nsrdb.enabled or not meteo.enabled:
            return

        shared_years = sorted(set(nsrdb.years) & set(meteo.years))
        if not shared_years:
            return

        year = shared_years[0]
        primary_key = PartitionKey(
            "weather",
            "nsrdb_goes_conus_v4",
            (("site", self.config.site.name), ("year", str(year))),
        )
        secondary_key = PartitionKey(
            "weather",
            "open_meteo_era5",
            (("site", self.config.site.name), ("year", str(year))),
        )
        if not (
            self.cache.has_valid(primary_key) and self.cache.has_valid(secondary_key)
        ):
            return

        result = cross_source_temperature_correlation(
            self.cache.read_partition(primary_key),
            self.cache.read_partition(secondary_key),
        )
        if result.passed:
            LOGGER.info("cross-source temperature check passed for %d", year)
        else:
            message = f"cross-source temperature check ({year}): {result.detail}"
            LOGGER.error(message)
            summary.failed.append(("cross_source_temperature", result.detail))

    def verify(self) -> list[str]:
        """Check cache integrity without any network access.

        Returns:
            Human-readable problem descriptions; empty means the cache is
            intact.
        """
        return self.cache.verify()

    def plan(self) -> Iterable[tuple[str, bool]]:
        """Report what a run would do, without fetching anything.

        Returns:
            Pairs of partition label and whether it is already cached.
        """
        for source in self.build_sources():
            for key in source.partitions():
                yield key.label(), self.cache.has_valid(key)
