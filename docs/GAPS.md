# What this is, and what it is not

An honest audit, written because "it seems too simple" was a fair challenge and
turned out to be right in a way that mattered.

## The size of the thing

```
python/arena/exchange     831 lines     matching engine, order book
python/arena/market       910           venue, instruments, live market
python/arena/sim          557           kernel, latency, messages
python/arena/contracts    503           underlying algebra, payoffs, specs
python/arena/agents       494           three rudimentary participants
python/arena/portfolio    361           positions, accounts, money
python/arena/settlement   278           oracle protocol, settlement engine
                        -----
                         3,934 lines of production code
                         3,215 lines of tests
```

A real exchange is six to seven figures of code. This is a research simulator,
so that comparison is not the right one -- but it does mean the honest claim is
"a correct core", not "an exchange".

## What the audit found

Two of these were not missing features. They were defects that made the market's
output largely fictional while every number continued to look plausible.

**Wash trading, now fixed.** Nothing stopped an agent matching against its own
resting order. The market maker quotes both sides, and its cancels are in flight
while it requotes, so its new bid crossed its own stale offer. Measured on the
live market: **90% of traded volume was one agent trading with itself.** Volume,
prices, the tape, and every impact figure derived from them were fiction. The
position nets to zero and the PnL nets to zero, which is precisely why it
survived so long -- there is no number that looks wrong. Self-match prevention
now defaults to cancel-oldest, is differentially tested against the reference
matcher, and real volume turns out to be about a tenth of what was reported.

**Expiries were never enforced, now fixed.** `Instrument.expiry` existed and was
documented as "trading stops when the observation window closes". Nothing called
it. Contracts traded indefinitely past the date their outcome was determined.

## What is genuinely absent

Checked for, and not present:

| | |
|---|---|
| Opening / closing auctions | absent. Real markets open with a call auction, not continuous trading, and the opening print is a distinct mechanism |
| Circuit breakers, price bands | absent. Nothing stops a runaway or a fat finger |
| Trading halts and session states | absent. The market is always open, forever |
| Stop and stop-limit orders | absent |
| Iceberg / reserve orders | absent, and they change queue dynamics materially |
| Post-only, pegged, MPL orders | absent |
| Maker/taker fee schedule | absent. A `fee` parameter exists on the ledger and is never populated |
| Tiered tick tables | absent. One tick size per instrument |
| Clearing, novation, a CCP | absent. Settlement is direct between counterparties |
| Margin, leverage, liquidation | absent by design; a later phase |
| Message throttling, kill switch | absent |
| Multiple venues, order routing | absent |

Order types implemented: **limit and market**, with GTC, IOC and FOK. A
production venue offers twenty or more.

## What is genuinely present, and correct

Not everything thin is wrong, and these have been checked rather than assumed:

- **Price-time priority matching**, differentially tested against a deliberately
  naive reference implementation across cancel-heavy random streams. That
  harness found a real depth-accounting bug that was corrupting fill-or-kill.
- **Deterministic replay.** Identical command streams produce identical event
  streams, which is the acceptance criterion the C++ port will have to meet.
- **A discrete-event kernel with per-agent latency**, FIFO per link, so two
  subscribers genuinely see the same print at different times.
- **Exact conservation of value** through trading and settlement, on integer
  money. Not nearly-zero: zero.
- **Exact collateral**, because every instrument settles inside a known
  interval -- arithmetic rather than a value-at-risk estimate.
- **Twelve instrument classes** from one algebra, including compositions nobody
  designed for, with put-call parity exact at settlement.
- **Lookahead prevented structurally** on five channels.

## What would make it a market rather than a mechanism

In the order that buys the most realism per unit of work:

1. **Validate against stylized facts.** The acid test, and currently absent. Real
   order-book markets exhibit fat-tailed returns, volatility clustering, no
   return autocorrelation but strong autocorrelation in absolute returns,
   long-memory order flow, and square-root price impact. A simulator that does
   not reproduce them is not producing realistic prices, however correct its
   matching engine. This should come before more agent sophistication, because
   it is the thing that tells you whether the agents are right.
2. **Realistic order flow.** Power-law order sizes, clustered arrivals, and a
   cancel rate above 90% -- real books are mostly cancellations, and queue
   dynamics depend on it.
3. **An arbitrageur.** Put-call parity does not hold in the live market and
   nothing enforces cross-instrument consistency. This is the cheapest agent to
   write and the one whose absence is most visible.
4. **Fees.** Maker/taker economics change market-maker behaviour qualitatively,
   and the hooks already exist.
5. **Auctions and halts.** Needed before any claim about opening dynamics or
   stressed markets.

Sophisticated market makers and buy-side firms come after all of these, because
until the market reproduces known statistical regularities there is no way to
tell a good market maker from a badly calibrated one.
