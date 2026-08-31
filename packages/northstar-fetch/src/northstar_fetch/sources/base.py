"""Common contract for every external data source adapter.

Each adapter declares the partitions it can supply and knows how to fetch and
harmonize one of them. The orchestrator owns skipping, ordering, validation and
manifest updates, so an adapter never needs to reason about resumption or
idempotency.

Adding a new provider, or a new market, means writing one subclass. Nothing
else in the package changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..cache import PartitionKey
from ..config import Credentials, FetchConfig, SourceConfig
from ..http import HttpClient
from ..validate import ValidationReport


@dataclass
class FetchResult:
    """Harmonized output of one partition fetch.

    Attributes:
        frame: Harmonized data ready to cache.
        transformations: Ordered harmonization steps, recorded in the manifest.
        expected_rows: Row count implied by the interval and period, used by
            the structural completeness check.
    """

    frame: pd.DataFrame
    transformations: list[str]
    expected_rows: int


class Source(ABC):
    """Base class for provider adapters.

    Args:
        config: Whole-run configuration, for site identity and harmonization.
        source_config: Settings for this source alone.
        credentials: Environment-supplied API credentials.
        client: HTTP client carrying this source's rate limiter.
    """

    #: Registry identifier from design document 19 section 3.
    source_id: str = ""
    #: Filesystem-safe provider and product name, used in cache paths.
    slug: str = ""
    #: Top-level cache directory: ``weather``, ``prices`` or ``grid``.
    domain: str = ""
    #: Human-readable provider and product description.
    provider: str = ""
    #: Endpoint or client function, recorded for provenance.
    endpoint: str = ""
    #: Library and version used, or ``"direct"`` for raw HTTP.
    client_name: str = "direct"
    #: Provider licensing note carried into the manifest.
    license_note: str = ""
    #: Caveats worth carrying forward to whoever uses the cache.
    notes: str = ""

    def __init__(
        self,
        config: FetchConfig,
        source_config: SourceConfig,
        credentials: Credentials,
        client: HttpClient,
    ) -> None:
        """Store collaborators for use by the concrete adapter."""
        self.config = config
        self.source_config = source_config
        self.credentials = credentials
        self.client = client

    @abstractmethod
    def partitions(self) -> list[PartitionKey]:
        """Enumerate the partitions this source will supply.

        Partition granularity determines resumption granularity, so it should be
        small enough that an interrupted run loses little work and large enough
        that request count stays within provider limits.

        Returns:
            Partition keys in the order they should be fetched.
        """

    @abstractmethod
    def fetch(self, key: PartitionKey) -> FetchResult:
        """Acquire and harmonize a single partition.

        Args:
            key: Partition to fetch, drawn from :meth:`partitions`.

        Returns:
            The harmonized result.

        Raises:
            FetchError: If the provider request fails permanently.
        """

    @abstractmethod
    def validate(self, key: PartitionKey, result: FetchResult) -> ValidationReport:
        """Run tier 0 checks against a fetched partition.

        Args:
            key: Partition being validated.
            result: Output of :meth:`fetch`.

        Returns:
            A report whose blocking failures prevent the write.
        """

    def check_credentials(self) -> None:
        """Assert that the credentials this source needs are present.

        The default implementation requires nothing. Adapters for authenticated
        providers override it so that a missing key fails immediately rather
        than after the first request round-trip.
        """
        return None

    def partition_for(self, **parts: Any) -> PartitionKey:
        """Build a partition key for this source.

        Args:
            **parts: Hive partition fields in the order they should appear in
                the cache path.

        Returns:
            A :class:`PartitionKey` bound to this source's domain and slug.
        """
        return PartitionKey(
            domain=self.domain,
            source_slug=self.slug,
            parts=tuple((key, str(value)) for key, value in parts.items()),
        )
