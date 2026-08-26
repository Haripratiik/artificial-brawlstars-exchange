"""Call auctions, trading sessions, and the circuit breaker.

The clearing rule is a sequence of tie-breaks, and each one exists because the
one before it can leave more than one answer. Testing only the first would pass
on an implementation that resolves ties arbitrarily -- and an arbitrary opening
price is exactly the failure an auction is meant to prevent, because it is the
price indices and settlements are struck at. So each tie-break gets a case
constructed to reach it and no further.

The other thing checked hard here is that auction fills go through the *same*
account path as continuous ones. An auction settling through its own accounting
would be the ideal place for a collateral or fee leak to hide.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from arena.contracts.payoff import Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Single
from arena.exchange.engine import MatchingEngine
from arena.exchange.events import Filled, Rejected, Submit, Traded
from arena.exchange.session import SessionState, indicative_auction
from arena.exchange.types import (
    AgentId,
    OrderType,
    Price,
    Quantity,
    RejectReason,
    Side,
    TimeInForce,
)
from arena.market.fees import MAKER_TAKER
from arena.market.instrument import Instrument
from arena.market.venue import FEE_ACCOUNT_ID, Venue
from arena.worlds.brawl.metrics import metric_ref

UTC = timezone.utc
START = datetime(2026, 8, 31, tzinfo=UTC)


def _book(orders, phase: SessionState = SessionState.PRE_OPEN) -> MatchingEngine:
    """Build a resting book from ``(agent, side, price_or_None, quantity)``."""
    engine = MatchingEngine("X")
    engine.phase = phase
    for who, side, price, quantity in orders:
        engine.apply(
            Submit(
                AgentId(who),
                side,
                Quantity(quantity),
                None if price is None else Price(price),
                OrderType.MARKET if price is None else OrderType.LIMIT,
                TimeInForce.IOC if price is None else TimeInForce.GTC,
            )
        )
    return engine


B, S = Side.BUY, Side.SELL


# --------------------------------------------------------------------------
# The clearing rule, tie-break by tie-break
# --------------------------------------------------------------------------


def test_the_auction_clears_where_most_volume_trades():
    """Rule one, and the only one most books ever reach."""
    engine = _book([("a", B, 105, 100), ("b", S, 95, 100), ("c", S, 104, 10)])
    result = indicative_auction(engine.book)
    assert result is not None
    assert result.volume == 100


def test_a_hand_computed_book_clears_where_it_should():
    """Worked through by hand, so the test is not the implementation restated.

    bids 102x50, 100x80, market x20   asks 99x60, 101x40

      p=99  demand 150 supply  60 -> volume  60, imbalance  +90
      p=100 demand 150 supply  60 -> volume  60, imbalance  +90
      p=101 demand  70 supply 100 -> volume  70, imbalance  -30
      p=102 demand  70 supply 100 -> volume  70, imbalance  -30

    Maximum volume is 70 at {101, 102}; both leave the same surplus; the surplus
    is on the sell side at both, so the lowest wins.
    """
    engine = _book(
        [
            ("a", B, 102, 50),
            ("b", B, 100, 80),
            ("m", B, None, 20),
            ("c", S, 99, 60),
            ("d", S, 101, 40),
        ]
    )
    result = indicative_auction(engine.book)
    assert (result.price, result.volume, result.imbalance) == (101, 70, -30)
    assert result.surplus_side is Side.SELL


def test_ties_on_volume_are_broken_by_the_smaller_surplus():
    """Rule two: among equal volumes, leave least unfilled."""
    engine = _book(
        [("a", B, 100, 100), ("b", B, 101, 100), ("c", S, 99, 100), ("d", S, 100, 5)]
    )
    result = indicative_auction(engine.book)
    surpluses = {}
    for price in (99, 100, 101):
        demand = sum(q for p, q in
                     ((100, 100), (101, 100)) if p >= price)
        supply = sum(q for p, q in ((99, 100), (100, 5)) if p <= price)
        surpluses[price] = (min(demand, supply), abs(demand - supply))
    best = max(surpluses.values())[0]
    assert result.volume == best
    assert abs(result.imbalance) == min(
        v[1] for v in surpluses.values() if v[0] == best
    )


def test_a_buy_surplus_pushes_the_price_up_and_a_sell_surplus_down():
    """Rule three, in both directions. A surplus means the price is wrong."""
    buy_heavy = _book([("a", B, 100, 200), ("b", B, 110, 200), ("c", S, 90, 50)])
    sell_heavy = _book([("a", B, 110, 50), ("c", S, 90, 200), ("d", S, 100, 200)])
    up = indicative_auction(buy_heavy.book)
    down = indicative_auction(sell_heavy.book)
    assert up.surplus_side is Side.BUY
    assert down.surplus_side is Side.SELL
    # Buyers unfilled everywhere -> the highest tied price; sellers -> the lowest.
    assert up.price == 110
    assert down.price == 90


def test_a_balanced_range_falls_back_to_the_reference_price():
    """Rule four. Any price in the range clears the same volume, so the previous
    price is the least arbitrary choice available."""
    engine = _book([("a", B, 110, 100), ("c", S, 90, 100)])
    low = indicative_auction(engine.book, reference=Price(92))
    high = indicative_auction(engine.book, reference=Price(108))
    assert low.imbalance == high.imbalance == 0
    assert low.price < high.price


def test_an_uncrossed_book_has_no_auction():
    engine = _book([("a", B, 90, 100), ("c", S, 110, 100)])
    assert indicative_auction(engine.book) is None


def test_a_one_sided_book_has_no_auction():
    assert indicative_auction(_book([("a", B, 100, 50)]).book) is None
    assert indicative_auction(_book([("c", S, 100, 50)]).book) is None


def test_market_orders_alone_clear_only_against_a_reference():
    """Two market orders imply no price at all, so there is nothing to clear at."""
    engine = _book([("a", B, None, 50), ("c", S, None, 50)])
    assert indicative_auction(engine.book) is None
    priced = indicative_auction(engine.book, reference=Price(100))
    assert priced is not None and priced.price == 100


def test_the_indicative_price_changes_nothing():
    """It is published during the call phase, so it must be a pure read."""
    engine = _book([("a", B, 102, 50), ("c", S, 99, 60)])
    before = engine.book.total_resting_quantity
    indicative_auction(engine.book)
    indicative_auction(engine.book)
    assert engine.book.total_resting_quantity == before
    assert engine.tape == ()


# --------------------------------------------------------------------------
# The call phase
# --------------------------------------------------------------------------


def test_a_crossed_book_does_not_trade_during_the_call():
    """The whole point: orders accumulate, however crossed they are."""
    engine = _book([("a", B, 200, 100), ("c", S, 50, 100)])
    assert engine.tape == ()
    assert engine.book.total_resting_quantity == 200


def test_immediate_orders_are_refused_during_a_call():
    """There is no 'immediately' in a call phase, so the instruction is refused
    rather than quietly turned into a resting order."""
    engine = MatchingEngine("X")
    engine.phase = SessionState.PRE_OPEN
    for tif in (TimeInForce.IOC, TimeInForce.FOK):
        events = engine.apply(
            Submit(AgentId("a"), B, Quantity(10), Price(100), OrderType.LIMIT, tif)
        )
        rejected = next(e for e in events if isinstance(e, Rejected))
        assert rejected.reason is RejectReason.NOT_ACCEPTED_IN_AUCTION
    assert engine.book.total_resting_quantity == 0


def test_a_market_order_becomes_a_market_on_open_order():
    """Required to be IOC in continuous trading, but nothing matches here."""
    engine = _book([("m", B, None, 25)])
    assert engine.book.total_resting_quantity == 25


def test_everyone_trades_at_one_price_and_the_eager_are_improved():
    """A buyer willing to pay 120 pays the clearing price, not its own limit.

    Price improvement is the reward for being in the auction, and it is why a
    cleared price is trustworthy where a first-arrival price is not.
    """
    engine = _book([("a", B, 120, 50), ("c", S, 100, 50)])
    engine.uncross()
    prices = {int(t.price) for t in engine.tape}
    assert len(prices) == 1
    assert prices.pop() < 120


def test_the_uncross_trades_exactly_the_indicative_volume():
    engine = _book(
        [("a", B, 102, 50), ("b", B, 100, 80), ("c", S, 99, 60), ("d", S, 101, 40)]
    )
    expected = indicative_auction(engine.book).volume
    engine.uncross()
    assert sum(int(t.quantity) for t in engine.tape) == expected


def test_an_auction_never_prints_a_wash_trade():
    """Worse here than in continuous trading: it would be struck at the official
    price and could move a settlement."""
    engine = _book([("same", B, 110, 100), ("same", S, 90, 100)])
    engine.uncross()
    assert engine.tape == ()


def test_nothing_trades_when_the_book_is_uncrossed():
    engine = _book([("a", B, 90, 50), ("c", S, 110, 50)])
    assert engine.uncross() == []
    assert engine.tape == ()


# --------------------------------------------------------------------------
# Sessions on the venue
# --------------------------------------------------------------------------


def _instrument() -> Instrument:
    return Instrument(
        "F",
        ContractSpec(
            contract_id="F",
            underlying=Single(metric_ref("adjusted_win_rate", "SUBJECT")),
            payoff=Linear(10_000.0),
            window=ObservationWindow(START, START + timedelta(days=28)),
            policy=DataPolicy(
                min_sample_size=1_000, min_stratum_battles=200, min_strata_coverage=0.80
            ),
            reference_id="ref-2026S09-v1",
            published_at=START - timedelta(days=1),
            tick_size="0.25",
        ),
    )


def _venue(**kwargs) -> Venue:
    venue = Venue("arena", starting_cash=80_000_000, **kwargs)
    venue.list_instrument(_instrument())
    return venue


def _send(venue, who, side, price, quantity, tif=TimeInForce.GTC):
    return venue.submit(
        AgentId(who),
        "F",
        Submit(
            AgentId(who),
            side,
            Quantity(quantity),
            None if price is None else Price(price),
            OrderType.MARKET if price is None else OrderType.LIMIT,
            tif,
        ),
    )


def test_a_listed_symbol_opens_continuous():
    assert _venue().session("F") is SessionState.CONTINUOUS


def test_an_opening_auction_sets_the_first_price():
    venue = _venue()
    venue.begin_session("F")
    _send(venue, "a", B, 18_800, 100)
    _send(venue, "c", S, 18_600, 100)
    assert venue.engine("F").tape == ()

    result = venue.uncross("F")
    assert result is not None and result.volume == 100
    assert venue.session("F") is SessionState.CONTINUOUS
    assert len(venue.engine("F").tape) == 1


def test_auction_fills_land_in_accounts_and_conserve_exactly():
    """The same account path as continuous trading, so nothing can diverge."""
    venue = _venue()
    venue.begin_session("F")
    _send(venue, "a", B, 18_800, 120)
    _send(venue, "c", S, 18_600, 200)
    venue.uncross("F")
    assert venue.account(AgentId("a")).positions["F"].quantity == 120
    assert venue.account(AgentId("c")).positions["F"].quantity == -120
    assert venue.conservation_check() == 0


def test_an_auction_charges_both_sides_the_maker_rate():
    """Nobody crossed a spread, so nobody is a taker. Venues that run auctions
    charge them this way, and the fee path must agree with the fill path."""
    venue = _venue(fees=MAKER_TAKER)
    venue.begin_session("F")
    _send(venue, "a", B, 18_800, 100)
    _send(venue, "c", S, 18_600, 100)
    venue.uncross("F")
    # Both sides rebated means the venue paid out, so its balance falls.
    assert int(venue.fees_collected) < 0
    assert venue.conservation_check() == 0


def test_a_halt_accumulates_orders_and_reopens_with_an_auction():
    venue = _venue()
    _send(venue, "a", B, 18_600, 50)
    venue.halt("F", reason="news")
    assert venue.session("F") is SessionState.AUCTION

    _send(venue, "b", B, 18_900, 100)
    _send(venue, "c", S, 18_500, 100)
    assert venue.engine("F").tape == (), "a halted book must not trade"

    venue.uncross("F")
    assert venue.session("F") is SessionState.CONTINUOUS
    assert len(venue.engine("F").tape) >= 1
    assert venue.conservation_check() == 0


def test_the_circuit_breaker_trips_on_a_print_outside_the_band():
    venue = _venue(price_band=0.02)
    _send(venue, "c", S, 18_000, 50)
    _send(venue, "t", B, None, 50, tif=TimeInForce.IOC)
    assert venue.session("F") is SessionState.CONTINUOUS, "the first print sets the reference"

    _send(venue, "c", S, 25_000, 50)
    _send(venue, "t", B, None, 50, tif=TimeInForce.IOC)
    assert venue.session("F") is SessionState.AUCTION
    assert venue.halts[-1]["reason"] == "price_band"


def test_the_breaker_stays_quiet_inside_the_band():
    venue = _venue(price_band=0.50)
    for price in (18_000, 18_500, 19_000):
        _send(venue, "c", S, price, 20)
        _send(venue, "t", B, None, 20, tif=TimeInForce.IOC)
    assert venue.session("F") is SessionState.CONTINUOUS
    assert venue.halts == []


def test_no_breaker_means_no_halts_however_far_price_moves():
    venue = _venue()
    for price in (18_000, 40_000, 1_000):
        _send(venue, "c", S, price, 20)
        _send(venue, "t", B, None, 20, tif=TimeInForce.IOC)
    assert venue.session("F") is SessionState.CONTINUOUS
    assert venue.halts == []


def test_a_closed_symbol_refuses_new_orders():
    venue = _venue()
    venue.close("F")
    assert venue.session("F") is SessionState.CLOSED
    events = _send(venue, "a", B, 18_600, 10)
    rejected = next(e for e in events if isinstance(e, Rejected))
    assert rejected.reason is RejectReason.ALREADY_TERMINAL


def test_halting_a_closed_symbol_does_nothing():
    """The outcome is already determined; there is nothing left to protect."""
    venue = _venue()
    venue.close("F")
    venue.halt("F")
    assert venue.session("F") is SessionState.CLOSED


def test_a_full_session_conserves_value_through_every_phase():
    """Open, trade, halt, reopen, close -- one ledger throughout."""
    venue = _venue(fees=MAKER_TAKER, price_band=0.05)
    venue.begin_session("F")
    rng = random.Random(12)
    for _ in range(30):
        _send(venue, "a", B, rng.randint(18_400, 18_700), rng.randint(1, 30))
        _send(venue, "c", S, rng.randint(18_500, 18_800), rng.randint(1, 30))
    venue.uncross("F")
    assert venue.conservation_check() == 0

    for _ in range(60):
        _send(venue, "a", B, rng.randint(18_400, 18_700), rng.randint(1, 20))
        _send(venue, "c", S, rng.randint(18_500, 18_800), rng.randint(1, 20))
        if venue.session("F") is SessionState.AUCTION:
            venue.uncross("F")
        assert venue.conservation_check() == 0

    venue.halt("F", reason="close")
    venue.uncross("F")
    venue.close("F")
    assert venue.conservation_check() == 0
