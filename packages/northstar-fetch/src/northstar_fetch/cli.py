"""Command-line interface for the NorthStar resource fetch client.

Four commands:

* ``plan`` - show what a run would fetch, without network access.
* ``fetch`` - acquire missing partitions, validate, and update the manifest.
* ``verify`` - check cache integrity offline. This is what the simulator runs
  at startup, and what a reviewer runs to confirm a dataset is regenerable.
* ``summary`` - report cache contents by source.

Reference: design document ``19_external_data_acquisition`` section 7.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Credentials, load_config
from .orchestrator import FetchOrchestrator
from .settlement_points import load_candidates, rank_candidates


def configure_logging(verbose: bool) -> None:
    """Set up structured console logging.

    Args:
        verbose: Emit debug-level records, including per-partition skip
            decisions.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        A parser exposing the four subcommands and shared options.
    """
    parser = argparse.ArgumentParser(
        prog="northstar-fetch",
        description="Acquire and cache external resource and market data.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/northstar.toml"),
        help="path to the TOML configuration file",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="show what would be fetched, without fetching")

    fetch = sub.add_parser("fetch", help="acquire missing partitions")
    fetch.add_argument(
        "--force",
        action="store_true",
        help="refetch every partition, including cached ones",
    )
    fetch.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="stop after N partitions per source; use --limit 1 to confirm "
        "every provider authenticates before committing to the full run",
    )

    select = sub.add_parser(
        "select-node",
        help="rank plant pricing-proxy candidates from an ERCOT extract",
    )
    select.add_argument(
        "extract_dir",
        type=Path,
        help="directory holding the unpacked np4-160-sg CSV files",
    )
    select.add_argument(
        "--zone", default="LZ_WEST", help="congestion load zone to filter to"
    )
    select.add_argument(
        "--include-storage",
        action="store_true",
        help="keep nodes with co-located battery storage",
    )
    select.add_argument(
        "--any-name",
        action="store_true",
        help="do not require a solar marker in the node name",
    )

    sub.add_parser("verify", help="check cache integrity offline")
    sub.add_parser("summary", help="report cache contents by source")
    return parser


#: Credentials each dataset needs, keyed by the label prefix the orchestrator
#: emits. Matching on source IDs such as "SRC-WX-01" silently matched nothing -
#: the plan labels carry dataset names - so every credential check was skipped
#: and the command reported "All present" with nothing set at all.
SOURCE_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "nsrdb_goes_conus_v4": ("nrel_api_key", "nrel_email"),
    "nsrdb_goes_tmy_v4": ("nrel_api_key", "nrel_email"),
    "open_meteo_era5": (),  # unauthenticated
    "ercot_rt_spp": (
        "ercot_username",
        "ercot_password",
        "ercot_subscription_key",
    ),
    "ercot_dam_spp": (
        "ercot_username",
        "ercot_password",
        "ercot_subscription_key",
    ),
}


def command_plan(orchestrator: FetchOrchestrator, credentials=None) -> int:
    """Print the fetch plan and check the credentials it would need.

    `plan` is the natural "check before committing" step, so it must actually
    check something. It previously enumerated partitions and returned zero
    without touching credentials at all - a blank API key produced a clean plan
    and then failed partway through a 445-partition fetch.

    Credential checking here is **local**: it confirms the values are present
    and non-blank, not that the provider accepts them. Only a request can
    establish that, and the cheapest one is `fetch --limit 1`.

    Args:
        orchestrator: Configured orchestrator.
        credentials: Credentials to check, or ``None`` to skip the check.

    Returns:
        Zero when the plan is printable and credentials are present, one when a
        source in the plan is missing credentials.
    """
    pending = 0
    planned_sources: set[str] = set()

    for label, cached in orchestrator.plan():
        marker = "cached " if cached else "PENDING"
        pending += 0 if cached else 1
        planned_sources.add(label.split("[", 1)[0])
        print(f"  {marker}  {label}")

    print(f"\n{pending} partition(s) would be fetched.")

    if credentials is None:
        return 0

    print("\nCredentials required by this plan:")
    problems: list[str] = []

    unknown = planned_sources - set(SOURCE_CREDENTIALS)
    if unknown:
        # A dataset with no entry would otherwise be checked silently as if it
        # needed nothing, which is how the first version of this check passed
        # with no credentials at all.
        print(f"  WARNING: no credential mapping for {sorted(unknown)}")

    for source_id in sorted(planned_sources & set(SOURCE_CREDENTIALS)):
        needed = SOURCE_CREDENTIALS[source_id]
        if not needed:
            print(f"  {source_id:<22} none required")
            continue
        try:
            credentials.require(*needed)
        except RuntimeError as error:
            print(f"  {source_id:<22} MISSING - {error}")
            problems.append(source_id)
        else:
            print(f"  {source_id:<22} present")

    if problems:
        print(
            f"\n{len(problems)} source(s) cannot be fetched. Set the values in "
            ".env and re-run."
        )
        return 1

    print(
        "\nAll present. Note this checks only that values are set, not that "
        "the\nproviders accept them - for that, run: northstar-fetch fetch --limit 1"
    )
    return 0


def command_fetch(
    orchestrator: FetchOrchestrator, force: bool, limit: int | None = None
) -> int:
    """Run the fetch and print the summary.

    Args:
        orchestrator: Configured orchestrator.
        force: Whether to refetch cached partitions.
        limit: Stop after this many partitions per source.

    Returns:
        Zero when every partition succeeded, one otherwise.
    """
    if limit is not None:
        print(f"Limit: {limit} partition(s) per source.\n")
    summary = orchestrator.run(force=force, limit=limit)
    print(summary.render())
    return 0 if summary.ok else 1


def command_verify(orchestrator: FetchOrchestrator) -> int:
    """Verify cache integrity.

    Args:
        orchestrator: Configured orchestrator.

    Returns:
        Zero when the cache is intact, one otherwise.
    """
    problems = orchestrator.verify()
    if not problems:
        print("Cache verified: all partitions present and checksums match.")
        return 0
    print(f"Cache verification failed with {len(problems)} problem(s):")
    for problem in problems:
        print(f"  - {problem}")
    return 1


def command_select_node(args: argparse.Namespace) -> int:
    """Rank pricing-proxy candidates from an unpacked ERCOT extract.

    Args:
        args: Parsed arguments carrying the extract directory and filters.

    Returns:
        Zero when at least one candidate survives the filters, one otherwise.
    """
    candidates = load_candidates(args.extract_dir)
    ranked = rank_candidates(
        candidates,
        load_zone=args.zone,
        require_solar_name=not args.any_name,
        exclude_storage=not args.include_storage,
    )
    print(f"{len(candidates)} resource nodes parsed; {len(ranked)} match the filters.\n")
    if not ranked:
        print("No candidates. Relax --zone, or pass --any-name: ERCOT node names")
        print("are not required to indicate fuel type.")
        return 1
    for rank, candidate in enumerate(ranked, start=1):
        print(f"{rank:>3}. {candidate.describe()}")
    print(
        "\nTop candidate is a starting point, not a confirmed choice. Fetch one "
        "year of prices\nfor it and confirm the history covers your target years "
        "and shows midday negatives."
    )
    return 0


def command_summary(orchestrator: FetchOrchestrator) -> int:
    """Print cache contents grouped by source.

    Args:
        orchestrator: Configured orchestrator.

    Returns:
        Process exit code, always zero.
    """
    totals = orchestrator.cache.summary()
    if not totals:
        print("Cache is empty.")
        return 0
    print(f"{'source':<12} {'partitions':>11} {'rows':>14}")
    for source_id in sorted(totals):
        entry = totals[source_id]
        print(f"{source_id:<12} {entry['partitions']:>11,} {entry['rows']:>14,}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    # Node selection reads a local extract and needs no configuration or
    # credentials, so it is handled before the orchestrator is built.
    if args.command == "select-node":
        return command_select_node(args)

    config = load_config(args.config)
    credentials = Credentials.from_env()
    orchestrator = FetchOrchestrator(config, credentials)

    if args.command == "plan":
        return command_plan(orchestrator, credentials)
    if args.command == "fetch":
        return command_fetch(orchestrator, force=args.force, limit=args.limit)
    if args.command == "verify":
        return command_verify(orchestrator)
    if args.command == "summary":
        return command_summary(orchestrator)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
