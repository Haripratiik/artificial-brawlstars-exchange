"""Message rate limits and the participant kill switch.

Both controls answer the same question -- what does a venue do when one
participant stops behaving like a participant -- and they answer it in different
registers.

A rate limit is the routine one, and it is not politeness. An algorithm that
malfunctions emits orders faster than anything downstream can process them, and
the venue with no limit is the one that goes down with it. It is also the only
defence against a participant that discovers it can profit by simply sending
more messages than everyone else. The window is a rolling second rather than a
fixed one, and that is the whole difficulty of the feature: a fixed window that
resets on a boundary lets a participant send its entire allowance twice in a few
milliseconds by straddling one.

A kill switch is the other one, reached for when a participant is doing
something nobody wants to reason about at the time. It is deliberately blunt,
because the point of a kill switch is that it is the one control that always
works: everything working is pulled and everything new is refused. Cancels are
the single exception, and that is not a softening of the rule. Refusing those
too would leave the participant trapped in the orders it already has -- unable
to place, unable to withdraw, holding exposure nobody is permitted to manage.

Neither control may create or destroy a penny, so every case here ends at the
ledger.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from arena.contracts.payoff import Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Single
from arena.exchange.events import (
    Acknowledged,
    Cancel,
    Cancelled,
    Rejected,
    Replace,
    Submit,
)
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
from arena.market.venue import Venue
from arena.worlds.brawl.metrics import metric_ref

UTC = timezone.utc
START = datetime(2026, 8, 31, tzinfo=UTC)

B, S = Side.BUY, Side.SELL

ONE_SECOND_NS = 1_000_000_000


def _instrument(symbol: str = "F") -> Instrument:
    return Instrument(
        symbol,
        ContractSpec(
            contract_id=symbol,
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


def _send(venue, who, side, price, quantity, tif=TimeInForce.GTC, symbol="F"):
    return venue.submit(
        AgentId(who),
        symbol,
        Submit(
            AgentId(who),
            side,
            Quantity(quantity),
            None if price is None else Price(price),
            OrderType.MARKET if price is None else OrderType.LIMIT,
            tif,
        ),
    )


def _reasons(events) -> list[RejectReason]:
    return [e.reason for e in events if isinstance(e, Rejected)]


def _reason(events) -> RejectReason:
    """The one reason a command was refused, so a test fails loudly if it was not."""
    refusals = _reasons(events)
    assert len(refusals) == 1, f"expected exactly one refusal, got {refusals}"
    return refusals[0]


def _order_id(events):
    return next(e.order_id for e in events if isinstance(e, Acknowledged))


def _resting(venue, symbol, who) -> list[int]:
    """What this agent has left standing in a book, in arrival order."""
    return [
        int(order.remaining)
        for order in venue.engine(symbol).book.resting_orders
        if order.agent_id == AgentId(who)
    ]


def _limited_venue(rate: int, clock: dict) -> Venue:
    """A venue whose clock the test moves by hand.

    ``sim_clock`` defaults to None, which means simulated time never advances --
    right for a venue being poked rather than run, and useless for a rolling
    window, which measures nothing at all if the clock is stuck.
    """
    venue = _venue(message_rate=rate)
    venue.sim_clock = lambda: clock["now"]
    return venue


# --------------------------------------------------------------------------
# The message rate limit
# --------------------------------------------------------------------------


def test_a_participant_is_refused_only_once_it_is_over_its_message_cap():
    """A cap that bites early throttles traffic the venue agreed to take; one
    that never bites is not a cap. The refused order also has to stay out of the
    book -- a refusal that quietly rested anyway would be worse than no limit,
    because the participant would be told no and get the order regardless.
    """
    clock = {"now": 0}
    venue = _limited_venue(5, clock)

    for i in range(5):
        assert _reasons(_send(venue, "a", B, 18_600 - i, 1)) == []

    assert _reason(_send(venue, "a", B, 18_700, 1)) is RejectReason.RATE_LIMITED
    assert venue.engine("F").book.total_resting_quantity == 5


def test_the_message_window_rolls_rather_than_resetting_on_a_boundary():
    """A fixed window is the obvious implementation and it gives away double the
    allowance.

    Spend it all just before the reset and all of it again just after, and the
    venue has taken twice the traffic it agreed to, delivered inside the few
    hundred milliseconds either side of one boundary -- which is precisely the
    burst the limit exists to stop. A rolling window still sees the first half,
    so the second one is refused.
    """
    clock = {"now": 0}
    venue = _limited_venue(4, clock)

    clock["now"] = 900_000_000
    for i in range(4):
        assert _reasons(_send(venue, "a", B, 18_600 - i, 1)) == []

    clock["now"] = 1_100_000_000
    for i in range(4):
        assert _reason(_send(venue, "a", B, 18_500 - i, 1)) is RejectReason.RATE_LIMITED


def test_a_quiet_second_restores_the_whole_allowance():
    """A limit that never let go would turn one momentary burst into permanent
    exclusion from the market, which is a far heavier penalty than the venue ever
    agreed to impose.
    """
    clock = {"now": 0}
    venue = _limited_venue(2, clock)
    for i in range(2):
        _send(venue, "a", B, 18_600 - i, 1)

    clock["now"] = ONE_SECOND_NS - 1_000_000
    assert _reason(_send(venue, "a", B, 18_550, 1)) is RejectReason.RATE_LIMITED

    clock["now"] = ONE_SECOND_NS + 1
    for i in range(2):
        assert _reasons(_send(venue, "a", B, 18_500 - i, 1)) == []


def test_being_refused_does_not_lengthen_a_lockout():
    """A refused message is refused, not remembered.

    Counting refusals against the allowance would mean a client that retries --
    which is what an automated client does when it is told no -- keeps its own
    lockout alive by trying to get out of it. How long a burst costs would then
    depend on how quickly the offender gave up, which is the wrong thing for it
    to depend on.
    """
    clock = {"now": 0}
    venue = _limited_venue(2, clock)
    for i in range(2):
        _send(venue, "a", B, 18_600 - i, 1)

    clock["now"] = 500_000_000
    for i in range(20):
        events = _send(venue, "a", B, 18_550 - i, 1)
        assert _reason(events) is RejectReason.RATE_LIMITED

    clock["now"] = ONE_SECOND_NS + 1
    assert _reasons(_send(venue, "a", B, 18_400, 1)) == []


def test_a_venue_with_no_message_rate_never_limits_anything():
    """The default, so every measurement taken before the limit existed still
    means what it meant.
    """
    venue = _venue()
    assert venue.message_rate is None
    for i in range(200):
        assert _reasons(_send(venue, "a", B, 18_600 - (i % 40), 1)) == []


def test_one_participant_exhausting_its_allowance_does_not_touch_another():
    """The limit is a fact about a participant, not about the venue's total
    traffic. A shared budget would let a single runaway algorithm lock every
    other participant out of the market -- the outage the limit exists to
    prevent, arriving by a different route.
    """
    clock = {"now": 0}
    venue = _limited_venue(3, clock)
    for i in range(6):
        _send(venue, "a", B, 18_600 - i, 1)
    assert _reason(_send(venue, "a", B, 18_580, 1)) is RejectReason.RATE_LIMITED

    for i in range(3):
        assert _reasons(_send(venue, "c", S, 18_900 + i, 1)) == []


# --------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------


def test_killing_a_participant_pulls_its_orders_and_names_the_symbols():
    """An operator throwing the switch needs to know what was standing in the
    participant's name, not only that the switch was thrown: liquidity has just
    left several books at once, and somebody has to account for it now rather
    than discover it later.
    """
    venue = _venue()
    venue.list_instrument(_instrument("G"))
    _send(venue, "a", B, 18_600, 30)
    _send(venue, "a", S, 18_900, 20, symbol="G")
    _send(venue, "c", B, 18_500, 10)

    assert venue.kill(AgentId("a"), reason="runaway") == ["F", "G"]
    assert venue.halted_participants[AgentId("a")] == "runaway"
    assert _resting(venue, "F", "a") == []
    assert _resting(venue, "G", "a") == []
    assert _resting(venue, "F", "c") == [10], "somebody else's order was pulled"
    assert venue.conservation_check() == 0


def test_a_killed_participant_cannot_place_or_amend_an_order():
    """An amendment is a request for risk exactly as an order is.

    Guarding only new orders leaves the way back in wide open: the participant
    reworks the orders it already had, at whatever size and price it likes, and
    is trading again without ever having been let back in.
    """
    venue = _venue()
    order_id = _order_id(_send(venue, "a", B, 18_600, 30))
    venue.kill(AgentId("a"))

    fresh = _send(venue, "a", B, 18_550, 10)
    assert _reason(fresh) is RejectReason.PARTICIPANT_HALTED

    amend = venue.submit(
        AgentId("a"),
        "F",
        Replace(AgentId("a"), order_id, Quantity(500), Price(18_800)),
    )
    assert _reason(amend) is RejectReason.PARTICIPANT_HALTED
    assert _resting(venue, "F", "a") == []


def test_a_killed_participant_can_still_cancel():
    """Refusing cancels too would trap the participant in the orders it already
    has, which is the opposite of what stopping it is for.

    The order named here has already gone, because the kill pulled it. What is
    being pinned is where the answer comes from: the book, rather than the
    venue's door.
    """
    venue = _venue()
    order_id = _order_id(_send(venue, "a", B, 18_600, 30))
    venue.kill(AgentId("a"))

    events = venue.submit(AgentId("a"), "F", Cancel(AgentId("a"), order_id))
    assert RejectReason.PARTICIPANT_HALTED not in _reasons(events)


def test_a_stopped_participant_can_pull_an_order_that_is_still_working():
    """The same rule where it has teeth: a live order and an owner who is not
    allowed to trade. The cancel has to reach the book and take the order out of
    it, or the exposure stays in the market with nobody permitted to manage it.
    """
    venue = _venue()
    order_id = _order_id(_send(venue, "a", B, 18_600, 25))
    # Stopped without its orders being pulled first, which is what a participant
    # that stops itself looks like from the venue's side.
    venue.halted_participants[AgentId("a")] = "self"

    events = venue.submit(AgentId("a"), "F", Cancel(AgentId("a"), order_id))
    assert any(isinstance(e, Cancelled) for e in events)
    assert _resting(venue, "F", "a") == []
    assert venue.conservation_check() == 0


def test_reviving_a_participant_lets_it_back_in_with_nothing_working():
    """Coming back to find its old orders restored would hand the participant
    exposure it never re-entered, at prices struck before whatever got it
    stopped.
    """
    venue = _venue()
    _send(venue, "a", B, 18_600, 30)
    _send(venue, "a", S, 18_950, 20)
    venue.kill(AgentId("a"))

    venue.revive(AgentId("a"))
    assert AgentId("a") not in venue.halted_participants
    assert not venue._working.get((AgentId("a"), "F"))
    assert _resting(venue, "F", "a") == []

    assert _reasons(_send(venue, "a", B, 18_450, 5)) == []
    assert _resting(venue, "F", "a") == [5]
    assert venue.conservation_check() == 0


def test_killing_one_participant_leaves_the_rest_of_the_market_trading():
    """Stopping the market is a halt, and a halt is a different decision with
    different costs -- everyone's, rather than one participant's. Confusing the
    two turns a narrow tool into an outage.
    """
    venue = _venue()
    _send(venue, "a", B, 18_600, 20)
    venue.kill(AgentId("a"))

    _send(venue, "c", B, 18_500, 20)
    _send(venue, "d", S, 18_500, 20)
    assert venue.engine("F").tape, "the book stopped trading for everybody"
    assert venue.account(AgentId("c")).positions["F"].quantity == 20
    assert venue.account(AgentId("a")).positions.get("F") is None
    assert venue.conservation_check() == 0


# --------------------------------------------------------------------------
# The ledger, through all of it
# --------------------------------------------------------------------------


def test_the_ledger_stays_exactly_balanced_through_limits_kills_and_revivals():
    """Neither control may cost or create a penny.

    Both refuse commands, and one of them cancels orders on the participant's
    behalf -- exactly the shape of thing that leaves collateral reserved against
    an order that no longer exists. A leak like that shows up nowhere else: the
    books look right, the positions look right, and the only symptom is a number
    that should be zero and is not.
    """
    clock = {"now": 0}
    venue = _limited_venue(3, clock)
    venue.fees = MAKER_TAKER
    rng = random.Random(7)

    refusals = 0
    for step in range(80):
        clock["now"] = step * 200_000_000
        for who, side in (("a", B), ("c", S)):
            low, high = (18_400, 18_700) if side is B else (18_500, 18_800)
            events = _send(venue, who, side, rng.randint(low, high), rng.randint(1, 20))
            refusals += _reasons(events).count(RejectReason.RATE_LIMITED)
        if step == 30:
            venue.kill(AgentId("a"), reason="runaway")
        if step == 50:
            venue.revive(AgentId("a"))
        assert venue.conservation_check() == 0

    assert venue.engine("F").tape, "nothing traded, so the run proved nothing"
    assert refusals > 0, "nothing was rate limited, so the run proved nothing"
    assert venue.conservation_check() == 0
