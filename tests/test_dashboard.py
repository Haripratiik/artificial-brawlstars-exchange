"""The exchange website: its API, and the views rendered against real data.

Two halves. The Python half exercises the HTTP and WebSocket surface against a
running market. The JavaScript half runs under node — the views are pure
functions of a store, so they can be rendered and inspected without a browser,
which catches the class of bug that looks fine until someone opens the page: a
server-side rename surfacing as ``undefined``, a decimal string rendering as
``NaN``, an object interpolated into text.

That does not replace looking at the page. It does mean renaming a field on the
server fails a test instead of silently blanking a panel.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.server import app, runner
from dashboard.state import FEE_SCHEDULES, MarketConfig

REPO = Path(__file__).resolve().parents[1]
SYMBOL = "SPIKE_WR_FUT"


@pytest.fixture(scope="module")
def client():
    """A live server with a market that has actually traded.

    Speed is raised so a few wall-clock seconds buy a few simulated minutes;
    the market is stepped by the server's own pump, never by the test, because
    two things advancing one event kernel corrupts it.
    """
    with TestClient(app) as test_client:
        runner.set_speed(40.0)
        time.sleep(8)
        runner.set_speed(1.0)
        yield test_client


# --------------------------------------------------------------------------
# Configuration comes from a browser, so it is never trusted
# --------------------------------------------------------------------------


def test_configuration_clamps_hostile_input():
    """Every field is bounded. An unbounded agent count or speed would let a
    page freeze the server that is serving it."""
    config = MarketConfig.from_dict(
        {
            "seed": -1,
            "speed": 10_000,
            "flow_traders": 999_999,
            "fees": "../../etc/passwd",
            "price_band": 900,
        }
    )
    assert 0 <= config.seed < 2**31
    assert config.speed <= 50.0
    assert config.flow_traders <= 24
    assert config.fees in FEE_SCHEDULES
    assert config.price_band <= 5.0


def test_configuration_survives_an_empty_payload():
    assert MarketConfig.from_dict({}).seed == 7


def test_a_blank_price_band_means_no_breaker():
    for blank in (None, "", "none"):
        assert MarketConfig.from_dict({"price_band": blank}).price_band is None


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------


def test_instruments_carry_their_contract_terms(client):
    """A market where you cannot read the contract is a casino with extra steps."""
    payload = client.get("/api/instruments").json()["instruments"]
    assert len(payload) >= 5
    for instrument in payload:
        assert instrument["symbol"]
        assert instrument["spec_digest"]
        assert instrument["expiry"]
        assert instrument["session"] in {"continuous", "pre_open", "auction", "closed"}


def test_the_session_endpoint_describes_the_venue(client):
    payload = client.get("/api/session").json()
    assert set(payload["config"]) >= {"seed", "fees", "flow_traders", "arbitrageur"}
    assert "maker-taker" in payload["fee_schedules"]
    assert payload["sessions"][SYMBOL]


def test_the_agent_roster_is_published(client):
    """Who else is in the market is part of the market's information."""
    agents = client.get("/api/agents").json()["agents"]
    # By role, not by class name. Asserting the class was wrong twice over: it
    # broke the moment a specialised maker was wired in, and it was checking
    # an implementation detail to answer a question about the market.
    roles = {a["role"] for a in agents}
    assert "market maker" in roles
    assert {"informed", "uninformed"} <= roles
    assert any(a["fills"] > 0 for a in agents), "nobody traded"


def test_the_book_endpoint_returns_a_two_sided_ladder(client):
    book = client.get(f"/api/book/{SYMBOL}?levels=12").json()
    assert book["bids"] and book["asks"]
    best_bid = float(book["bids"][0][0])
    best_ask = float(book["asks"][0][0])
    assert best_bid < best_ask, "the published ladder is crossed"


def test_the_book_endpoint_bounds_the_level_count(client):
    """A browser asking for a million levels must not get them."""
    book = client.get(f"/api/book/{SYMBOL}?levels=100000").json()
    assert len(book["bids"]) <= 60


def test_history_accumulates(client):
    """Asserts that the series *grows*, not that it grows at a given rate.

    Two earlier versions of this test asserted a sample count -- first `> 5`,
    then `>= 12` -- and both failed for the same reason: the recorder samples on
    the server's own tick, so how many points exist at any moment depends on how
    loaded the machine is and how many instruments are listed. Neither number
    was ever the point. What the chart needs is a series that accumulates and
    never doubles back, and that is what is checked.
    """
    first = client.get(f"/api/history/{SYMBOL}").json()
    deadline = time.monotonic() + 25
    later = first
    while len(later["t"]) <= len(first["t"]) and time.monotonic() < deadline:
        time.sleep(0.5)
        later = client.get(f"/api/history/{SYMBOL}").json()

    assert len(later["t"]) > len(first["t"]), "the series never grew"
    assert len(later["t"]) == len(later["mid"])
    # Simulated time only moves forward, so a chart drawn from this cannot
    # double back on itself.
    assert later["t"] == sorted(later["t"])


def test_diagnostics_use_the_research_estimators(client):
    """The same code the papers would quote, not a second implementation."""
    report = client.get(f"/api/diagnostics/{SYMBOL}").json()
    if report.get("pending"):
        pytest.skip("not enough observations yet in this run")
    names = {v["name"] for v in report["verdicts"]}
    assert any("Hill" in n for n in names)
    assert any("variance ratio" in n for n in names)


@pytest.mark.parametrize("path", ["/api/history/NOPE", "/api/diagnostics/NOPE", "/api/book/NOPE"])
def test_unknown_symbols_are_refused(client, path):
    assert client.get(path).status_code == 404


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------


def test_halting_and_reopening_round_trips(client):
    """A halt accumulates orders; the reopen is an auction, not a free-for-all."""
    halted = client.post(f"/api/session/{SYMBOL}/halt").json()
    assert halted["ok"] and halted["session"] == "auction"

    reopened = client.post(f"/api/session/{SYMBOL}/uncross").json()
    assert reopened["ok"] and reopened["session"] == "continuous"


def test_uncrossing_a_continuous_symbol_is_refused(client):
    """There is no call phase to clear, and saying so beats pretending."""
    client.post(f"/api/session/{SYMBOL}/uncross")
    again = client.post(f"/api/session/{SYMBOL}/uncross").json()
    assert again["ok"] is False


def test_halting_an_unknown_symbol_is_refused(client):
    assert client.post("/api/session/NOPE/halt").json()["ok"] is False


# --------------------------------------------------------------------------
# The live socket
# --------------------------------------------------------------------------


def test_the_socket_carries_everything_a_screen_needs(client):
    with client.websocket_connect("/ws") as socket:
        payload = socket.receive_json()
    assert set(payload) >= {
        "clock", "events", "books", "tape", "account", "orders", "conservation",
        "sessions", "generation", "speed",
    }
    book = payload["books"][SYMBOL]
    assert set(book) >= {"bids", "asks", "mark", "class", "contract", "tick"}
    assert book["contract"]["payoff"]


def test_value_is_conserved_in_the_served_market(client):
    """The invariant the header light reports. It must be exactly zero."""
    with client.websocket_connect("/ws") as socket:
        payload = socket.receive_json()
    assert int(payload["conservation"]) == 0


@pytest.mark.parametrize("tif", ["gtc", "ioc", "fok", "post_only"])
def test_every_time_in_force_reaches_the_venue(client, tif):
    """The browser can reach post-only and fill-or-kill, not just the defaults."""
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json(
            {"action": "submit", "symbol": SYMBOL, "side": "buy",
             "quantity": 1, "price": "4600", "tif": tif}
        )
        for _ in range(40):
            message = socket.receive_json()
            if "ack" in message:
                assert message["ack"]["ok"] is True
                return
    pytest.fail("no acknowledgement returned")


def test_an_unknown_time_in_force_is_rejected(client):
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json(
            {"action": "submit", "symbol": SYMBOL, "side": "buy",
             "quantity": 1, "price": "4600", "tif": "whenever"}
        )
        for _ in range(40):
            message = socket.receive_json()
            if "ack" in message:
                assert message["ack"]["ok"] is False
                return
    pytest.fail("no acknowledgement returned")


def test_a_nonsense_action_is_reported_not_ignored(client):
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json({"action": "self_destruct"})
        for _ in range(40):
            message = socket.receive_json()
            if "ack" in message:
                assert message["ack"]["ok"] is False
                return
    pytest.fail("no acknowledgement returned")


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------


def test_the_server_starts_the_way_it_is_documented():
    """`python -m dashboard.server` must work outside pytest.

    It did not. The project keeps `arena` under `python/`, and the only thing
    putting that on the path was `pythonpath` in the pytest configuration -- so
    every test passed while the documented command, the sole way anyone opens
    the UI, died on ModuleNotFoundError.

    This runs in a subprocess with a clean environment precisely so pytest's
    own path setup cannot hide the problem a second time.
    """
    environment = dict(os.environ)
    # Anything that would smuggle the package path in gets removed first.
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-c", "import dashboard.server; print(dashboard.server.app.title)"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=environment,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Arena Markets" in result.stdout


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/static/js/main.js", "javascript"),
        ("/static/js/views.js", "javascript"),
        ("/static/js/format.js", "javascript"),
        ("/static/css/terminal.css", "text/css"),
    ],
)
def test_assets_are_served_with_a_usable_content_type(client, path, expected):
    """A 200 is not enough: the browser checks the type before it runs anything.

    Starlette serves static files with whatever `mimetypes` reports, and on
    Windows that reads the registry, where `.js` is commonly mapped to
    `text/plain`. Browsers enforce the MIME type of `<script type="module">`
    strictly and refuse a module served as anything else -- so the entire front
    end silently did not run. The page painted its static HTML, no handler was
    bound, no button worked, and the server logged nothing, because every
    request had answered 200.

    Checking status alone is exactly how that survived a green test suite.
    """
    response = client.get(path)
    assert response.status_code == 200
    content_type = response.headers["content-type"]
    assert expected in content_type, f"{path} served as {content_type}"


def test_every_static_asset_the_page_asks_for_exists():
    """A 404 on a module leaves a blank screen and one line in the console."""
    html = (REPO / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert referenced, "the page references no local assets at all"
    for reference in referenced:
        asset = REPO / "dashboard" / reference.lstrip("/")
        assert asset.is_file(), f"{reference} is referenced but missing"

    # And the modules the entry point pulls in behind it.
    entry = (REPO / "dashboard" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    for module in re.findall(r"from '\./([\w.]+\.js)'", entry):
        assert (REPO / "dashboard" / "static" / "js" / module).is_file(), module


# --------------------------------------------------------------------------
# The views, rendered under node
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_view_renders_against_a_real_snapshot(client, tmp_path):
    """Renders all five screens and fails on `undefined`, `NaN`, unbalanced tags.

    Uses a genuine snapshot rather than a hand-written one, so a field the
    server stops sending shows up here rather than as a blank panel later.
    """
    snapshot = runner.market.snapshot()
    snapshot["generation"] = runner.generation
    snapshot["sessions"] = {
        s: runner.market.venue.session(s).value
        for s in runner.market.venue.registry.symbols
    }
    snapshot["speed"] = runner.market.speed

    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "snapshot": snapshot,
                "instruments": client.get("/api/instruments").json()["instruments"],
                "session": client.get("/api/session").json(),
                "agents": client.get("/api/agents").json()["agents"],
                "depth": client.get(f"/api/book/{SYMBOL}?levels=18").json(),
                "diagnostics": client.get(f"/api/diagnostics/{SYMBOL}").json(),
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(REPO / "tests" / "frontend" / "render.mjs"), str(fixture)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_controller_loads_and_throttles_its_rendering(client, tmp_path):
    """main.js under a stub DOM, driving frames by hand.

    Two things are being caught. The first is that the controller loads at all
    -- it owns the socket, the render loop and every binding, and had no
    coverage. The second is that a burst of snapshots does not turn into a
    burst of subtree rebuilds: replacing the panel destroys focus, scroll and
    text selection, so rebuilding on every message at 20Hz made the whole
    screen impossible to use with a keyboard.
    """
    snapshot = runner.market.snapshot()
    snapshot["generation"] = runner.generation
    snapshot["sessions"] = {
        s: runner.market.venue.session(s).value
        for s in runner.market.venue.registry.symbols
    }
    snapshot["speed"] = runner.market.speed

    fixture = tmp_path / "controller.json"
    fixture.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")

    result = subprocess.run(
        ["node", str(REPO / "tests" / "frontend" / "controller.mjs"), str(fixture)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_revealed_value_is_a_price_not_a_tick_count(client):
    """The one number whose job is to be compared against the market.

    It was reported in ticks while the mark beside it was in contract units,
    so a future marking at 4,663 revealed a settlement of 18,677 and the chart
    drew a target line four times off the top of its own series. Nothing looked
    broken; it just looked like a number.
    """
    listed = client.get("/api/instruments").json()["instruments"]
    snapshot = runner.market.snapshot()

    checked = 0
    for entry in listed:
        settles = entry.get("settles_at")
        if settles in (None, 0):
            continue
        low, high = (float(b) for b in snapshot["books"][entry["symbol"]]["bounds"])
        assert low <= settles <= high, (
            f"{entry['symbol']} reveals {settles}, outside the range {low}..{high} "
            "the same page prints -- which is what a tick count looks like when "
            "it is read as a price"
        )
        checked += 1
    assert checked > 5, "too few contracts had a value to check"
