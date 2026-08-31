"""NSRDB PSM v4 adapters for solar resource data.

Three provider facts drive this module and invalidate most published tutorials:

* The NREL developer domain was retired on 29 May 2026. Requests now go to
  ``developer.nlr.gov``.
* PSM v3.2.2 is deprecated in favour of PSM v4. The GOES CONUS product supplies
  2 km, 5-minute data from 2018 onward, which is the reason this design
  downscales only from 5 minutes to 1 minute rather than from an hour.
* pvlib removed ``get_psm3`` and added ``get_nsrdb_psm4_*`` in 0.13. The PSM4
  client is used where available, with a direct HTTP fallback so the package
  does not hard-depend on pvlib being installed in the fetch environment.

Reference: design document ``19_external_data_acquisition`` sections 2 and 4.
"""

from __future__ import annotations

import calendar
import io
import logging

import pandas as pd

from ..cache import PartitionKey
from ..harmonize import apply_harmonization
from ..sources.base import FetchResult, Source
from ..validate import ValidationReport, validate_weather_partition

LOGGER = logging.getLogger(__name__)

NSRDB_HOST = "https://developer.nlr.gov"

# Provider field names requested by default. These are PSM4 attribute names, not
# pvlib names; harmonization renames them afterwards.
DEFAULT_ATTRIBUTES = (
    "ghi",
    "dni",
    "dhi",
    "air_temperature",
    "dew_point",
    "wind_speed",
    "wind_direction",
    "relative_humidity",
    "surface_pressure",
    "surface_albedo",
    "solar_zenith_angle",
)


def _read_nsrdb_csv(text: str) -> pd.DataFrame:
    """Parse the NSRDB standard time-series CSV layout.

    The format carries two metadata rows before the data header, so the data
    block starts at the third line.

    Args:
        text: Raw CSV response body.

    Returns:
        A frame with a timezone-aware UTC ``time`` column and the provider's
        data columns unchanged.

    Raises:
        ValueError: If the expected year, month, day, hour and minute columns
            are absent, which indicates a changed provider format.
    """
    frame = pd.read_csv(io.StringIO(text), skiprows=2)
    required = {"Year", "Month", "Day", "Hour", "Minute"}
    if not required.issubset(frame.columns):
        raise ValueError(
            "unexpected NSRDB CSV layout: missing "
            f"{sorted(required - set(frame.columns))}"
        )
    frame["time"] = pd.to_datetime(
        frame[["Year", "Month", "Day", "Hour", "Minute"]], utc=True
    )
    return frame.drop(columns=["Year", "Month", "Day", "Hour", "Minute"])


class NsrdbConusSource(Source):
    """NSRDB GOES CONUS PSM v4: 2 km, 5-minute, 2018 onward.

    One partition per calendar year. A year of 5-minute data is a single
    provider request, which keeps request count far below the daily ceiling
    while still allowing an interrupted multi-year fetch to resume cheaply.
    """

    source_id = "SRC-WX-01"
    slug = "nsrdb_goes_conus_v4"
    domain = "weather"
    provider = "NSRDB GOES CONUS v4 (PSM4)"
    endpoint = "/api/nsrdb/v2/solar/nsrdb-GOES-conus-v4-0-0-download"
    client_name = "pvlib.iotools.get_nsrdb_psm4_conus"
    license_note = (
        "US Government work, public domain. Attribution to NSRDB and NLR requested."
    )
    notes = (
        "PSM4 supersedes PSM3 and can differ in annual energy by roughly 1-3%. "
        "Do not mix products across years within one dataset."
    )

    #: The provider labels 5-minute samples at the interval beginning.
    SOURCE_INTERVAL_CONVENTION = "beginning"

    def check_credentials(self) -> None:
        """Assert that an NLR API key and contact email are configured.

        Raises:
            RuntimeError: If either credential is missing.
        """
        self.credentials.require("nrel_api_key", "nrel_email")

    def partitions(self) -> list[PartitionKey]:
        """Enumerate one partition per configured calendar year.

        Returns:
            Partition keys ordered by year ascending.
        """
        return [
            self.partition_for(site=self.config.site.name, year=year)
            for year in sorted(self.source_config.years)
        ]

    def _time_step(self) -> int:
        """Resolve the requested temporal resolution.

        Returns:
            Interval in minutes, defaulting to the product's native 5 minutes.
        """
        return self.source_config.time_step_min or 5

    def _expected_rows(self, year: int) -> int:
        """Compute the row count a complete year should contain.

        Args:
            year: Calendar year, used to account for leap days.

        Returns:
            Expected sample count at the configured interval.
        """
        days = 366 if calendar.isleap(year) else 365
        return days * 24 * (60 // self._time_step())

    def _fetch_raw(self, year: int) -> pd.DataFrame:
        """Retrieve one year of raw provider data.

        Prefers the pvlib PSM4 client when it is installed, because it tracks
        provider parameter changes. Falls back to a direct request against the
        documented endpoint so the fetch client remains usable without pvlib.

        Args:
            year: Calendar year to retrieve.

        Returns:
            Raw provider data with a ``time`` column.
        """
        attributes = list(self.source_config.fields) or list(DEFAULT_ATTRIBUTES)
        try:
            from pvlib.iotools import get_nsrdb_psm4_conus  # noqa: PLC0415
        except ImportError:
            LOGGER.info("pvlib PSM4 client unavailable; using direct HTTP")
        else:
            frame, _metadata = get_nsrdb_psm4_conus(
                latitude=self.config.site.latitude,
                longitude=self.config.site.longitude,
                api_key=self.credentials.nrel_api_key,
                email=self.credentials.nrel_email,
                year=year,
                time_step=self._time_step(),
                parameters=attributes,
                utc=True,
                map_variables=False,
            )
            return frame.reset_index(names="time")

        response = self.client.get(
            f"{NSRDB_HOST}{self.endpoint}.csv",
            params={
                "api_key": self.credentials.nrel_api_key,
                "email": self.credentials.nrel_email,
                "wkt": (
                    f"POINT({self.config.site.longitude} {self.config.site.latitude})"
                ),
                "names": str(year),
                "interval": str(self._time_step()),
                "attributes": ",".join(attributes),
                "utc": "true",
                "leap_day": "true",
            },
        )
        return _read_nsrdb_csv(response.text)

    def fetch(self, key: PartitionKey) -> FetchResult:
        """Acquire and harmonize one year of CONUS resource data.

        Args:
            key: Partition key carrying the target year.

        Returns:
            The harmonized result.
        """
        year = int(dict(key.parts)["year"])
        raw = self._fetch_raw(year)
        frame, steps = apply_harmonization(
            raw,
            self.config.harmonization,
            source_convention=self.SOURCE_INTERVAL_CONVENTION,
            interval_minutes=self._time_step(),
            correct_wind=True,
        )
        return FetchResult(
            frame=frame,
            transformations=steps,
            expected_rows=self._expected_rows(year),
        )

    def validate(self, key: PartitionKey, result: FetchResult) -> ValidationReport:
        """Run the weather check set against a fetched year.

        Args:
            key: Partition being validated.
            result: Output of :meth:`fetch`.

        Returns:
            The validation report.
        """
        return validate_weather_partition(
            result.frame, label=key.label(), expected_rows=result.expected_rows
        )


class NsrdbTmySource(Source):
    """NSRDB GOES TMY v4: 4 km, hourly typical meteorological year.

    Fetched once. Supplies the P50 expected-energy baseline used for
    weather-adjusted budget-versus-actual variance. Hourly resolution is
    acceptable here because TMY drives annual expectation, not ramp analysis.
    """

    source_id = "SRC-WX-02"
    slug = "nsrdb_goes_tmy_v4"
    domain = "weather"
    provider = "NSRDB GOES TMY v4 (PSM4)"
    endpoint = "/api/nsrdb/v2/solar/nsrdb-GOES-tmy-v4-0-0-download"
    client_name = "pvlib.iotools.get_nsrdb_psm4_tmy"
    license_note = (
        "US Government work, public domain. Attribution to NSRDB and NLR requested."
    )
    notes = (
        "Typical year; timestamps are synthetic and must not be joined to actual years."
    )

    SOURCE_INTERVAL_CONVENTION = "beginning"
    #: TMY products are hourly only.
    INTERVAL_MINUTES = 60

    def check_credentials(self) -> None:
        """Assert that an NLR API key and contact email are configured.

        Raises:
            RuntimeError: If either credential is missing.
        """
        self.credentials.require("nrel_api_key", "nrel_email")

    def partitions(self) -> list[PartitionKey]:
        """Enumerate the single TMY partition.

        Returns:
            A one-element list keyed by site.
        """
        return [self.partition_for(site=self.config.site.name)]

    def _fetch_raw(self) -> pd.DataFrame:
        """Retrieve the typical meteorological year.

        Returns:
            Raw provider data with a ``time`` column.
        """
        attributes = list(self.source_config.fields) or list(DEFAULT_ATTRIBUTES)
        try:
            from pvlib.iotools import get_nsrdb_psm4_tmy  # noqa: PLC0415
        except ImportError:
            LOGGER.info("pvlib PSM4 client unavailable; using direct HTTP")
        else:
            frame, _metadata = get_nsrdb_psm4_tmy(
                latitude=self.config.site.latitude,
                longitude=self.config.site.longitude,
                api_key=self.credentials.nrel_api_key,
                email=self.credentials.nrel_email,
                year="tmy",
                time_step=self.INTERVAL_MINUTES,
                parameters=attributes,
                utc=True,
                map_variables=False,
            )
            return frame.reset_index(names="time")

        response = self.client.get(
            f"{NSRDB_HOST}{self.endpoint}.csv",
            params={
                "api_key": self.credentials.nrel_api_key,
                "email": self.credentials.nrel_email,
                "wkt": (
                    f"POINT({self.config.site.longitude} {self.config.site.latitude})"
                ),
                "names": "tmy",
                "interval": str(self.INTERVAL_MINUTES),
                "attributes": ",".join(attributes),
                "utc": "true",
            },
        )
        return _read_nsrdb_csv(response.text)

    def fetch(self, key: PartitionKey) -> FetchResult:
        """Acquire and harmonize the typical meteorological year.

        Args:
            key: The single TMY partition key.

        Returns:
            The harmonized result.
        """
        raw = self._fetch_raw()
        frame, steps = apply_harmonization(
            raw,
            self.config.harmonization,
            source_convention=self.SOURCE_INTERVAL_CONVENTION,
            interval_minutes=self.INTERVAL_MINUTES,
            correct_wind=True,
        )
        return FetchResult(frame=frame, transformations=steps, expected_rows=8760)

    def validate(self, key: PartitionKey, result: FetchResult) -> ValidationReport:
        """Run the weather check set against the TMY partition.

        Args:
            key: Partition being validated.
            result: Output of :meth:`fetch`.

        Returns:
            The validation report.
        """
        return validate_weather_partition(
            result.frame, label=key.label(), expected_rows=result.expected_rows
        )
