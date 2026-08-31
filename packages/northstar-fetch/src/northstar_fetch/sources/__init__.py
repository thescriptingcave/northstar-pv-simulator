"""Provider adapters.

Each adapter declares the partitions it supplies and knows how to fetch and
harmonize one of them. Adding a provider or a market means adding one subclass
and one registry entry.
"""

from .base import FetchResult, Source
from .market import ErcotDayAheadPriceSource, ErcotPriceSource, OpenMeteoSource
from .nsrdb import NsrdbConusSource, NsrdbTmySource

__all__ = [
    "ErcotDayAheadPriceSource",
    "ErcotPriceSource",
    "FetchResult",
    "NsrdbConusSource",
    "NsrdbTmySource",
    "OpenMeteoSource",
    "Source",
]
