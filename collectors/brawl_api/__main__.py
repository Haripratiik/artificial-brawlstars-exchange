"""Collector entrypoint.

    export BRAWL_API_KEY=...
    python -m collectors.brawl_api --data-dir data/raw

Runs until killed. Designed to be put under a process supervisor (systemd on
the collector host) and then forgotten about.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from collectors.brawl_api.client import (
    PROXY_WHITELIST_IP,
    BrawlAPIError,
    BrawlClient,
    ClientConfig,
    InvalidAPIKey,
    KeyNotAuthorizedForIP,
)
from collectors.brawl_api.crawler import Crawler, CrawlConfig
from collectors.brawl_api.store import CollectorStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="collectors.brawl_api", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--rate", type=float, default=7.5, help="requests per second")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--recrawl-hours", type=float, default=12.0)
    parser.add_argument(
        "--proxy",
        action="store_true",
        help=(
            "route through the RoyaleAPI proxy. Requires the key to allow-list "
            f"{PROXY_WHITELIST_IP} instead of your own IP, which lets the collector run "
            "from any machine including one with a dynamic address"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the API key and IP configuration, then exit",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="populate the frontier from leaderboards and exit",
    )
    parser.add_argument(
        "--status", action="store_true", help="print corpus statistics and exit"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


async def check(api_key: str, config: ClientConfig) -> int:
    """Make one cheap request and explain, precisely, whatever went wrong.

    Setup fails in exactly two ways -- a bad key, or the key allow-listing the
    wrong IP -- and both return 403. Telling them apart by hand is miserable, so
    this does it.
    """
    route = "RoyaleAPI proxy" if config.use_proxy else "Supercell directly"
    print(f"endpoint  {config.base_url}")
    print(f"route     {route}")
    print(
        f"key must allow-list  "
        f"{PROXY_WHITELIST_IP + ' (the proxy)' if config.use_proxy else 'this machine public IP'}"
    )
    print()

    client = BrawlClient(api_key, config)
    try:
        brawlers = await client.brawlers()
    except (KeyNotAuthorizedForIP, InvalidAPIKey) as denied:
        print("FAILED\n")
        print(str(denied))
        if not config.use_proxy:
            # On the direct route the fix is "paste this number into the
            # portal", so hand over the number rather than the instruction.
            address = await current_public_ip()
            if address:
                print(
                    f"\nThis machine's current public IP is: {address}\n"
                    "Add exactly that to the key's allowed addresses at "
                    "https://developer.brawlstars.com/ ."
                )
        return 3
    except BrawlAPIError as error:
        print(f"FAILED: {error}")
        return 4
    finally:
        await client.close()

    print(f"OK -- authenticated, {len(brawlers)} brawlers returned.")
    print("The key and IP configuration are correct. Seed the frontier next:")
    print("  python -m collectors.brawl_api --seed-only" + (" --proxy" if config.use_proxy else ""))
    return 0


async def run(args: argparse.Namespace) -> int:
    args.data_dir.mkdir(parents=True, exist_ok=True)

    if args.status:
        with CollectorStore(args.data_dir) as store:
            print(f"players known    : {store.player_count():,}")
            print(f"players crawled  : {store.crawled_count():,}")
            print(f"battles stored   : {store.battle_count():,}")
            for name, value in sorted(store.counters().items()):
                print(f"  {name:<20} {value:,}")
        return 0

    api_key = os.environ.get("BRAWL_API_KEY", "")
    if not api_key:
        print(
            "BRAWL_API_KEY is not set.\n"
            "Create a key at https://developer.brawlstars.com/ , allow-listing the "
            "public IP of the machine that will run this collector.",
            file=sys.stderr,
        )
        return 2

    config = ClientConfig(requests_per_second=args.rate, use_proxy=args.proxy)

    if args.check:
        return await check(api_key, config)

    store = CollectorStore(args.data_dir)
    client = BrawlClient(api_key, config)
    crawler = Crawler(
        client,
        store,
        CrawlConfig(
            concurrency=args.concurrency,
            batch_size=args.batch_size,
            recrawl_after_hours=args.recrawl_hours,
        ),
    )

    try:
        if args.seed_only:
            added = await crawler.seed()
            print(f"seeded {added:,} new players; frontier is {store.player_count():,}")
            return 0
        await crawler.run_forever()
    except (KeyNotAuthorizedForIP, InvalidAPIKey) as denied:
        print(str(denied), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        logging.getLogger("arena.collector").info("interrupted; state is durable")
        return 0
    finally:
        await client.close()
        store.close()
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
