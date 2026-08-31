"""NorthStar PV plant simulator.

Phase 1 is complete: declarative plant configuration, a deterministic asset
identity model with site geometry, and a validation gate that reconciles
derived capacity against the instantiated asset tree.

Phases 2 onward - the pvlib physics core, spatial environment, state machines,
sensor model, loss attribution, scenarios and settlement - are not yet
implemented. The physics oracle gate in ``16_implementation_roadmap`` section 5
must pass before plant complexity grows beyond a single inverter.
"""

from .assets import Asset, AssetType, Plant, Position
from .builder import block_positions, build_plant, site_extent
from .plant_config import PlantConfig, load_plant_config
from .validation import ValidationReport, validate_plant

__version__ = "0.1.0"

__all__ = [
    "Asset",
    "AssetType",
    "Plant",
    "PlantConfig",
    "Position",
    "ValidationReport",
    "block_positions",
    "build_plant",
    "load_plant_config",
    "site_extent",
    "validate_plant",
    "__version__",
]
