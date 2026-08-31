"""Asset identity, hierarchy and the instantiated plant.

Asset identifiers must be stable across a simulation run and across runs of the
same configuration, because every longitudinal analysis joins on them. An
identifier that changes between runs makes degradation estimation, peer
comparison and fault-history analysis silently wrong rather than obviously
broken.

Identifiers are therefore **derived deterministically from position in the
hierarchy**, never generated randomly and never dependent on iteration order:

.. code-block:: text

    NSPV                      site
    NSPV-BLK03                power block 3
    NSPV-BLK03-INV2           inverter 2 in block 3
    NSPV-BLK03-INV2-CMB07     combiner 7 on that inverter
    NSPV-BLK03-TRK1           tracker row-block 1 in block 3
    NSPV-BLK03-XFMR           that block's step-up transformer
    NSPV-WS2                  weather station 2

Reference: design documents ``03_reference_solar_farm`` section 6 and
``05_equipment_catalog`` section 11.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum


class AssetType(StrEnum):
    """Kinds of asset the plant model instantiates.

    Modules and individual strings are deliberately absent. They exist in the
    configuration and influence production through counts and electrical
    characteristics, but instantiating 216,000 module objects would consume
    memory to represent something no analysis addresses individually.
    """

    SITE = "SITE"
    POWER_BLOCK = "POWER_BLOCK"
    INVERTER = "INVERTER"
    COMBINER = "COMBINER"
    TRACKER_ROW_BLOCK = "TRACKER_ROW_BLOCK"
    TRANSFORMER = "TRANSFORMER"
    WEATHER_STATION = "WEATHER_STATION"
    SUBSTATION = "SUBSTATION"
    REVENUE_METER = "REVENUE_METER"
    PLANT_CONTROLLER = "PLANT_CONTROLLER"
    POINT_OF_INTERCONNECTION = "POINT_OF_INTERCONNECTION"


#: Asset types that emit telemetry. Everything else exists in the model for
#: structure, control or configuration lineage.
TELEMETRY_BEARING: frozenset[AssetType] = frozenset(
    {
        AssetType.WEATHER_STATION,
        AssetType.INVERTER,
        AssetType.COMBINER,
        AssetType.TRACKER_ROW_BLOCK,
        AssetType.TRANSFORMER,
        AssetType.POWER_BLOCK,
        AssetType.SITE,
        AssetType.REVENUE_METER,
    }
)


@dataclass(frozen=True)
class Position:
    """Location within the site coordinate system.

    The origin is the south-west corner of the site footprint, x increasing
    east and y increasing north, in metres. This frame exists so the advected
    cloud field can compute a per-asset time offset from the wind vector.

    Attributes:
        x_m: Metres east of the origin.
        y_m: Metres north of the origin.
    """

    x_m: float
    y_m: float


@dataclass(frozen=True)
class Asset:
    """One addressable element of the plant.

    Attributes:
        asset_id: Stable identifier, derived from hierarchy position.
        asset_type: Kind of asset.
        parent_id: Identifier of the containing asset, or ``None`` for the site.
        position: Location in the site frame. Present for every
            telemetry-bearing asset.
        rated_capacity_kw: Nameplate where meaningful, otherwise ``None``.
        attributes: Type-specific properties, such as string counts on a
            combiner or rotation limits on a tracker row-block.
    """

    asset_id: str
    asset_type: AssetType
    parent_id: str | None
    position: Position | None = None
    rated_capacity_kw: float | None = None
    attributes: dict[str, float | int | str | bool] = field(default_factory=dict)

    @property
    def emits_telemetry(self) -> bool:
        """Whether this asset produces a telemetry stream.

        Returns:
            ``True`` when the asset type is telemetry-bearing.
        """
        return self.asset_type in TELEMETRY_BEARING


class Plant:
    """An instantiated plant: every asset, indexed and navigable.

    Args:
        assets: Assets in creation order, which is hierarchy order.
        config_version: Version of the configuration that produced this plant,
            carried so a dataset can be traced back to its definition.
    """

    def __init__(self, assets: list[Asset], config_version: str) -> None:
        """Index the assets by identifier and by parent."""
        self.config_version = config_version
        self._assets: dict[str, Asset] = {}
        self._children: dict[str, list[str]] = {}

        for asset in assets:
            if asset.asset_id in self._assets:
                raise ValueError(f"duplicate asset id: {asset.asset_id}")
            self._assets[asset.asset_id] = asset
            if asset.parent_id is not None:
                self._children.setdefault(asset.parent_id, []).append(asset.asset_id)

    def __len__(self) -> int:
        """Count all assets.

        Returns:
            Total asset count.
        """
        return len(self._assets)

    def __iter__(self) -> Iterator[Asset]:
        """Iterate assets in creation order.

        Returns:
            An iterator over every asset.
        """
        return iter(self._assets.values())

    def __contains__(self, asset_id: object) -> bool:
        """Test whether an identifier is present.

        Args:
            asset_id: Identifier to look for.

        Returns:
            ``True`` when the asset exists.
        """
        return asset_id in self._assets

    def get(self, asset_id: str) -> Asset:
        """Retrieve one asset.

        Args:
            asset_id: Identifier to look up.

        Returns:
            The matching asset.

        Raises:
            KeyError: If no such asset exists.
        """
        if asset_id not in self._assets:
            raise KeyError(f"unknown asset: {asset_id}")
        return self._assets[asset_id]

    def of_type(self, asset_type: AssetType) -> list[Asset]:
        """List every asset of one kind.

        Args:
            asset_type: Kind to filter on.

        Returns:
            Matching assets in creation order.
        """
        return [a for a in self._assets.values() if a.asset_type == asset_type]

    def children(self, asset_id: str) -> list[Asset]:
        """List the direct children of an asset.

        Args:
            asset_id: Parent identifier.

        Returns:
            Direct children in creation order, empty if the asset is a leaf.
        """
        return [self._assets[cid] for cid in self._children.get(asset_id, [])]

    def descendants(self, asset_id: str) -> list[Asset]:
        """List every asset beneath one asset, at any depth.

        Args:
            asset_id: Root of the subtree.

        Returns:
            Descendants in breadth-first order.
        """
        found: list[Asset] = []
        queue = list(self._children.get(asset_id, []))
        while queue:
            current = queue.pop(0)
            found.append(self._assets[current])
            queue.extend(self._children.get(current, []))
        return found

    def ancestors(self, asset_id: str) -> list[Asset]:
        """Walk from an asset up to the site.

        Args:
            asset_id: Starting asset.

        Returns:
            Ancestors ordered nearest first.

        Raises:
            KeyError: If the asset does not exist.
        """
        chain: list[Asset] = []
        current = self.get(asset_id).parent_id
        while current is not None:
            parent = self._assets[current]
            chain.append(parent)
            current = parent.parent_id
        return chain

    def telemetry_assets(self) -> list[Asset]:
        """List every asset that produces a telemetry stream.

        Returns:
            Telemetry-bearing assets in creation order.
        """
        return [a for a in self._assets.values() if a.emits_telemetry]

    def counts(self) -> dict[str, int]:
        """Count assets by type.

        Returns:
            A mapping of asset type name to count, useful for reconciliation
            and for the dataset acceptance report.
        """
        return dict(Counter(a.asset_type.value for a in self._assets.values()))
