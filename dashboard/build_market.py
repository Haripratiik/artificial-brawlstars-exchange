"""Assemble the live market the dashboard serves.

The instruments settle from the real fixture dataset, and the fundamental
agents are told what each will actually settle at -- then given a *noisy* view of
it, with a different precision each. So the market has a true value to converge
toward, and whether it gets there is something you can watch rather than
something asserted.

Latency is heterogeneous on purpose. The market maker is effectively colocated,
the funds are a few milliseconds out, the noise traders are far away, and the
human is somewhere in between. That is the same configuration the research
experiments use; the dashboard is not a special case of the simulator, it is the
simulator with a browser attached.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from arena.agents.arbitrageur import Arbitrageur
from arena.agents.flow import FlowTrader
from arena.agents.fundamental import FundamentalTrader
from arena.agents.market_maker import MarketMaker
from arena.agents.noise import NoiseTrader
from arena.contracts.payoff import Binary, Call, Linear, Put
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Basket, Difference, Single
from arena.exchange.types import AgentId
from arena.market.instrument import Instrument
from arena.market.live import HUMAN_ID, VENUE_ID, HumanAgent, LiveMarket
from arena.market.fees import FREE, FeeSchedule
from arena.market.venue import Venue
from arena.market.venue_agent import VenueAgent
from arena.settlement.engine import settle
from arena.sim.kernel import Kernel
from arena.sim.latency import PairwiseLatency
from arena.sim.time import micros, millis
from arena.worlds.brawl.dataset import CanonicalDataset
from arena.worlds.brawl.metrics import metric_ref
from arena.worlds.brawl.oracle import BrawlOracle
from arena.worlds.brawl.reference import load_reference

REPO = Path(__file__).resolve().parents[1]
REFERENCE_ID = "ref-2026S09-v1"

# What a person opens the exchange with. Large enough to take a real
# position in the futures, which settle around 4,700 a contract, and small
# enough that a profit is a number you can see.
HUMAN_STARTING_CASH = 250_000
UTC = timezone.utc

POLICY = DataPolicy(
    min_sample_size=1_000, min_stratum_battles=200, min_strata_coverage=0.80
)
WINDOW = ObservationWindow(
    datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)
)


def _wr(subject: str):
    return Single(metric_ref("adjusted_win_rate", subject))


def _spec(contract_id: str, underlying, payoff, tick: str = "0.25") -> ContractSpec:
    return ContractSpec(
        contract_id=contract_id,
        underlying=underlying,
        payoff=payoff,
        window=WINDOW,
        policy=POLICY,
        reference_id=REFERENCE_ID,
        published_at=WINDOW.start - timedelta(days=1),
        tick_size=tick,
    )


def instruments() -> list[Instrument]:
    return [
        Instrument("SPIKE_WR_FUT", _spec("SPIKE_WR_FUT", _wr("SPIKE"), Linear(10_000.0))),
        Instrument("CROW_WR_FUT", _spec("CROW_WR_FUT", _wr("CROW"), Linear(10_000.0))),
        Instrument(
            "SPIKE_GT48",
            _spec("SPIKE_GT48", _wr("SPIKE"), Binary(">", 0.48, payout=1.0), tick="0.01"),
        ),
        Instrument(
            "SPIKE_CROW",
            _spec("SPIKE_CROW", Difference(_wr("SPIKE"), _wr("CROW")), Linear(10_000.0)),
        ),
        # Options are payoffs on the same underlying as the future, so they
        # settle from the same metric at the same instant and need no separate
        # machinery. Struck either side of where SPIKE actually settles, so one
        # expires worthless and the other in the money.
        Instrument(
            "SPIKE_C4700",
            _spec("SPIKE_C4700", _wr("SPIKE"), Call(4_700.0, 10_000.0), tick="0.25"),
        ),
        Instrument(
            "SPIKE_P4700",
            _spec("SPIKE_P4700", _wr("SPIKE"), Put(4_700.0, 10_000.0), tick="0.25"),
        ),
        Instrument(
            "ASSASSIN_IDX",
            _spec(
                "ASSASSIN_IDX",
                Basket(((_wr("SPIKE"), 0.5), (_wr("CROW"), 0.3), (_wr("PIPER"), 0.2))),
                Linear(10_000.0),
            ),
        ),
    ]


@lru_cache(maxsize=1)
def _world():
    """The dataset and reference snapshot, loaded once.

    Pure functions of two committed files, so caching changes nothing about
    what any run produces -- it only stops every market build from re-reading a
    CSV and re-running settlement, which dominated the test suite.
    """
    dataset = CanonicalDataset.from_csv(REPO / "data" / "fixtures" / "brawl_aggregates.csv")
    reference = load_reference(REPO / "data" / "reference" / f"{REFERENCE_ID}.json")
    return dataset, reference, BrawlOracle(dataset, reference, POLICY)


def true_levels(listed: list[Instrument]) -> dict[str, float]:
    """The true *metric level* each contract is written on.

    Not the settlement value: agents are given a view on the underlying rate
    and derive what it implies for each contract themselves, which is both how
    a fundamental analyst actually works and what makes a single noise
    parameter meaningful across a future, an option and an event contract
    alike.
    """
    _dataset, _reference, oracle = _world()

    levels: dict[str, float] = {}
    for instrument in listed:
        result = settle(instrument.spec, oracle)
        if result.settled and result.underlying_level is not None:
            levels[instrument.symbol] = float(result.underlying_level)
    return levels


def true_values(listed: list[Instrument]) -> dict[str, float]:
    """What each contract will actually settle at, in ticks. For reporting."""
    _dataset, _reference, oracle = _world()

    values: dict[str, float] = {}
    for instrument in listed:
        result = settle(instrument.spec, oracle)
        if result.settled and result.settlement_value is not None:
            values[instrument.symbol] = float(
                instrument.to_ticks(result.settlement_value)
            )
    return values


def build(
    seed: int = 7,
    speed: float = 1.0,
    arbitrageur: bool = False,
    recycle_capital: bool = True,
    flow_traders: int = 0,
    fees: FeeSchedule = FREE,
    price_band: float | None = None,
    human_cash: int = HUMAN_STARTING_CASH,
) -> LiveMarket:
    listed = instruments()
    by_symbol = {i.symbol: i for i in listed}
    levels = true_levels(listed)

    # Sized for the contracts on offer, not picked round. A 10,000-scale future
    # quoted 30 lots a side ties up ~300k per side per symbol, and full
    # collateralisation means that capital is genuinely committed rather than
    # notional. Too little and every agent spends the session rejected, which
    # looks like a broken market rather than a poor one.
    venue = Venue(
        "arena",
        starting_cash=40_000_000,
        fees=fees,
        price_band=price_band,
        # A person starts with an account they can actually read. The bots keep
        # the large balance because a market maker quoting seven books at once
        # genuinely needs it -- but a trader watching a gain of a hundred against
        # forty million learns nothing about what their trade did.
        balances={HUMAN_ID: human_cash},
    )
    for instrument in listed:
        venue.list_instrument(instrument)

    maker_id = AgentId("mm-1")
    arb_id = AgentId("arb-1")
    fund_ids = [AgentId("fund-sharp"), AgentId("fund-vague")]
    noise_ids = [AgentId(f"noise-{i:02d}") for i in range(14)]
    flow_ids = [AgentId(f"flow-{i:02d}") for i in range(flow_traders)]

    latency = PairwiseLatency(
        default=millis(4),
        per_agent={
            maker_id: micros(150),                       # colocated
            arb_id: millis(2),
            fund_ids[0]: millis(3),
            fund_ids[1]: millis(9),
            HUMAN_ID: millis(20),                        # a person on a browser
            **{a: millis(45) for a in noise_ids},        # retail, far away
            **{a: millis(6) for a in flow_ids},          # brokers' algos
        },
        jitter_fraction=0.15,
        seed=seed,
    )

    kernel = Kernel(seed=seed, latency=latency)
    venue_agent = VenueAgent(VENUE_ID, venue)
    human = HumanAgent(VENUE_ID, by_symbol)

    maker = MarketMaker(
        maker_id,
        VENUE_ID,
        by_symbol,
        wake_interval=millis(300),
        half_spread=5,
        quote_size=30,
        max_skew_fraction=0.10,
        position_limit=1_200,
        # Opens each book near the middle of its range rather than at the true
        # value: if the maker started on the answer there would be nothing for
        # the market to discover.
        reference={s: float(sum(i.tick_bounds) / 2) for s, i in by_symbol.items()},
    )

    funds = [
        FundamentalTrader(
            fund_ids[0], VENUE_ID, by_symbol, levels,
            wake_interval=millis(600), precision=3.0, base_size=20,
            max_position=900,
        ),
        FundamentalTrader(
            fund_ids[1], VENUE_ID, by_symbol, levels,
            wake_interval=millis(1_100), precision=0.8, base_size=12,
            max_position=600,
        ),
    ]
    noise = [
        NoiseTrader(a, VENUE_ID, by_symbol, wake_interval=millis(1_100))
        for a in noise_ids
    ]

    # Off unless asked for. These agents *assume* power-law sizes and bursty
    # arrivals, so any claim that this market produces fat tails emergently is
    # only meaningful with them absent -- see arena/agents/flow.py.
    flow = [
        FlowTrader(agent_id, VENUE_ID, by_symbol, wake_interval=millis(500))
        for agent_id in flow_ids
    ]

    agents = [maker, *funds, *noise, *flow]
    if arbitrageur:
        # Off by default, on the evidence. It derives the right identities and
        # trades them, but measured across four paired seeds it improved spread
        # consistency on three and made it worse on the fourth, and on one seed
        # it took visible ask depth from 877 lots to 69. Enforcing a relation by
        # repeatedly lifting the book buys consistency with liquidity, and a
        # market that cannot absorb an order is broken more fundamentally than
        # one carrying a stale spread. See docs/GAPS.md for the numbers.
        agents.append(
            Arbitrageur(
                arb_id,
                VENUE_ID,
                by_symbol,
                wake_interval=millis(400),
                recycle_capital=recycle_capital,
            )
        )
    kernel.add(venue_agent)
    kernel.add(human)
    kernel.add_all(agents)

    return LiveMarket(
        venue=venue,
        kernel=kernel,
        venue_agent=venue_agent,
        human=human,
        agents=agents,
        speed=speed,
    )
