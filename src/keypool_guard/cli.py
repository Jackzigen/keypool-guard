"""Command-line interface.

    keypool-guard check  -c pool.yaml      probe once, print a report
    keypool-guard daemon -c pool.yaml      publish the healthy subpool
    keypool-guard init                     print an example config
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import EXAMPLE_CONFIG, ConfigError, load_config
from .daemon import GuardDaemon
from .pool import KeyPool
from .prober import probe_provider
from .report import render_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keypool-guard",
        description=(
            "Active health guardian for LLM API key pools: probe every "
            "key, quarantine the bad ones, feed a healthy subpool to "
            "your existing gateway."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"keypool-guard {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "check", help="probe all keys once and print a report"
    )
    check.add_argument("-c", "--config", required=True, help="YAML config path")
    check.add_argument(
        "--no-color", action="store_true", help="disable ANSI colors"
    )
    check.add_argument(
        "-v", "--verbose", action="store_true", help="log probe errors to stderr"
    )

    daemon = sub.add_parser(
        "daemon",
        help="probe on a schedule and publish the healthy subpool",
    )
    daemon.add_argument("-c", "--config", required=True, help="YAML config path")
    daemon.add_argument(
        "--once",
        action="store_true",
        help="run a single round and exit (cron-friendly)",
    )
    daemon.add_argument(
        "--output", help="override the healthy-subpool output file"
    )
    daemon.add_argument("--host", help="override health endpoint host")
    daemon.add_argument("--port", type=int, help="override health endpoint port")
    daemon.add_argument(
        "-v", "--verbose", action="store_true", help="log each round to stderr"
    )

    sub.add_parser("init", help="print an example config to stdout")
    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _cmd_check(args: argparse.Namespace) -> int:
    _configure_logging(args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    pool = KeyPool(config)
    for provider in config.providers:
        results = asyncio.run(probe_provider(provider))
        for result in results:
            pool.apply(provider, result)

    print(render_report(pool.snapshot(), colored=not args.no_color))
    total = sum(len(p.keys) for p in config.providers)
    healthy = sum(len(pool.healthy_keys(p.name)) for p in config.providers)
    return 0 if healthy == total else 1


def _cmd_daemon(args: argparse.Namespace) -> int:
    _configure_logging(args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.output:
        config.output_file = args.output
    if args.host:
        config.server.host = args.host
        config.server.enabled = True
    if args.port is not None:
        config.server.port = args.port
        config.server.enabled = True

    daemon = GuardDaemon(config)
    if args.once:
        asyncio.run(daemon.round())
        return 0
    try:
        asyncio.run(daemon.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    """Entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "daemon":
        return _cmd_daemon(args)
    if args.command == "init":
        sys.stdout.write(EXAMPLE_CONFIG)
        return 0
    return 2  # unreachable: subparsers are required


if __name__ == "__main__":
    sys.exit(main())
