"""Tests for the NorthStar resource fetch client.

Network access is never exercised. Provider behaviour is supplied through stub
adapters and stub sessions, so the whole pipeline - partitioning, harmonization,
validation, caching, resumption - is testable offline and deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from northstar_fetch.cache import PartitionKey, ResourceCache, sha256_file
from northstar_fetch.config import (
    Credentials,
    FetchConfig,
    HarmonizationConfig,
    SiteConfig,
    SourceConfig,
)
from northstar_fetch.harmonize import (
    apply_harmonization,
    correct_wind_height,
    normalize_time,
    rename_to_canonical,
)
from northstar_fetch.http import RateLimiter, RateLimitError, RetryPolicy
from northstar_fetch.orchestrator import FetchOrchestrator
from northstar_fetch.sources.base import FetchResult, Source
from northstar_fetch.sources.market import ErcotPriceSource
from northstar_fetch.validate import (
    check_monotonic_time,
    check_negative_prices_present,
    check_night_irradiance_zero,
    check_no_duplicate_times,
    check_timezone_aware,
    cross_source_temperature_correlation,
    validate_weather_partition,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def site() -> SiteConfig:
    """Build the locked NorthStar site identity.

    Returns:
        A :class:`SiteConfig` matching design decision DR-001.
    """
    return SiteConfig(
        name="northstar",
        latitude=31.35,
        longitude=-103.30,
        elevation_m=850.0,
        timezone="America/Chicago",
        hub_settlement_point="HB_WEST",
        zone_settlement_point="LZ_WEST",
        node_settlement_point="NORTHSTAR_RN",
    )


@pytest.fixture
def config(tmp_path: Path, site: SiteConfig) -> FetchConfig:
    """Build a fetch configuration pointing at a temporary cache.

    Args:
        tmp_path: pytest-provided temporary directory.
        site: Site identity fixture.

    Returns:
        A :class:`FetchConfig` with one stub source enabled.
    """
    return FetchConfig(
        cache_version="test.1",
        cache_root=tmp_path / "resource_cache",
        site=site,
        harmonization=HarmonizationConfig(),
        sources=[SourceConfig(source_id="SRC-WX-01", years=[2023])],
    )


def make_weather_frame(
    year: int = 2023, interval_minutes: int = 60, days: int = 2
) -> pd.DataFrame:
    """Build a synthetic but physically coherent weather series.

    Irradiance follows a clipped cosine of solar elevation, and the zenith angle
    is derived from the same curve, so the night-irradiance and closure checks
    behave as they would on real data.

    Args:
        year: Calendar year for the timestamps.
        interval_minutes: Sample spacing.
        days: Number of days to generate.

    Returns:
        A frame in provider-style column naming, ready for harmonization.
    """
    periods = days * 24 * (60 // interval_minutes)
    times = pd.date_range(
        f"{year}-06-01", periods=periods, freq=f"{interval_minutes}min", tz="UTC"
    )
    hour_of_day = times.hour + times.minute / 60.0
    elevation = np.sin(np.pi * (hour_of_day - 6.0) / 12.0)
    elevation = np.where(elevation > 0, elevation, 0.0)

    zenith = np.degrees(np.arccos(np.clip(elevation, 0.0, 1.0)))
    zenith = np.where(elevation > 0, zenith, 100.0)

    ghi = 950.0 * elevation
    dni = 850.0 * elevation
    dhi = np.where(elevation > 0, 100.0 * elevation, 0.0)
    # Force closure to hold: GHI = DHI + DNI * cos(zenith).
    ghi = dhi + dni * np.cos(np.radians(zenith)) * (elevation > 0)

    return pd.DataFrame(
        {
            "time": times,
            "ghi": ghi,
            "dni": dni,
            "dhi": dhi,
            "air_temperature": 20.0 + 15.0 * elevation,
            "wind_speed": 4.0 + 2.0 * elevation,
            "wind_direction": 180.0,
            "relative_humidity": 30.0,
            "surface_pressure": 910.0,
            "solar_zenith_angle": zenith,
        }
    )


class StubSource(Source):
    """In-memory source adapter used to exercise the orchestrator.

    Records how many times each partition was fetched so that skip behaviour can
    be asserted directly rather than inferred.
    """

    source_id = "SRC-WX-01"
    slug = "stub_weather"
    domain = "weather"
    provider = "Stub provider"
    endpoint = "/stub"
    client_name = "stub"

    def __init__(self, *args, frames: dict[str, pd.DataFrame], **kwargs) -> None:
        """Initialise with pre-built frames keyed by year.

        Args:
            *args: Positional arguments forwarded to :class:`Source`.
            frames: Provider-style frames keyed by year string.
            **kwargs: Keyword arguments forwarded to :class:`Source`.
        """
        super().__init__(*args, **kwargs)
        self.frames = frames
        self.fetch_calls: list[str] = []

    def partitions(self) -> list[PartitionKey]:
        """Enumerate one partition per available year.

        Returns:
            Partition keys ordered by year.
        """
        return [
            self.partition_for(site=self.config.site.name, year=year)
            for year in sorted(self.frames)
        ]

    def fetch(self, key: PartitionKey) -> FetchResult:
        """Return the pre-built frame for a partition.

        Args:
            key: Partition to fetch.

        Returns:
            The harmonized result.
        """
        year = dict(key.parts)["year"]
        self.fetch_calls.append(year)
        frame, steps = apply_harmonization(
            self.frames[year],
            self.config.harmonization,
            source_convention="beginning",
            interval_minutes=60,
            correct_wind=True,
        )
        return FetchResult(frame=frame, transformations=steps, expected_rows=len(frame))

    def validate(self, key: PartitionKey, result: FetchResult):
        """Run the weather check set.

        Args:
            key: Partition being validated.
            result: Output of :meth:`fetch`.

        Returns:
            The validation report.
        """
        return validate_weather_partition(
            result.frame, label=key.label(), expected_rows=result.expected_rows
        )


# --------------------------------------------------------------------------
# Harmonization
# --------------------------------------------------------------------------


def test_rename_maps_provider_columns_to_pvlib_names() -> None:
    """Provider column names are mapped and unknown columns survive."""
    frame = pd.DataFrame(
        {"air_temperature": [1.0], "temperature_2m": [2.0], "mystery": [3.0]}
    )
    renamed = rename_to_canonical(frame)
    assert "temp_air" in renamed.columns
    assert "mystery" in renamed.columns


def test_interval_ending_is_shifted_back_to_interval_beginning() -> None:
    """An interval-ending source is shifted by exactly one interval."""
    frame = pd.DataFrame(
        {"time": pd.to_datetime(["2023-06-01T00:15:00Z", "2023-06-01T00:30:00Z"])}
    )
    result = normalize_time(
        frame,
        source_convention="ending",
        target_convention="beginning",
        interval_minutes=15,
    )
    assert result["time"].iloc[0] == pd.Timestamp("2023-06-01T00:00:00Z")
    assert result["time"].iloc[1] == pd.Timestamp("2023-06-01T00:15:00Z")


def test_matching_conventions_leave_timestamps_untouched() -> None:
    """No shift is applied when source and target conventions agree."""
    frame = pd.DataFrame({"time": pd.to_datetime(["2023-06-01T00:00:00Z"])})
    result = normalize_time(
        frame,
        source_convention="beginning",
        target_convention="beginning",
        interval_minutes=15,
    )
    assert result["time"].iloc[0] == pd.Timestamp("2023-06-01T00:00:00Z")


def test_wind_height_correction_reduces_speed_toward_ground() -> None:
    """Correcting from 10 m to 3 m lowers wind speed by the log-profile ratio."""
    wind = pd.Series([10.0])
    corrected = correct_wind_height(
        wind, from_height_m=10.0, to_height_m=3.0, roughness_length_m=0.03
    )
    assert 0.0 < corrected.iloc[0] < 10.0
    # log(3/0.03) / log(10/0.03) = 4.6052 / 5.8091
    assert corrected.iloc[0] == pytest.approx(7.9275, abs=1e-3)


def test_wind_height_correction_preserves_missing_values() -> None:
    """NaN wind speeds stay NaN rather than becoming zero."""
    wind = pd.Series([5.0, np.nan])
    corrected = correct_wind_height(
        wind, from_height_m=10.0, to_height_m=3.0, roughness_length_m=0.03
    )
    assert pd.isna(corrected.iloc[1])


def test_wind_height_correction_rejects_height_below_roughness() -> None:
    """Heights at or below the roughness length are rejected."""
    with pytest.raises(ValueError):
        correct_wind_height(
            pd.Series([5.0]),
            from_height_m=10.0,
            to_height_m=0.01,
            roughness_length_m=0.03,
        )


def test_harmonization_records_every_step() -> None:
    """Each applied transformation is described for the manifest."""
    frame, steps = apply_harmonization(
        make_weather_frame(),
        HarmonizationConfig(),
        source_convention="beginning",
        interval_minutes=60,
    )
    assert "temp_air" in frame.columns
    assert any("wind speed" in step for step in steps)
    assert any("UTC" in step for step in steps)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_clean_weather_partition_passes_all_blocking_checks() -> None:
    """A coherent synthetic series produces no blocking failures."""
    frame, _ = apply_harmonization(
        make_weather_frame(),
        HarmonizationConfig(),
        source_convention="beginning",
        interval_minutes=60,
    )
    report = validate_weather_partition(frame, label="test", expected_rows=len(frame))
    assert report.ok, [c.detail for c in report.blocking_failures]


def test_night_irradiance_check_catches_a_timezone_shift() -> None:
    """Shifting irradiance against the zenith angle is detected as night sun."""
    frame, _ = apply_harmonization(
        make_weather_frame(),
        HarmonizationConfig(),
        source_convention="beginning",
        interval_minutes=60,
    )
    # Roll irradiance by 12 hours while leaving the zenith angle alone, which is
    # exactly the signature of a timezone error.
    frame["ghi"] = np.roll(frame["ghi"].to_numpy(), 12)
    result = check_night_irradiance_zero(frame)
    assert not result.passed
    assert result.blocking
    assert "timezone" in result.detail


def test_naive_timestamps_are_rejected() -> None:
    """A timestamp column without an offset fails the blocking check."""
    frame = pd.DataFrame({"time": pd.to_datetime(["2023-06-01T00:00:00"])})
    result = check_timezone_aware(frame)
    assert not result.passed and result.blocking


def test_unsorted_timestamps_are_rejected() -> None:
    """Out-of-order timestamps fail the monotonicity check."""
    frame = pd.DataFrame(
        {"time": pd.to_datetime(["2023-06-02T00:00Z", "2023-06-01T00:00Z"])}
    )
    assert not check_monotonic_time(frame).passed


def test_duplicate_keys_are_rejected() -> None:
    """A repeated timestamp fails the duplicate check."""
    frame = pd.DataFrame(
        {"time": pd.to_datetime(["2023-06-01T00:00Z", "2023-06-01T00:00Z"])}
    )
    assert not check_no_duplicate_times(frame).passed


def test_price_series_without_negatives_is_rejected() -> None:
    """A West Texas year with no negative prices means a broken fetch."""
    frame = pd.DataFrame({"price_usd_mwh": [10.0, 25.0, 40.0]})
    result = check_negative_prices_present(frame)
    assert not result.passed and result.blocking


def test_price_series_with_negatives_passes() -> None:
    """Signed prices including negatives pass the check."""
    frame = pd.DataFrame({"price_usd_mwh": [10.0, -30.0, 40.0]})
    assert check_negative_prices_present(frame).passed


def test_cross_source_temperature_check_passes_for_agreeing_sources() -> None:
    """Two sources differing only by noise correlate above the threshold."""
    primary = make_weather_frame(interval_minutes=60, days=5).rename(
        columns={"air_temperature": "temp_air"}
    )
    secondary = primary.copy()
    rng = np.random.default_rng(0)
    secondary["temp_air"] = secondary["temp_air"] + rng.normal(0, 0.5, len(secondary))
    assert cross_source_temperature_correlation(primary, secondary).passed


def test_cross_source_temperature_check_catches_a_time_shift() -> None:
    """A twelve-hour offset between sources drops correlation below threshold."""
    primary = make_weather_frame(interval_minutes=60, days=5).rename(
        columns={"air_temperature": "temp_air"}
    )
    secondary = primary.copy()
    secondary["temp_air"] = np.roll(secondary["temp_air"].to_numpy(), 12)
    result = cross_source_temperature_correlation(primary, secondary)
    assert not result.passed
    assert "coordinate, timezone or unit" in result.detail


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def test_partition_path_is_hive_partitioned() -> None:
    """Partition keys render as hive-style directories."""
    key = PartitionKey(
        "weather", "nsrdb_goes_conus_v4", (("site", "northstar"), ("year", "2023"))
    )
    assert str(key.relative_path()) == (
        "weather/source=nsrdb_goes_conus_v4/site=northstar/year=2023/data.parquet"
    )


def test_written_partition_round_trips_and_is_checksummed(
    tmp_path: Path, site: SiteConfig
) -> None:
    """A written partition reads back identically and validates by checksum."""
    cache = ResourceCache(tmp_path / "cache", "test.1", site.model_dump())
    key = PartitionKey("weather", "stub", (("year", "2023"),))
    frame = make_weather_frame()

    record = cache.write_partition(
        key,
        frame,
        source_id="SRC-WX-01",
        provider="Stub",
        endpoint="/stub",
        client="stub",
        transformations=["none"],
    )
    cache.write_manifest()

    assert cache.has_valid(key)
    assert record.row_count == len(frame)
    pd.testing.assert_frame_equal(cache.read_partition(key), frame)


def test_corrupted_partition_is_treated_as_absent(
    tmp_path: Path, site: SiteConfig
) -> None:
    """A file whose bytes changed no longer counts as cached."""
    cache = ResourceCache(tmp_path / "cache", "test.1", site.model_dump())
    key = PartitionKey("weather", "stub", (("year", "2023"),))
    cache.write_partition(
        key,
        make_weather_frame(),
        source_id="SRC-WX-01",
        provider="Stub",
        endpoint="/stub",
        client="stub",
        transformations=[],
    )
    cache.write_manifest()

    target = cache.root / key.relative_path()
    target.write_bytes(target.read_bytes() + b"corruption")

    assert not cache.has_valid(key)
    assert cache.verify()


def test_manifest_from_a_different_cache_version_is_ignored(
    tmp_path: Path, site: SiteConfig
) -> None:
    """A version bump invalidates existing records rather than merging them."""
    root = tmp_path / "cache"
    first = ResourceCache(root, "test.1", site.model_dump())
    key = PartitionKey("weather", "stub", (("year", "2023"),))
    first.write_partition(
        key,
        make_weather_frame(),
        source_id="SRC-WX-01",
        provider="Stub",
        endpoint="/stub",
        client="stub",
        transformations=[],
    )
    first.write_manifest()

    second = ResourceCache(root, "test.2", site.model_dump())
    assert not second.has_valid(key)


def test_manifest_records_provenance_and_checksum(
    tmp_path: Path, site: SiteConfig
) -> None:
    """The manifest carries everything needed to regenerate the partition."""
    cache = ResourceCache(tmp_path / "cache", "test.1", site.model_dump())
    key = PartitionKey("weather", "stub", (("year", "2023"),))
    cache.write_partition(
        key,
        make_weather_frame(),
        source_id="SRC-WX-01",
        provider="NSRDB GOES CONUS v4",
        endpoint="/api/nsrdb/v2/solar/nsrdb-GOES-conus-v4-0-0-download",
        client="pvlib 0.15.2",
        transformations=["renamed columns"],
        license_note="Public domain",
    )
    cache.write_manifest(harmonization={"roughness_length_m": 0.03})

    payload = json.loads(cache.manifest_path.read_text())
    entry = payload["partitions"][0]
    assert entry["provider"] == "NSRDB GOES CONUS v4"
    assert entry["sha256"] == sha256_file(cache.root / key.relative_path())
    assert payload["site"]["latitude"] == 31.35
    assert payload["harmonization"]["roughness_length_m"] == 0.03


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_rate_limiter_enforces_the_daily_ceiling() -> None:
    """Exceeding the client-side ceiling raises rather than risking a block."""
    limiter = RateLimiter(min_interval_s=0.0, max_requests_per_day=2)
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RateLimitError):
        limiter.acquire()


def test_retry_delay_grows_and_is_capped() -> None:
    """Backoff increases with attempt number and never exceeds the ceiling."""
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=8.0, jitter=0.0)
    assert policy.delay_for(1) == pytest.approx(1.0)
    assert policy.delay_for(3) == pytest.approx(4.0)
    assert policy.delay_for(10) == pytest.approx(8.0)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _orchestrator_with_stub(
    config: FetchConfig, frames: dict[str, pd.DataFrame]
) -> tuple[FetchOrchestrator, StubSource]:
    """Build an orchestrator wired to a stub source.

    Args:
        config: Fetch configuration.
        frames: Provider-style frames keyed by year string.

    Returns:
        The orchestrator and the stub source it will run.
    """
    stub = StubSource(
        config,
        config.sources[0],
        Credentials(),
        client=None,  # type: ignore[arg-type]
        frames=frames,
    )
    # The stub performs no HTTP, but the orchestrator reads the request count.
    stub.client = type(
        "NullClient",
        (),
        {"rate_limiter": RateLimiter(min_interval_s=0, max_requests_per_day=1)},
    )()
    return FetchOrchestrator(
        config, Credentials(), source_overrides={"SRC-WX-01": stub}
    ), stub


def test_run_fetches_then_skips_on_a_second_pass(config: FetchConfig) -> None:
    """A second run is idempotent: nothing is refetched."""
    frames = {"2023": make_weather_frame()}
    orchestrator, stub = _orchestrator_with_stub(config, frames)

    first = orchestrator.run()
    assert first.ok
    assert len(first.fetched) == 1
    assert stub.fetch_calls == ["2023"]

    second = orchestrator.run()
    assert second.ok
    assert len(second.skipped) == 1
    assert len(second.fetched) == 0
    assert stub.fetch_calls == ["2023"]


def test_force_refetches_a_cached_partition(config: FetchConfig) -> None:
    """The force flag bypasses the cache without a version bump."""
    frames = {"2023": make_weather_frame()}
    orchestrator, stub = _orchestrator_with_stub(config, frames)
    orchestrator.run()
    orchestrator.run(force=True)
    assert stub.fetch_calls == ["2023", "2023"]


def test_validation_failure_prevents_the_partition_being_cached(
    config: FetchConfig,
) -> None:
    """A blocking check failure aborts the write and is reported."""
    bad = make_weather_frame()
    # Roll irradiance against the zenith angle: the night-sun signature.
    bad["ghi"] = np.roll(bad["ghi"].to_numpy(), 12)
    orchestrator, _ = _orchestrator_with_stub(config, {"2023": bad})

    summary = orchestrator.run()
    assert not summary.ok
    assert any("night_irradiance_zero" in reason for _, reason in summary.failed)

    key = PartitionKey(
        "weather", "stub_weather", (("site", "northstar"), ("year", "2023"))
    )
    assert not orchestrator.cache.has_valid(key)


def test_interrupted_run_leaves_a_valid_partial_manifest(
    config: FetchConfig,
) -> None:
    """A manifest written per partition describes exactly what completed."""
    good = make_weather_frame()
    bad = make_weather_frame()
    bad["ghi"] = np.roll(bad["ghi"].to_numpy(), 12)

    config.sources[0].years = [2022, 2023]
    orchestrator, _ = _orchestrator_with_stub(config, {"2022": good, "2023": bad})

    summary = orchestrator.run()
    assert len(summary.fetched) == 1
    assert len(summary.failed) == 1

    payload = json.loads(orchestrator.cache.manifest_path.read_text())
    assert len(payload["partitions"]) == 1
    assert "year=2022" in payload["partitions"][0]["path"]


def test_verify_reports_a_clean_cache(config: FetchConfig) -> None:
    """Verification of an intact cache reports no problems and needs no network."""
    orchestrator, _ = _orchestrator_with_stub(config, {"2023": make_weather_frame()})
    orchestrator.run()
    assert orchestrator.verify() == []


def test_plan_reports_pending_before_and_cached_after(config: FetchConfig) -> None:
    """The plan command reflects cache state without fetching."""
    orchestrator, stub = _orchestrator_with_stub(config, {"2023": make_weather_frame()})
    assert [cached for _, cached in orchestrator.plan()] == [False]
    orchestrator.run()
    assert [cached for _, cached in orchestrator.plan()] == [True]
    assert stub.fetch_calls == ["2023"]


# --------------------------------------------------------------------------
# Settlement point selection
# --------------------------------------------------------------------------


def _write_extract(directory: Path) -> Path:
    """Write a miniature ERCOT extract covering every filter branch.

    Args:
        directory: Destination directory.

    Returns:
        The directory, for chaining.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "20260805_Resource_Node_to_Unit.csv").write_text(
        "RESOURCE_NODE,UNIT_SUBSTATION,UNIT_NAME\n"
        "GOOD_SLR_RN,GOODSUB,UNIT1\n"
        "GOOD_SLR_RN,GOODSUB,UNIT2\n"
        "GOOD_SLR_RN,GOODSUB,UNIT3\n"
        "HYBRID_SLR_RN,HYBSUB,UNIT1\n"
        "HYBRID_SLR_RN,HYBSUB,ESR1\n"
        "LOWKV_SLR_RN,LOWSUB,UNIT1\n"
        "EASTZONE_SLR_RN,EASTSUB,UNIT1\n"
        "NOFUEL_RN,PLAINSUB,UNIT1\n"
        "HUGE_SLR_RN,HUGESUB,UNIT1\n"
        "HUGE_SLR_RN,HUGESUB,UNIT2\n"
        "HUGE_SLR_RN,HUGESUB,UNIT3\n"
        "HUGE_SLR_RN,HUGESUB,UNIT4\n"
        "HUGE_SLR_RN,HUGESUB,UNIT5\n"
    )
    (directory / "20260805_Settlement_Points.csv").write_text(
        "ELECTRICAL_BUS,SUBSTATION,SETTLEMENT_LOAD_ZONE,RESOURCE_NODE,VOLTAGE_LEVEL\n"
        "B1,GOODSUB,LZ_WEST,GOOD_SLR_RN,345\n"
        "B2,GOODSUB,LZ_WEST,GOOD_SLR_RN,34.5\n"
        "B3,HYBSUB,LZ_WEST,HYBRID_SLR_RN,345\n"
        "B4,LOWSUB,LZ_WEST,LOWKV_SLR_RN,138\n"
        "B5,EASTSUB,LZ_SOUTH,EASTZONE_SLR_RN,345\n"
        "B6,PLAINSUB,LZ_WEST,NOFUEL_RN,345\n"
        "B7,HUGESUB,LZ_WEST,HUGE_SLR_RN,345\n"
    )
    return directory


def test_selection_applies_zone_voltage_storage_and_name_filters(
    tmp_path: Path,
) -> None:
    """Only the standalone West-zone 345 kV solar node survives the defaults."""
    from northstar_fetch.settlement_points import load_candidates, rank_candidates

    ranked = rank_candidates(load_candidates(_write_extract(tmp_path / "extract")))
    names = [c.resource_node for c in ranked]

    assert "GOOD_SLR_RN" in names
    assert "HYBRID_SLR_RN" not in names, "co-located storage must be excluded"
    assert "LOWKV_SLR_RN" not in names, "138 kV is below the interconnection floor"
    assert "EASTZONE_SLR_RN" not in names, "wrong load zone"
    assert "NOFUEL_RN" not in names, "no solar marker in the node name"


def test_selection_ranks_scale_match_above_a_much_larger_plant(
    tmp_path: Path,
) -> None:
    """A three-unit node outranks a five-unit node of the same zone and voltage."""
    from northstar_fetch.settlement_points import load_candidates, rank_candidates

    ranked = rank_candidates(load_candidates(_write_extract(tmp_path / "extract")))
    assert ranked[0].resource_node == "GOOD_SLR_RN"
    assert "HUGE_SLR_RN" in [c.resource_node for c in ranked]
    assert ranked[0].scale_distance < ranked[-1].scale_distance


def test_storage_detection_flags_hybrid_nodes(tmp_path: Path) -> None:
    """A node carrying an ESR unit is marked as hybrid rather than solar."""
    from northstar_fetch.settlement_points import load_candidates

    by_name = {
        c.resource_node: c for c in load_candidates(_write_extract(tmp_path / "extract"))
    }
    assert by_name["HYBRID_SLR_RN"].has_storage
    assert not by_name["GOOD_SLR_RN"].has_storage


def test_any_name_filter_admits_nodes_without_a_fuel_marker(tmp_path: Path) -> None:
    """Relaxing the name filter surfaces nodes ERCOT did not mark as solar."""
    from northstar_fetch.settlement_points import load_candidates, rank_candidates

    ranked = rank_candidates(
        load_candidates(_write_extract(tmp_path / "extract")),
        require_solar_name=False,
    )
    assert "NOFUEL_RN" in [c.resource_node for c in ranked]


def test_duplicate_extract_vintages_are_rejected(tmp_path: Path) -> None:
    """Two extract vintages in one directory raise rather than silently merging."""
    from northstar_fetch.settlement_points import load_candidates

    extract = _write_extract(tmp_path / "extract")
    (extract / "20260812_Settlement_Points.csv").write_text("ELECTRICAL_BUS\nB1\n")
    with pytest.raises(ValueError, match="one extract vintage at a time"):
        load_candidates(extract)


def test_blank_credentials_are_treated_as_missing(monkeypatch) -> None:
    """A variable present in .env but left blank loads as "", not None.

    Checking only for None let an empty ERCOT_SUBSCRIPTION_KEY pass validation
    and fail later as an opaque 401, several minutes into a fetch. The whole
    point of `require` is to fail before the network call.
    """
    from northstar_fetch.config import Credentials

    monkeypatch.setenv("ERCOT_USERNAME", "user@example.com")
    monkeypatch.setenv("ERCOT_PASSWORD", "secret")
    monkeypatch.setenv("ERCOT_SUBSCRIPTION_KEY", "")

    credentials = Credentials.from_env()
    with pytest.raises(RuntimeError, match="ERCOT_SUBSCRIPTION_KEY"):
        credentials.require("ercot_username", "ercot_password", "ercot_subscription_key")


def test_whitespace_only_credentials_are_missing(monkeypatch) -> None:
    """A key pasted with a stray space is not a key."""
    from northstar_fetch.config import Credentials

    monkeypatch.setenv("EIA_API_KEY", "   ")
    with pytest.raises(RuntimeError, match="EIA_API_KEY"):
        Credentials.from_env().require("eia_api_key")


def test_every_planned_dataset_has_a_credential_mapping() -> None:
    """A dataset with no entry is checked as if it needed nothing.

    The first version of this check keyed on source IDs like "SRC-WX-01" while
    the plan emits dataset names. Nothing matched, nothing was checked, and
    `plan` reported "All present" with no credentials set at all.
    """
    from northstar_fetch.cli import SOURCE_CREDENTIALS
    from northstar_fetch.orchestrator import SOURCE_REGISTRY

    assert SOURCE_CREDENTIALS, "mapping must not be empty"
    # Every mapped name must be a plausible dataset label, not a source ID.
    for name in SOURCE_CREDENTIALS:
        assert not name.startswith("SRC-"), name
    assert len(SOURCE_CREDENTIALS) == len(SOURCE_REGISTRY)


def test_ercot_sources_require_the_subscription_key() -> None:
    """The key is separate from the login and easy to leave blank."""
    from northstar_fetch.cli import SOURCE_CREDENTIALS

    for name in ("ercot_rt_spp", "ercot_dam_spp"):
        assert "ercot_subscription_key" in SOURCE_CREDENTIALS[name], name


def test_open_meteo_needs_no_credentials() -> None:
    """It is unauthenticated; demanding a key would block a usable source."""
    from northstar_fetch.cli import SOURCE_CREDENTIALS

    assert SOURCE_CREDENTIALS["open_meteo_era5"] == ()


def test_limit_stops_after_n_partitions(config: FetchConfig) -> None:
    """`--limit 1` must confirm a provider works without fetching everything.

    The full plan is 445 partitions across five providers. Credentials that are
    merely *present* are not credentials that *work*, and discovering the
    difference at partition 300 is expensive.
    """
    frames = {year: make_weather_frame() for year in ("2021", "2022", "2023")}
    orchestrator, stub = _orchestrator_with_stub(config, frames)

    orchestrator.run(limit=1)
    assert len(stub.fetch_calls) == 1, stub.fetch_calls


def test_limit_none_fetches_everything(config: FetchConfig) -> None:
    """The limit is opt-in; omitting it must not change behaviour."""
    frames = {year: make_weather_frame() for year in ("2021", "2022", "2023")}
    orchestrator, stub = _orchestrator_with_stub(config, frames)

    orchestrator.run()
    assert len(stub.fetch_calls) == 3


def test_limit_counts_fetches_not_cached_partitions(config: FetchConfig) -> None:
    """Cached partitions must not consume the limit.

    Otherwise a smoke test after a partial run would skip straight past the
    cache and exercise no network at all - reporting success without having
    contacted the provider.
    """
    frames = {year: make_weather_frame() for year in ("2021", "2022", "2023")}
    orchestrator, stub = _orchestrator_with_stub(config, frames)

    orchestrator.run()  # populate the cache
    stub.fetch_calls.clear()

    orchestrator.run(limit=1)
    assert stub.fetch_calls == [], "everything was cached; nothing to fetch"


def test_credentials_are_loaded_from_a_dotenv_file(tmp_path, monkeypatch) -> None:
    """`.env` must be read explicitly; `os.environ` alone ignores it.

    Nothing loaded the file. Every credentialed source reported "missing
    credentials" while the values sat correct on disk, and only the
    unauthenticated provider succeeded - which reads as a credential problem
    rather than a loading problem.
    """
    from northstar_fetch.config import Credentials

    (tmp_path / ".env").write_text(
        "# a comment\nNREL_API_KEY=fromfile\nNREL_EMAIL=a@b.c\n"
    )
    for name in ("NREL_API_KEY", "NREL_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    assert Credentials.from_env().nrel_api_key == "fromfile"


def test_dotenv_is_found_from_a_subdirectory(tmp_path, monkeypatch) -> None:
    """The command must work from anywhere in the checkout, as git does."""
    from northstar_fetch.config import Credentials

    (tmp_path / ".env").write_text("NREL_API_KEY=fromfile\n")
    nested = tmp_path / "packages" / "deep"
    nested.mkdir(parents=True)

    monkeypatch.delenv("NREL_API_KEY", raising=False)
    monkeypatch.chdir(nested)

    assert Credentials.from_env().nrel_api_key == "fromfile"


def test_real_environment_beats_the_file(tmp_path, monkeypatch) -> None:
    """An explicitly exported value must win, or overrides are impossible."""
    from northstar_fetch.config import Credentials

    (tmp_path / ".env").write_text("NREL_API_KEY=fromfile\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NREL_API_KEY", "fromenv")

    assert Credentials.from_env().nrel_api_key == "fromenv"


def test_dotenv_values_may_be_quoted(tmp_path, monkeypatch) -> None:
    """A password with spaces has to be quotable without the quotes surviving."""
    from northstar_fetch.config import Credentials

    (tmp_path / ".env").write_text('ERCOT_PASSWORD="a secret"\n')
    monkeypatch.delenv("ERCOT_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)

    assert Credentials.from_env().ercot_password == "a secret"


def test_an_empty_partition_fails_validation() -> None:
    """Every other structural check passes trivially over zero rows.

    Observed against the real ERCOT API: a month returned 0 rows and was
    reported as "4 passed, 0 warning, 0 blocking" - a successful fetch of
    nothing, written to the cache as valid and skipped on the next run. A full
    445-partition run would have completed with no price data and no failure.
    """
    from northstar_fetch.validate import validate_price_partition

    empty = pd.DataFrame(
        columns=["time", "settlement_point", "price_type", "price_usd_mwh"]
    )
    report = validate_price_partition(empty, label="test", require_negatives=False)

    assert not report.ok
    assert any(c.name == "non_empty" for c in report.blocking_failures)


def test_an_empty_weather_partition_fails_validation() -> None:
    """Same guard on the weather path, naming the cause explicitly."""
    from northstar_fetch.validate import validate_weather_partition

    report = validate_weather_partition(
        pd.DataFrame(columns=["time", "ghi"]), label="test", expected_rows=8760
    )
    assert not report.ok
    assert any(c.name == "non_empty" for c in report.blocking_failures)


def test_a_populated_partition_still_passes() -> None:
    """The guard must not reject good data."""
    from northstar_fetch.validate import check_non_empty

    frame = pd.DataFrame({"time": pd.date_range("2023-01-01", periods=3, tz="UTC")})
    assert check_non_empty(frame).passed


class _PagedProbe(ErcotPriceSource):
    """An ERCOT price source with a scripted, paginated API behind it."""

    def __init__(self, pages: list[dict]) -> None:
        """Store the scripted pages.

        Args:
            pages: Payloads to return, one per requested page.
        """
        self.endpoint = "/np6-905-cd/spp_node_zone_hub"
        self._pages = pages
        self.requested: list[int] = []

    @property
    def token_manager(self):
        """Return a stub token manager.

        Returns:
            A mock supplying empty headers.
        """
        manager = MagicMock()
        manager.headers.return_value = {}
        return manager

    @property
    def client(self):
        """Return a stub HTTP client serving the scripted pages.

        Returns:
            An object with a ``get`` method.
        """
        outer = self

        class _Client:
            def get(self, url, params=None, **kwargs):
                outer.requested.append(params["page"])
                index = min(params["page"], len(outer._pages)) - 1
                response = MagicMock()
                response.json.return_value = outer._pages[index]
                return response

        return _Client()


def _price_payload(page: int, total_pages: int, total: int, rows: int) -> dict:
    """Build an ERCOT-shaped price payload.

    Args:
        page: Current page number.
        total_pages: Reported page count.
        total: Reported total record count.
        rows: Rows to include on this page.

    Returns:
        A payload mirroring the real API's structure.
    """
    return {
        "fields": [
            {"name": name}
            for name in (
                "deliveryDate",
                "deliveryHour",
                "deliveryInterval",
                "settlementPoint",
                "settlementPointType",
                "settlementPointPrice",
                "DSTFlag",
            )
        ],
        "data": [
            ["2026-08-20", 1, i + 1, "HRNT_SLR_RN", "RN", 24.69, False]
            for i in range(rows)
        ],
        "_meta": {
            "currentPage": page,
            "totalPages": total_pages,
            "totalRecords": total,
        },
    }


def test_price_fetch_reads_every_page() -> None:
    """The API paginates and reading page one silently loses most of the data.

    Measured against the real API: one month of one node reported
    ``totalRecords`` 2,974 across 3 pages while returning 1,000 rows on the
    first. The partial result validated cleanly, because the rows that were
    present were well-formed.
    """
    probe = _PagedProbe(
        [
            _price_payload(1, 3, 2974, 1000),
            _price_payload(2, 3, 2974, 1000),
            _price_payload(3, 3, 2974, 974),
        ]
    )
    frame = probe._fetch_all_pages("HRNT_SLR_RN", "2026-07-21", "2026-08-20")

    assert probe.requested == [1, 2, 3]
    assert len(frame) == 2974


def test_a_truncated_paged_fetch_raises() -> None:
    """A short month must fail, not return quietly.

    The row-count check cannot catch this: a truncated month and a month with a
    genuine market outage both have the wrong count, which is why that check
    warns rather than blocks.
    """
    probe = _PagedProbe([_price_payload(1, 3, 2974, 1000)])

    with pytest.raises(RuntimeError, match="totalRecords"):
        probe._fetch_all_pages("HRNT_SLR_RN", "2026-07-21", "2026-08-20")


def test_single_page_responses_still_work() -> None:
    """Pagination must not break a result that fits in one page."""
    probe = _PagedProbe([_price_payload(1, 1, 96, 96)])
    frame = probe._fetch_all_pages("HRNT_SLR_RN", "2026-08-20", "2026-08-20")

    assert probe.requested == [1]
    assert len(frame) == 96


def test_a_redirect_is_an_error_not_a_success() -> None:
    """A 3xx returned as success hands a redirect body to `response.json()`.

    The failure then surfaces as a JSON decode error that says nothing about
    the cause. Observed as an HTTP 302 from ERCOT, which looked like malformed
    data rather than an unfollowed redirect.
    """
    from northstar_fetch.http import FetchError, HttpClient, RateLimiter

    response = MagicMock()
    response.status_code = 302
    response.headers = {"Location": "https://elsewhere.example/report"}

    session = MagicMock()
    session.get.return_value = response
    client = HttpClient(
        rate_limiter=RateLimiter(min_interval_s=0.0, max_requests_per_day=10),
        session=session,
    )

    with pytest.raises(FetchError, match="302"):
        client.get("https://api.example/thing")


def test_a_200_still_succeeds() -> None:
    """The redirect guard must not reject good responses."""
    from northstar_fetch.http import HttpClient, RateLimiter

    response = MagicMock()
    response.status_code = 200

    session = MagicMock()
    session.get.return_value = response
    client = HttpClient(
        rate_limiter=RateLimiter(min_interval_s=0.0, max_requests_per_day=10),
        session=session,
    )

    assert client.get("https://api.example/thing") is response


def _spp_payload(rows: list[list]) -> dict:
    """Build an ERCOT settlement-point-price payload.

    Args:
        rows: Raw data rows.

    Returns:
        A payload matching the real API's field order.
    """
    return {
        "fields": [
            {"name": n}
            for n in (
                "deliveryDate",
                "deliveryHour",
                "deliveryInterval",
                "settlementPoint",
                "settlementPointType",
                "settlementPointPrice",
                "DSTFlag",
            )
        ],
        "data": rows,
    }


def test_delivery_dates_are_central_time_not_utc() -> None:
    """ERCOT reports Central Prevailing Time; parsing as UTC shifts everything.

    Every price landed 5-6 hours from where it belonged, which would have
    joined prices to the wrong hours of generation and silently corrupted
    capture rate, curtailment economics and lost-revenue attribution. The
    series stays well-formed and the right length throughout, so nothing
    downstream objects.
    """
    source = ErcotPriceSource.__new__(ErcotPriceSource)
    frame = source._parse_response(
        _spp_payload([["2025-06-10", 1, 1, "HB_WEST", "HU", 25.0, False]]), "HB_WEST"
    )
    # Midnight CDT is 05:00 UTC.
    assert str(frame["time"].iloc[0]) == "2025-06-10 05:00:00+00:00"


def test_timestamps_are_interval_beginning() -> None:
    """Doc 11 section 3 is explicit, and ERCOT labels by interval end.

    The mismatch shifts the whole series by one interval, and at a DST
    fall-back it also destroys the disambiguation - see the test below.
    """
    source = ErcotPriceSource.__new__(ErcotPriceSource)
    frame = source._parse_response(
        _spp_payload([["2025-06-10", 1, 1, "HB_WEST", "HU", 25.0, False]]), "HB_WEST"
    )
    assert frame["time"].iloc[0].minute == 0, "first interval begins at the hour"


def test_dst_fall_back_rows_resolve_to_distinct_times() -> None:
    """The repeated local hour is disambiguated by DSTFlag.

    Interval-ending labels break this: the interval ending 02:00 is not an
    ambiguous local time, so the flag cannot separate the two occurrences and
    they collapse into one row. Interval-beginning puts all four inside the
    repeated hour.
    """
    source = ErcotPriceSource.__new__(ErcotPriceSource)
    rows = []
    for interval in (1, 2, 3, 4):
        rows.append(["2025-11-02", 2, interval, "HB_WEST", "HU", 20.0, True])
        rows.append(["2025-11-02", 2, interval, "HB_WEST", "HU", 21.0, False])

    frame = source._parse_response(_spp_payload(rows), "HB_WEST")

    assert len(frame) == 8
    assert frame["time"].duplicated().sum() == 0


def test_duplicate_point_types_are_collapsed() -> None:
    """LZ_WEST is reported as both LZ and LZEW with identical prices.

    Observed on a real backfill as 2,976 duplicate rows in a 2,976-row month -
    every interval doubled.
    """
    source = ErcotPriceSource.__new__(ErcotPriceSource)
    rows = []
    for interval in (1, 2, 3):
        rows.append(["2025-06-10", 1, interval, "LZ_WEST", "LZEW", 30.0, False])
        rows.append(["2025-06-10", 1, interval, "LZ_WEST", "LZ", 30.0, False])

    frame = source._parse_response(_spp_payload(rows), "LZ_WEST")

    assert len(frame) == 3
    assert frame["time"].duplicated().sum() == 0


def test_divergent_prices_select_the_preferred_type() -> None:
    """The two series disagree across a full year, so a choice must be made.

    An earlier version raised when they differed. On a single sampled day they
    matched; over a year they do not, and raising stranded every remaining
    partition in the backfill. The type is now selected by documented
    preference and the disagreement logged.
    """
    source = ErcotPriceSource.__new__(ErcotPriceSource)
    rows = [
        ["2025-06-10", 1, 1, "LZ_WEST", "LZEW", 99.0, False],
        ["2025-06-10", 1, 1, "LZ_WEST", "LZ", 30.0, False],
    ]
    frame = source._parse_response(_spp_payload(rows), "LZ_WEST")

    assert len(frame) == 1
    assert frame["price_usd_mwh"].iloc[0] == 30.0, "LZ is preferred over LZEW"


def test_type_selection_is_deterministic_not_first_seen() -> None:
    """Row order from the API must not decide which price is kept."""
    source = ErcotPriceSource.__new__(ErcotPriceSource)
    forward = [
        ["2025-06-10", 1, 1, "LZ_WEST", "LZ", 30.0, False],
        ["2025-06-10", 1, 1, "LZ_WEST", "LZEW", 99.0, False],
    ]
    reverse = list(reversed(forward))

    a = source._parse_response(_spp_payload(forward), "LZ_WEST")
    b = source._parse_response(_spp_payload(reverse), "LZ_WEST")

    assert a["price_usd_mwh"].iloc[0] == b["price_usd_mwh"].iloc[0] == 30.0


def test_completeness_counts_rows_received_not_rows_kept() -> None:
    """`totalRecords` counts raw rows, before type filtering.

    A load zone publishes every interval under two type codes, so a complete
    January arrives as 5,952 rows and is correctly reduced to 2,976. Comparing
    the post-filter count against the pre-filter total failed a perfectly good
    month - a guard added to catch truncation instead rejecting valid data.
    """
    probe = _PagedProbe(
        [
            _dual_type_payload(3, total=12, pages=2, page=1),
            _dual_type_payload(3, total=12, pages=2, page=2),
        ]
    )
    frame = probe._fetch_all_pages("LZ_WEST", "2025-01-01", "2025-01-31")

    assert len(frame) == 6, "one row per interval after keeping a single type"


def test_genuine_truncation_still_raises() -> None:
    """Loosening the check must not disable it."""
    probe = _PagedProbe(
        [
            _dual_type_payload(3, total=99, pages=2, page=1),
            _dual_type_payload(3, total=99, pages=2, page=2),
        ]
    )
    with pytest.raises(RuntimeError, match="totalRecords"):
        probe._fetch_all_pages("LZ_WEST", "2025-01-01", "2025-01-31")


def _dual_type_payload(intervals: int, *, total: int, pages: int, page: int) -> dict:
    """Build a payload where each interval appears under two type codes.

    Args:
        intervals: Number of distinct intervals on this page.
        total: Reported total record count.
        pages: Reported page count.
        page: Current page number.

    Returns:
        A payload shaped like a load-zone response.
    """
    data = []
    for i in range(intervals):
        for point_type in ("LZ", "LZEW"):
            data.append(["2025-01-01", 1, i + 1, "LZ_WEST", point_type, 30.0, False])
    payload = _spp_payload(data)
    payload["_meta"] = {
        "currentPage": page,
        "totalPages": pages,
        "totalRecords": total,
    }
    return payload


def _dam_payload(rows: list[list]) -> dict:
    """Build an ERCOT day-ahead settlement point price payload.

    Args:
        rows: Raw data rows.

    Returns:
        A payload matching the day-ahead report's field order.
    """
    return {
        "fields": [
            {"name": n}
            for n in (
                "deliveryDate",
                "hourEnding",
                "settlementPoint",
                "settlementPointType",
                "settlementPointPrice",
                "DSTFlag",
            )
        ],
        "data": rows,
    }


def test_day_ahead_applies_the_same_timezone_rules_as_real_time() -> None:
    """A subclass that overrides parsing inherits none of the parent's fixes.

    The day-ahead parser carried all three defects the real-time parser had -
    UTC parsing, interval-ending labels, no DST handling - and they were fixed
    only in the parent. Day-ahead November then failed with exactly 1 duplicate
    per partition, one repeated hour, long after real-time was correct.
    """
    from northstar_fetch.sources.market import ErcotDayAheadPriceSource

    source = ErcotDayAheadPriceSource.__new__(ErcotDayAheadPriceSource)
    frame = source._parse_response(
        _dam_payload([["2025-06-10", "01:00", "HB_WEST", "HU", 25.0, False]]),
        "HB_WEST",
    )
    # Hour ending 01:00 begins at midnight CDT, which is 05:00 UTC.
    assert str(frame["time"].iloc[0]) == "2025-06-10 05:00:00+00:00"


def test_day_ahead_dst_fall_back_resolves() -> None:
    """The exact failure seen on three November day-ahead partitions."""
    from northstar_fetch.sources.market import ErcotDayAheadPriceSource

    source = ErcotDayAheadPriceSource.__new__(ErcotDayAheadPriceSource)
    rows = [
        ["2025-11-02", "02:00", "HB_WEST", "HU", 20.0, True],
        ["2025-11-02", "02:00", "HB_WEST", "HU", 21.0, False],
    ]
    frame = source._parse_response(_dam_payload(rows), "HB_WEST")

    assert len(frame) == 2
    assert frame["time"].duplicated().sum() == 0


def test_day_ahead_collapses_duplicate_point_types() -> None:
    """Load zones carry two type codes on the day-ahead report too."""
    from northstar_fetch.sources.market import ErcotDayAheadPriceSource

    source = ErcotDayAheadPriceSource.__new__(ErcotDayAheadPriceSource)
    rows = [
        ["2025-06-10", "01:00", "LZ_WEST", "LZEW", 30.0, False],
        ["2025-06-10", "01:00", "LZ_WEST", "LZ", 30.0, False],
    ]
    assert len(source._parse_response(_dam_payload(rows), "LZ_WEST")) == 1


def test_both_price_sources_share_one_localisation_path() -> None:
    """Anything true of both parsers belongs in a shared helper.

    Duplicated logic in an override is how the day-ahead parser stayed broken
    through three rounds of fixes to the real-time one.
    """
    from northstar_fetch.sources.market import (
        ErcotDayAheadPriceSource,
        ErcotPriceSource,
    )

    assert ErcotDayAheadPriceSource._localize is ErcotPriceSource._localize
    assert (
        ErcotDayAheadPriceSource._drop_duplicate_point_types
        is ErcotPriceSource._drop_duplicate_point_types
    )


def test_cache_root_expands_a_home_relative_path(tmp_path) -> None:
    """A cache outside the checkout survives delete-and-re-extract.

    Losing it costs a full 197-request backfill, and providers rate-limit
    those. Without expansion, "~/.cache/northstar" creates a directory
    literally named "~" in the working directory.
    """
    import pathlib

    from northstar_fetch.config import load_config

    source = pathlib.Path("config/northstar.toml")
    if not source.exists():  # pragma: no cover - layout guard
        pytest.skip("config not present")

    target = tmp_path / "c.toml"
    target.write_text(
        source.read_text().replace(
            'cache_root = "resource_cache"', 'cache_root = "~/.cache/northstar"'
        )
    )
    assert "~" not in str(load_config(target).cache_root)
