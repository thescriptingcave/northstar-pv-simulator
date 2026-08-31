"""Open-Meteo precipitation and ERCOT market price adapters.

Open-Meteo is not optional. NSRDB carries no precipitation field, and the
soiling model is driven by real rainfall, so without this source soiling
degrades to a climatological guess. Its ambient temperature series doubles as
the independent cross-check that catches coordinate and timezone errors.

ERCOT supplies the price series that makes economic curtailment and cause-
attributed lost revenue possible.

Reference: design document ``19_external_data_acquisition`` sections 4.3-4.4.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date

import pandas as pd

from ..cache import PartitionKey
from ..harmonize import apply_harmonization
from ..http import ErcotTokenManager
from ..sources.base import FetchResult, Source
from ..validate import (
    ValidationReport,
    validate_price_partition,
    validate_weather_partition,
)

LOGGER = logging.getLogger(__name__)

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ERCOT_API_HOST = "https://api.ercot.com/api/public-reports"

#: ERCOT reports delivery dates in Central Prevailing Time - local clock time
#: including daylight saving - not UTC.
MARKET_TIMEZONE = "America/Chicago"

#: Preference order when a settlement point is published under several type
#: codes. "LZ" is the plain load zone price; "LZEW" is an alternative series
#: for the same zone. Hubs ("HU") and resource nodes ("RN") are unambiguous.
#:
#: Measured over June 2025 at LZ_WEST: LZ and LZEW have identical mean
#: (34.36), identical min (-5.58), identical negative-interval count (173), and
#: agree exactly on 80% of intervals; where they differ the mean gap is -0.012
#: and the largest 0.96. They are the same product with rounding or settlement
#: revision, not competing series - so the default is safe. See `42 §9`.
PREFERRED_POINT_TYPES = ("RN", "HU", "LZ", "LZEW")

#: Rows per page. The API caps page size; a request for 10,000 is rejected
#: rather than clamped, which is one cause of an apparently empty result.
PAGE_SIZE = 1000

# ERCOT product endpoints. Real-time settlement point prices are produced from
# SCED every 15 minutes and are the settlement grain used by the financial model.
ERCOT_ENDPOINTS = {
    "RT_SPP": "/np6-905-cd/spp_node_zone_hub",
    "DA_SPP": "/np4-190-cd/dam_stlmnt_pnt_prices",
    "RT_LMP": "/np6-788-cd/lmp_node_zone_hub",
}


class OpenMeteoSource(Source):
    """Open-Meteo ERA5 archive: hourly precipitation and cross-check fields.

    One partition per calendar year, aligned with the NSRDB years so the
    cross-source temperature check always has a matching pair.
    """

    source_id = "SRC-WX-03"
    slug = "open_meteo_era5"
    domain = "weather"
    provider = "Open-Meteo Archive (ERA5 reanalysis)"
    endpoint = OPEN_METEO_ARCHIVE_URL
    client_name = "direct"
    license_note = "Free for non-commercial use; ERA5 attribution to Copernicus / ECMWF."
    notes = (
        "Primary role is precipitation, which NSRDB does not carry. Temperature "
        "is retained as the independent cross-check for coordinate and timezone "
        "errors."
    )

    #: Open-Meteo labels hourly values at the interval beginning.
    SOURCE_INTERVAL_CONVENTION = "beginning"
    INTERVAL_MINUTES = 60

    DEFAULT_VARIABLES = (
        "precipitation",
        "rain",
        "snowfall",
        "temperature_2m",
        "wind_speed_10m",
        "wind_direction_10m",
        "relative_humidity_2m",
        "cloud_cover",
    )

    def partitions(self) -> list[PartitionKey]:
        """Enumerate one partition per configured calendar year.

        Returns:
            Partition keys ordered by year ascending.
        """
        return [
            self.partition_for(site=self.config.site.name, year=year)
            for year in sorted(self.source_config.years)
        ]

    def _expected_rows(self, year: int) -> int:
        """Compute the hourly row count for a complete year.

        Args:
            year: Calendar year, used to account for leap days.

        Returns:
            Expected sample count.
        """
        return (366 if calendar.isleap(year) else 365) * 24

    def fetch(self, key: PartitionKey) -> FetchResult:
        """Acquire and harmonize one year of reanalysis data.

        Args:
            key: Partition key carrying the target year.

        Returns:
            The harmonized result.
        """
        year = int(dict(key.parts)["year"])
        variables = list(self.source_config.fields) or list(self.DEFAULT_VARIABLES)

        response = self.client.get(
            OPEN_METEO_ARCHIVE_URL,
            params={
                "latitude": self.config.site.latitude,
                "longitude": self.config.site.longitude,
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "hourly": ",".join(variables),
                "timezone": "UTC",
                "wind_speed_unit": "ms",
            },
        )
        payload = response.json()["hourly"]
        raw = pd.DataFrame(payload).rename(columns={"time": "time"})
        raw["time"] = pd.to_datetime(raw["time"], utc=True)

        frame, steps = apply_harmonization(
            raw,
            self.config.harmonization,
            source_convention=self.SOURCE_INTERVAL_CONVENTION,
            interval_minutes=self.INTERVAL_MINUTES,
            correct_wind=True,
        )
        return FetchResult(
            frame=frame,
            transformations=steps,
            expected_rows=self._expected_rows(year),
        )

    def validate(self, key: PartitionKey, result: FetchResult) -> ValidationReport:
        """Run the weather check set against a fetched year.

        Irradiance-specific checks pass trivially because this source carries no
        irradiance; the structural and temperature checks are the substance.

        Args:
            key: Partition being validated.
            result: Output of :meth:`fetch`.

        Returns:
            The validation report.
        """
        return validate_weather_partition(
            result.frame, label=key.label(), expected_rows=result.expected_rows
        )


class ErcotPriceSource(Source):
    """ERCOT Public API settlement point prices.

    One partition per settlement point per month. Monthly granularity keeps a
    single request within the provider's pagination limits and makes the trailing
    re-fetch for price corrections cheap: only recent months are revisited.

    Prices are signed and are never clipped at zero. A West Texas year without
    negative prices means the fetch is wrong, and the validation layer treats
    that as a blocking failure rather than a curiosity.
    """

    source_id = "SRC-PX-01"
    slug = "ercot_rt_spp"
    domain = "prices"
    provider = "ERCOT Public API, real-time settlement point prices"
    endpoint = ERCOT_ENDPOINTS["RT_SPP"]
    client_name = "direct"
    license_note = "Public data; review ERCOT terms of use before redistribution."
    notes = (
        "ERCOT issues price corrections after initial publication, so recent "
        "months are re-fetched on every run and flagged when values change."
    )

    #: ERCOT labels settlement intervals at the interval ending.
    SOURCE_INTERVAL_CONVENTION = "ending"
    INTERVAL_MINUTES = 15
    PRICE_TYPE = "RT_SPP"

    #: B2C application identifier for the ERCOT public API.
    CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"

    def __init__(self, *args, token_manager: ErcotTokenManager | None = None, **kwargs):
        """Initialise the adapter and its token manager.

        Args:
            *args: Positional arguments forwarded to :class:`Source`.
            token_manager: Optional pre-built manager, injectable for testing.
            **kwargs: Keyword arguments forwarded to :class:`Source`.
        """
        super().__init__(*args, **kwargs)
        self._token_manager = token_manager

    def check_credentials(self) -> None:
        """Assert that ERCOT account credentials are configured.

        Raises:
            RuntimeError: If any required credential is missing.
        """
        self.credentials.require(
            "ercot_username", "ercot_password", "ercot_subscription_key"
        )

    @property
    def token_manager(self) -> ErcotTokenManager:
        """Lazily construct the token manager.

        Returns:
            A manager bound to the configured credentials.
        """
        if self._token_manager is None:
            self._token_manager = ErcotTokenManager(
                username=self.credentials.ercot_username or "",
                password=self.credentials.ercot_password or "",
                subscription_key=self.credentials.ercot_subscription_key or "",
                client_id=self.CLIENT_ID,
                timeout_s=self.config.request_timeout_s,
            )
        return self._token_manager

    def settlement_points(self) -> list[str]:
        """List the settlement points to acquire.

        Returns:
            Hub, load zone and plant resource node, in that order. The hub is
            the hedge index, the node supports basis calculation, and the zone
            provides regional context.
        """
        site = self.config.site
        return [
            site.hub_settlement_point,
            site.zone_settlement_point,
            site.node_settlement_point,
        ]

    def partitions(self) -> list[PartitionKey]:
        """Enumerate one partition per settlement point per month.

        Returns:
            Partition keys ordered by point, then year, then month.
        """
        keys: list[PartitionKey] = []
        for point in self.settlement_points():
            for year in sorted(self.source_config.years):
                for month in range(1, 13):
                    keys.append(
                        self.partition_for(point=point, year=year, month=f"{month:02d}")
                    )
        return keys

    def _expected_rows(self, year: int, month: int) -> int:
        """Compute the settlement interval count for one month.

        Args:
            year: Calendar year.
            month: Calendar month, one-based.

        Returns:
            Expected interval count at the settlement grain.
        """
        days = calendar.monthrange(year, month)[1]
        return days * 24 * (60 // self.INTERVAL_MINUTES)

    def fetch(self, key: PartitionKey) -> FetchResult:
        """Acquire and harmonize one settlement point month.

        Args:
            key: Partition key carrying point, year and month.

        Returns:
            The harmonized result.
        """
        parts = dict(key.parts)
        point = parts["point"]
        year = int(parts["year"])
        month = int(parts["month"])
        last_day = calendar.monthrange(year, month)[1]

        raw = self._fetch_all_pages(
            point,
            date(year, month, 1).isoformat(),
            date(year, month, last_day).isoformat(),
        )

        frame, steps = apply_harmonization(
            raw,
            self.config.harmonization,
            source_convention=self.SOURCE_INTERVAL_CONVENTION,
            interval_minutes=self.INTERVAL_MINUTES,
            correct_wind=False,
        )
        return FetchResult(
            frame=frame,
            transformations=steps,
            expected_rows=self._expected_rows(year, month),
        )

    def _fetch_all_pages(self, point: str, date_from: str, date_to: str) -> pd.DataFrame:
        """Fetch every page of a settlement-point price query.

        **The API paginates and this client originally read only page one.** A
        single month of one node returned 1,000 rows against a reported
        ``totalRecords`` of 2,974 across 3 pages - a silent loss of two thirds
        of the data, which then validated cleanly because the rows that were
        present were well-formed.

        The row-count check does not catch it: a partial month has the wrong
        count, but so does a month containing a genuine market outage, which is
        why that check warns rather than blocks.

        Inherited by :class:`ErcotDayAheadPriceSource`, which overrides only
        the parsing.

        Args:
            point: Settlement point identifier.
            date_from: Inclusive start date, ISO format.
            date_to: Inclusive end date, ISO format.

        Returns:
            Every row across every page, concatenated.

        Raises:
            RuntimeError: If the row count disagrees with the API's own
                ``totalRecords``, so a truncated month is never accepted.
        """
        frames: list[pd.DataFrame] = []
        received = 0
        page = 1
        total_pages = 1
        total_records: int | None = None

        while page <= total_pages:
            response = self.client.get(
                f"{ERCOT_API_HOST}{self.endpoint}",
                params={
                    "settlementPoint": point,
                    "deliveryDateFrom": date_from,
                    "deliveryDateTo": date_to,
                    "size": PAGE_SIZE,
                    "page": page,
                },
                headers=self.token_manager.headers(),
                on_unauthorized=self.token_manager.refresh_headers,
            )
            payload = response.json()
            # Count what the API sent, before any filtering. The completeness
            # check below compares against the API's own totalRecords, which
            # counts raw rows - including the second type code for a load zone.
            received += len(payload.get("data") or [])
            frames.append(self._parse_response(payload, point))

            meta = payload.get("_meta") or {}
            total_pages = int(meta.get("totalPages") or 1)
            if total_records is None and meta.get("totalRecords") is not None:
                total_records = int(meta["totalRecords"])
            page += 1

        combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        # Trust the API's own count over our arithmetic. A truncated price
        # month is worse than no month at all.
        #
        # Compare **rows received**, not rows kept. `totalRecords` counts raw
        # rows, and a load zone publishes every interval under two type codes -
        # so a complete January arrives as 5,952 rows and is correctly reduced
        # to 2,976. Comparing the post-filter count against the pre-filter total
        # failed a perfectly good month.
        if total_records is not None and received != total_records:
            raise RuntimeError(
                f"paged fetch for {point} {date_from}..{date_to} received "
                f"{received} rows against a reported totalRecords of "
                f"{total_records}"
            )
        return combined

    def _parse_response(self, payload: dict, point: str) -> pd.DataFrame:
        """Convert an ERCOT API JSON payload into a tidy frame.

        The API returns a ``fields`` descriptor alongside a positional ``data``
        array, so column order is read from the payload rather than assumed.

        Args:
            payload: Decoded JSON response body.
            point: Settlement point identifier to stamp onto every row.

        Returns:
            A frame carrying ``time``, ``settlement_point``, ``price_type``,
            ``price_usd_mwh`` and ``is_corrected``.

        Raises:
            ValueError: If the payload lacks the expected structure.
        """
        if "data" not in payload or "fields" not in payload:
            raise ValueError("unexpected ERCOT payload: missing 'data' or 'fields'")

        columns = [field["name"] for field in payload["fields"]]
        frame = pd.DataFrame(payload["data"], columns=columns)

        date_column = _first_present(frame, ["deliveryDate", "DeliveryDate"])
        hour_column = _first_present(
            frame, ["deliveryHour", "DeliveryHour", "hourEnding", "HourEnding"]
        )
        interval_column = _first_present(frame, ["deliveryInterval", "DeliveryInterval"])
        price_column = _first_present(
            frame, ["settlementPointPrice", "SettlementPointPrice", "price"]
        )

        # A settlement point can be reported under more than one type code with
        # an identical price - LZ_WEST appears as both "LZ" and "LZEW", giving
        # two rows per interval that differ in nothing but the label. Keeping
        # both doubles every partition and trips the uniqueness check.
        frame = self._drop_duplicate_point_types(frame, point)

        # ERCOT labels intervals by their END: hour ending 1, interval 1 is the
        # first 15 minutes of the day and is labelled 00:15.
        #
        # The canonical convention is **interval-beginning** (`11 §3`), so one
        # interval is subtracted here rather than in harmonization. Doc 11 warns
        # that getting this wrong "shifts everything by one interval and is
        # invisible until reconciliation fails" - and it is worse than that at a
        # DST fall-back: the interval ending 02:00 is not an ambiguous local
        # time, so DSTFlag cannot separate the two occurrences and they collapse
        # into one row. Interval-beginning puts all four inside the repeated
        # hour, where the flag resolves them.
        minutes = (
            frame[hour_column].astype(int) * 60
            - 60
            + frame[interval_column].astype(int) * self.INTERVAL_MINUTES
            - self.INTERVAL_MINUTES
        )

        # **ERCOT delivery dates are Central Prevailing Time, not UTC.**
        # Parsing them as UTC shifted every price by 5-6 hours, which would
        # have joined prices to the wrong hours of generation and silently
        # corrupted capture rate, curtailment economics and lost-revenue
        # attribution. Nothing detects that: the series is well-formed, the
        # right length, and wrong.
        naive = pd.to_datetime(frame[date_column]) + pd.to_timedelta(minutes, unit="m")

        # At fall-back the local hour repeats and DSTFlag is the disambiguator:
        # True is the first (daylight) occurrence, False the second (standard).
        # pandas uses the same convention for `ambiguous`.
        times = self._localize(naive, frame)

        return pd.DataFrame(
            {
                "time": times,
                "settlement_point": point,
                "price_type": self.PRICE_TYPE,
                "price_usd_mwh": pd.to_numeric(frame[price_column], errors="coerce"),
                "is_corrected": False,
            }
        )

    @staticmethod
    def _localize(naive: pd.Series, frame: pd.DataFrame) -> pd.Series:
        """Convert naive Central Prevailing Time to UTC, resolving DST.

        Shared by the real-time and day-ahead parsers. It exists as a helper
        because the day-ahead subclass overrides `_parse_response` and
        therefore inherited none of the timezone fixes applied to the parent -
        a defect that surfaced as exactly one duplicate row in each November
        day-ahead partition, long after real-time was correct.

        Args:
            naive: Timezone-naive local timestamps, interval-beginning.
            frame: Source rows, read for a ``DSTFlag`` column.

        Returns:
            UTC timestamps.
        """
        dst_column = _first_present_optional(frame, ["DSTFlag", "dstFlag"])
        # At fall-back the local hour repeats; DSTFlag True is the first
        # (daylight) occurrence, False the second, matching pandas.
        ambiguous = (
            frame[dst_column].astype(bool).to_numpy()
            if dst_column is not None
            else "raise"
        )
        return naive.dt.tz_localize(
            MARKET_TIMEZONE,
            ambiguous=ambiguous,
            # Spring-forward has no 02:00 local. ERCOT should not emit one,
            # but shifting is safer than raising mid-backfill.
            nonexistent="shift_forward",
        ).dt.tz_convert("UTC")

    @staticmethod
    def _drop_duplicate_point_types(frame: pd.DataFrame, point: str) -> pd.DataFrame:
        """Collapse rows that repeat a point under a second type code.

        ``LZ_WEST`` is returned as both ``LZ`` and ``LZEW`` with the same price,
        so every interval appears twice. Observed on a real backfill as 2,976
        duplicate rows in a 2,976-row month.

        On a single sampled day the two agreed, so an earlier version dropped
        duplicates only when prices matched and raised otherwise. Across a full
        year they **do** diverge, and raising stranded every remaining partition
        in the backfill.

        A type is now selected by documented preference and the disagreement is
        logged rather than fatal. Which series is correct for settlement is a
        question about ERCOT's products, not about this code, and
        `make ercot-lz-compare` is the tool for answering it.

        Args:
            frame: Raw rows as returned by the API.
            point: Settlement point being fetched, for the error message.

        Returns:
            One row per interval, from the preferred type.
        """
        type_column = _first_present_optional(
            frame, ["settlementPointType", "SettlementPointType"]
        )
        if type_column is None or frame[type_column].nunique() <= 1:
            return frame

        keys = [
            c
            for c in ("deliveryDate", "deliveryHour", "deliveryInterval", "DSTFlag")
            if c in frame.columns
        ]
        price_column = _first_present(
            frame, ["settlementPointPrice", "SettlementPointPrice", "price"]
        )

        present = sorted(frame[type_column].unique())

        # Choose deterministically by preference rather than by whichever row
        # the API happened to return first. A load zone is reported under both
        # "LZ" and "LZEW"; on a single sampled day the prices matched, and
        # across a full year they do not, so the choice is real.
        chosen = next((t for t in PREFERRED_POINT_TYPES if t in present), present[0])

        spread = frame.groupby(keys, dropna=False)[price_column].nunique()
        disagreeing = int((spread > 1).sum())
        if disagreeing:
            # Not fatal. These are alternative published series for the same
            # point, and refusing to fetch strands every other partition in the
            # run. Recorded loudly so the choice is visible and reviewable -
            # `make ercot-lz-compare` quantifies the difference.
            LOGGER.warning(
                "%s: types %s disagree on %d of %d intervals; keeping %s. "
                "Run `make ercot-lz-compare` to see how far apart they are.",
                point,
                present,
                disagreeing,
                len(spread),
                chosen,
            )

        return frame[frame[type_column] == chosen].copy()

    def validate(self, key: PartitionKey, result: FetchResult) -> ValidationReport:
        """Run the price check set against a fetched month.

        The negative-price assertion is deliberately not applied here. A single
        month can legitimately contain no negative intervals, so asserting at
        month level would produce false failures. The assertion is made at year
        level by the orchestrator once all twelve months are cached.

        Args:
            key: Partition being validated.
            result: Output of :meth:`fetch`.

        Returns:
            The validation report.
        """
        return validate_price_partition(
            result.frame, label=key.label(), require_negatives=False
        )


class ErcotDayAheadPriceSource(ErcotPriceSource):
    """ERCOT day-ahead market settlement point prices, hourly.

    Not used for settlement in V1, which assumes the plant is fully real-time
    exposed. Acquired because the day-ahead to real-time spread is a required
    forecasting input, and because a future version can add day-ahead offer
    behaviour without a second acquisition project.
    """

    source_id = "SRC-PX-02"
    slug = "ercot_dam_spp"
    provider = "ERCOT Public API, day-ahead settlement point prices"
    endpoint = ERCOT_ENDPOINTS["DA_SPP"]
    INTERVAL_MINUTES = 60
    PRICE_TYPE = "DA_SPP"

    def _parse_response(self, payload: dict, point: str) -> pd.DataFrame:
        """Convert a day-ahead payload into a tidy frame.

        Day-ahead results are hourly and carry no interval field, so the
        timestamp is derived from the hour-ending value alone.

        Args:
            payload: Decoded JSON response body.
            point: Settlement point identifier.

        Returns:
            A frame in the same shape as the real-time parser produces.

        Raises:
            ValueError: If the payload lacks the expected structure.
        """
        if "data" not in payload or "fields" not in payload:
            raise ValueError("unexpected ERCOT payload: missing 'data' or 'fields'")

        columns = [field["name"] for field in payload["fields"]]
        frame = pd.DataFrame(payload["data"], columns=columns)

        date_column = _first_present(frame, ["deliveryDate", "DeliveryDate"])
        hour_column = _first_present(frame, ["hourEnding", "HourEnding", "deliveryHour"])
        price_column = _first_present(
            frame, ["settlementPointPrice", "SettlementPointPrice", "price"]
        )

        # This override carried the same three defects the real-time parser
        # had, and they were fixed only there: dates treated as UTC, labels
        # left interval-ending, and no DST handling. Day-ahead November failed
        # with exactly 1 duplicate - one repeated hour - after real-time was
        # already correct.
        #
        # A subclass that overrides parsing inherits none of the parent's
        # fixes. Anything true of both belongs in a shared helper, which is
        # what `_localize` now is.
        frame = self._drop_duplicate_point_types(frame, point)

        # "hourEnding" is 1-24 and may arrive as "01:00". Interval-beginning
        # means subtracting one hour (`11 §3`).
        hours = frame[hour_column].astype(str).str.split(":").str[0].astype(int) - 1
        naive = pd.to_datetime(frame[date_column]) + pd.to_timedelta(hours, unit="h")

        return pd.DataFrame(
            {
                "time": self._localize(naive, frame),
                "settlement_point": point,
                "price_type": self.PRICE_TYPE,
                "price_usd_mwh": pd.to_numeric(frame[price_column], errors="coerce"),
                "is_corrected": False,
            }
        )


def _first_present_optional(frame: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the first candidate column present, or ``None``.

    Unlike :func:`_first_present`, absence is a valid answer. Used for fields
    that some report revisions omit - ``DSTFlag`` and ``settlementPointType``
    are both optional in older payloads, and a missing one should degrade the
    handling rather than fail the fetch.

    Args:
        frame: Frame to inspect.
        candidates: Acceptable column names, in preference order.

    Returns:
        The matching column name, or ``None`` if none is present.
    """
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    return None


def _first_present(frame: pd.DataFrame, candidates: list[str]) -> str:
    """Return the first candidate column name present in a frame.

    Provider field casing has changed across ERCOT API revisions, so column
    resolution is tolerant rather than exact.

    Args:
        frame: Frame to inspect.
        candidates: Acceptable column names, in preference order.

    Returns:
        The matching column name.

    Raises:
        ValueError: If none of the candidates is present.
    """
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    raise ValueError(
        f"none of {candidates} present; available columns: {list(frame.columns)}"
    )
