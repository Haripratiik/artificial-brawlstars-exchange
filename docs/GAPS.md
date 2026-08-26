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

**The mark ignored the book when the book was one-sided, now fixed.** `mark`
returned the last trade whenever either side of the touch was missing, so a
sweep that cleared every offer left the mark to be set by whatever printed
next -- a single lot on the bid was enough to value every open position below a
touch that hundreds of lots were standing at. A print is a fact about the past
and a resting order is an offer about the present, so the mark is now held
inside whichever side of the touch is standing. It was found by a test that had
been passing for the wrong reason.

**The revealed truth was in the wrong unit, now fixed.** Every contract page
offers to show what it will actually be worth, which exists so the market's
price can be checked against the answer -- the single most useful thing a
simulated exchange can offer. It was reported in ticks while every price beside
it was in contract units, so a future marking at 4,663 revealed a "settlement"
of 18,677 and the chart drew that as a target line four times off the top of
its own series. On a commodity at a 0.05 tick the factor was twenty. It
survived because the wrong number looks exactly like a number, and it was found
by opening the page and reading it rather than by any test.

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

## Two venues now, and the comparison between them

`arena/market/lmsr_venue.py` runs Hanson's logarithmic scoring rule beside the
order book. It subclasses `Venue` rather than reimplementing it, so accounts,
collateral, expiry, settlement and the conservation check are literally the same
code on both -- if they were separate implementations, a difference between the
two experiments could be an accounting difference.

The cost curve is rendered as an L2 book (level `T` holds the shares that move
the marginal price from tick `T` to `T+1`), so `VenueAgent`, the market-data
feeds and every agent work against it unchanged. Two honest departures from a
book, both documented in the module: nothing rests, so every order is
effectively immediate-or-cancel; and raw LMSR has **no bid-ask spread at all**
because it is path independent -- the spread here comes from quantising prices to
the tick grid, always in the maker's favour, which is also what keeps the ledger
exact and the bounded-loss guarantee intact.

Liquidity is parameterised by **subsidy**, not by `b`: the subsidy is what the
venue will lose making the market, which is the quantity anyone actually
decides, and `b` is its consequence. `subsidy_for_depth` inverts it again so the
two venues can be calibrated to the same depth at the touch -- without that, a
comparison of mechanisms would really be a comparison of depths.

**Result: the mechanism does not matter.** 200 paired trials, order book 0.03108
against scoring rule 0.03115, difference +0.00007 with a 95% interval of
[-0.0035, +0.0031]. A tight null, not an underpowered one. Sweeping depth across
a 70x range does not rescue it either -- error is U-shaped with its minimum at
the depth-matched point and never approaches the precision-weighted baseline.

## Fees, sessions, halts and realistic flow

All four are built, all four default to off or inert, and each records what it
actually delivers rather than what it was aimed at.

**Fees** (`arena/market/fees.py`) are maker-taker on notional in basis points.
They land in a real venue account inside the conservation check, because the
quiet failure here is charging the right amount and banking it nowhere -- the
ledger would still balance to within a rounding error. Rounding always goes
toward the venue, or many tiny fills would extract a fraction of a unit each.
`POST_ONLY` exists because maker-taker creates it: a maker that crosses by
accident pays the taker fee instead of earning the rebate.

**Call auctions** (`arena/exchange/session.py`) clear on four tie-breaks --
maximum volume, minimum surplus, the surplus's own side, nearest the reference.
Everyone trades at one price, market orders become market-on-open orders, and
limit IOC/FOK are refused during a call rather than silently rested. Auction
fills have no aggressor, so both sides book passive and earn the maker rate.

**Sessions and halts** replace the old closed-symbol set with a phase map
(PRE_OPEN / CONTINUOUS / AUCTION / CLOSED). A halt accumulates orders and
reopens through an uncross; a price band trips it automatically.

**Realistic order flow** (`arena/agents/flow.py`) carries power-law order sizes,
power-law placement from the touch, heavy cancellation and Hawkes-clustered
arrivals, with parameters quoted from the literature rather than fitted here.

### Two things it measured that were not what was expected

**The cancel rate is ~60%, not the >90% of real equity books** -- 58.8% without
these agents and 60.4% with, so they barely move it. The missing thirty points
are structural: real cancellation is dominated by makers requoting on every tick
at microsecond scale, and nothing here requotes faster than 300ms. Left as a gap
rather than tuned, since a cancel rate reached by inflating one agent's churn
would be the number without the mechanism.

**Assumed power-law order sizes did not inflate the measured tails.** The
warning first written into that module said they would, and made the emergence
result look fragile. Three paired seeds say otherwise:

| statistic | without flow | with flow |
|---|---|---|
| Hill tail index | 1.86 | 2.06 (lighter) |
| excess kurtosis | 152.9 | 131.4 |
| volatility clustering | 0.16 | 0.14 |
| bid-ask bounce (lag 1) | +0.13 | **-0.05** |

The extra population deepens the book faster than heavy sizes can move it. The
bid-ask bounce is the more interesting line: it had the *wrong sign* without
these agents, and becomes correctly negative with them, because they both post
and take. Three seeds is not many, so the claim is "did not inflate the tails"
rather than "reduced them".

### The explosion that was caught

The first Hawkes implementation added excitation in the wrong units with no
stability condition. One agent reached 26,000x its baseline intensity, its
inter-arrival time collapsed to microseconds, and it emitted roughly 6,000
orders a second forever -- the suite simply stopped returning. It is now a real
Hawkes process parameterised by branching ratio, the constructor refuses any
value at or above one, and a test checks the realised rate against the
theoretical `mu / (1 - n)`.

## Where this stands now

Most of the list below has been done since it was written; it is kept because
the reasoning still holds and because the measured outcomes are recorded under
each item. Struck items link to what actually happened.

1. ~~Validate against stylized facts~~ — done, and three of four predictions
   were wrong.
2. ~~Realistic order flow~~ — done. Power-law sizes, Hawkes arrivals. The cancel
   rate reaches ~60%, not the >90% of real books; see below.
3. ~~An arbitrageur~~ — written, measured, and **off by default**; see below.
4. ~~Fees~~ — maker-taker, off by default.
5. ~~Auctions and halts~~ — done, off by default.

### Still open, in the order that buys the most per unit of work

1. ~~The option surface is internally inconsistent.~~ **Fixed**, and the
   cause was not the one named here. See below.
2. **Fees, auctions, halts and the scoring-rule venue are built and never run.**
   All tested, all defaulted off, so the live exchange exercises none of them.
3. **No auth.** Every browser shares one account, so two tabs are one trader.
4. **Liquidity does not replenish, and one maker is the whole other side.**
   Measured by sweeping 60% of the standing offers on `SPIKE_WR_FUT`: the
   maker absorbs **89%** of the order, ends short about 1,150 lots, and is then
   past the point where its collateral lets it quote at all. The offer it was
   run over at never returns -- the spread goes from 0.50 to 1,021.00 and is
   **exactly** 1,021.00 three minutes later, with the maker having worked none
   of the position off. Not a slow repair: no repair. A real book mends in
   milliseconds because the maker that got run over is one of several; here it
   is the only one. `test_a_large_order_moves_the_price` asserts this as it is,
   so the assertion fails on the day replenishment works.
5. **Cancel rate is ~60%, not >90%.** Nothing requotes faster than 300ms.
6. ~~A share and its future have no relation the arbitrageur knows.~~
   **Fixed by listing the legs.** The 0.4x relation to the four-week future was
   never an identity -- the four weekly rates are each battle-weighted, so they
   do not average to the four-week rate, and they differ by 0.08%. Small, and
   small is what makes it dangerous to trade as though it were exact. So the
   four weekly futures are listed instead: `SPIKE_EQ` = `SPIKE_WR_W1` + ... +
   `W4`, exactly, because both sides resolve the same metric over the same
   windows under the same evidential bar. Settlement confirms it to the tick:
   1,874 + 1,859 + 1,875 + 1,869 = 7,477. `CROW_EQ` deliberately has no legs,
   so one share is arbitrage-linked and one is not.

## The option surface, and the bug underneath it

The symptom was a riskless trade sitting in the book: `SPIKE_C4700` marking at
72.7 while `SPIKE_C4600` marked at 59.1. A call struck higher cannot be worth
more than one struck lower, because the lower strike pays whatever the higher
one pays and sometimes more. Put-call parity was out by 35 ticks at the same
moment.

The diagnosis written here was that the market maker priced each book
independently. That was true and it was the smaller half.

**The larger half was that every agent held a separate view of the same Brawler
for every contract written on it.** `FundamentalTrader` drew its estimate, and
its Monte Carlo sample, per *symbol*. So `SPIKE_C4600` and `SPIKE_C4650` were
valued from independent draws of the same posterior, the sampling error between
them was independent, and the ladder the agent believed in was not monotone.
Measured before the fix: `fund-vague` valued the 4,650 call at **119.03** and
the strictly more valuable 4,600 call at **36.67** -- and then traded on the
difference, which was entirely its own Monte Carlo error. `BayesianFundamental`
had the same shape of bug one level deeper: it drew a fresh posterior per
symbol, so it observed a different sample of battles for each contract on one
Brawler.

Both now hold one view per *underlying*, keyed by what the contract is written
on. Common random numbers make ``max(F - K, 0)`` decreasing in ``K`` draw by
draw, so monotonicity holds pathwise rather than on average.

The maker was the other half. `SurfaceMarketMaker` quotes an entire chain from
a **single distribution** of where the underlying settles, which is what real
desks do and which makes the ladder arbitrage-free by construction: a set of
call prices at one maturity is free of static arbitrage exactly when it is
decreasing and convex in strike with slope in [-1, 0] (Davis and Hobson 2007;
Carr and Madan 2005), and all three are automatic for prices of the form
``E[(F - K)+]`` under any fixed law. The put is *defined* as the call's parity
partner, so parity is exact rather than close.

Two things it does not do, because they would make it price the answer:

- the distribution is centred on the **market's** live mid for the underlying,
  never on the settlement value. If the future is mispriced the whole chain is
  consistently mispriced, which is the point -- consistency is what was broken,
  not accuracy.
- its width is **estimated from the tape**, an exponentially-weighted variance
  of prints around its anchor, converted to a Beta concentration by matching
  moments. A constant there would have been a number chosen to make option
  prices look plausible, and it would have frozen the one quantity an option
  market is about.

Inventory skews the *underlying*, in units of that estimated dispersion, and
the whole ladder reprices from the shifted forward. Skewing each strike by a
fraction of its own settlement range -- what the plain maker does, and what this
class first copied -- was measured at a 165-point shift from 66 lots of net
delta, which sent every call in the chain to zero and tripled the puts.

### Measured, over six minutes of live market, seed 7

| | plain maker | plain + arb | surface maker | surface + arb |
|---|---|---|---|---|
| every strike two-sided | 71% | 74% | **100%** | **100%** |
| `SPIKE_C4700` two-sided | 0% | 0% | **100%** | **100%** |
| monotonicity breached | 0/29 | 0/31 | 0/68 | 0/68 |
| vertical bound breached | 0/29 | 0/31 | 0/68 | 0/68 |
| butterfly breached | n/a | n/a | 0/34 | 0/34 |
| parity gap, mean | n/a | n/a | 14.88 | **8.64** |

The plain maker's zeros are not a pass. `SPIKE_C4700` never has two sides at
all under it, so a third of the chain has no price, there is no butterfly to
check and parity cannot be measured -- its consistency is three quotable books
out of five. Each check is scored only when the books it needs are two-sided,
which is why the denominators differ.

The arbitrageur now also enforces **bands** rather than only identities, since
the conditions that make a chain consistent are inequalities. `Relation` gained
a `[lower, upper]` interval and an identity is the zero-width case, so nothing
that existed before changed. It derives vertical spreads, butterflies, and the
strip relation below.

**A latent bug fell out of it.** The arbitrageur keyed contracts by their
underlying alone, so two contracts differing only in the *window* they measure
were indistinguishable and the last one listed silently won the lookup. Nothing
was mispriced by it -- no composite referenced a weekly contract at the time --
but listing weekly futures is exactly what would have made it start forming
identities between two things that are not the same thing.

### What is still missing, and it is not consistency

The chain is consistent and carries very little time value, because the
underlying barely moves: realised dispersion of `SPIKE_WR_FUT` over a ten-minute
session is **14.6** on a price near 4,670. The market has an information
*stock*, not an information *flow* -- every agent receives its whole sample at
t=0 and the price converges within seconds, so there is nothing left to arrive.
Releasing each agent's evidence progressively would make the underlying
genuinely diffuse and give options something to be about. That, not the
surface, is the next thing worth building for them.

## Asset classes

Eight classes, derived from the contract rather than declared: `future`,
`event`, `call`, `put`, `spread`, `index`, and now `commodity` and `equity`.
Twenty-one contracts listed on the live exchange.

**Commodities** are a claim on an *amount delivered* rather than on a
proportion, which is what makes the delivery window part of the contract and
gives four consecutive weeks a term structure instead of four copies of the same
thing. `battle_volume` is deliberately neither shrunk nor standardized:
reweighting a count onto reference proportions produces a number that is the
count of nothing. The consequence, stated in the metric and worth repeating, is
that it measures volume **in the canonical corpus, not in the game** — a wider
crawl sees more battles. Sample-based volume indices trade on that footing in
the real world; the honest move is to say so rather than imply a census.

**Shares** pay before they settle, which is the entire difference between a
share and a future. `SPIKE_EQ` pays 1,000 times a Brawler's adjusted win rate
at the end of each of four weeks and then expires worth nothing, because it has
paid everything out. Each week is measured on its own evidence, so a bad week
is a smaller payment rather than a smaller number at the end -- and the price is
the stream that is left, which is why a share reprices on news about one period
rather than only on news about its last day.

Three pieces of machinery had to be honest for that to work, and each is the
kind of thing that would have looked fine while being wrong:

- **A distribution is not profit.** The holder receives cash and the contract is
  worth exactly that much less, so equity does not move. Booking it as realised
  gain would report a holder as making money for holding.
- **Collateral narrows in the same instant the cash moves.** Paying `d` per unit
  lowers both ends of what is left by `d`, so a short's requirement falls by
  precisely the cash it just paid. Without that, meeting an obligation it always
  had would look like a margin breach.
- **The range a contract collateralises against is not the range it settles in.**
  A share settles at zero; it is worth up to 4,000. `value_bounds` covers the
  claim and `settlement_bounds` covers the ending, and collateral uses the
  first.

**What is deliberately absent is the perpetuity.** A stock has no expiry, and
every contract here settles inside a known interval -- which is exactly what
makes collateral arithmetic rather than a value-at-risk estimate. A claim that
never settles has no such interval. Real perpetuals live without one by using
funding rates and margin calls, which is a different risk model from the one
this venue is built on, and adopting it quietly to make the word "stock" fit
would weaken the guarantee everything else here depends on. What is listed is
the honest finite version, and it is called a share rather than a stock for
that reason.

**What is missing to make the timing matter.** With no interest rate, a stream
and a lump sum of the same size are worth the same, so a share and 0.4 of the
matching future differ only in when collateral comes back. That should be worth
something here, because Experiment 1 found capital is the binding constraint on
this market -- but it is a prediction, not a result, and measuring it needs the
relation in the arbitrageur and a controlled run.

## Superseded reasoning, kept for the record

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

The cause turned out not to be a missing market maker. Both options are struck
at 4700 while SPIKE settles at 4669, so the call is worth **exactly zero** and
the put 123 ticks of an 18,800-tick range. Both sit pinned on the price floor,
and a book at zero cannot carry a bid below it -- measured across a session, the
options show a bid 14-16% of the time and an ask 100% of the time, which is what
a worthless option is *supposed* to look like. The binary on the same underlying
is also worth zero but stays two-sided, because its range is 100 ticks rather
than 18,800, so "near zero" still leaves room to quote.

So the asset class works; the demo contracts are badly struck. The fix is a
**strike ladder** spanning the plausible range, as a real exchange lists,
rather than a single pair placed either side of the settlement value. Some
strikes are then near the money and liquid, and parity becomes testable on
those. Choosing strikes near where the underlying already trades is standard
listing practice; choosing them near where it *settles* would be leaking the
answer, and is not what this means.

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
