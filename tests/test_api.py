"""The programmatic surface, exercised the way a trading client would drive it.

Everything here runs against a real market -- a real kernel, real agents, real
latency -- because the claims worth testing about this API are claims about a
market. A mocked venue would pass every one of them and prove nothing: that an
order reaches the book, that it appears in the account that sent it and in no
other, that value is still conserved afterwards, and that a credential issued
before a rebuild still trades its own seat after one.

The app is built here rather than imported from ``dashboard.server``. The
router is meant to be mountable by anything, and a test that could only reach it
through one particular application would not be testing that.

Time is driven by the test, not by a wall clock. ``LiveMarket.step`` advances
simulated time in proportion to elapsed real time, which makes a test's runtime
decide how much market it gets -- so these drive ``Kernel.advance`` directly and
ask for an exact number of simulated milliseconds. Slower machines then run the
same market rather than a shorter one.
"""

from __future__ import annotations

import itertools
import json
import time
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from arena.api import rest
from arena.api.keys import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KeyStore,
    body_bytes,
    sign,
)
from arena.market.instrument import InstrumentClass
from arena.sim.time import Timestamp
from dashboard.state import MarketConfig, MarketRunner

# The nine classes the venue lists. Read off ``InstrumentClass`` rather than
# typed out, so a tenth added later fails this file instead of quietly escaping
# it -- which is the whole point of a test that says "every asset class".
ALL_CLASSES = sorted(
    value
    for name, value in vars(InstrumentClass).items()
    if not name.startswith("_") and isinstance(value, str)
)

# Distinct browser sessions per test. A seat token is supposed to be stable and
# unique per person, and the router remembers names against tokens for the
# lifetime of the process, so reusing one string across tests would have two
# tests sharing a seat -- which is the exact confusion these tests exist to
# rule out.
_TOKENS = itertools.count()


def _token(prefix: str) -> str:
    return f"{prefix}-{next(_TOKENS)}"


# --------------------------------------------------------------------------
# The harness
# --------------------------------------------------------------------------


class Exchange:
    """A market, a router mounted over it, and a client that can sign.

    Bundled rather than passed around as five fixtures because they are one
    thing: the router is configured against this runner and this key store, and
    a test holding a client for one and a runner for another would be testing
    an arrangement that cannot exist.
    """

    def __init__(self, config: MarketConfig | None = None, **hooks) -> None:
        self.runner = MarketRunner(config or MarketConfig(opening_auction=False))
        self.runner.start()
        self.keys = KeyStore()
        self.seats: dict[str, rest.Seat] = {}

        def browser_seat(request) -> rest.Seat | None:
            """Who is at the browser, as this test app decides it.

            The dashboard reads a signed cookie. Nothing about the router
            depends on that, so the test app uses a header instead -- the point
            of the hook is that the application, not this module, says how a
            browser session is recognised.
            """
            token = request.headers.get("x-test-session", "")
            return self.seats.get(token)

        app = FastAPI()
        app.include_router(rest.router)
        self._hooks = {
            "browser_seat": hooks.get("browser_seat", browser_seat),
            # Passed explicitly, and as a function rather than as ``None``,
            # because :func:`rest.configure` deliberately ignores a missing
            # argument: a test that left this set from the previous test would
            # be running against a delegation it never asked for.
            "seat_now": hooks.get("seat_now", lambda token: None),
        }
        self._client = TestClient(app)

    @property
    def client(self) -> TestClient:
        """The HTTP client, with the router pointed at *this* market first.

        The router's configuration is module state, because the application
        that mounts it has exactly one market. A test file has several, so
        every request re-asserts which one it belongs to rather than relying on
        whichever test ran last -- otherwise the order the tests happen to run
        in decides which exchange a request reaches, which is a test suite that
        passes for the wrong reason.
        """
        rest.configure(keys=self.keys, runner=self.runner, **self._hooks)
        return self._client

    # -- time ------------------------------------------------------------

    def pump(self, milliseconds: int = 250, slices: int = 10) -> None:
        """Advance the market by an exact amount of simulated time.

        Sliced, and each slice followed by ``runner.step``, so the history
        buffer the chart endpoint reads gets samples at distinct timestamps
        rather than one repeated one. ``runner.step`` advances almost nothing
        on its own here: it scales elapsed wall clock, and no wall clock has
        elapsed.
        """
        step = int(milliseconds * 1_000_000 / slices)
        for _ in range(slices):
            target = Timestamp(int(self.runner.market.kernel.now) + step)
            self.runner.market.kernel.advance(until=target, max_events=500_000)
            self.runner.step()

    # -- identity --------------------------------------------------------

    def browser(self, name: str = "Ash") -> str:
        """Seat a browser session and return the header value that names it."""
        token = _token("sid")
        self.seats[token] = rest.Seat(token=token, name=name)
        return token

    def issue(self, token: str, label: str = "algo") -> dict:
        response = self.client.post(
            "/v1/keys", json={"label": label}, headers={"x-test-session": token}
        )
        assert response.status_code == 201, response.text
        return response.json()

    def trader(self, name: str = "Ash", label: str = "algo") -> "Client":
        """A browser session, a key minted from it, and a signing client."""
        token = self.browser(name)
        return Client(self, self.issue(token, label), token)

    # -- reading ---------------------------------------------------------

    @property
    def venue(self):
        return self.runner.market.venue

    def symbols(self) -> tuple[str, ...]:
        return self.venue.registry.symbols

    def symbol_of(self, instrument_class: str) -> str:
        for symbol in self.symbols():
            if self.venue.registry.require(symbol).instrument_class == instrument_class:
                return symbol
        raise AssertionError(f"nothing listed in class {instrument_class}")

    def close(self) -> None:
        self._client.close()


class Client:
    """A signing client, in the shape a client library would take.

    Written out here rather than imported so the test proves the documented
    scheme -- timestamp, method, path with query, raw body -- and not whatever
    some helper happens to do.
    """

    def __init__(self, exchange: Exchange, key: dict, token: str = "") -> None:
        self.exchange = exchange
        self.key_id = key["key_id"]
        self.secret = key["secret"]
        self.account_id = key["account_id"]
        self.token = token

    def headers(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        timestamp: str | None = None,
        secret: str | None = None,
    ) -> dict[str, str]:
        stamp = timestamp if timestamp is not None else f"{time.time():.3f}"
        query = path.partition("?")[2]
        signed = rest.signed_path(path.partition("?")[0], query)
        return {
            HEADER_KEY: self.key_id,
            HEADER_TIMESTAMP: stamp,
            HEADER_SIGNATURE: sign(secret or self.secret, method, signed, stamp, body),
        }

    def request(self, method: str, path: str, payload=None, **overrides):
        body = body_bytes(payload) if payload is not None else b""
        headers = self.headers(method, path, body, **overrides)
        if payload is None:
            return self.exchange.client.request(method, path, headers=headers)
        headers["content-type"] = "application/json"
        return self.exchange.client.request(method, path, content=body, headers=headers)

    def get(self, path: str, **overrides):
        return self.request("GET", path, None, **overrides)

    def post(self, path: str, payload, **overrides):
        return self.request("POST", path, payload, **overrides)

    def delete(self, path: str, **overrides):
        return self.request("DELETE", path, None, **overrides)


@pytest.fixture(scope="module")
def exchange():
    """One market for the read-only tests, warmed until it has traded.

    Module-scoped because building and warming a market costs more than the
    tests that read from it, and none of them mutate it in a way another can
    see: each one signs in as its own seat.
    """
    venue = Exchange()
    venue.pump(600, slices=24)
    yield venue
    venue.close()


@pytest.fixture
def fresh():
    """A market of this test's own, for anything that writes to one."""
    made: list[Exchange] = []

    def build(config: MarketConfig | None = None, **hooks) -> Exchange:
        venue = Exchange(config, **hooks)
        made.append(venue)
        return venue

    yield build
    for venue in made:
        venue.close()


def resting_price(exchange: Exchange, symbol: str, side: str = "buy") -> Decimal:
    """A price that will rest rather than trade, on the instrument's own grid.

    Derived from the book and the tick table rather than chosen, because a
    constant would be a price on one contract and off the grid of another --
    the contracts here are listed on different increments, and one of them
    carries a tick *table* whose increment changes with the level.
    """
    instrument = exchange.venue.registry.require(symbol)
    snapshot = exchange.venue.engine(symbol).book.snapshot(4)
    mark = exchange.venue.mark_price(symbol)
    if side == "buy":
        reference = (
            instrument.from_ticks(snapshot.priced_bids[0][0])
            if snapshot.priced_bids
            else mark
        )
        step = -1
    else:
        reference = (
            instrument.from_ticks(snapshot.priced_asks[0][0])
            if snapshot.priced_asks
            else mark
        )
        step = 1
    low, high = instrument.value_bounds
    for away in range(1, 400):
        increment = instrument.increment_at(reference)
        candidate = reference + step * away * increment
        if not low <= candidate <= high:
            break
        if instrument.on_grid(candidate):
            return candidate
    raise AssertionError(f"no restable price found for {symbol}")


# --------------------------------------------------------------------------
# Public: the exchange describes itself
# --------------------------------------------------------------------------


def test_the_exchange_endpoint_answers_without_a_credential(exchange):
    """Reference data is public. A client should be able to see what is listed
    before it decides whether to ask for a key."""
    payload = exchange.client.get("/v1/exchange").json()
    assert payload["counts"]["instruments"] == len(exchange.symbols())
    assert payload["clock"] > 0
    assert payload["generation"] == 0
    assert set(payload["session"]) >= {"phases", "fees", "price_band", "message_rate"}
    assert sum(payload["session"]["phases"].values()) == len(exchange.symbols())


def test_the_exchange_publishes_conservation_as_an_exact_integer(exchange):
    """Not as a price. The claim is that it is *exactly* zero, and a rounded
    zero would be a different and much weaker claim."""
    conservation = exchange.client.get("/v1/exchange").json()["conservation"]
    assert int(conservation) == 0
    assert "." not in conservation


def test_every_asset_class_is_quotable_through_the_instruments_endpoint(exchange):
    """The venue is uniform over its classes and so is this API.

    Nothing in the router branches on a class, so this is really a test that
    nothing has quietly started to: each of the nine has to arrive with a tick,
    bounds, an expiry, a session and a mark, through the same code path.
    """
    rows = exchange.client.get("/v1/instruments?limit=1000").json()["instruments"]
    listed = {row["class"] for row in rows}
    assert listed == set(ALL_CLASSES), f"missing {set(ALL_CLASSES) - listed}"
    for row in rows:
        assert Decimal(row["tick"]) > 0
        low, high = (Decimal(bound) for bound in row["bounds"])
        assert low <= high
        assert row["expiry"].endswith("Z")
        assert row["session"]
        assert Decimal(row["mark"]) >= 0
        assert row["subjects"]


def test_a_price_from_the_instruments_endpoint_is_never_a_json_number(exchange):
    """Prices cross the wire as strings, in both directions. A JSON number is a
    double, and this venue's accounting is exact integers precisely so that the
    conservation check can be exactly zero."""
    raw = json.loads(exchange.client.get("/v1/instruments?limit=1000").text)
    for row in raw["instruments"]:
        for money in ("tick", "mark", "bid", "ask"):
            assert row[money] is None or isinstance(row[money], str), (row["symbol"], money)


@pytest.mark.parametrize("instrument_class", ALL_CLASSES)
def test_the_class_filter_selects_exactly_that_class(exchange, instrument_class):
    payload = exchange.client.get(
        f"/v1/instruments?class={instrument_class}&limit=1000"
    ).json()
    assert payload["instruments"], f"nothing listed in {instrument_class}"
    assert {row["class"] for row in payload["instruments"]} == {instrument_class}
    assert payload["filters"]["class"] == instrument_class


def test_the_subject_filter_reaches_every_leg_of_a_multi_leg_contract(exchange):
    """A spread is written on two subjects and an index on a basket of them.

    Filtering by subject has to find a contract by any leg, or the filter means
    something different for a future than for the two classes that are not
    written on one thing -- which is exactly the special-casing this venue is
    arranged to avoid.
    """
    rows = exchange.client.get("/v1/instruments?limit=1000").json()["instruments"]
    multi = [row for row in rows if len(row["subjects"]) > 1]
    assert multi, "no multi-leg contract listed"
    for row in multi:
        for subject in row["subjects"]:
            found = exchange.client.get(
                f"/v1/instruments?subject={subject}&limit=1000"
            ).json()["instruments"]
            assert row["symbol"] in {hit["symbol"] for hit in found}


def test_an_unknown_symbol_is_refused_with_a_catalogued_code(exchange):
    for path in (
        "/v1/instruments/NOPE",
        "/v1/instruments/NOPE/book",
        "/v1/instruments/NOPE/trades",
        "/v1/instruments/NOPE/history",
    ):
        response = exchange.client.get(path)
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == "invalid_symbol"
        assert "detail" not in response.json(), "a bare FastAPI detail escaped"


def test_one_instrument_carries_the_contract_it_settles_by(exchange):
    """A market where you cannot read the contract is a casino with extra steps."""
    symbol = exchange.symbols()[0]
    payload = exchange.client.get(f"/v1/instruments/{symbol}").json()
    assert payload["symbol"] == symbol
    assert payload["contract"]["payoff"]
    assert payload["contract"]["underlying"]
    assert payload["contract"]["window"]["start"] < payload["contract"]["window"]["end"]


def test_the_book_is_two_sided_and_bounded(exchange):
    symbol = exchange.symbols()[0]
    payload = exchange.client.get(f"/v1/instruments/{symbol}/book?depth=5").json()
    assert payload["depth"] == 5
    assert payload["cap"] == rest.BOOK_DEPTH_CAP
    assert len(payload["bids"]) <= 5 and len(payload["asks"]) <= 5
    for price, quantity in payload["bids"] + payload["asks"]:
        assert isinstance(price, str)
        assert quantity > 0
    # Descending on the bid, ascending on the ask: a ladder is only readable if
    # the best price is the first one.
    bids = [Decimal(price) for price, _ in payload["bids"]]
    asks = [Decimal(price) for price, _ in payload["asks"]]
    assert bids == sorted(bids, reverse=True)
    assert asks == sorted(asks)


def test_a_book_deeper_than_the_cap_is_clamped_not_refused(exchange):
    """A client that does not know the cap is served the cap. A client that asks
    for zero levels has a bug, and an empty list would hide it."""
    symbol = exchange.symbols()[0]
    clamped = exchange.client.get(f"/v1/instruments/{symbol}/book?depth=99999").json()
    assert clamped["depth"] == rest.BOOK_DEPTH_CAP

    refused = exchange.client.get(f"/v1/instruments/{symbol}/book?depth=0")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_request"

    nonsense = exchange.client.get(f"/v1/instruments/{symbol}/book?depth=deep")
    assert nonsense.status_code == 400
    assert nonsense.json()["error"]["code"] == "invalid_request"


def test_the_tape_is_capped_and_most_recent_first(exchange):
    symbol = max(
        exchange.symbols(), key=lambda s: len(exchange.venue.engine(s).tape)
    )
    payload = exchange.client.get(f"/v1/instruments/{symbol}/trades?limit=5").json()
    assert payload["limit"] == 5
    assert payload["cap"] == rest.TRADES_CAP
    assert payload["count"] <= 5
    if payload["count"] > 1:
        sequences = [trade["sequence"] for trade in payload["trades"]]
        assert sequences == sorted(sequences, reverse=True)
    for trade in payload["trades"]:
        assert isinstance(trade["price"], str)
        assert trade["aggressor_side"] in ("buy", "sell")


def test_history_publishes_the_sampled_path_as_strings(exchange):
    """The runner keeps this as floats because a chart is a float. It leaves
    here as a string anyway: a client should not have to know which endpoints
    are exact."""
    symbol = exchange.symbols()[0]
    payload = exchange.client.get(f"/v1/instruments/{symbol}/history?limit=50").json()
    assert payload["count"] == len(payload["t"]) == len(payload["mid"])
    assert payload["count"] > 0
    assert payload["cap"] >= payload["count"]
    for mid in payload["mid"]:
        assert mid is None or isinstance(mid, str)
        if mid is not None:
            Decimal(mid)  # exact, or this raises


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_a_key_is_issued_to_a_browser_session_and_shows_its_secret_once(fresh):
    venue = fresh()
    token = venue.browser("Ash")
    issued = venue.issue(token, label="my bot")

    assert issued["secret"]
    assert issued["label"] == "my bot"
    assert issued["account_id"].startswith("you-")

    listed = venue.client.get("/v1/keys", headers={"x-test-session": token}).json()
    assert [row["key_id"] for row in listed["keys"]] == [issued["key_id"]]
    assert all("secret" not in row for row in listed["keys"])
    # The seat token is the value the owner's own browser session is identified
    # by; there is no reason for it to come back out of the key list.
    assert all(token not in json.dumps(row) for row in listed["keys"])


def test_key_management_refuses_a_caller_with_no_browser_session(fresh):
    venue = fresh()
    for method, path in (("POST", "/v1/keys"), ("GET", "/v1/keys"), ("DELETE", "/v1/keys/x")):
        response = venue.client.request(method, path, json={})
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "auth_required"


def test_a_key_belonging_to_another_session_cannot_be_revoked(fresh):
    """And answers exactly as a key that never existed does, so this endpoint
    cannot be used to discover which key ids are real."""
    venue = fresh()
    mine = venue.browser("Ash")
    theirs = venue.browser("Nita")
    victim = venue.issue(theirs)

    refused = venue.client.delete(
        f"/v1/keys/{victim['key_id']}", headers={"x-test-session": mine}
    )
    invented = venue.client.delete(
        "/v1/keys/ak_0000000000000000", headers={"x-test-session": mine}
    )
    assert refused.status_code == invented.status_code == 404
    assert refused.json() == invented.json()


def test_revoking_a_key_is_idempotent_and_stops_it_trading(fresh):
    venue = fresh()
    token = venue.browser("Ash")
    trader = Client(venue, venue.issue(token), token)

    assert trader.get("/v1/account").status_code == 200

    first = venue.client.delete(
        f"/v1/keys/{trader.key_id}", headers={"x-test-session": token}
    )
    second = venue.client.delete(
        f"/v1/keys/{trader.key_id}", headers={"x-test-session": token}
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["already_done"] is False
    assert second.json()["already_done"] is True

    refused = trader.get("/v1/account")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "auth_invalid"


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------


def test_a_signed_request_succeeds(exchange):
    trader = exchange.trader("Signed")
    response = trader.get("/v1/account")
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == trader.account_id
    assert Decimal(payload["cash"]) > 0
    assert Decimal(payload["equity"]) == Decimal(payload["starting_cash"])


def test_an_unsigned_request_is_refused(exchange):
    for method, path in (
        ("GET", "/v1/account"),
        ("GET", "/v1/account/positions"),
        ("GET", "/v1/account/fills"),
        ("GET", "/v1/orders"),
        ("POST", "/v1/orders"),
        ("GET", "/v1/orders/X/1"),
        ("DELETE", "/v1/orders/X/1"),
        ("DELETE", "/v1/orders"),
    ):
        response = exchange.client.request(method, path, json={})
        assert response.status_code == 401, path
        body = response.json()
        assert body["error"]["code"] == "auth_required", path
        assert HEADER_SIGNATURE in body["error"]["detail"]["headers"]


def test_a_stale_signature_is_refused(exchange):
    """Thirty seconds is the window ``keys.py`` documents. A signature is
    replayable only inside it, which is the point of signing a timestamp."""
    trader = exchange.trader("Stale")
    old = f"{time.time() - 120:.3f}"
    response = trader.get("/v1/account", timestamp=old)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_invalid"


def test_a_signature_cannot_be_moved_onto_another_request(exchange):
    """The signature covers the method, the path *with its query string*, and
    the body. Each of those is checked by lifting a valid signature onto a
    request that differs in exactly one of them."""
    trader = exchange.trader("Tamper")
    symbol = exchange.symbols()[0]
    body = body_bytes({"symbol": symbol, "side": "buy", "quantity": 1})

    honest = trader.headers("POST", "/v1/orders", body)

    # Same signature, different body: a captured order for one lot replayed as
    # an order for a thousand.
    swapped = body_bytes({"symbol": symbol, "side": "buy", "quantity": 1000})
    response = exchange.client.post("/v1/orders", content=swapped, headers=honest)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_invalid"

    # Same signature, different path.
    moved = exchange.client.request(
        "DELETE", "/v1/orders", content=body, headers=honest
    )
    assert moved.status_code == 401

    # Same signature, different query string on the same path.
    listed = trader.headers("GET", "/v1/orders?limit=1")
    shifted = exchange.client.get("/v1/orders?limit=1000", headers=listed)
    assert shifted.status_code == 401
    assert shifted.json()["error"]["code"] == "auth_invalid"


def test_a_signature_from_the_wrong_secret_is_refused(exchange):
    trader = exchange.trader("Wrong")
    response = trader.get("/v1/account", secret="not the secret")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_invalid"


def test_an_unknown_key_and_a_bad_signature_are_indistinguishable(exchange):
    """One message for every authentication failure, exactly as
    ``SignatureError`` does it. Saying which one went wrong tells a caller
    holding no valid key which key ids exist."""
    trader = exchange.trader("Opaque")
    bad_secret = trader.get("/v1/account", secret="wrong")

    stamp = f"{time.time():.3f}"
    unknown = exchange.client.get(
        "/v1/account",
        headers={
            HEADER_KEY: "ak_0000000000000000",
            HEADER_TIMESTAMP: stamp,
            HEADER_SIGNATURE: sign("anything", "GET", "/v1/account", stamp),
        },
    )
    assert bad_secret.status_code == unknown.status_code == 401
    assert bad_secret.json() == unknown.json()


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


def test_an_order_reaches_the_book_and_appears_in_positions(fresh):
    """The whole point of routing through ``LiveMarket``: the order enters the
    same event queue, at the same latency, and is settled by the same ledger."""
    venue = fresh()
    venue.pump(400, slices=16)
    trader = venue.trader("Buyer")
    symbol = venue.symbols()[0]

    placed = trader.post(
        "/v1/orders",
        {"symbol": symbol, "side": "buy", "quantity": 2, "client_order_id": "cid-1"},
    )
    assert placed.status_code == 202, placed.text
    assert placed.json()["client_order_id"] == "cid-1"
    assert placed.json()["status"] == "accepted"

    venue.pump(300, slices=12)

    positions = trader.get("/v1/account/positions").json()["positions"]
    held = {row["symbol"]: row for row in positions}
    assert symbol in held, positions
    assert held[symbol]["quantity"] == 2

    fills = trader.get("/v1/account/fills").json()
    assert fills["fills"], "a market order that did not fill"
    assert fills["fills"][0]["symbol"] == symbol
    assert isinstance(fills["fills"][0]["price"], str)

    account = trader.get("/v1/account").json()
    assert Decimal(account["collateral"]) > 0


def test_a_resting_order_is_reconcilable_by_its_client_order_id(fresh):
    """The client chooses one identifier and the exchange chooses another.
    Nothing carries the first into the engine, so the join is made in the API
    -- and if it were not, a client could not tell which of its orders is which."""
    venue = fresh()
    venue.pump(400, slices=16)
    trader = venue.trader("Resting")
    symbol = venue.symbols()[0]
    price = resting_price(venue, symbol, "buy")

    placed = trader.post(
        "/v1/orders",
        {
            "symbol": symbol,
            "side": "buy",
            "quantity": 3,
            "price": str(price),
            "type": "limit",
            "time_in_force": "gtc",
            "client_order_id": "cid-rest",
        },
    )
    assert placed.status_code == 202, placed.text

    # Before the acknowledgement has crossed back, the order is in neither the
    # book nor the working list -- and the client is told so rather than left
    # to guess, because "not placed" and "not there yet" differ by a duplicate.
    in_flight = trader.get("/v1/orders").json()
    assert "cid-rest" in {row["client_order_id"] for row in in_flight["pending"]}

    venue.pump(300, slices=12)

    working = trader.get("/v1/orders").json()
    mine = [row for row in working["orders"] if row["client_order_id"] == "cid-rest"]
    assert mine, working
    order = mine[0]
    assert order["symbol"] == symbol
    assert order["side"] == "buy"
    assert Decimal(order["price"]) == price
    assert order["remaining"] == 3
    assert working["cap"] == rest.ORDERS_CAP

    detail = trader.get(f"/v1/orders/{symbol}/{order['order_id']}").json()
    assert detail["order_id"] == order["order_id"]
    assert detail["client_order_id"] == "cid-rest"


def test_a_reused_client_order_id_is_refused_rather_than_replayed(fresh):
    """Replaying it would mean answering "accepted" for an order this call did
    not place, and a client retrying a timed-out POST cannot tell that answer
    from the truth."""
    venue = fresh()
    venue.pump(200, slices=8)
    trader = venue.trader("Duplicate")
    symbol = venue.symbols()[0]
    order = {"symbol": symbol, "side": "buy", "quantity": 1, "client_order_id": "same"}

    assert trader.post("/v1/orders", order).status_code == 202
    repeat = trader.post("/v1/orders", order)
    assert repeat.status_code == 400
    assert repeat.json()["error"]["code"] == "invalid_request"


def test_cancelling_is_idempotent(fresh):
    """The decision, and the reason, are in ``cancel_order``'s docstring: the
    client wanted that order not to be resting and it is not resting, so a
    refusal would make a correct outcome look like an error."""
    venue = fresh()
    venue.pump(400, slices=16)
    trader = venue.trader("Canceller")
    symbol = venue.symbols()[0]
    price = resting_price(venue, symbol, "buy")

    trader.post(
        "/v1/orders",
        {"symbol": symbol, "side": "buy", "quantity": 2, "price": str(price)},
    )
    venue.pump(300, slices=12)

    working = trader.get("/v1/orders").json()["orders"]
    assert working, "nothing resting to cancel"
    order_id = working[0]["order_id"]

    first = trader.delete(f"/v1/orders/{symbol}/{order_id}")
    assert first.status_code == 200
    assert first.json()["already_done"] is False

    venue.pump(200, slices=8)

    second = trader.delete(f"/v1/orders/{symbol}/{order_id}")
    assert second.status_code == 200
    assert second.json()["already_done"] is True

    # An id that never existed, and one belonging to nobody, answer the same
    # way -- which is what makes this endpoint disclose nothing.
    never = trader.delete(f"/v1/orders/{symbol}/999999")
    assert never.status_code == 200
    assert never.json()["already_done"] is True

    # A typo in the *symbol* is still a refusal. That is not a race, and
    # answering it with success would let a client believe it had cancelled
    # something in a market that does not exist.
    mistyped = trader.delete("/v1/orders/NOT_A_SYMBOL/1")
    assert mistyped.status_code == 400
    assert mistyped.json()["error"]["code"] == "invalid_symbol"


def test_cancel_all_pulls_every_working_order_and_leaves_positions_alone(fresh):
    venue = fresh()
    venue.pump(400, slices=16)
    trader = venue.trader("Flattener")
    symbols = venue.symbols()[:3]

    for symbol in symbols:
        trader.post(
            "/v1/orders",
            {
                "symbol": symbol,
                "side": "buy",
                "quantity": 1,
                "price": str(resting_price(venue, symbol, "buy")),
            },
        )
    venue.pump(400, slices=16)

    before = trader.get("/v1/orders").json()
    assert before["count"] >= 1

    cleared = trader.delete("/v1/orders")
    assert cleared.status_code == 200
    assert cleared.json()["count"] == before["count"]

    venue.pump(400, slices=16)
    after = trader.get("/v1/orders").json()
    assert after["count"] == 0, after


def test_one_account_never_sees_another_account(fresh):
    """Two keys are two traders. Sharing one blotter is the failure the seat
    binding exists to prevent, seen from the ordinary direction."""
    venue = fresh()
    venue.pump(400, slices=16)
    one = venue.trader("Ash")
    two = venue.trader("Nita")
    symbol = venue.symbols()[0]

    assert one.account_id != two.account_id

    one.post(
        "/v1/orders",
        {
            "symbol": symbol,
            "side": "buy",
            "quantity": 4,
            "price": str(resting_price(venue, symbol, "buy")),
        },
    )
    venue.pump(400, slices=16)

    assert one.get("/v1/orders").json()["count"] >= 1
    assert two.get("/v1/orders").json()["count"] == 0

    mine = one.get("/v1/orders").json()["orders"][0]
    # The other account cannot read it, and cannot cancel it either. Both
    # answer as though it did not exist.
    assert two.get(f"/v1/orders/{symbol}/{mine['order_id']}").status_code == 404
    assert two.delete(f"/v1/orders/{symbol}/{mine['order_id']}").json()["already_done"]

    venue.pump(300, slices=12)
    assert one.get("/v1/orders").json()["count"] >= 1


# --------------------------------------------------------------------------
# Orders the venue should not have to see
# --------------------------------------------------------------------------


def _order(symbol: str, **fields) -> dict:
    return {"symbol": symbol, "side": "buy", "quantity": 1, **fields}


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"side": "byu"}, "invalid_side"),
        ({"side": None}, "invalid_side"),
        ({"quantity": 0}, "invalid_quantity"),
        ({"quantity": -3}, "invalid_quantity"),
        ({"quantity": 1.5}, "invalid_quantity"),
        ({"quantity": "many"}, "invalid_quantity"),
        ({"quantity": True}, "invalid_quantity"),
        ({"quantity": None}, "invalid_quantity"),
        ({"price": "9,233.75"}, "invalid_price"),
        ({"price": "-100"}, "invalid_price"),
        ({"price": "1e9"}, "invalid_price"),
        ({"time_in_force": "eventually"}, "invalid_time_in_force"),
        ({"type": "pegged", "price": "1"}, "invalid_order_type"),
        ({"type": "nonsense"}, "invalid_order_type"),
        ({"display": -1}, "invalid_quantity"),
    ],
)
def test_a_malformed_order_is_refused_in_its_own_terms(exchange, fields, code):
    """Every one of these is a fact about the request, so the request is told.
    What is left to the venue -- collateral, the price band, an auction phase --
    is only knowable there."""
    trader = exchange.trader("Malformed")
    symbol = exchange.symbols()[0]
    response = trader.post("/v1/orders", _order(symbol, **fields))
    assert response.status_code in (400, 422), response.text
    assert response.json()["error"]["code"] == code, response.text


def test_a_price_sent_as_a_json_number_is_refused(exchange):
    """``json.loads('{"price": 4700.10}')`` yields a binary double that is not
    4700.10. Accepting it and rounding is how an order rests at a price nobody
    chose, which is the argument ``Instrument.to_ticks`` already makes."""
    trader = exchange.trader("Floaty")
    symbol = exchange.symbols()[0]
    response = trader.post("/v1/orders", _order(symbol, price=4700.10))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_price"
    assert "string" in response.json()["error"]["message"]


def test_a_price_off_the_instruments_grid_is_refused(exchange):
    """Per instrument, from the instrument's own increment. Nothing here knows
    what any contract's tick is."""
    trader = exchange.trader("Offgrid")
    for symbol in exchange.symbols():
        instrument = exchange.venue.registry.require(symbol)
        mark = exchange.venue.mark_price(symbol)
        off = mark + instrument.increment_at(mark) / 3
        if instrument.on_grid(off):
            continue
        response = trader.post("/v1/orders", _order(symbol, price=str(off)))
        assert response.status_code == 400, symbol
        assert response.json()["error"]["code"] == "invalid_price", symbol
        return
    pytest.skip("no instrument with a divisible increment")


def test_an_order_type_that_disagrees_with_its_fields_is_refused(exchange):
    """A declared type is checked against the fields rather than obeyed, so a
    client whose declaration and whose fields disagree is told which -- instead
    of one of them silently winning."""
    trader = exchange.trader("Mismatch")
    symbol = exchange.symbols()[0]
    response = trader.post("/v1/orders", _order(symbol, type="limit"))
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_order_type"
    assert body["detail"] == {"declared": "limit", "derived": "market"}


def test_an_off_lot_quantity_is_refused_before_it_costs_a_round_trip(fresh):
    """The venue refuses it asynchronously, as a ``RejectReason`` in a blotter
    the client has to poll for. Measured on a contract listed in lots of ten,
    an order for seven was acknowledged and rested."""
    venue = fresh()
    trader = venue.trader("Lots")
    symbol = venue.symbols()[0]
    instrument = venue.venue.registry.require(symbol)
    # The listing is in lots of one here, so the rule is exercised by changing
    # the listing rather than by finding a contract that happens to break it --
    # there is no special symbol to reach for and there should not be.
    object.__setattr__(instrument, "lot_size", 10)
    try:
        response = trader.post("/v1/orders", _order(symbol, quantity=7))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_quantity"
        assert trader.post("/v1/orders", _order(symbol, quantity=20)).status_code == 202
    finally:
        object.__setattr__(instrument, "lot_size", 1)


def test_a_body_that_is_not_json_is_refused_as_a_bad_request(exchange):
    trader = exchange.trader("Garbage")
    stamp = f"{time.time():.3f}"
    body = b"not json at all"
    response = exchange.client.post(
        "/v1/orders",
        content=body,
        headers={
            HEADER_KEY: trader.key_id,
            HEADER_TIMESTAMP: stamp,
            HEADER_SIGNATURE: sign(trader.secret, "POST", "/v1/orders", stamp, body),
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------
# The rate limit the venue already has, said out loud
# --------------------------------------------------------------------------


def test_the_venues_message_rate_surfaces_as_a_429(fresh):
    """The venue throttles on the far side of a latency link, so a client that
    overruns it gets 202 for every order and then silence -- the refusals land
    in a blotter it has to poll for and correlate. The rate is the venue's own;
    nothing here invents a number."""
    venue = fresh()
    venue.pump(200, slices=8)
    venue.venue.message_rate = 5
    trader = venue.trader("Firehose")
    symbol = venue.symbols()[0]

    accepted = 0
    refusal = None
    for _ in range(12):
        response = trader.post("/v1/orders", _order(symbol, quantity=1))
        if response.status_code == 202:
            accepted += 1
        else:
            refusal = response
            break

    assert accepted == 5, f"accepted {accepted} against a rate of 5"
    assert refusal is not None and refusal.status_code == 429
    body = refusal.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["detail"]["limit"] == 5

    # A cancel is counted and never refused, exactly as the venue treats a
    # reducing command: a participant that cannot withdraw is one holding
    # exposure nobody is permitted to manage.
    assert trader.delete(f"/v1/orders/{symbol}/1").status_code == 200
    assert trader.delete("/v1/orders").status_code == 200


def test_the_rate_limit_is_per_seat_not_per_venue(fresh):
    venue = fresh()
    venue.pump(200, slices=8)
    venue.venue.message_rate = 2
    loud = venue.trader("Loud")
    quiet = venue.trader("Quiet")
    symbol = venue.symbols()[0]

    for _ in range(3):
        loud.post("/v1/orders", _order(symbol))
    assert loud.post("/v1/orders", _order(symbol)).status_code == 429
    assert quiet.post("/v1/orders", _order(symbol)).status_code == 202


def test_no_configured_rate_means_no_throttle(fresh):
    """Copied from the venue rather than invented, so the two cannot disagree
    about what is allowed. The dashboard's venue configures none."""
    venue = fresh()
    venue.pump(200, slices=8)
    assert venue.venue.message_rate is None
    trader = venue.trader("Unlimited")
    symbol = venue.symbols()[0]
    for _ in range(30):
        assert trader.post("/v1/orders", _order(symbol)).status_code == 202


# --------------------------------------------------------------------------
# The trap: a key is bound to a seat, not to an account id
# --------------------------------------------------------------------------


def test_a_key_still_trades_its_own_account_after_a_rebuild(fresh):
    """The failure this module is arranged around.

    ``reconfigure`` discards the market and every account in it, and
    ``LiveMarket.trader`` answers an id it has never heard of with the *shared*
    account. A key that had captured ``you-1`` at issue time would therefore
    trade a communal seat after the first rebuild -- one balance, one blotter,
    every stale credential able to cancel every other's orders. That is the
    same bug the browser cookie was fixed for, and this asserts the fix rather
    than the intention: the two keys must land in two different accounts, and
    neither of them in the shared one.
    """
    venue = fresh()
    venue.pump(200, slices=8)
    one = venue.trader("Ash")
    two = venue.trader("Nita")
    before = {one.account_id, two.account_id}
    assert len(before) == 2

    shared = str(venue.runner.market.human.agent_id)
    assert shared not in before

    venue.runner.reconfigure(MarketConfig(seed=11, opening_auction=False))
    venue.pump(300, slices=12)
    assert venue.runner.generation == 1

    after_one = one.get("/v1/account").json()
    after_two = two.get("/v1/account").json()
    assert after_one["generation"] == after_two["generation"] == 1
    assert after_one["account_id"] != after_two["account_id"]
    assert after_one["account_id"] != shared
    assert after_two["account_id"] != shared
    assert after_one["seat"] == "Ash" and after_two["seat"] == "Nita"

    # And they trade separately. One buys; the other's blotter stays empty.
    symbol = venue.symbols()[0]
    assert one.post("/v1/orders", _order(symbol, quantity=2)).status_code == 202
    venue.pump(400, slices=16)

    mine = one.get("/v1/account/positions").json()["positions"]
    theirs = two.get("/v1/account/positions").json()["positions"]
    assert any(row["quantity"] for row in mine), mine
    assert theirs == []
    assert Decimal(two.get("/v1/account").json()["cash"]) == Decimal(
        two.get("/v1/account").json()["starting_cash"]
    )


def test_a_rebuild_does_not_leak_a_new_account_per_request(fresh):
    """Re-seating on *every* request would be the same bug with the opposite
    sign: a key would get a fresh empty account each call and never find its
    own position again. The binding is re-resolved per request and re-seated
    only when the market underneath it has changed."""
    venue = fresh()
    trader = venue.trader("Steady")
    first = trader.get("/v1/account").json()["account_id"]
    seats = len(venue.runner.market.traders)
    for _ in range(5):
        assert trader.get("/v1/account").json()["account_id"] == first
    assert len(venue.runner.market.traders) == seats

    venue.runner.reconfigure(MarketConfig(seed=3, opening_auction=False))
    after = trader.get("/v1/account").json()["account_id"]
    seats_after = len(venue.runner.market.traders)
    for _ in range(5):
        assert trader.get("/v1/account").json()["account_id"] == after
    assert len(venue.runner.market.traders) == seats_after


def test_the_application_can_own_the_re_seating_instead(fresh):
    """``dashboard/server.py`` already resolves a session id to the account it
    holds now, from the same table its cookies use. When the app supplies that,
    a key and the browser session that minted it stay in one account across a
    rebuild rather than being seated separately into two."""
    routed: dict[str, object] = {}

    def seat_now(token: str):
        return routed.get(token)

    venue = fresh(seat_now=seat_now)
    token = venue.browser("Delegated")
    # The application seats this person itself, exactly as the dashboard does
    # when the page is first loaded.
    routed[token] = venue.runner.market.seat("Delegated")
    trader = Client(venue, venue.issue(token), token)

    assert trader.get("/v1/account").json()["account_id"] == str(routed[token])

    venue.runner.reconfigure(MarketConfig(seed=5, opening_auction=False))
    routed[token] = venue.runner.market.seat("Delegated")
    assert trader.get("/v1/account").json()["account_id"] == str(routed[token])


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------


def test_value_is_conserved_exactly_through_the_api(fresh):
    """Zero, as an integer, after everything this API can do to a market.

    The sharpest check available on the whole portfolio layer, and the reason
    the ledger is kept in integers: trading moves value between participants,
    it does not create it. A near-zero would mean an accounting leak that the
    money layer's own docstring says compounds per fill.
    """
    venue = fresh()
    venue.pump(400, slices=16)
    trader = venue.trader("Conserver")
    other = venue.trader("Counterparty")

    for symbol in venue.symbols()[:6]:
        trader.post("/v1/orders", _order(symbol, quantity=2))
        trader.post(
            "/v1/orders",
            _order(
                symbol,
                quantity=3,
                price=str(resting_price(venue, symbol, "buy")),
                time_in_force="gtc",
            ),
        )
        other.post(
            "/v1/orders",
            _order(
                symbol,
                side="sell",
                quantity=2,
                price=str(resting_price(venue, symbol, "sell")),
                time_in_force="gtc",
            ),
        )
        venue.pump(200, slices=8)
        assert int(venue.venue.conservation_check()) == 0

    trader.delete("/v1/orders")
    other.delete("/v1/orders")
    venue.pump(600, slices=24)

    assert int(venue.venue.conservation_check()) == 0
    assert int(venue.client.get("/v1/exchange").json()["conservation"]) == 0


def test_every_asset_class_takes_an_order_through_the_api(fresh):
    """The venue is uniform over its classes, so the API is too. Nine classes,
    one code path, no branch anywhere on what kind of claim it is."""
    venue = fresh()
    venue.pump(400, slices=16)
    trader = venue.trader("Everything")

    for instrument_class in ALL_CLASSES:
        symbol = venue.symbol_of(instrument_class)
        response = trader.post(
            "/v1/orders",
            _order(
                symbol,
                quantity=1,
                price=str(resting_price(venue, symbol, "buy")),
                time_in_force="gtc",
                client_order_id=f"cid-{instrument_class}",
            ),
        )
        assert response.status_code == 202, (instrument_class, response.text)

    venue.pump(500, slices=20)
    working = trader.get("/v1/orders?limit=1000").json()
    classes = {
        venue.venue.registry.require(row["symbol"]).instrument_class
        for row in working["orders"]
    }
    assert classes == set(ALL_CLASSES), f"never rested: {set(ALL_CLASSES) - classes}"
    assert int(venue.venue.conservation_check()) == 0
