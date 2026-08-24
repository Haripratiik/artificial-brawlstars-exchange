"""The complete loop: define contracts, trade them, settle them, see the PnL.

    python examples/full_market.py

Until now the project had two halves that never met -- a contract layer that
could settle a future, and an exchange that could match orders in an abstract
instrument. Nothing said *this symbol settles by that contract*, so a trade
could never become a settlement and a settlement could never reach anyone's
account.

This runs the whole cycle on four instruments at once, all settling from the
same Brawl dataset:

    SPIKE_WR_FUT     linear performance future
    SPIKE_GT54       binary event contract
    SPIKE_CROW       relative-value spread
    ASSASSIN_IDX     weighted index

Two things are worth watching. **Collateral is exact**, because every contract
settles inside a known interval -- a short on the future is charged what it can
actually lose, not a volatility estimate. And **value is conserved to the unit**
through trading and settlement both, which is the check that makes any PnL
figure here believable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arena.contracts.payoff import Binary, Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Basket, Difference, Single
from arena.exchange.events import Submit
from arena.exchange.types import AgentId, OrderType, Quantity, Side, TimeInForce
from arena.market.instrument import Instrument
from arena.market.venue import Venue
from arena.portfolio.money import from_money
from arena.settlement.engine import settle
from arena.worlds.brawl.dataset import CanonicalDataset
from arena.worlds.brawl.metrics import metric_ref
from arena.worlds.brawl.oracle import BrawlOracle
from arena.worlds.brawl.reference import load_reference

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
REFERENCE_ID = "ref-2026S09-v1"
POLICY = DataPolicy(min_sample_size=1_000, min_stratum_battles=200, min_strata_coverage=0.80)

WINDOW = ObservationWindow(
    datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)
)

WR = lambda who: Single(metric_ref("adjusted_win_rate", who))  # noqa: E731


def spec(contract_id, underlying, payoff, tick="0.25") -> ContractSpec:
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


def build_instruments() -> list[Instrument]:
    return [
        Instrument("SPIKE_WR_FUT", spec("SPIKE_WR_FUT", WR("SPIKE"), Linear(10_000.0))),
        Instrument(
            "SPIKE_GT54",
            spec("SPIKE_GT54", WR("SPIKE"), Binary(">", 0.54, payout=1.0), tick="0.01"),
        ),
        Instrument(
            "SPIKE_CROW",
            spec("SPIKE_CROW", Difference(WR("SPIKE"), WR("CROW")), Linear(10_000.0)),
        ),
        Instrument(
            "ASSASSIN_IDX",
            spec(
                "ASSASSIN_IDX",
                Basket(((WR("SPIKE"), 0.5), (WR("CROW"), 0.3), (WR("PIPER"), 0.2))),
                Linear(10_000.0),
            ),
        ),
    ]


def order(agent, side, ticks, qty) -> Submit:
    return Submit(agent, side, Quantity(qty), ticks, OrderType.LIMIT, TimeInForce.GTC)


def main() -> None:
    dataset = CanonicalDataset.from_csv(REPO / "data" / "fixtures" / "brawl_aggregates.csv")
    reference = load_reference(REPO / "data" / "reference" / f"{REFERENCE_ID}.json")
    oracle = BrawlOracle(dataset, reference, POLICY)

    venue = Venue("arena", starting_cash=1_000_000)
    instruments = build_instruments()
    for instrument in instruments:
        venue.list_instrument(instrument)

    print("LISTED")
    for instrument in instruments:
        low, high = instrument.settlement_bounds
        print(
            f"  {instrument.symbol:<14} {instrument.instrument_class:<8} "
            f"tick {instrument.tick_size:<6} settles in [{low}, {high}]"
        )

    # Two traders take opposite sides of everything, at prices near where each
    # contract will actually settle.
    bull, bear = AgentId("bull"), AgentId("bear")
    quotes = {
        "SPIKE_WR_FUT": (Decimal("4800"), 40),
        "SPIKE_GT54": (Decimal("0.35"), 500),
        "SPIKE_CROW": (Decimal("100"), 25),
        "ASSASSIN_IDX": (Decimal("4900"), 30),
    }

    print("\nTRADING")
    for symbol, (price, qty) in quotes.items():
        instrument = venue.registry.require(symbol)
        ticks = instrument.to_ticks(price)
        venue.submit(bear, symbol, order(bear, Side.SELL, ticks, qty))
        venue.submit(bull, symbol, order(bull, Side.BUY, ticks, qty))
        bull_collateral = venue.account(bull).collateral.get(symbol, 0)
        bear_collateral = venue.account(bear).collateral.get(symbol, 0)
        print(
            f"  {symbol:<14} {qty:>4} lots @ {price:<8} "
            f"collateral  long {from_money(bull_collateral):>10,}  "
            f"short {from_money(bear_collateral):>10,}"
        )

    print(f"\n  bull free cash {from_money(venue.account(bull).free_cash):>12,}")
    print(f"  bear free cash {from_money(venue.account(bear).free_cash):>12,}")
    print(f"  conservation   {venue.conservation_check()}")

    print("\nSETTLEMENT (from the real fixture dataset)")
    for instrument in instruments:
        result = settle(instrument.spec, oracle)
        realised = venue.settle(instrument.symbol, result)
        value = result.settlement_value
        entry = quotes[instrument.symbol][0]
        print(
            f"  {instrument.symbol:<14} settled {str(value):>10}  "
            f"(traded at {entry})   "
            f"bull {from_money(realised[bull]):>+12,}   "
            f"bear {from_money(realised[bear]):>+12,}"
        )

    print("\nFINAL")
    for agent in (bull, bear):
        account = venue.account(agent)
        pnl = from_money(account.cash) - from_money(account.starting_cash)
        print(
            f"  {agent:<6} cash {from_money(account.cash):>14,}   "
            f"PnL {pnl:>+12,}   collateral held {from_money(account.posted_collateral)}"
        )

    residual = venue.conservation_check()
    print(f"\n  conservation check: {residual}   ({'exact' if residual == 0 else 'LEAK'})")
    print("  trading moves value between participants; it never creates it")


if __name__ == "__main__":
    main()
