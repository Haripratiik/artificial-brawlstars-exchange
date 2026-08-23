"""The snowball crawler.

The sampling problem, stated plainly: there is no endpoint that enumerates
players, and no endpoint that returns aggregate statistics. The only way to
find a player tag is a leaderboard, and leaderboards contain exclusively elite
players. A crawl seeded only from the global top 200 would measure the metagame
of the strongest few hundred players on earth and nothing else -- which is,
incidentally, close to the bias that third-party stats sites acknowledge in
their own data.

Two mechanisms widen it, and both are free.

**Seed broadly, not deeply.** Leaderboards are per-country, and the trophy
threshold to appear on a small country's leaderboard is far lower than on the
global one. Seeding from many countries therefore spans a much wider skill
range than seeding from the global list, at identical request cost.

**Snowball through opponents.** A battlelog entry names every participant, not
just the player whose log it is. Each fetch therefore yields up to nine new tags
belonging to players who were *matched against* the seed -- and matchmaking
pairs similar-but-not-identical skill, so the frontier diffuses outward from the
seeds rather than staying pinned to them.

What this does not do is produce a representative sample, and no amount of
crawling would. That is handled downstream: the adjusted metric standardizes
onto pinned stratum weights, so the composition of the crawl cancels out of the
settlement value. The crawler's job is coverage -- reaching enough distinct
strata with enough battles each -- not representativeness.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from collectors.brawl_api.client import BrawlAPIError, BrawlClient, NotFound
from collectors.brawl_api.store import CollectorStore

__all__ = ["CrawlConfig", "Crawler", "SEED_COUNTRIES"]

log = logging.getLogger("arena.collector")

# A deliberately mixed list. Large gaming markets supply volume; small ones
# supply low-trophy players who would never appear on the global board. The
# spread across leaderboard thresholds is the point, not the country coverage.
SEED_COUNTRIES: tuple[str, ...] = (
    "global",
    # high-volume markets
    "us", "br", "mx", "de", "fr", "gb", "it", "es", "ru", "tr",
    "kr", "jp", "id", "th", "vn", "ph", "in", "cn", "pl", "ca",
    # mid-size
    "nl", "se", "no", "fi", "dk", "pt", "gr", "cz", "ro", "hu",
    "ar", "cl", "co", "pe", "au", "nz", "za", "eg", "sa", "ae",
    # small boards, where the trophy threshold to appear is lowest
    "is", "mt", "lu", "cy", "ee", "lv", "lt", "si", "hr", "sk",
    "uy", "py", "bo", "cr", "pa", "jm", "tt", "mu", "fj", "bh",
)


@dataclass(slots=True)
class CrawlConfig:
    # How many battlelogs to have in flight. Kept small: the token bucket, not
    # concurrency, is what sets throughput, and a deep queue only adds latency
    # between a 429 and the backoff that answers it.
    concurrency: int = 4
    batch_size: int = 200
    # A player's log holds 25 battles. Re-fetching sooner than they can plausibly
    # have played 25 more games mostly returns battles already stored.
    recrawl_after_hours: float = 12.0
    # Re-seeding refreshes the leaderboards, which turn over constantly.
    reseed_every_batches: int = 50
    # Stop expanding once the frontier is large enough to keep the crawler busy;
    # past this, new tags cost dedupe work without improving coverage.
    max_frontier: int = 400_000
    seed_countries: tuple[str, ...] = field(default=SEED_COUNTRIES)


class Crawler:
    def __init__(
        self, client: BrawlClient, store: CollectorStore, config: CrawlConfig | None = None
    ) -> None:
        self._client = client
        self._store = store
        self._config = config or CrawlConfig()

    async def seed(self) -> int:
        """Populate the frontier from every configured leaderboard."""
        added = 0
        for country in self._config.seed_countries:
            try:
                entries = await self._client.player_rankings(country)
            except NotFound:
                log.warning("no leaderboard for %r; skipping", country)
                continue
            except BrawlAPIError as error:
                log.warning("leaderboard %r failed: %s", country, error)
                continue

            tags = [entry["tag"] for entry in entries if entry.get("tag")]
            trophies = {
                entry["tag"]: entry.get("trophies")
                for entry in entries
                if entry.get("tag") and entry.get("trophies") is not None
            }
            new = self._store.add_players(tags, discovered_by=f"rankings:{country}", trophies=trophies)
            added += new
            log.info("seed %-7s %3d entries, %3d new", country, len(tags), new)

        self._store.bump("seed_runs")
        return added

    async def _crawl_one(self, tag: str) -> tuple[int, int]:
        """Fetch one battlelog. Returns (new battles, new tags discovered)."""
        try:
            battles = await self._client.battlelog(tag)
        except NotFound:
            # The tag is gone. Mark it crawled so the frontier stops offering it.
            self._store.mark_crawled(tag)
            self._store.bump("players_missing")
            return (0, 0)
        except BrawlAPIError as error:
            log.debug("battlelog %s failed: %s", tag, error)
            self._store.bump("fetch_errors")
            return (0, 0)

        new_battles, participants = self._store.record_battles(tag, battles)
        self._store.mark_crawled(tag)

        new_tags = 0
        if self._store.player_count() < self._config.max_frontier:
            new_tags = self._store.add_players(participants, discovered_by=f"battle:{tag}")

        self._store.bump("battlelogs_fetched")
        self._store.bump("battles_new", new_battles)
        return (new_battles, new_tags)

    async def run_batch(self) -> tuple[int, int, int]:
        """Crawl one batch of the frontier. Returns (players, battles, new tags)."""
        tags = self._store.next_to_crawl(
            self._config.batch_size, self._config.recrawl_after_hours
        )
        if not tags:
            return (0, 0, 0)

        semaphore = asyncio.Semaphore(self._config.concurrency)

        async def guarded(tag: str) -> tuple[int, int]:
            async with semaphore:
                return await self._crawl_one(tag)

        results = await asyncio.gather(*(guarded(tag) for tag in tags))
        return (len(tags), sum(r[0] for r in results), sum(r[1] for r in results))

    async def run_forever(self) -> None:
        """The unattended loop. Seeds, crawls, re-seeds, and never exits cleanly."""
        if self._store.player_count() == 0:
            log.info("empty frontier, seeding")
            await self.seed()

        batches = 0
        while True:
            batches += 1
            players, battles, new_tags = await self.run_batch()

            if players == 0:
                # Everything in the frontier is inside its re-crawl interval.
                # Sleep rather than spin; the interval exists to stop us
                # re-fetching logs that cannot have changed yet.
                log.info("frontier exhausted for now; sleeping 60s")
                await asyncio.sleep(60)
                continue

            if batches % 10 == 0:
                log.info(
                    "batch %d | %d players | +%d battles | +%d tags | "
                    "frontier %d (%d crawled) | corpus %d battles",
                    batches,
                    players,
                    battles,
                    new_tags,
                    self._store.player_count(),
                    self._store.crawled_count(),
                    self._store.battle_count(),
                )

            if batches % self._config.reseed_every_batches == 0:
                log.info("re-seeding leaderboards")
                await self.seed()
