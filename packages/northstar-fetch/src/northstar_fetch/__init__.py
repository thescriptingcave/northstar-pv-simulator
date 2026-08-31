"""NorthStar PV resource acquisition client.

Fetches, harmonizes, validates and caches the external solar resource and market
data that the simulator consumes. The simulator itself performs no network
access; this package produces the versioned, checksummed cache that makes a
simulation run reproducible.

Reference: design document ``19_external_data_acquisition``.
"""

from .cache import PartitionKey, ResourceCache
from .config import Credentials, FetchConfig, load_config
from .orchestrator import FetchOrchestrator, RunSummary

__version__ = "0.1.0"

__all__ = [
    "Credentials",
    "FetchConfig",
    "FetchOrchestrator",
    "PartitionKey",
    "ResourceCache",
    "RunSummary",
    "load_config",
    "__version__",
]
