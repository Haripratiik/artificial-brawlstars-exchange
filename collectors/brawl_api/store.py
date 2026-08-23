"""Durable state for a crawler that has to survive months unattended.

Two stores, split by how they are used rather than by what they contain.

**SQLite** holds the things the crawler reads back: the player frontier and the
battle-deduplication index. It is in the standard library, it is ACID, and a
crash mid-write leaves a valid database rather than a truncated file. Restart
safety is not a nice-to-have here -- an unattended process *will* be killed at
some point, and a crawl that has to start over has lost weeks.

**Gzipped JSONL** holds the battles themselves, append-only, one file per day.
Append-only because raw observations are evidence: a settlement six months from
now has to be able to name the bytes it came from, and a mutable store makes
that claim unverifiable.

On deduplication: the API returns each battle in the log of *every* participant,
so a single 3v3 battle can arrive six times. Storing all six copies would
multiply the corpus by six for no information. So each battle is written once,
**verbatim as the API returned it**, and the identity key plus the tag of the
player whose log surfaced it are recorded alongside. The battle JSON itself is
never edited.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["CollectorStore", "battle_key"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    tag           TEXT PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    last_crawled  TEXT,
    trophies      INTEGER,
    discovered_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS players_frontier
    ON players (last_crawled);

CREATE TABLE IF NOT EXISTS battles (
    battle_key  TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    battle_time TEXT,
    mode        TEXT,
    map         TEXT
);
CREATE INDEX IF NOT EXISTS battles_by_time
    ON battles (battle_time);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


def battle_key(battle: dict[str, Any]) -> str:
    """Stable identity for a battle, independent of whose log it arrived in.

    Built from the battle timestamp, the full set of participating tags, and the
    event. The tag set is sorted so that the same battle seen from two different
    players' logs produces the same key -- which is the entire point.
    """
    event = battle.get("event") or {}
    detail = battle.get("battle") or {}
    identity = {
        "battleTime": battle.get("battleTime"),
        "mode": detail.get("mode") or event.get("mode"),
        "map": event.get("map"),
        "eventId": event.get("id"),
        "tags": sorted(_participant_tags(battle)),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _participant_tags(battle: dict[str, Any]) -> list[str]:
    """Every player tag in a battle, across all team shapes the API uses.

    Team modes report ``teams`` as a list of lists; showdown reports a flat
    ``players`` list; duo showdown reports ``teams`` of two. Solo entries may
    also appear under ``starPlayer``. All shapes are walked so the identity key
    is complete regardless of mode.
    """
    tags: list[str] = []
    detail = battle.get("battle") or {}

    for team in detail.get("teams") or []:
        for player in team or []:
            tag = (player or {}).get("tag")
            if tag:
                tags.append(tag)

    for player in detail.get("players") or []:
        tag = (player or {}).get("tag")
        if tag:
            tags.append(tag)

    star = detail.get("starPlayer") or {}
    if star.get("tag"):
        tags.append(star["tag"])

    return list(dict.fromkeys(tags))


class CollectorStore:
    """Frontier, dedupe index, and the append-only raw battle log."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._raw_dir = self._root / "battlelogs"
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._root / "collector.db", isolation_level=None)
        # WAL so a reader (a notebook inspecting progress) never blocks the
        # crawler, and so an abrupt kill cannot corrupt the file.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> CollectorStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- frontier ----------------------------------------------------------

    def add_players(
        self, tags: Iterable[str], discovered_by: str, trophies: dict[str, int] | None = None
    ) -> int:
        """Register tags we have not seen before. Returns how many were new."""
        now = _now()
        trophies = trophies or {}
        rows = [(tag, now, None, trophies.get(tag), discovered_by) for tag in set(tags) if tag]
        if not rows:
            return 0
        before = self.player_count()
        self._db.executemany(
            "INSERT OR IGNORE INTO players "
            "(tag, first_seen, last_crawled, trophies, discovered_by) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        return self.player_count() - before

    def next_to_crawl(self, limit: int, recrawl_after_hours: float) -> list[str]:
        """The frontier, in priority order.

        Never-crawled players first -- expanding into unseen parts of the
        population is worth more than refreshing a player whose last 25 battles
        we already have. Then least-recently-crawled, and only those past the
        re-crawl interval, so a small frontier does not spin re-fetching the
        same logs.
        """
        cutoff = _iso(
            datetime.now(timezone.utc).timestamp() - recrawl_after_hours * 3600.0
        )
        rows = self._db.execute(
            "SELECT tag FROM players "
            "WHERE last_crawled IS NULL OR last_crawled < ? "
            "ORDER BY last_crawled IS NOT NULL, last_crawled ASC "
            "LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def mark_crawled(self, tag: str) -> None:
        self._db.execute(
            "UPDATE players SET last_crawled = ? WHERE tag = ?", (_now(), tag)
        )

    def player_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM players").fetchone()[0])

    def crawled_count(self) -> int:
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM players WHERE last_crawled IS NOT NULL"
            ).fetchone()[0]
        )

    # -- battles -----------------------------------------------------------

    def record_battles(
        self, player_tag: str, battles: Sequence[dict[str, Any]]
    ) -> tuple[int, list[str]]:
        """Persist battles not seen before; return (new count, participant tags).

        The participant tags are returned even for already-seen battles: a
        battle we have may still contain a player we do not, and discarding
        those would cripple the snowball.
        """
        now = _now()
        fresh: list[dict[str, Any]] = []
        index_rows: list[tuple[str, str, str | None, str | None, str | None]] = []
        discovered: list[str] = []

        for battle in battles:
            discovered.extend(_participant_tags(battle))
            key = battle_key(battle)
            if self._seen(key):
                continue
            event = battle.get("event") or {}
            detail = battle.get("battle") or {}
            index_rows.append(
                (
                    key,
                    now,
                    battle.get("battleTime"),
                    detail.get("mode") or event.get("mode"),
                    event.get("map"),
                )
            )
            fresh.append(
                {
                    "battle_key": key,
                    "fetched_at": now,
                    "surfaced_by": player_tag,
                    # Verbatim. Never reshaped, never trimmed.
                    "battle": battle,
                }
            )

        if index_rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO battles "
                "(battle_key, first_seen, battle_time, mode, map) VALUES (?, ?, ?, ?, ?)",
                index_rows,
            )
            self._append_raw(fresh)

        return len(fresh), list(dict.fromkeys(discovered))

    def _seen(self, key: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM battles WHERE battle_key = ? LIMIT 1", (key,)
            ).fetchone()
            is not None
        )

    def _append_raw(self, records: Sequence[dict[str, Any]]) -> None:
        """Append to today's gzipped JSONL shard.

        Reopened per batch rather than held open: an unattended process that is
        killed between batches must leave a readable file, and gzip members
        concatenate cleanly, so appending fresh members is safe.
        """
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._raw_dir / f"{day}.jsonl.gz"
        with gzip.open(path, "at", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def battle_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM battles").fetchone()[0])

    # -- counters ----------------------------------------------------------

    def bump(self, name: str, amount: int = 1) -> None:
        self._db.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + ?",
            (name, amount, amount),
        )

    def counters(self) -> dict[str, int]:
        return {
            name: value
            for name, value in self._db.execute("SELECT name, value FROM counters")
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
