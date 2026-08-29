"""The streaming half of the trading API, against a market that is running.

Everything here talks to a real :class:`~dashboard.state.MarketRunner` with a
real pump behind it, because the properties worth testing are all properties of
a feed under load: that a conflated channel publishes the latest state rather
than a stale one, that an event channel loses nothing while a client is behind,
that a private channel follows its own seat across a rebuild that discards
every account in the market.

The application is built here rather than imported from ``dashboard.server``.
Mounting the endpoint the way an application mounts it is part of what is under
test, and the dashboard's own module-level market would tie every test in this
file to whatever the rest of that test suite had already done to it.

Two mechanisms are worth explaining before they appear:

**Ping as a fence.** ``TestClient``'s websocket has no receive timeout, so a
test that reads one frame too many hangs until the whole suite is killed rather
than failing. Every read here goes through :func:`drain`, which sends a ``ping``
carrying a unique id and reads until the matching ``pong``. The server answers
every ping, so the read always terminates, and the pong is a point in the
stream after which everything earlier has certainly been delivered.

**A fake socket for the slow-client case.** ``TestClient`` buffers what the
server sends without bound, so a test client that stops reading applies no
backpressure at all and cannot exercise the path that matters. The conflation
test therefore drives the endpoint directly with a socket whose ``send_json``
can be stalled on demand, and steps the market from the same event loop while
it is stalled -- which is the actual question: does a client that has stopped
reading stop the market, and does it lose anything when it comes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.api.keys import KeyStore, sign
from arena.api.stream import STREAM_PATH, configure, stream_endpoint
from arena.exchange.session import SessionState
from arena.exchange.types import AgentId
from dashboard.state import MarketConfig, MarketRunner

# The server's own cadence, and the pump's. Both matter more than they look:
# a kernel step blocks the event loop that also serves the feed, so a market
# stepped at a high speed multiple starves the flush loop and the connection
# publishes in bursts. Speed 1.0 keeps a step at a few milliseconds, which is
# what the dashboard runs at and what leaves the feed responsive.
TICK = 0.02
PUMP = 0.05

# Field names that belong to an account and must never appear on a public
# channel. ``buy_order_id`` and ``sell_order_id`` are here because the engine
# gives an order the same id on the public print and on the private
# acknowledgement, so publishing them on the tape lets any observer work out
# who traded.
PRIVATE_FIELDS = frozenset(
    {
        "account",
        "agent_id",
        "aggressor",
        "buy_order_id",
        "cash",
        "collateral",
        "counterparties",
        "equity",
        "free_cash",
        "log",
        "order_id",
        "pnl",
        "position",
        "positions",
        "remaining",
        "seat",
        "sell_order_id",
        "traders",
        "you",
    }
)


# --------------------------------------------------------------------------
# An application that mounts the endpoint, and the controls a test needs
# --------------------------------------------------------------------------


@dataclass
class Venue:
    """The pieces of the running application a test reaches for."""

    runner: MarketRunner
    keys: KeyStore
    client: TestClient

    def control(self, path: str, **params: Any) -> Any:
        method = self.client.post if path.startswith("/control/do") else self.client.get
        response = method(path, params=params)
        assert response.status_code == 200, response.text
        return response.json()


async def _run_market(runner: MarketRunner) -> None:
    """Advance the kernel on a timer, exactly as the dashboard's server does."""
    runner.start()
    while True:
        try:
            runner.step()
        except Exception as failure:  # keep serving, and say so
            print(f"market step failed: {failure!r}")
        await asyncio.sleep(PUMP)


def build_app(runner: MarketRunner, keys: KeyStore) -> FastAPI:
    """The endpoint mounted the way an application mounts it, plus test controls.

    Every control is an ordinary route, which is the point: they run on the
    event loop that steps the market, so a test that rebuilds the venue or
    sends an order cannot land in the middle of a kernel step. Reaching into
    the runner from the test thread instead would be two threads advancing one
    event kernel, which corrupts it.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        pump = asyncio.create_task(_run_market(runner))
        try:
            yield
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    # Where each of this application's people is sitting, by seat token. The
    # same shape ``dashboard.server`` has and hands to both halves of the API:
    # a token that outlives the market, an account that does not, and a
    # re-seat whenever the generation moves. Both halves must be given the
    # same one or a credential ends up on two accounts -- placing orders on
    # one and watching the other's fills.
    seats: dict[str, tuple[str, AgentId]] = {}

    def seat_now(token: str) -> AgentId | None:
        held = seats.get(token)
        if held is None:
            return None
        name, agent_id = held
        if agent_id not in runner.market.venue.accounts:
            agent_id = runner.market.seat(name)
            seats[token] = (name, agent_id)
        return agent_id

    app = FastAPI(lifespan=lifespan)
    configure(
        keys=keys,
        runner=runner,
        path=STREAM_PATH,
        tick_seconds=TICK,
        seat_now=seat_now,
    )
    app.add_api_websocket_route(STREAM_PATH, stream_endpoint())

    @app.get("/control/state")
    async def state() -> dict[str, Any]:
        venue = runner.market.venue
        return {
            "generation": runner.generation,
            "shared": str(runner.market.human.agent_id),
            "traders": {str(k): v.display_name for k, v in runner.market.traders.items()},
            "logs": {str(k): len(v.log) for k, v in runner.market.traders.items()},
            "conservation": int(venue.conservation_check()),
            "symbols": list(venue.registry.symbols),
            "prints": {s: len(venue.engine(s).tape) for s in venue.registry.symbols},
        }

    @app.get("/control/target")
    async def target() -> dict[str, Any]:
        """The busiest contract a market order can actually trade in right now.

        Ranked by how much it has printed rather than by resting depth. A
        contract can show a fat ladder and hardly ever trade -- an option
        nobody is taking, a spread quoted wide -- and a test that waits for a
        print on one of those is waiting for the wrong thing.
        """
        venue = runner.market.venue
        best: tuple[str, int] | None = None
        for symbol in venue.registry.symbols:
            if venue.session(symbol) is not SessionState.CONTINUOUS:
                continue
            book = venue.engine(symbol).book.snapshot(4)
            if not book.priced_bids or not book.priced_asks:
                continue
            prints = len(venue.engine(symbol).tape)
            if best is None or prints > best[1]:
                best = (symbol, prints)
        return {"symbol": None if best is None else best[0]}

    @app.get("/control/tape")
    async def tape(symbol: str) -> dict[str, Any]:
        prints = runner.market.venue.engine(symbol).tape
        return {"symbol": symbol, "sequences": [int(p.sequence) for p in prints]}

    @app.post("/control/do/seat")
    async def do_seat(name: str) -> dict[str, Any]:
        """Seat a person, and hand back the token a key would be issued for."""
        token = f"tok-{name}-{len(seats)}"
        seats[token] = (name, runner.market.seat(name))
        return {"token": token, "id": str(seats[token][1]), "name": name}

    @app.get("/control/whoami")
    async def whoami(token: str) -> dict[str, Any]:
        """Where the application thinks a token is sitting, right now."""
        agent_id = seat_now(token)
        return {"token": token, "id": None if agent_id is None else str(agent_id)}

    @app.post("/control/do/rebuild")
    async def do_rebuild(decoys: str = "") -> dict[str, Any]:
        runner.reconfigure(runner.config)
        seated = {
            name: str(runner.market.seat(name))
            for name in (n for n in decoys.split(",") if n)
        }
        return {"generation": runner.generation, "decoys": seated}

    @app.post("/control/do/order")
    async def do_order(symbol: str, side: str, quantity: int, agent: str = "") -> Any:
        return runner.market.submit(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=None,
            trader=AgentId(agent) if agent else None,
        )

    return app


@pytest.fixture(scope="module")
def venue():
    """A live application with a market that has actually traded.

    The opening auction is off so every contract is trading continuously from
    the first tick; a test that had to wait for the uncross would be measuring
    the calendar rather than the feed.
    """
    runner = MarketRunner(
        MarketConfig(seed=7, speed=1.0, makers=2, flow_traders=2, opening_auction=False)
    )
    keys = KeyStore()
    with TestClient(build_app(runner, keys)) as client:
        time.sleep(1.5)
        yield Venue(runner=runner, keys=keys, client=client)


# --------------------------------------------------------------------------
# Reading a socket without hanging
# --------------------------------------------------------------------------

_fences = itertools.count(1)

# Reads happen on worker threads purely so they can be given a deadline; see
# :func:`receive`.
_READS = ThreadPoolExecutor(max_workers=16, thread_name_prefix="stream-read")


def receive(socket: Any, *, seconds: float = 15.0) -> dict[str, Any]:
    """One frame, or a failure. Never an indefinite wait.

    ``TestClient``'s websocket has no receive timeout, so a frame that never
    arrives hangs the whole suite until somebody kills it -- and a hang says
    far less about what broke than a failure does. The read runs on a worker
    thread so the deadline can be enforced; the thread left behind on a timeout
    goes away with the process, and by then the test has already failed.
    """
    future = _READS.submit(socket.receive_json)
    try:
        return future.result(timeout=seconds)
    except FutureTimeout:
        raise AssertionError(
            f"the stream sent nothing for {seconds:.0f}s -- it has stopped answering"
        ) from None


def drain(socket: Any) -> list[dict[str, Any]]:
    """Everything the server has sent us up to now.

    Terminates because the server answers every ping, and fails rather than
    waits if it stops doing so.
    """
    token = f"fence-{next(_fences)}"
    socket.send_json({"op": "ping", "id": token})
    frames: list[dict[str, Any]] = []
    while True:
        frame = receive(socket)
        frames.append(frame)
        if frame.get("type") == "pong" and frame.get("id") == token:
            return frames


def gather(
    socket: Any,
    want: Any,
    *,
    seconds: float = 20.0,
    hint: str = "",
    sink: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wait for the market to produce frames matching ``want``.

    Returns the matches and everything seen along the way, because most tests
    here want to assert something about the frames that did *not* match too. A
    ``sink`` collects into a list the caller already has, which is what the
    gapless test needs: a helper that quietly swallowed the frames it read past
    would manufacture the very gap that test is looking for.
    """
    deadline = time.monotonic() + seconds
    seen: list[dict[str, Any]] = sink if sink is not None else []
    while True:
        seen.extend(drain(socket))
        hits = [frame for frame in seen if want(frame)]
        if hits:
            return hits, seen
        if time.monotonic() > deadline:
            kinds = sorted({str(frame.get("type")) for frame in seen})
            raise AssertionError(f"no frame matched {hint or want}; saw {kinds}")
        time.sleep(0.05)


def subscribe(
    socket: Any, *channels: str, sink: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    token = f"sub-{next(_fences)}"
    socket.send_json({"op": "subscribe", "channels": list(channels), "id": token})
    matches, _ = gather(
        socket,
        lambda f: f.get("id") == token and f.get("type") in ("subscribed", "error"),
        hint=f"a reply to subscribing {channels}",
        sink=sink,
    )
    return matches[0]


def authenticate(venue: Venue, socket: Any, key: Any) -> dict[str, Any]:
    stamp = str(time.time())
    token = f"auth-{next(_fences)}"
    socket.send_json(
        {
            "op": "auth",
            "id": token,
            "key_id": key.key_id,
            "timestamp": stamp,
            "signature": sign(key.secret, "GET", STREAM_PATH, stamp, b""),
        }
    )
    matches, _ = gather(
        socket,
        lambda f: f.get("id") == token and f.get("type") in ("auth", "error"),
        hint="a reply to auth",
    )
    return matches[0]


def tradeable(venue: Venue, *, seconds: float = 20.0) -> str:
    """A contract a market order can trade in, once the venue has one.

    Polled rather than read once. Straight after a rebuild the books are empty
    and the phases have not settled, so there is a window in which the honest
    answer is "none yet" -- and a test that took that answer would be asserting
    something about the calendar rather than about the feed.
    """
    deadline = time.monotonic() + seconds
    while True:
        symbol = venue.control("/control/target")["symbol"]
        if symbol:
            return str(symbol)
        if time.monotonic() > deadline:
            raise AssertionError("no contract became tradeable")
        time.sleep(0.1)


def until(
    venue: Venue,
    ready: Any,
    *,
    hint: str,
    seconds: float = 20.0,
    drains: tuple[Any, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Wait for the market to reach a state, reading the socket while waiting.

    The socket is drained into the caller's list rather than left alone,
    because several tests need to assert that nothing arrived during the wait
    and a frame still sitting in the client's buffer is not "nothing arrived".
    """
    deadline = time.monotonic() + seconds
    while True:
        if drains is not None:
            drains[1].extend(drain(drains[0]))
        state = venue.control("/control/state")
        if ready(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {hint}")
        time.sleep(0.1)


def walk(value: Any) -> Any:
    """Every value nested anywhere inside a frame, with its key."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield None, item
            yield from walk(item)


# --------------------------------------------------------------------------
# The handshake
# --------------------------------------------------------------------------


def test_the_first_frame_names_the_channels_and_the_contracts(venue):
    """A client should not need a REST round trip to know what it can ask for."""
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        hello = socket.receive_json()
    assert hello["type"] == "hello"
    assert hello["seq"] == 1
    assert hello["channel"] is None
    assert set(hello["public_channels"]) == {"ticker", "book", "trades"}
    assert set(hello["private_channels"]) == {"orders", "fills"}
    assert len(hello["symbols"]) > 5


def test_ping_is_answered_with_pong(venue):
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        socket.send_json({"op": "ping", "id": "abc"})
        matches, _ = gather(socket, lambda f: f["type"] == "pong", hint="a pong")
    assert matches[0]["id"] == "abc"
    assert matches[0]["channel"] is None


def test_subscribing_starts_a_feed_and_unsubscribing_stops_it(venue):
    symbol = venue.control("/control/state")["symbols"][0]
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        ack = subscribe(socket, f"book.{symbol}")
        assert ack["type"] == "subscribed"
        assert ack["channels"] == [f"book.{symbol}"]
        assert f"book.{symbol}" in ack["subscriptions"]

        books, _ = gather(
            socket, lambda f: f["type"] == "book", hint=f"a book for {symbol}"
        )
        assert books[0]["channel"] == f"book.{symbol}"
        assert books[0]["symbol"] == symbol

        socket.send_json({"op": "unsubscribe", "channels": [f"book.{symbol}"]})
        stopped, _ = gather(
            socket, lambda f: f["type"] == "unsubscribed", hint="an unsubscribe ack"
        )
        assert stopped[0]["subscriptions"] == []
        drain(socket)
        # Nothing after the market has had time to move the book again.
        time.sleep(0.3)
        after = drain(socket)
    assert [f for f in after if f["type"] == "book"] == []


def test_the_sequence_is_gapless(venue):
    """A client that sees 41 after 39 knows it lost one. That is the whole point."""
    symbols = venue.control("/control/state")["symbols"][:3]
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        frames = drain(socket)
        subscribe(socket, *[f"ticker.{s}" for s in symbols], sink=frames)
        subscribe(socket, *[f"trades.{s}" for s in symbols], sink=frames)
        for _ in range(4):
            time.sleep(0.15)
            frames.extend(drain(socket))
    numbers = [frame["seq"] for frame in frames]
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(numbers) > 10
    assert all(frame["channel"] is not None or frame["type"] != "ticker" for frame in frames)


# --------------------------------------------------------------------------
# Public channels
# --------------------------------------------------------------------------


def test_the_wildcard_follows_the_registry_across_every_asset_class(venue):
    """One path for every contract the venue lists, not one per class.

    The venue lists futures, event contracts, an index, a volatility contract, a
    spread, shares, options and a commodity. A feed that had learned any of
    them by name would stream some subset of that and quietly omit the rest.
    """
    state = venue.control("/control/state")
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        subscribe(socket, "ticker.*")
        _, seen = gather(
            socket,
            lambda f: f["type"] == "ticker",
            hint="tickers on the wildcard",
        )
        for _ in range(3):
            time.sleep(0.2)
            seen.extend(drain(socket))
    streamed = {frame["symbol"] for frame in seen if frame["type"] == "ticker"}
    assert streamed == set(state["symbols"])
    classes = {
        venue.runner.market.venue.registry.require(s).instrument_class for s in streamed
    }
    assert len(classes) >= 5
    labels = {frame["channel"] for frame in seen if frame["type"] == "ticker"}
    # Frames are labelled with the concrete channel, never with the wildcard a
    # client happened to ask by -- otherwise routing on the label is impossible.
    assert labels == {f"ticker.{s}" for s in streamed}


def test_a_ticker_carries_the_mark_the_touch_and_the_change(venue):
    symbol = tradeable(venue)
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        subscribe(socket, f"ticker.{symbol}")
        tickers, _ = gather(socket, lambda f: f["type"] == "ticker", hint="a ticker")
    first = tickers[0]
    assert Decimal(first["mark"]) > 0
    assert Decimal(first["change"]) == Decimal(first["mark"]) - Decimal(first["open"])
    assert first["session"]
    if first["bid"] is not None and first["ask"] is not None:
        assert Decimal(first["bid"]) <= Decimal(first["ask"])


def test_a_book_publishes_priced_levels_only(venue):
    """A market-on-open order rests at a sentinel and names no price at all.

    Publishing it put a bid of 4,611,686,018,427,387,904 on the dashboard. The
    ladder here is the filtered view, so the largest price on it stays inside
    what the contract could conceivably settle at.
    """
    symbol = tradeable(venue)
    instrument = venue.runner.market.venue.registry.require(symbol)
    low, high = instrument.value_bounds
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        subscribe(socket, f"book.{symbol}")
        books, _ = gather(socket, lambda f: f["type"] == "book", hint="a book")
    for book in books:
        for price, quantity in book["bids"] + book["asks"]:
            assert isinstance(price, str)
            assert low <= Decimal(price) <= high
            assert isinstance(quantity, int) and quantity > 0
        if book["bids"] and book["asks"]:
            assert Decimal(book["bids"][0][0]) >= Decimal(book["bids"][-1][0])
            assert Decimal(book["asks"][0][0]) <= Decimal(book["asks"][-1][0])


def test_public_channels_carry_no_account_data(venue):
    """A public subscription must never leak a private one.

    Checked by name over every value nested anywhere in a frame, and then again
    by looking for the account ids themselves: a field renamed on the server
    would slip past the first check and not the second.
    """
    state = venue.control("/control/state")
    symbols = state["symbols"][:4]
    channels = [f"{kind}.{s}" for s in symbols for kind in ("ticker", "book", "trades")]
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        subscribe(socket, *channels)
        _, seen = gather(socket, lambda f: f["type"] == "trade", hint="a print")
        for _ in range(3):
            time.sleep(0.2)
            seen.extend(drain(socket))

    public = [f for f in seen if f["type"] in ("ticker", "book", "trade")]
    assert len(public) > 10
    for frame in public:
        for key, _value in walk(frame):
            assert key not in PRIVATE_FIELDS, f"{key} leaked onto {frame['channel']}"
    blob = json.dumps(public)
    for agent_id in state["traders"]:
        assert agent_id not in blob


def test_no_price_anywhere_on_the_feed_is_a_float(venue):
    """Money and prices cross the wire as strings, everywhere, without exception.

    A float in a price path is a price path that eventually disagrees with the
    ledger, and the disagreement shows up as a rounding difference nobody can
    attribute. Asserted over every value in every frame rather than over a list
    of fields, so a new field cannot be added in the wrong type.
    """
    state = venue.control("/control/state")
    symbols = state["symbols"][:3]
    channels = [f"{kind}.{s}" for s in symbols for kind in ("ticker", "book", "trades")]
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        subscribe(socket, *channels)
        _, seen = gather(socket, lambda f: f["type"] == "trade", hint="a print")
        seen.extend(drain(socket))
    for frame in seen:
        for key, value in walk(frame):
            assert not isinstance(value, float), f"{key} is a float in {frame}"
    for frame in (f for f in seen if f["type"] == "ticker"):
        for field in ("mark", "open", "change", "bid", "ask", "last"):
            assert frame[field] is None or isinstance(frame[field], str)
            if frame[field] is not None:
                Decimal(frame[field])


def test_a_print_carries_the_exchange_sequence_not_the_order_ids(venue):
    symbol = tradeable(venue)
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        subscribe(socket, f"trades.{symbol}")
        prints, _ = gather(socket, lambda f: f["type"] == "trade", hint="a print")
    numbers = [frame["exchange_seq"] for frame in prints]
    assert numbers == sorted(numbers)
    for frame in prints:
        assert frame["side"] in ("buy", "sell")
        assert isinstance(frame["price"], str)
        assert frame["quantity"] > 0
        assert "buy_order_id" not in frame and "sell_order_id" not in frame


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_private_channel_without_a_signature_is_refused(venue):
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        for channel in ("orders", "fills"):
            refusal = subscribe(socket, channel)
            assert refusal["type"] == "error"
            assert refusal["error"]["code"] == "auth_required"
            assert refusal["channel"] == channel
        # And nothing arrives on them.
        time.sleep(0.3)
        seen = drain(socket)
    assert [f for f in seen if f["type"] in ("order", "fill")] == []


def test_one_bad_channel_refuses_the_whole_subscription(venue):
    """A partial subscribe is the worst of both: an error and a feed."""
    symbol = venue.control("/control/state")["symbols"][0]
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        refusal = subscribe(socket, f"ticker.{symbol}", "fills")
        assert refusal["type"] == "error"
        assert refusal["error"]["code"] == "auth_required"
        time.sleep(0.2)
        seen = drain(socket)
    assert [f for f in seen if f["type"] == "ticker"] == []


def test_unknown_ops_and_channels_are_typed_errors_and_not_disconnections(venue):
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        socket.send_json({"op": "levitate", "id": "one"})
        socket.send_json({"op": "subscribe", "channels": ["weather.LONDON"], "id": "two"})
        socket.send_json({"op": "subscribe", "channels": ["ticker.NOT_LISTED"], "id": "3"})
        socket.send_json({"op": "subscribe", "channels": ["fills.SOMETHING"], "id": "4"})
        socket.send_json({"op": "subscribe", "channels": "not-a-list", "id": "5"})
        socket.send_json({"op": "subscribe", "id": "6"})
        errors, _ = gather(
            socket,
            lambda f: f["type"] == "error",
            hint="typed errors",
        )
        # The connection is still usable afterwards, which is the point.
        pongs, _ = gather(socket, lambda f: f["type"] == "pong", hint="a pong")
        assert pongs

        codes = [frame["error"]["code"] for frame in errors]
        assert codes.count("invalid_request") >= 4
        assert "invalid_symbol" in codes
        for frame in errors:
            assert frame["error"]["message"]
            assert isinstance(frame["seq"], int)
    symbol_error = [f for f in errors if f["error"]["code"] == "invalid_symbol"][0]
    assert symbol_error["error"]["detail"]["symbol"] == "NOT_LISTED"


def test_a_forged_signature_is_refused_and_says_nothing_about_why(venue):
    """One message for every auth failure, as ``SignatureError`` insists.

    Saying which part was wrong tells a caller holding no valid key which key
    ids exist, and a caller holding a valid one never needs the difference.
    """
    key = venue.keys.issue(agent_id="you-does-not-exist", label="forged")
    stamp = str(time.time())
    attempts = [
        {"key_id": key.key_id, "timestamp": stamp, "signature": "0" * 64},
        {"key_id": "ak_nope", "timestamp": stamp, "signature": "0" * 64},
        {
            "key_id": key.key_id,
            "timestamp": str(time.time() - 600),
            "signature": sign(
                key.secret, "GET", STREAM_PATH, str(time.time() - 600), b""
            ),
        },
        {
            "key_id": key.key_id,
            "timestamp": stamp,
            # A valid signature over a different path. The path is part of what
            # is signed precisely so a signature cannot be lifted.
            "signature": sign(key.secret, "GET", "/v1/orders", stamp, b""),
        },
        {"key_id": key.key_id},
    ]
    messages = set()
    with venue.client.websocket_connect(STREAM_PATH) as socket:
        for index, attempt in enumerate(attempts):
            socket.send_json({"op": "auth", "id": f"a{index}", **attempt})
            reply = authenticate_reply(socket, f"a{index}")
            assert reply["type"] == "error", attempt
            assert reply["error"]["code"] == "auth_invalid"
            messages.add(reply["error"]["message"])
        # Still refused a private channel afterwards.
        assert subscribe(socket, "orders")["error"]["code"] == "auth_required"
    assert len(messages) == 1


def authenticate_reply(socket: Any, token: str) -> dict[str, Any]:
    matches, _ = gather(
        socket,
        lambda f: f.get("id") == token and f.get("type") in ("auth", "error"),
        hint=f"a reply to {token}",
    )
    return matches[0]


# --------------------------------------------------------------------------
# Private channels
# --------------------------------------------------------------------------


def test_a_signed_socket_streams_its_own_orders_and_fills(venue):
    """And lands on the account the application says the token holds.

    A key names a seat *token*, not an account, and the application resolves
    the token. If the stream resolved it any other way it would seat the
    credential a second time, and the client would place orders through the
    signed REST surface on one account while watching another account's fills
    -- which looks exactly like a feed that is simply not working.
    """
    seat = venue.control("/control/do/seat", name="blotter-test")
    key = venue.keys.issue(agent_id=seat["token"], label="blotter-test")
    symbol = tradeable(venue)

    with venue.client.websocket_connect(STREAM_PATH) as socket:
        granted = authenticate(venue, socket, key)
        assert granted["type"] == "auth"
        assert granted["seat"]["id"] == seat["id"]
        assert granted["seat"]["id"] == venue.control(
            "/control/whoami", token=seat["token"]
        )["id"]

        assert subscribe(socket, "orders", "fills")["type"] == "subscribed"
        venue.control(
            "/control/do/order", symbol=symbol, side="buy", quantity=1, agent=seat["id"]
        )

        # One sink for both, because a fill can arrive in the same read as the
        # acknowledgement and a helper that dropped what it read past would
        # lose it.
        seen: list[dict[str, Any]] = []
        gather(socket, lambda f: f["type"] == "order", hint="an ack", sink=seen)
        gather(socket, lambda f: f["type"] == "fill", hint="a fill", sink=seen)

    acks = [frame for frame in seen if frame["type"] == "order"]
    fills = [frame for frame in seen if frame["type"] == "fill"]
    assert acks[0]["channel"] == "orders"
    assert acks[0]["status"] == "ack", acks
    assert acks[0]["agent_id"] == seat["id"]
    assert acks[0]["symbol"] == symbol

    fill = fills[0]
    assert fill["channel"] == "fills"
    assert fill["agent_id"] == seat["id"]
    assert fill["symbol"] == symbol
    assert fill["side"] == "buy"
    assert isinstance(fill["price"], str)
    assert Decimal(fill["price"]) > 0
    assert isinstance(fill["exchange_seq"], int)


def test_two_seats_never_see_each_others_events(venue):
    """The failure this whole layer exists to prevent, stated directly."""
    mine = venue.control("/control/do/seat", name="mine")
    theirs = venue.control("/control/do/seat", name="theirs")
    key = venue.keys.issue(agent_id=mine["token"], label="mine")
    symbol = tradeable(venue)

    with venue.client.websocket_connect(STREAM_PATH) as socket:
        assert authenticate(venue, socket, key)["seat"]["id"] == mine["id"]
        subscribe(socket, "orders", "fills")
        venue.control(
            "/control/do/order", symbol=symbol, side="buy", quantity=2, agent=theirs["id"]
        )
        venue.control("/control/do/order", symbol=symbol, side="buy", quantity=2)
        venue.control(
            "/control/do/order", symbol=symbol, side="buy", quantity=1, agent=mine["id"]
        )
        _, seen = gather(socket, lambda f: f["type"] == "fill", hint="my own fill")

    state = venue.control("/control/state")
    # The other two really did trade, or this proves nothing.
    assert state["logs"][theirs["id"]] > 0
    assert state["logs"][state["shared"]] > 0
    private = [f for f in seen if f["type"] in ("order", "fill")]
    assert private
    assert {frame["agent_id"] for frame in private} == {mine["id"]}


# --------------------------------------------------------------------------
# Conflation, and a client that stops reading
# --------------------------------------------------------------------------


class StallableSocket:
    """A websocket that can be made to stop accepting writes.

    ``TestClient`` buffers without bound, so a test client that stops reading
    applies no backpressure and the interesting path is never taken. This one
    holds the server inside ``send_json`` until it is released, which is what a
    genuinely slow consumer does.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.gate = asyncio.Event()
        self.gate.set()
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.gate.wait()
        self.sent.append(payload)

    async def receive_json(self) -> Any:
        return await self.inbox.get()

    async def close(self, code: int = 1000) -> None:
        pass


def test_a_stalled_client_neither_stops_the_market_nor_loses_a_print():
    """Conflation, not dropping -- the failure that deadlocked this market.

    An earlier feed discarded an update when a subscriber was behind. A maker
    that has not moved its quote sends no order, so nothing republishes, and
    every agent waiting for a price waited forever: a trial that traded 2,039
    times traded 0. So this asserts both halves. The market keeps stepping
    while the connection is stuck mid-write, and when the connection comes back
    every print that happened while it was stuck is delivered, in order and
    exactly once -- while the book, which is state rather than event, is
    conflated to the latest rather than replayed.
    """
    runner = MarketRunner(
        MarketConfig(seed=11, speed=3.0, makers=2, flow_traders=2, opening_auction=False)
    )
    runner.start()
    socket = StallableSocket()
    endpoint = stream_endpoint(runner=runner, keys=KeyStore(), tick_seconds=0.005)

    async def step_market(ticks: int) -> None:
        for _ in range(ticks):
            runner.step()
            await asyncio.sleep(0.01)

    async def scenario() -> dict[str, Any]:
        served = asyncio.create_task(endpoint(socket))
        await step_market(4)
        symbols = [
            s
            for s in runner.market.venue.registry.symbols
            if runner.market.venue.session(s) is SessionState.CONTINUOUS
        ][:3]
        await socket.inbox.put(
            {
                "op": "subscribe",
                "channels": [f"trades.{s}" for s in symbols]
                + [f"book.{s}" for s in symbols],
            }
        )
        await step_market(6)

        socket.gate.clear()  # the client stops reading, mid-write
        stalled_from = {
            s: len(runner.market.venue.engine(s).tape) for s in symbols
        }
        frames_before = len(socket.sent)
        await step_market(25)
        stalled_to = {s: len(runner.market.venue.engine(s).tape) for s in symbols}

        socket.gate.set()  # and comes back
        await step_market(6)
        # One quiet moment with the market still, so the last flush has run and
        # the newest book on the wire is the book that is actually standing.
        await asyncio.sleep(0.1)
        result = {
            "symbols": symbols,
            "frames_before": frames_before,
            "traded_while_stalled": {
                s: stalled_to[s] - stalled_from[s] for s in symbols
            },
            "tapes": {
                s: [int(t.sequence) for t in runner.market.venue.engine(s).tape]
                for s in symbols
            },
        }
        served.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await served
        return result

    outcome = asyncio.run(scenario())
    symbols = outcome["symbols"]

    # The market ran while the connection was stuck inside a write.
    assert sum(outcome["traded_while_stalled"].values()) > 0
    assert len(socket.sent) > outcome["frames_before"]

    # Every print, exactly once, in order, and contiguous with the venue's tape.
    for symbol in symbols:
        streamed = [
            frame["exchange_seq"]
            for frame in socket.sent
            if frame["type"] == "trade" and frame["symbol"] == symbol
        ]
        assert streamed == sorted(streamed)
        assert len(streamed) == len(set(streamed))
        tape = outcome["tapes"][symbol]
        if streamed:
            start = tape.index(streamed[0])
            assert streamed == tape[start : start + len(streamed)], symbol

    # State was conflated rather than replayed: a book channel published far
    # fewer frames than the number of flushes that went past it, and the last
    # one it published is the book as it stands now.
    for symbol in symbols:
        books = [f for f in socket.sent if f["type"] == "book" and f["symbol"] == symbol]
        assert books
        snapshot = runner.market.venue.engine(symbol).book.snapshot(10)
        instrument = runner.market.venue.registry.require(symbol)
        assert books[-1]["bids"] == [
            [str(instrument.from_ticks(p)), int(q)] for p, q in snapshot.priced_bids
        ]
    trades = len([f for f in socket.sent if f["type"] == "trade"])
    books = len([f for f in socket.sent if f["type"] == "book"])
    assert trades > books

    numbers = [frame["seq"] for frame in socket.sent]
    assert numbers == list(range(1, len(numbers) + 1))


# --------------------------------------------------------------------------
# A rebuild, which discards every account in the market
# --------------------------------------------------------------------------


def test_a_rebuild_reseats_the_stream_instead_of_following_a_reused_id(venue):
    """The bug this is built against, reproduced and then refused.

    ``reconfigure`` discards the market and every account in it, and the next
    caller to seat gets ``you-1`` -- the id this connection was holding a
    moment ago. So the decoy below is deliberately seated first: a stream that
    had captured its account id once would now be reading the decoy's blotter,
    and a stream that fell back to ``LiveMarket.trader`` would be reading the
    shared account that every unseated caller shares. Both are silent; both
    accounts exist and have a blotter.
    """
    seat = venue.control("/control/do/seat", name="survivor")
    key = venue.keys.issue(agent_id=seat["token"], label="survivor")
    symbol = tradeable(venue)

    with venue.client.websocket_connect(STREAM_PATH) as socket:
        assert authenticate(venue, socket, key)["seat"]["id"] == seat["id"]
        subscribe(socket, "orders", "fills")

        rebuilt = venue.control("/control/do/rebuild", decoys="squatter")
        squatter = rebuilt["decoys"]["squatter"]

        resets, _ = gather(socket, lambda f: f["type"] == "reset", hint="a reset")
        reset = resets[-1]
        assert reset["reason"] == "rebuild"
        assert reset["generation"] == rebuilt["generation"]

        state = venue.control("/control/state")
        seated = reset["seat"]["id"]
        assert seated != squatter
        assert seated != state["shared"]
        assert seated != seat["id"]
        assert seated in state["traders"]
        assert state["traders"][seated] == "survivor"
        # And it is the same account the rest of the application now says this
        # token holds, so the two halves of the API have not drifted apart.
        assert seated == venue.control("/control/whoami", token=seat["token"])["id"]

        # The decoy that inherited the old id, and the shared account, both
        # trade. Neither may appear on this connection.
        symbol = tradeable(venue)
        venue.control(
            "/control/do/order", symbol=symbol, side="buy", quantity=2, agent=squatter
        )
        venue.control("/control/do/order", symbol=symbol, side="sell", quantity=2)
        quiet: list[dict[str, Any]] = []
        after = until(
            venue,
            lambda s: s["logs"].get(squatter, 0) > 0 and s["logs"][s["shared"]] > 0,
            hint="the decoy and the shared account to trade",
            drains=(socket, quiet),
        )
        assert after["logs"][squatter] > 0
        assert after["logs"][after["shared"]] > 0
        quiet.extend(drain(socket))
        assert [f for f in quiet if f["type"] in ("order", "fill")] == []

        # Its own order still reaches it, on the new account.
        venue.control(
            "/control/do/order", symbol=symbol, side="buy", quantity=1, agent=seated
        )
        mine, _ = gather(socket, lambda f: f["type"] == "order", hint="my own ack")
        assert {frame["agent_id"] for frame in mine} == {seated}


def test_with_no_application_resolver_the_stream_reseats_by_name():
    """The same guarantee when nobody has told the stream where anyone sits.

    An application that does not answer for a token -- or does not supply a
    resolver at all -- leaves the stream to bind the credential itself, and it
    must still not follow a reused id into somebody else's account. So the same
    trap: rebuild, let a decoy take the id this connection held, and check that
    the connection is seated afresh under its own name.
    """
    runner = MarketRunner(
        MarketConfig(seed=5, speed=1.0, makers=2, flow_traders=1, opening_auction=False)
    )
    runner.start()
    keys = KeyStore()
    key = keys.issue(agent_id="a-token-no-application-knows", label="survivor")
    socket = StallableSocket()
    endpoint = stream_endpoint(runner=runner, keys=keys, tick_seconds=0.005)

    async def step_market(ticks: int) -> None:
        for _ in range(ticks):
            runner.step()
            await asyncio.sleep(0.01)

    async def scenario() -> dict[str, Any]:
        served = asyncio.create_task(endpoint(socket))
        stamp = str(time.time())
        await socket.inbox.put(
            {
                "op": "auth",
                "key_id": key.key_id,
                "timestamp": stamp,
                "signature": sign(key.secret, "GET", STREAM_PATH, stamp, b""),
            }
        )
        await step_market(4)

        runner.reconfigure(runner.config)
        squatter = str(runner.market.seat("squatter"))
        await step_market(6)

        served.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await served
        return {
            "squatter": squatter,
            "shared": str(runner.market.human.agent_id),
            "names": {str(k): v.display_name for k, v in runner.market.traders.items()},
        }

    outcome = asyncio.run(scenario())
    granted = [f for f in socket.sent if f["type"] == "auth"]
    resets = [f for f in socket.sent if f["type"] == "reset"]
    assert granted and resets

    before = granted[0]["seat"]["id"]
    after = resets[-1]["seat"]["id"]
    assert resets[-1]["reason"] == "rebuild"
    assert after != before
    assert after != outcome["squatter"]
    assert after != outcome["shared"]
    assert outcome["names"][after] == "survivor"
    assert int(runner.market.venue.conservation_check()) == 0


def test_the_ledger_still_balances_exactly(venue):
    """Seating a stream's account moves no value, and the check is integer zero.

    The sharpest check the portfolio layer has, run after this file has opened
    a dozen accounts and traded through several of them. Anything other than
    exactly zero means the streaming layer created or destroyed value by
    existing, which is the one thing it must not be able to do.
    """
    assert venue.control("/control/state")["conservation"] == 0
    assert int(venue.runner.market.venue.conservation_check()) == 0
