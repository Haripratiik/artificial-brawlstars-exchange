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
2. ~~Fees, auctions, halts and the scoring-rule venue are built and never
   run.~~ **All running now**, and switching them on found four bugs in tested
   code and cost the market half its accuracy. See below.
3. ~~No auth.~~ **Every browser is its own trader now** -- its own account,
   balance, blotter and working orders, and it cannot cancel anyone else's.
   There are still no passwords, and that is stated rather than implied; see
   below.
4. ~~Liquidity does not replenish, and one maker is the whole other side.~~
   **Fixed by having three of them**, differing in spread, quote size and
   inventory limit -- identical makers would be one maker with three times the
   balance sheet. After the same 60% sweep the spread now comes back from 1.25
   to 2.50 rather than to 24.50, and `test_a_large_order_moves_the_price`
   asserts the recovery it used to assert the absence of. What it replaced is
   worth keeping, because the contrast is the finding: against a *single*
   maker the same sweep was absorbed **89%** by it, left it short about 1,150
   lots and past the point its collateral allowed a quote, and the spread went
   from 0.50 to 1,021.00 and was **exactly** 1,021.00 three minutes later. Not
   a slow repair: no repair. A real book mends in milliseconds because the
   maker that got run over is one of several; there it was the only one.
5. ~~Cancel rate is ~60%, not >90%.~~ **The measurement was of a bug.** Most
   quotes were never cancelled at all, because agents had lost track of them;
   see below. What replaced it is a maker that leaves a quote alone when it has
   not moved, which is what a real maker does and what a cancel-to-trade ratio
   is really measuring.
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

## A flow of information, and the two bugs it uncovered

Every agent used to receive its whole sample at `t=0`. The market therefore had
an information *stock*: it converged within seconds and then nothing could move
it, because there was nothing left to arrive. Realised dispersion of
`SPIKE_WR_FUT` over ten minutes was **14.6** on a price near 4,670, so options
were worth their intrinsic value and nothing more.

Evidence now arrives over the session as ordinary Gaussian updating, written in
units of precision because that is the unit evidence comes in. With a prior
`N(mu, 1/t0)` and evidence of accumulated precision `te` centred on the truth,

    m = (t0 * mu + te * truth + W(te)) / (t0 + te)

for a standard Brownian motion `W`, with posterior variance exactly
`1 / (t0 + te)`. The Brownian term is what makes it a **martingale**: an agent's
view at any moment is its own best guess at its later view, so it never drifts
predictably and cannot be anticipated by anything except better information.
Redrawing a view each wakeup would have been far simpler and would have
produced a noise trader wearing a posterior.

**Beliefs start at the pre-window level, not at the truth.** SPIKE ran at 0.4839
for the twelve weeks before the contract window and settles at 0.4669, so a
market that opens on history opens wrong and has something to discover. Starting
from the truth and adding noise makes every agent unbiased from the first
instant: the market opens at the answer with a wide spread and merely tightens.
The prior is lookahead-free by construction -- the window it measures ends where
the contract's window begins.

**Six informed traders, log-spaced in precision, rather than two.** Two is not a
population, it is an anecdote, and it had a measurable consequence: both ran
into their position limits about a minute in and the price stopped there.
`fund-sharp` believed 4,687 against a true 4,669, was short its full 900 lots,
and could do nothing while the market printed 5,005 -- with the makers holding
capacity for 1,300 more.

### What it cost, on six paired seeds

| | stock | flow |
|---|---|---|
| mean pricing error, end of session | 10.5% of range | 12.7% |
| late-session dispersion of the future | 11.0 | 278.6 |

The accuracy difference is **+2.20%, 95% interval [-0.21%, +4.61%]** -- leaning
worse and not distinguishable from zero. The dispersion difference is a factor
of twenty-five, and it is the point: an option is written on how much something
moves, and under a stock of information the answer was "it does not". Two of the
six seeds show a dispersion of exactly zero under flow, which is not stillness
but a halted symbol, and that is worth saying rather than averaging away.

## Two bugs that only a moving market revealed

**Order ids are unique within a book, not across the exchange.** Every agent
keyed its working orders by id alone. There is one matching engine per symbol,
so id 5 exists on all twenty-six contracts at once: a new acknowledgement
overwrote the entry for a different symbol's order, and completing one order put
its id in the "already finished" set, after which another book's order with the
same id was discarded as a late duplicate and never cancelled again. Measured
after two minutes, `mm-3` believed it had **8 working orders across the whole
exchange** and had **123 resting in one book**. The stale ones formed a wall the
price could not move through.

It also explains the cancel-rate gap this document used to record. The ratio was
low because most quotes were never cancelled, not because nothing requoted fast
enough.

**An auction filled orders and told nobody.** `SessionOperator` called
`venue.uncross()` directly, which moved cash and positions in the ledger and
sent not one participant a fill. Measured after two minutes: **362 of 494**
(agent, symbol) pairs had an agent's belief about its own position disagreeing
with the venue's record -- `mm-1` believed +807 where the ledger said -63. A
market maker skewing its quotes off an inventory that is not its inventory is
not managing risk, it is guessing. Uncrossing now goes through the venue agent,
which owns the mailbox. Afterwards: **2 of 494**, and both are fills genuinely
still in flight to agents 45ms away.

### And the performance problem the first bug was hiding

With agents finally tracking their orders, they started actually cancelling
them: **1.6 million events per simulated minute**. The leak had been acting as
an accidental rate limiter. Two fixes, both of them things real participants do:

- **A quote that has not moved is left alone.** Cancelling and replacing an
  order at the price it is already at surrenders queue priority for nothing and
  pays a message for the privilege.
- **Market data is conflated to each agent's own decision cadence.** An agent
  cannot act on a book update that arrives between two of its wakeups. Real
  feeds conflate for this reason and unconflated ones are a product you pay
  extra for. Trades are *not* conflated: the tape is the record of what
  happened, and the maker's anchor is an average over it.

Conflation has to *delay*, not drop, and the first version dropped. A maker that
has not moved its quote sends no order, so nothing publishes; if the one update
that announced the book was thrown away, every agent waiting for a price waited
forever. An experiment trial went from 2,039 trades to **zero**. Held and
flushed on a wakeup: **1.64M events down to 317K**, and 38.8s of wall clock down
to 11.0s per simulated minute.

## A collar, and three things it took to get right

A resting bid at **0.25** was filled on a contract worth 4,700. A market order
names no price, so nothing stopped it walking a thin book to the floor, and the
circuit breaker then halted a symbol whose damage was already done. Real venues
collar unpriced orders, and now so does this one: a market order stops at the
edge of the band and cancels whatever is left.

Getting there took two wrong turns worth recording, because both looked more
principled than the answer.

**Collaring limit orders too.** They slid to the band's edge, the band later
moved away from them, and the book locked -- bid above offer, neither permitted
to trade, and nothing in continuous trading able to clear it. On that version:
**2,492 limit states in five minutes** and a future marking at 9,267 against a
settlement of 4,669. A trader who says 30,000 has said 30,000; the collar is for
orders that said nothing.

**A reference that fell back to the last cleared price.** A symbol that goes
quiet keeps a reference from whenever it last printed, the market walks away
from it, and every unpriced order is collared against a price that no longer
exists. Measured: the band on `SPIKE_WR_FUT` sat at 6,392 while the book quoted
4,760, a third of the way across the contract's range, and no market order could
trade at all. The fallback is now the quote -- weaker evidence than a trade,
which is why it is the fallback, and much better evidence than a price from a
minute ago.

**A limit state triggered by a print.** Once trades cannot leave the band, a
rule written in terms of prints outside it can never fire. A symbol is in a
limit state when the best bid or offer is *at* a band -- interest that wants to
be somewhere the venue will not let it go -- which is what the rule says and
what it now checks.

## Running the machinery, and what that found

Fees, an opening call auction, a circuit breaker and a scoring-rule venue were
all written, all tested in isolation, and all defaulted off. Turning them on
found four bugs inside an hour, every one of them in code that had tests.

**A market-on-open order that did not fill stayed in the book.** It rests at a
sentinel price so that it crosses every candidate the auction considers, which
is the whole point of it -- and makes it the best offer in a continuous book by
a margin of 2^61. The first order afterwards matched it *at that price*: trades
printed at -4,611,686,018,427,387,904, the mark went to zero, and the venue
billed 4.8e22 in fees. A market order is an instruction about the auction it
was entered for; once that has cleared there is no price it was ever willing to
pay.

**Cancelling it back out did not work either.** `Book.remove` reduces a level's
total and leaves the order in the queue as a tombstone; what makes the matcher
skip it is its *status* being terminal. The first fix removed without marking,
so the order vanished from the depth and from every diagnostic that reads
resting orders while remaining perfectly tradeable. Invisible and matchable is
the worst of both, and it is why the sentinel prints survived two attempts at
fixing them.

**`best_bid` and `best_ask` reported sentinel levels as prices.** An order that
names no price cannot be the best price. The levels still carry them, because
the auction has to count that interest to know what would trade.

**A venue that pays a maker rebate on both sides of its own auction loses money
on every cross.** An auction has no aggressor, so billing every fill at the
maker rate looked right and was: twenty-six opening auctions took venue revenue
to **minus 1,251**. Exchanges charge for cross executions rather than paying for
them, so an auction fill now pays the taker rate on both sides.

### Three things that had to change shape

**The breaker needed a second clock.** The calendar decides whether a contract
has expired; elapsed simulated time decides how long a symbol has been outside
its band. Sharing one meant the limit-state timer never advanced -- 241
excursions in three minutes and not one pause.

**The band is a fraction of what a contract can be *worth*, not of what it
costs.** A percentage of price is what equity venues use and it is meaningless
for a bounded claim: a binary trading at fifty cents gets two and a half cents
of room, which any ordinary change of opinion breaks. Under a price-percentage
band the breaker paused every event contract on the exchange repeatedly while
never once touching the future, whose 5% is sixteen standard deviations. Every
contract here has a settlement range, which is the same reasoning that already
governs the maker's inventory skew.

**Its time constants are ratios, not durations.** Limit up-limit down uses a
fifteen-second limit state, a five-minute pause and a five-minute trailing
reference against a six-and-a-half-hour session. Used literally here, where a
session lasts minutes and does a day of price discovery in the first one, the
reference is stale for the entire session and the breaker spends its time
policing the walk to fair value: twelve of twenty-six symbols halted at once.
Scaled by the same fraction of the session, the reference keeps up.

### The opening auction, and who brings interest to it

Withdrawing the makers from the call was tried, on sound reasoning -- a maker
that turns up with a mid-range guess makes the guess the official opening
price, and the market's walk away from it then trips the breaker. It was worse.
With two informed agents and a crowd of random market orders, an auction with
no maker in it cleared `SPIKE_WR_FUT` at **9,377** against a fair value of
4,669. A mediocre anchored open beats a wild unanchored one.

What was genuinely missing is that every agent here reacts to a price, so with
nobody posting first the auction collected nothing. The informed agents now
bring two-sided interest at their own valuation, widened by their own
uncertainty, which is what an opening auction is for.

### What it cost: +4.00 percentage points of pricing error

Six paired seeds, common random numbers, ten minutes of market each. The metric
is mean |mark - settlement| across all 26 contracts, as a share of each
contract's range.

| seed | bare | operating | difference |
|---|---|---|---|
| 7 | 4.88% | 9.56% | +4.68% |
| 11 | 6.52% | 8.76% | +2.25% |
| 19 | 6.06% | 8.63% | +2.57% |
| 23 | 5.98% | 7.52% | +1.54% |
| 41 | 4.59% | 11.70% | +7.11% |
| 97 | 6.64% | 12.48% | +5.85% |

**Mean +4.00%, standard error 0.91%, 95% interval [+2.22%, +5.78%].** Same
direction on all six. Running an exchange the way exchanges are run roughly
doubles the pricing error here: a halt stops price discovery by design, a fee
widens the band inside which a mispricing is not worth correcting, and an
auction that opens away from fair value gives the market somewhere worse to
start from. None of that is an argument for switching them off -- it is what
these mechanisms cost, and the point of building them was to be able to say so
with a number.

Single-mechanism ablations do not isolate a cause, which is worth stating
rather than hiding: removing the breaker, the auction or two of the three
makers each moved the error by less than the run-to-run spread, and removing
the auction made it *worse*. The cost is of the combination.

## Who is at the browser

Every connection used to trade one account. Two tabs shared a balance and a
blotter and either could cancel the other's working orders, which on a venue
whose premise is people trading against each other is the premise not holding.

A signed session cookie now carries an account id and a display name,
authenticated by an HMAC over both. The browser can read what it is and cannot
write itself a different account. The account is a real participant: its own
`HumanAgent`, its own opening capital -- the amount a person can read a profit
against, not the bots' forty million -- and the same latency to the exchange as
anyone else at a browser.

Joining happens on arrival rather than from a pool of seats. A pool would have
been simpler and would have made "the exchange is full" something that could
happen to a visitor, which is a property of a workaround rather than of an
exchange. `Kernel.join` is the one way an agent may enter a running simulation,
and it is documented as such: a market with a human in it was never
byte-reproducible, since the human acts at wall-clock moments the seed knows
nothing about, so a second human arriving is the same kind of event as the
first one placing an order. Every experiment harness builds its population up
front and never calls it. A test pins the rest -- seating someone mid-session
leaves the tape of everyone else's trading identical, because each agent's
random stream is seeded from its own id.

**There are no passwords, and the page does not pretend there are.** Signing in
means choosing a name, a name is not an identity -- two people called Ada get
two accounts -- and losing the cookie loses the account. That is the right
shape for an exchange whose capital is imaginary, and saying so is the point:
the distance between "signed in" and "authenticated" is exactly the sort of
thing that is comfortable to leave vague. A real one needs credentials to
store, which means password hashes, a reset path, and a way to be wrong about
all of it. None of that is built.

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
