"""Two browsers are two traders.

Before this, every connection to the exchange traded one account. Two tabs
shared a balance, a blotter and a set of working orders, and either could
cancel the other's. That is not a missing feature on a venue whose entire
premise is people trading against each other -- it is the premise not holding.

What is here is a signed session cookie: an account id and a display name,
authenticated by an HMAC the browser cannot forge. What is deliberately *not*
here is a password, and the docstring in `dashboard/identity.py` says so at
length rather than leaving the difference between "signed in" and
"authenticated" comfortably vague.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arena.sim.time import Timestamp, millis, seconds

from dashboard.identity import COOKIE, display_name, sign, verify
import dashboard.server as server


# --------------------------------------------------------------------------
# The cookie
# --------------------------------------------------------------------------


def test_a_cookie_survives_a_round_trip():
    payload = {"sid": "abc", "name": "Ada"}
    assert verify(sign(payload)) == payload


def test_a_tampered_cookie_is_refused():
    """The browser can read what it is and cannot write itself another."""
    value = sign({"sid": "abc", "name": "Ada"})
    body, mac = value.split(".")
    forged = sign({"sid": "abc", "name": "Ada"}).split(".")[0]
    assert verify(f"{forged}x.{mac}") is None
    assert verify(f"{body}.{mac[:-1]}x") is None
    assert verify("nonsense") is None
    assert verify(None) is None
    assert verify("") is None


def test_a_name_is_trimmed_capped_and_stripped():
    """It is rendered on other people's screens, so it arrives hostile."""
    assert display_name("  Ada  ") == "Ada"
    assert len(display_name("x" * 400)) == 24
    assert display_name("Ada\x00\x07Lovelace") == "AdaLovelace"
    assert display_name("") != ""
    assert display_name(None) != ""
    assert " " in display_name(None), "a generated name should read as a name"


def _open_book(market, symbol: str = "SPIKE_WR_FUT", until: int = 240):
    """Advance until ``symbol`` is trading two-sided, and return its touch.

    A market with a circuit breaker spends part of its session halted, and the
    opening minutes are when it happens most. A test that acts at a fixed
    moment is testing what the breaker happened to be doing rather than what it
    set out to.
    """
    from arena.exchange.session import SessionState

    moment = int(market.kernel.now // 1_000_000_000) + 5
    while moment <= until:
        market.kernel.advance(until=seconds(moment))
        book = market.venue.engine(symbol).book.snapshot()
        if (
            market.venue.session(symbol) is SessionState.CONTINUOUS
            and book.best_bid is not None
            and book.best_ask is not None
        ):
            return book
        moment += 5
    raise AssertionError(f"{symbol} never traded two-sided by t={until}")


# --------------------------------------------------------------------------
# The exchange
# --------------------------------------------------------------------------


def test_two_people_get_two_accounts():
    """Separate cash, separate positions, separate blotters."""
    from dashboard.build_market import build

    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(45))

    ada = market.seat("Ada")
    grace = market.seat("Grace")
    assert ada != grace

    market.submit("SPIKE_WR_FUT", "buy", 5, None, trader=ada)
    market.kernel.advance(until=seconds(60))

    mine = market.snapshot(ada)
    theirs = market.snapshot(grace)
    assert mine["you"]["name"] == "Ada"
    assert theirs["you"]["name"] == "Grace"
    assert mine["account"]["cash"] != theirs["account"]["cash"]
    assert not theirs["account"]["positions"], "Grace was given Ada's position"
    assert int(market.venue.conservation_check()) == 0


def test_a_seat_starts_with_the_capital_a_person_can_read():
    """Not the bots' balance sheet: a gain of a hundred against forty million
    teaches a trader nothing about what their trade did."""
    from dashboard.build_market import HUMAN_STARTING_CASH, build

    market = build(seed=7)
    market.kernel.start()
    ada = market.seat("Ada")
    account = market.venue.account(ada)
    assert int(account.starting_cash) == HUMAN_STARTING_CASH * 1_000_000


def test_a_seat_is_as_far_from_the_exchange_as_anyone_at_a_browser():
    from dashboard.build_market import build
    from arena.market.live import HUMAN_ID

    market = build(seed=7)
    market.kernel.start()
    ada = market.seat("Ada")
    assert market.latency.per_agent[ada] == market.latency.per_agent[HUMAN_ID]


def test_one_person_cannot_cancel_another_persons_order():
    from dashboard.build_market import build

    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(180))
    book = _open_book(market, until=300)

    ada = market.seat("Ada")
    grace = market.seat("Grace")
    # Just behind the touch, and checked a fraction of a second later.
    #
    # Two earlier versions were defeated by the market itself. A bid at 1,000
    # was *filled*, because with evidence arriving over the session the price
    # swings far enough that a price nobody should hit gets hit; a bid at 0.25
    # was then refused outright once trades outside the band were prevented.
    # An order behind the touch, inspected before anyone has had time to lift
    # it, is a working order for the reason a working order is usually one.
    # Placed until one of them survives long enough to be looked at.
    #
    # Three earlier versions were defeated by the market rather than by the
    # code. A bid at 1,000 was *filled*, because with evidence arriving over
    # the session the price swings far enough that a price nobody should hit
    # gets hit. A bid at 0.25 was then refused outright once unpriced orders
    # were collared. A bid just behind the touch was lifted seventy
    # milliseconds after it was acknowledged, because the book is about sixty
    # lots a side and something is always sweeping it. A working order is a
    # thing markets keep taking away; the test asks again.
    instrument = market.venue.registry.require("SPIKE_WR_FUT")
    working: list = []
    for attempt in range(12):
        book = _open_book(market, until=600)
        resting = float(instrument.from_ticks(book.best_bid)) - (
            float(instrument.tick_size) * (1 + attempt * 4)
        )
        market.submit("SPIKE_WR_FUT", "buy", 5, f"{resting:.2f}", trader=ada)
        market.kernel.advance(
            until=Timestamp(int(market.kernel.now) + int(millis(120)))
        )
        working = list(market.traders[ada].live_orders)
        if working:
            break
    assert working, "Ada has no working order; the test proves nothing"
    result = market.cancel(working[0], trader=grace)
    assert result["ok"] is False
    assert list(market.traders[ada].live_orders) == working


def test_an_account_cannot_be_restated_after_it_exists():
    """Opening cash is what every PnL figure is measured against."""
    from dashboard.build_market import build

    market = build(seed=7)
    ada = market.seat("Ada")
    with pytest.raises(ValueError, match="cannot be restated"):
        market.venue.open_account(ada, 1_000_000)


def test_joining_a_running_market_does_not_disturb_anyone_else():
    """A newcomer's random stream is its own, so nobody else's draws shift."""
    from dashboard.build_market import build

    quiet = build(seed=7)
    quiet.kernel.start()
    quiet.kernel.advance(until=seconds(45))

    busy = build(seed=7)
    busy.kernel.start()
    busy.kernel.advance(until=seconds(20))
    busy.seat("Ada")
    busy.kernel.advance(until=seconds(45))

    assert len(quiet.venue.engine("SPIKE_WR_FUT").tape) == len(
        busy.venue.engine("SPIKE_WR_FUT").tape
    ), "seating someone changed what everyone else did"


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------


@pytest.fixture()
def browsers():
    with TestClient(server.app) as first, TestClient(server.app) as second:
        first.get("/")
        second.get("/")
        yield first, second


def test_each_browser_is_issued_its_own_session(browsers):
    first, second = browsers
    assert first.cookies.get(COOKIE) and second.cookies.get(COOKIE)
    assert first.cookies.get(COOKIE) != second.cookies.get(COOKIE)


def test_the_socket_reports_the_account_it_is_signed_in_as(browsers):
    first, second = browsers
    first.post("/api/me", json={"name": "Ada"})
    second.post("/api/me", json={"name": "Grace"})

    with first.websocket_connect("/ws") as one, second.websocket_connect("/ws") as two:
        mine = one.receive_json()
        theirs = two.receive_json()

    assert mine["you"]["name"] == "Ada"
    assert theirs["you"]["name"] == "Grace"
    assert mine["you"]["id"] != theirs["you"]["id"]


def test_a_connection_with_no_cookie_still_works(browsers):
    """The API and every test reach the shared account, exactly as before."""
    with TestClient(server.app) as bare:
        bare.cookies.clear()
        with bare.websocket_connect("/ws") as socket:
            payload = socket.receive_json()
    assert payload["you"]["id"] == "you"


def test_the_roster_names_the_people_as_well_as_the_bots(browsers):
    first, _second = browsers
    first.post("/api/me", json={"name": "Ada"})
    with first.websocket_connect("/ws") as socket:
        payload = socket.receive_json()
    assert "Ada" in {t["name"] for t in payload["traders"]}
