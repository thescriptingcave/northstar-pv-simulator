"""Versioned, checksummed resource cache.

The cache is the artifact that makes simulation reproducible. The simulator
reads only from here and never from the network, so a run is pinned by
``(cache_version, config_version, seed, simulator_version)``.

Layout is hive-partitioned so that DuckDB and pandas can push predicates down
without loading the whole tree:

.. code-block:: text

    resource_cache/
      manifest.json
      weather/source=nsrdb_goes_conus_v4/site=northstar/year=2019/data.parquet
      prices/source=ercot_rt_spp/point=HB_WEST/year=2019/month=01/data.parquet

Reference: design document ``19_external_data_acquisition`` section 5.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class PartitionKey:
    """Identifies one independently fetchable and cacheable unit of data.

    Partitioning at this granularity is what makes a fetch run resumable: a
    partition is either complete and checksummed, or absent and refetched.

    Attributes:
        domain: Top-level cache directory, one of ``weather``, ``prices`` or
            ``grid``.
        source_slug: Filesystem-safe provider and product name.
        parts: Ordered hive partition fields, for example
            ``(("site", "northstar"), ("year", "2019"))``.
    """

    domain: str
    source_slug: str
    parts: tuple[tuple[str, str], ...]

    def relative_path(self) -> Path:
        """Build the cache-relative path to this partition's data file.

        Returns:
            A path of the form
            ``<domain>/source=<slug>/<key>=<value>/.../data.parquet``.
        """
        path = Path(self.domain) / f"source={self.source_slug}"
        for key, value in self.parts:
            path = path / f"{key}={value}"
        return path / "data.parquet"

    def label(self) -> str:
        """Produce a short human-readable identifier for logs.

        Returns:
            A string such as ``nsrdb_goes_conus_v4[site=northstar,year=2019]``.
        """
        inner = ",".join(f"{key}={value}" for key, value in self.parts)
        return f"{self.source_slug}[{inner}]"


@dataclass
class PartitionRecord:
    """Manifest entry describing one cached partition.

    Attributes:
        source_id: Registry identifier from design document 19 section 3.
        provider: Human-readable provider and product name.
        endpoint: Endpoint or client function used to acquire the data.
        client: Library and version, or ``"direct"`` for raw HTTP.
        path: Cache-relative path to the parquet file.
        row_count: Number of rows written.
        time_min: Earliest timestamp in the partition, ISO 8601 UTC.
        time_max: Latest timestamp in the partition, ISO 8601 UTC.
        columns: Column names present after harmonization.
        sha256: Checksum of the written parquet bytes.
        fetched_utc: When the partition was acquired.
        transformations: Harmonization steps applied, in order.
        license: Provider licensing note.
        notes: Free-text caveats worth carrying forward.
    """

    source_id: str
    provider: str
    endpoint: str
    client: str
    path: str
    row_count: int
    time_min: str | None
    time_max: str | None
    columns: list[str]
    sha256: str
    fetched_utc: str
    transformations: list[str] = field(default_factory=list)
    license: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise the record for manifest storage.

        Returns:
            A JSON-compatible dictionary of every field.
        """
        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "client": self.client,
            "path": self.path,
            "row_count": self.row_count,
            "time_min": self.time_min,
            "time_max": self.time_max,
            "columns": self.columns,
            "sha256": self.sha256,
            "fetched_utc": self.fetched_utc,
            "transformations": self.transformations,
            "license": self.license,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PartitionRecord:
        """Rebuild a record from its manifest representation.

        Args:
            payload: Dictionary previously produced by :meth:`to_dict`.

        Returns:
            The reconstructed :class:`PartitionRecord`.
        """
        return cls(**payload)


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 checksum of a file.

    Reads in fixed-size blocks so that arbitrarily large parquet files do not
    need to be held in memory.

    Args:
        path: File to hash.

    Returns:
        Lowercase hexadecimal digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ResourceCache:
    """Reads and writes the versioned resource cache and its manifest.

    The manifest is the source of truth for what the cache contains. A
    partition is considered present only when the manifest records it *and* the
    file on disk still matches the recorded checksum, so silent corruption or a
    partial write is treated as absence rather than as valid data.
    """

    def __init__(self, root: Path, cache_version: str, site: dict[str, Any]) -> None:
        """Open, or prepare to create, a cache tree.

        Args:
            root: Directory holding the cache and its manifest.
            cache_version: Version stamp for this cache generation.
            site: Site identity block copied verbatim into the manifest.
        """
        self.root = Path(root)
        self.cache_version = cache_version
        self.site = site
        self._records: dict[str, PartitionRecord] = {}
        self._harmonization: dict[str, Any] = {}
        self._created_utc: str | None = None
        if self.manifest_path.exists():
            self.load_manifest()

    @property
    def manifest_path(self) -> Path:
        """Location of the manifest file.

        Returns:
            Path to ``manifest.json`` inside the cache root.
        """
        return self.root / MANIFEST_FILENAME

    def load_manifest(self) -> None:
        """Read the manifest from disk into memory.

        Records for a different ``cache_version`` are ignored rather than
        merged, because a version bump means the cache contents are no longer
        interchangeable.
        """
        payload = json.loads(self.manifest_path.read_text())
        if payload.get("cache_version") != self.cache_version:
            self._records = {}
            return
        self._created_utc = payload.get("created_utc")
        self._harmonization = payload.get("harmonization", {})
        self._records = {
            entry["path"]: PartitionRecord.from_dict(entry)
            for entry in payload.get("partitions", [])
        }

    def write_manifest(self, harmonization: dict[str, Any] | None = None) -> None:
        """Persist the manifest to disk.

        Args:
            harmonization: Harmonization rules to record. When omitted, any
                previously loaded rules are preserved.
        """
        if harmonization is not None:
            self._harmonization = harmonization
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": self.cache_version,
            "created_utc": self._created_utc or _utc_now(),
            "updated_utc": _utc_now(),
            "site": self.site,
            "harmonization": self._harmonization,
            "partitions": [self._records[key].to_dict() for key in sorted(self._records)],
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=False))

    def has_valid(self, key: PartitionKey) -> bool:
        """Check whether a partition is present, recorded and uncorrupted.

        Args:
            key: Partition to test.

        Returns:
            ``True`` when the manifest records the partition, the file exists,
            and its checksum still matches. ``False`` otherwise, which causes
            the orchestrator to refetch.
        """
        relative = str(key.relative_path())
        record = self._records.get(relative)
        if record is None:
            return False
        absolute = self.root / relative
        if not absolute.exists():
            return False
        return sha256_file(absolute) == record.sha256

    def record_for(self, key: PartitionKey) -> PartitionRecord | None:
        """Retrieve the manifest record for a partition.

        Args:
            key: Partition to look up.

        Returns:
            The stored :class:`PartitionRecord`, or ``None`` if absent.
        """
        return self._records.get(str(key.relative_path()))

    def write_partition(
        self,
        key: PartitionKey,
        frame: pd.DataFrame,
        *,
        source_id: str,
        provider: str,
        endpoint: str,
        client: str,
        transformations: list[str],
        license_note: str = "",
        notes: str = "",
    ) -> PartitionRecord:
        """Write one partition to parquet and register it in the manifest.

        The file is written to a temporary sibling and then moved into place, so
        an interrupted write never leaves a partially valid partition that a
        later run would mistake for complete.

        Args:
            key: Partition identity, determining the output path.
            frame: Harmonized data to store. Must carry a ``time`` column.
            source_id: Registry identifier for provenance.
            provider: Human-readable provider and product name.
            endpoint: Endpoint or client function used.
            client: Library and version string.
            transformations: Ordered harmonization steps applied.
            license_note: Provider licensing note.
            notes: Free-text caveats.

        Returns:
            The :class:`PartitionRecord` that was registered.

        Raises:
            ValueError: If the frame has no ``time`` column.
        """
        if "time" not in frame.columns:
            raise ValueError(f"{key.label()}: frame is missing a 'time' column")

        relative = key.relative_path()
        absolute = self.root / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)

        temporary = absolute.with_suffix(".parquet.tmp")
        frame.to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(absolute)

        times = pd.to_datetime(frame["time"], utc=True)
        record = PartitionRecord(
            source_id=source_id,
            provider=provider,
            endpoint=endpoint,
            client=client,
            path=str(relative),
            row_count=int(len(frame)),
            time_min=times.min().isoformat() if len(frame) else None,
            time_max=times.max().isoformat() if len(frame) else None,
            columns=list(frame.columns),
            sha256=sha256_file(absolute),
            fetched_utc=_utc_now(),
            transformations=transformations,
            license=license_note,
            notes=notes,
        )
        self._records[str(relative)] = record
        return record

    def read_partition(self, key: PartitionKey) -> pd.DataFrame:
        """Load one cached partition.

        Args:
            key: Partition to read.

        Returns:
            The stored data as a DataFrame.

        Raises:
            FileNotFoundError: If the partition is not present on disk.
        """
        absolute = self.root / key.relative_path()
        if not absolute.exists():
            raise FileNotFoundError(f"{key.label()}: not in cache at {absolute}")
        return pd.read_parquet(absolute)

    def verify(self) -> list[str]:
        """Check every manifest record against the files on disk.

        This is the mechanism behind the offline ``verify`` command and behind
        the simulator's startup check. It performs no network access.

        Returns:
            A list of human-readable problem descriptions. An empty list means
            the cache is intact.
        """
        problems: list[str] = []
        for relative, record in sorted(self._records.items()):
            absolute = self.root / relative
            if not absolute.exists():
                problems.append(f"missing file: {relative}")
                continue
            actual = sha256_file(absolute)
            if actual != record.sha256:
                problems.append(
                    f"checksum mismatch: {relative} "
                    f"(expected {record.sha256[:12]}, found {actual[:12]})"
                )
        return problems

    def summary(self) -> dict[str, Any]:
        """Summarise cache contents by source.

        Returns:
            A dictionary keyed by ``source_id``, each value holding the
            partition count and total row count for that source.
        """
        totals: dict[str, dict[str, int]] = {}
        for record in self._records.values():
            entry = totals.setdefault(record.source_id, {"partitions": 0, "rows": 0})
            entry["partitions"] += 1
            entry["rows"] += record.row_count
        return totals


def _utc_now() -> str:
    """Produce the current UTC time as an ISO 8601 string.

    Returns:
        Timestamp with second resolution and an explicit UTC offset.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat()
