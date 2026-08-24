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
from pathlib import Path

from arena.agents.fundamental import FundamentalTrader
from arena.agents.market_maker import MarketMaker
from arena.agents.noise import NoiseTrader
from arena.contracts.payoff import Binary, Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Basket, Difference, Single
from arena.exchange.types import AgentId
from arena.market.instrument import Instrument
from arena.market.live import HUMAN_ID, VENUE_ID, HumanAgent, LiveMarket
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
        Instrument(
            "ASSASSIN_IDX",
            _spec(
                "ASSASSIN_IDX",
                Basket(((_wr("SPIKE"), 0.5), (_wr("CROW"), 0.3), (_wr("PIPER"), 0.2))),
                Linear(10_000.0),
            ),
        ),
    ]


def true_values(listed: list[Instrument]) -> dict[str, float]:
    """What each contract will actually settle at, in ticks.

    Computed by running the real settlement engine against the real dataset --
    so the fundamental agents are anchored to the same number the contract will
    pay, not to an invented one. The market's job is to find it.
    """
    dataset = CanonicalDataset.from_csv(REPO / "data" / "fixtures" / "brawl_aggregates.csv")
    reference = load_reference(REPO / "data" / "reference" / f"{REFERENCE_ID}.json")
    oracle = BrawlOracle(dataset, reference, POLICY)

    values: dict[str, float] = {}
    for instrument in listed:
        result = settle(instrument.spec, oracle)
        if result.settled and result.settlement_value is not None:
            values[instrument.symbol] = float(
                instrument.to_ticks(result.settlement_value)
            )
    return values


def build(seed: int = 7, speed: float = 1.0) -> LiveMarket:
    listed = instruments()
    by_symbol = {i.symbol: i for i in listed}
    truth = true_values(listed)

    # Sized for the contracts on offer, not picked round. A 10,000-scale future
    # quoted 30 lots a side ties up ~300k per side per symbol, and full
    # collateralisation means that capital is genuinely committed rather than
    # notional. Too little and every agent spends the session rejected, which
    # looks like a broken market rather than a poor one.
    venue = Venue("arena", starting_cash=40_000_000)
    for instrument in listed:
        venue.list_instrument(instrument)

    maker_id = AgentId("mm-1")
    fund_ids = [AgentId("fund-sharp"), AgentId("fund-vague")]
    noise_ids = [AgentId(f"noise-{i:02d}") for i in range(14)]

    latency = PairwiseLatency(
        default=millis(4),
        per_agent={
            maker_id: micros(150),                       # colocated
            fund_ids[0]: millis(3),
            fund_ids[1]: millis(9),
            HUMAN_ID: millis(20),                        # a person on a browser
            **{a: millis(45) for a in noise_ids},        # retail, far away
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
            fund_ids[0], VENUE_ID, by_symbol, truth,
            wake_interval=millis(600), precision=3.0, base_size=20,
            max_position=900,
        ),
        FundamentalTrader(
            fund_ids[1], VENUE_ID, by_symbol, truth,
            wake_interval=millis(1_100), precision=0.8, base_size=12,
            max_position=600,
        ),
    ]
    noise = [
        NoiseTrader(a, VENUE_ID, by_symbol, wake_interval=millis(1_100))
        for a in noise_ids
    ]

    agents = [maker, *funds, *noise]
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
