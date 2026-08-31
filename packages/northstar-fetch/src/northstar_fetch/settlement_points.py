"""Settlement point selection from the ERCOT network model extract.

The plant resource node determines basis - the difference between the price at
the plant's own node and the price at the hub used as the hedge index. Basis is
the one price component structurally correlated with the plant's own output,
because West Texas nodes go deeply negative relative to the hub precisely when
every solar plant in the region is producing at once. Choosing the node badly
does not fail loudly; it silently produces a basis series that means nothing.

This module turns that choice into a reproducible filter over the ERCOT
``Settlement Points List and Electrical Buses Mapping`` extract (EMIL
``np4-160-sg``, MIS report type 10008) rather than a one-time manual judgement.

Two things this module deliberately does not do:

* It does not match on commercial plant names. ERCOT resource node names are not
  derived from the names a developer markets a project under, so grepping for
  "Roserock" or "Buckthorn" produces false positives in the wrong load zone.
* It does not establish county. The extract carries no coordinates. Geographic
  confirmation requires an external join, and for a pricing proxy it is usually
  unnecessary - what matters is that the node *behaves* like a West Texas solar
  node, which is established empirically from its price history.

Reference: design document ``19_external_data_acquisition`` section 4.4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger(__name__)

# Filenames inside the ERCOT extract. The archive prefixes each with a timestamp,
# so matching is by substring rather than exact name.
RESOURCE_NODE_FILE = "Resource_Node_to_Unit"
SETTLEMENT_POINTS_FILE = "Settlement_Points"

# Substrings that mark a node as solar. ERCOT does not require node names to
# indicate fuel type, so this identifies a convenience subset, never a complete
# inventory of solar resources in a zone.
SOLAR_MARKERS = ("SLR", "SOLAR", "_PV", "PV_")

# Unit-name markers for co-located energy storage. A hybrid node's price
# reflects charge and discharge behaviour that a standalone PV plant does not
# have, so those nodes are excluded rather than merely down-ranked.
STORAGE_UNIT_MARKERS = ("ESR", "BESS", "BATT")

# Utility-scale PV in ERCOT interconnects at transmission voltage. A 138 kV
# interconnection indicates a plant materially smaller than the 100 MW reference.
MIN_INTERCONNECTION_KV = 300.0


@dataclass(frozen=True)
class NodeCandidate:
    """One resource node considered as the plant pricing proxy.

    Attributes:
        resource_node: ERCOT settlement point identifier.
        substation: Switchyard the node's units connect through.
        load_zone: Congestion load zone.
        interconnection_kv: Highest voltage level at the node's substation.
        unit_count: Number of registered generation units at the node.
        bus_count: Electrical buses at the substation, a rough proxy for the
            physical build-out of the plant.
        has_storage: Whether any unit name marks co-located battery storage.
        unit_names: Registered unit names, for manual inspection.
    """

    resource_node: str
    substation: str
    load_zone: str
    interconnection_kv: float
    unit_count: int
    bus_count: int
    has_storage: bool
    unit_names: tuple[str, ...]

    @property
    def scale_distance(self) -> int:
        """Distance from the unit count that best matches the reference plant.

        A 100 MW single-site plant typically registers two to four units. A node
        with eight is a far larger facility whose congestion contribution is not
        representative; a node with one is usually a smaller project.

        Returns:
            Absolute distance from a target of three units.
        """
        return abs(self.unit_count - 3)

    def describe(self) -> str:
        """Render a single-line summary for logs and CLI output.

        Returns:
            A fixed-width description of the candidate.
        """
        storage = "hybrid" if self.has_storage else "solar "
        return (
            f"{self.resource_node:<16} {storage} {self.load_zone:<8} "
            f"{self.interconnection_kv:>6.1f}kV  units={self.unit_count:<2} "
            f"buses={self.bus_count:<3} sub={self.substation}"
        )


def _locate(directory: Path, fragment: str) -> Path:
    """Find the extract file whose name contains a fragment.

    Args:
        directory: Directory holding the unpacked ERCOT extract.
        fragment: Distinctive part of the filename.

    Returns:
        Path to the single matching CSV.

    Raises:
        FileNotFoundError: If no file matches.
        ValueError: If more than one file matches, which usually means two
            extract vintages were unpacked into the same directory.
    """
    matches = sorted(p for p in directory.glob("*.csv") if fragment in p.name)
    if not matches:
        raise FileNotFoundError(f"no file containing {fragment!r} in {directory}")
    if len(matches) > 1:
        raise ValueError(
            f"multiple files contain {fragment!r} in {directory}: "
            f"{[p.name for p in matches]} - unpack one extract vintage at a time"
        )
    return matches[0]


def load_candidates(extract_dir: Path) -> list[NodeCandidate]:
    """Read the ERCOT extract and build a candidate for every resource node.

    Args:
        extract_dir: Directory holding the unpacked extract CSVs.

    Returns:
        One :class:`NodeCandidate` per resource node, unfiltered and unranked.

    Raises:
        FileNotFoundError: If a required extract file is absent.
    """
    units = pd.read_csv(_locate(extract_dir, RESOURCE_NODE_FILE))
    points = pd.read_csv(_locate(extract_dir, SETTLEMENT_POINTS_FILE))

    # Settlement_Points carries one row per electrical bus. Resource node,
    # zone and voltage are attributes of the bus, so they are reduced to one row
    # per node before joining.
    buses = points.dropna(subset=["RESOURCE_NODE"])
    buses = buses[buses["RESOURCE_NODE"].astype(str).str.strip() != ""]

    per_node = buses.groupby("RESOURCE_NODE").agg(
        load_zone=("SETTLEMENT_LOAD_ZONE", "first"),
        interconnection_kv=("VOLTAGE_LEVEL", "max"),
        substation=("SUBSTATION", "first"),
    )
    bus_counts = points.groupby("SUBSTATION").size().rename("bus_count")

    candidates: list[NodeCandidate] = []
    for node, group in units.groupby("RESOURCE_NODE"):
        if node not in per_node.index:
            LOGGER.debug("%s has no bus entry in Settlement_Points; skipped", node)
            continue
        attributes = per_node.loc[node]
        unit_names = tuple(sorted(str(name) for name in group["UNIT_NAME"]))
        unit_substation = str(group["UNIT_SUBSTATION"].iloc[0])
        substation = str(attributes["substation"])

        candidates.append(
            NodeCandidate(
                resource_node=str(node),
                substation=substation,
                load_zone=str(attributes["load_zone"]),
                interconnection_kv=float(attributes["interconnection_kv"]),
                unit_count=len(group),
                bus_count=int(
                    bus_counts.get(unit_substation, bus_counts.get(substation, 0))
                ),
                has_storage=any(
                    marker in name
                    for name in unit_names
                    for marker in STORAGE_UNIT_MARKERS
                ),
                unit_names=unit_names,
            )
        )
    return candidates


def rank_candidates(
    candidates: list[NodeCandidate],
    *,
    load_zone: str = "LZ_WEST",
    require_solar_name: bool = True,
    exclude_storage: bool = True,
    min_kv: float = MIN_INTERCONNECTION_KV,
) -> list[NodeCandidate]:
    """Filter and rank candidates for use as the plant pricing proxy.

    Ranking prefers a unit count near the reference plant's scale, then a larger
    physical build-out as a tiebreak, on the basis that a better-instrumented
    site is more likely to have a long and continuous price history.

    Args:
        candidates: Output of :func:`load_candidates`.
        load_zone: Congestion zone the plant sits in.
        require_solar_name: Restrict to nodes whose name marks them as solar.
            This is a convenience filter and will miss solar plants whose node
            names carry no fuel marker.
        exclude_storage: Drop nodes with co-located battery storage.
        min_kv: Minimum interconnection voltage.

    Returns:
        Matching candidates, best first.
    """
    selected = [c for c in candidates if c.load_zone == load_zone]
    selected = [c for c in selected if c.interconnection_kv >= min_kv]
    if require_solar_name:
        selected = [
            c
            for c in selected
            if any(marker in c.resource_node.upper() for marker in SOLAR_MARKERS)
        ]
    if exclude_storage:
        selected = [c for c in selected if not c.has_storage]

    return sorted(
        selected, key=lambda c: (c.scale_distance, -c.bus_count, c.resource_node)
    )
