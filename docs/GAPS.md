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
3. ~~**An arbitrageur.**~~ **Written, and it half-works.** See below.
4. **Fees.** Maker/taker economics change market-maker behaviour qualitatively,
   and the hooks already exist.
5. **Auctions and halts.** Needed before any claim about opening dynamics or
   stressed markets.

Sophisticated market makers and buy-side firms come after all of these, because
until the market reproduces known statistical regularities there is no way to
tell a good market maker from a badly calibrated one.

## The arbitrageur, and what measuring it actually showed

`arena/agents/arbitrageur.py` derives its pricing identities from the listed
contracts rather than from a hand-written table -- list a new spread and it
becomes arbitrageable with no code change; list an index whose component has no
future and no relation is formed, because a relation traded against a proxy is
a bet rather than an arbitrage.

Three predictions were made before it was measured. **Two were wrong.**

**It is off by default, on the evidence.** `build()` takes `arbitrageur=True`
to switch it on.

**The spread gap: bought with liquidity, and not worth the price.** Paired
seeds, 600 simulated seconds, mean `|SPIKE_CROW - (SPIKE_WR_FUT - CROW_WR_FUT)|`
over the second half of each session, against visible ask depth in the top ten
levels of `SPIKE_WR_FUT`:

| version | mean gap (ticks) | ask depth | attempts |
|---|---|---|---|
| no arbitrageur | 307.7 | 785 | -- |
| aggressive (fires on every wakeup) | 180.7 | ~130 | 337 |
| restrained (scale-in rule) | 282.9 | 625 | 78 |

The aggressive version really does enforce the relation better -- 41% tighter --
and it does so by **stripping the book**, taking depth down by 83%. It fired on
93% of its wakeups, roughly 1,400 times a session, because the gap never closed
and nothing stopped it re-entering the same trade. A market that thin cannot
absorb anyone else's order, and it broke a previously-verified property: a
5,000-lot sweep that used to walk the book filled 26 lots at the touch.

Restraining it (do not add to a package unless the dislocation has widened
1.5x, and never take more than 25% of visible depth) restores the book and
loses most of the consistency gain. Per seed, the gap ratio with the restrained
version is 0.65, 1.29, 0.61, 0.90 -- it makes one of four seeds **worse**, and
four seeds cannot distinguish a mean of 0.86 from 1.0.

So the honest verdict is that this design does not reliably do its job. The
resolution is not tuning: a taker enforces relations by consuming liquidity, and
the agent that enforces them while *adding* liquidity is one that **posts** at
relation-implied prices and hedges on fill. That is the deferred market-maker
work, not an arbitrageur.

What it does do reliably, and what the tests assert: derive the correct
identities from the listed contracts with nothing hand-configured, respect its
position limits on every leg, size to the depth it can actually see, and
conserve value exactly.

**Put-call parity: unenforceable, for a reason worth recording.** The call book
is two-sided in **2% of samples**. The arbitrageur cannot check the relation
98% of the time -- not because it declines, but because there is no price. Its
own inventory in the option never exceeds 72 of a 300 limit; it is starved, not
constrained.

No arbitrageur fixes this. Parity needs a market maker that quotes the option
off the *replicating portfolio* -- underlying plus put -- rather than off the
option's own last trade, which is exactly how real option market makers work,
and is the deferred "sophisticated market maker" work. Recorded here rather than
patched, because tightening the arbitrageur's band would produce a smaller
number without producing a two-sided option book.

**The spread's mean reversion was diagnosed wrongly.** The plan called
VR(32) = 0.25 on the spread a pathology caused by the spread being untethered
from its legs. It is not. Measured per instrument:

| symbol | VR(32) without arb | with arb |
|---|---|---|
| SPIKE_CROW (spread) | 0.27 | 0.25 |
| SPIKE_WR_FUT (leg) | 1.73 | 1.26 |
| CROW_WR_FUT (leg) | 1.47 | 1.09 |

Both legs trend; their difference mean-reverts. That is the signature of
**cointegration**, not of a broken market -- it is the reason pairs trading
exists. Tying the spread to its legs did not move its variance ratio and should
not have. The prediction that it would go to 1 was wrong on the theory, and the
market was right.
