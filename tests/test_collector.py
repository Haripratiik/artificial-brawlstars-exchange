"""Collector tests that need no API key and no network.

The parts worth testing offline are the ones that decide what the corpus
actually contains: battle identity, deduplication, and frontier ordering. A bug
in any of them is silent and compounds for months before anyone notices the
dataset is wrong.
"""

from __future__ import annotations

import gzip
import json

import pytest

from collectors.brawl_api.store import CollectorStore, battle_key


def team_battle(battle_time: str, tags: list[str], mode: str = "gemGrab") -> dict:
    """A 3v3 battle in the shape the API returns."""
    return {
        "battleTime": battle_time,
        "event": {"id": 15000123, "mode": mode, "map": "Hard Rock Mine"},
        "battle": {
            "mode": mode,
            "type": "ranked",
            "result": "victory",
            "starPlayer": {"tag": tags[0], "brawler": {"id": 1, "name": "SPIKE"}},
            "teams": [
                [{"tag": tag, "brawler": {"id": i, "name": "X"}} for i, tag in enumerate(tags[:3])],
                [{"tag": tag, "brawler": {"id": i, "name": "Y"}} for i, tag in enumerate(tags[3:])],
            ],
        },
    }


def showdown_battle(battle_time: str, tags: list[str]) -> dict:
    """Showdown reports a flat players list rather than teams."""
    return {
        "battleTime": battle_time,
        "event": {"id": 15000456, "mode": "soloShowdown", "map": "Skull Creek"},
        "battle": {
            "mode": "soloShowdown",
            "rank": 3,
            "players": [{"tag": tag, "brawler": {"id": 1, "name": "Z"}} for tag in tags],
        },
    }


TAGS = ["#AAA", "#BBB", "#CCC", "#DDD", "#EEE", "#FFF"]


def test_same_battle_from_different_logs_has_one_key():
    """The whole dedupe strategy rests on this.

    The same battle arrives in up to six players' logs. Team membership is
    reported in a different order depending on whose log it is, so the key must
    be invariant to that ordering or the corpus inflates sixfold.
    """
    from_a = team_battle("20260823T120000.000Z", TAGS)
    from_d = team_battle("20260823T120000.000Z", TAGS[3:] + TAGS[:3])
    assert battle_key(from_a) == battle_key(from_d)


def test_different_battles_have_different_keys():
    base = team_battle("20260823T120000.000Z", TAGS)
    later = team_battle("20260823T130000.000Z", TAGS)
    other_players = team_battle("20260823T120000.000Z", TAGS[:5] + ["#ZZZ"])

    assert battle_key(base) != battle_key(later)
    assert battle_key(base) != battle_key(other_players)


def test_showdown_participants_are_extracted(tmp_path):
    """A flat players list must snowball just as well as teams."""
    with CollectorStore(tmp_path) as store:
        tags = [f"#S{i}" for i in range(10)]
        _new, participants = store.record_battles("#S0", [showdown_battle("20260823T120000.000Z", tags)])
        assert set(participants) == set(tags)


def test_duplicate_battles_are_stored_once(tmp_path):
    with CollectorStore(tmp_path) as store:
        battle = team_battle("20260823T120000.000Z", TAGS)
        first, _ = store.record_battles("#AAA", [battle])
        second, _ = store.record_battles("#DDD", [battle])

        assert (first, second) == (1, 0)
        assert store.battle_count() == 1


def test_participants_are_returned_even_for_known_battles(tmp_path):
    """A battle we already have can still contain a player we do not.

    Dropping those tags would stall the snowball as soon as the crawl started
    revisiting popular players.
    """
    with CollectorStore(tmp_path) as store:
        battle = team_battle("20260823T120000.000Z", TAGS)
        store.record_battles("#AAA", [battle])
        _new, participants = store.record_battles("#DDD", [battle])
        assert set(participants) == set(TAGS)


def test_raw_battles_are_written_verbatim(tmp_path):
    """Evidence must survive unedited, or provenance claims are hollow."""
    with CollectorStore(tmp_path) as store:
        battle = team_battle("20260823T120000.000Z", TAGS)
        store.record_battles("#AAA", [battle])

    shards = list((tmp_path / "battlelogs").glob("*.jsonl.gz"))
    assert len(shards) == 1
    with gzip.open(shards[0], "rt", encoding="utf-8") as handle:
        record = json.loads(handle.readline())

    assert record["battle"] == battle
    assert record["surfaced_by"] == "#AAA"
    assert record["battle_key"] == battle_key(battle)


def test_frontier_prefers_never_crawled_players(tmp_path):
    """Expanding coverage beats refreshing a log we already hold."""
    with CollectorStore(tmp_path) as store:
        store.add_players(["#OLD"], discovered_by="test")
        store.mark_crawled("#OLD")
        store.add_players(["#NEW"], discovered_by="test")

        assert store.next_to_crawl(limit=10, recrawl_after_hours=0.0)[0] == "#NEW"


def test_recrawl_interval_holds_back_recent_players(tmp_path):
    with CollectorStore(tmp_path) as store:
        store.add_players(["#A"], discovered_by="test")
        store.mark_crawled("#A")
        assert store.next_to_crawl(limit=10, recrawl_after_hours=12.0) == []


def test_state_survives_reopening(tmp_path):
    """An unattended crawler will be killed; it must not lose the frontier."""
    with CollectorStore(tmp_path) as store:
        store.add_players(TAGS, discovered_by="test")
        store.record_battles("#AAA", [team_battle("20260823T120000.000Z", TAGS)])

    with CollectorStore(tmp_path) as reopened:
        assert reopened.player_count() == len(TAGS)
        assert reopened.battle_count() == 1
        # And the dedupe index is still authoritative across the restart.
        new, _ = reopened.record_battles("#BBB", [team_battle("20260823T120000.000Z", TAGS)])
        assert new == 0


def test_adding_known_players_is_idempotent(tmp_path):
    with CollectorStore(tmp_path) as store:
        assert store.add_players(TAGS, discovered_by="test") == len(TAGS)
        assert store.add_players(TAGS, discovered_by="test") == 0


@pytest.mark.parametrize("tag,expected", [("#ABC", "%23ABC"), ("abc", "%23ABC"), (" #aBc ", "%23ABC")])
def test_tags_are_normalized_and_encoded(tag, expected):
    from collectors.brawl_api.client import _encode_tag

    assert _encode_tag(tag) == expected


# --------------------------------------------------------------------------
# 403 diagnosis: the single most common setup failure
# --------------------------------------------------------------------------


def test_wrong_ip_and_bad_key_are_told_apart():
    """Both come back as 403; conflating them makes setup a guessing game."""
    from collectors.brawl_api.client import (
        InvalidAPIKey,
        KeyNotAuthorizedForIP,
        _diagnose_403,
    )

    bad_ip = _diagnose_403(
        '{"reason":"accessDenied.invalidIp",'
        '"message":"Invalid authorization: API key does not allow access from 1.2.3.4"}',
        via_proxy=False,
    )
    bad_key = _diagnose_403(
        '{"reason":"accessDenied","message":"Invalid authorization"}', via_proxy=False
    )

    assert isinstance(bad_ip, KeyNotAuthorizedForIP)
    assert isinstance(bad_key, InvalidAPIKey)
    assert "not an IP problem" in str(bad_key)


def test_ip_guidance_depends_on_the_route():
    """Through the proxy, the address to allow-list is the proxy's, not yours."""
    from collectors.brawl_api.client import PROXY_WHITELIST_IP, _diagnose_403

    body = '{"reason":"accessDenied.invalidIp","message":"bad ip address"}'
    direct = str(_diagnose_403(body, via_proxy=False))
    proxied = str(_diagnose_403(body, via_proxy=True))

    assert "api.ipify.org" in direct
    assert PROXY_WHITELIST_IP not in direct
    assert PROXY_WHITELIST_IP in proxied


def test_proxy_flag_selects_the_brawl_stars_proxy_host():
    from collectors.brawl_api.client import (
        DIRECT_BASE_URL,
        PROXY_BASE_URL,
        ClientConfig,
    )

    assert ClientConfig().base_url == DIRECT_BASE_URL
    assert ClientConfig(use_proxy=True).base_url == PROXY_BASE_URL
    # The Brawl Stars proxy is a distinct host from the Clash Royale one.
    assert PROXY_BASE_URL.startswith("https://bsproxy.royaleapi.dev")
