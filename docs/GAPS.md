# Fidelity audit: this exchange against a real venue

What is here, what is deliberately not, and how closely each piece tracks the
mechanics of a production venue. Written so that anyone reading the code knows
exactly which claims are load-bearing.

## The size of the thing

```
dashboard                4,973 lines    the exchange as a website
python/arena/market      3,224          venue, instruments, sessions, live market
python/arena/agents      2,719          participants, market makers, surface maker
python/arena/exchange    2,564          matching engine, order book, order types
python/arena/worlds      1,939          the underlying: stratified estimation, dispersion
python/arena/research    1,364          trial harness, manifests, baselines
collectors               1,251          Supercell crawl, normalisation, store
python/arena/contracts     832          underlying algebra, payoffs, specs
python/arena/sim           759          kernel, latency, messages
tools + experiments      1,226          diagnostics and the experimental runs
python/arena/portfolio     623          positions, accounts, money, netting
python/arena/settlement     398          oracle protocol, settlement engine
                        ------
                        22,085 lines of production code
                        11,867 lines of tests
```

A production venue is six to seven figures of code and clears real money. This
is a research instrument, so the useful question is not size but whether the
mechanics it does implement are correct, and that is what the rest of this
document answers.

## Invariants the venue enforces

Each of these is enforced in code and covered by a test that fails without it.
They are listed here because they are the properties a market has to have
before any measurement taken from it means anything, and each one is invisible
from the outside: a market that violates them still produces plausible numbers.

**No self-matching.** An agent cannot trade against its own resting order. A
market maker quotes both sides and its cancels are in flight while it requotes,
so without this its new bid crosses its own stale offer, and volume, prices,
the tape and every impact figure derived from them measure one participant
talking to itself. Position and P&L both net to zero when that happens, which
is exactly why no number looks wrong. Self-match prevention defaults to
cancel-oldest and is differentially tested against the reference matcher.

**Expiries are enforced.** Trading stops when the observation window closes.
A contract whose outcome is already determined is not tradeable.

**The mark is held inside the touch.** A print is a fact about the past and a
resting order is an offer about the present, so when the book is one-sided the
mark is clamped into whichever side is standing rather than left to the last
trade. Without it, a sweep that clears every offer lets a single lot on the bid
value every open position below a touch that hundreds of lots are standing at.

**One unit, everywhere.** Settlement values, marks and quotes are all in
contract units, and the conversion happens once at the tick boundary. A
settlement rendered in ticks beside a price in contract units is off by the
tick factor and still looks like a number, which is why it is a checked
invariant rather than a convention.

## Venue mechanics, present and absent

Every line here was checked against how a real venue does it, and implemented
or deliberately left out on the stated reasoning.

| | |
|---|---|
| Opening / closing auctions | present. The market opens with a call, and a paused symbol reopens through one |
| Circuit breakers, price bands | present, and they *prevent* trades outside the band as well as pausing after one |
| Trading halts and session states | present: pre-open, continuous, auction, closed |
| Stop and stop-limit orders | present, with cascades measured rather than prevented |
| Iceberg / reserve orders | present, refreshing to the back of the queue |
| Maker/taker fee schedule | present, with a separate auction rate, because a venue that rebates both sides of its own cross pays to open |
| Post-only | present |
| Tiered tick tables | present on one contract, so the rule is exercised rather than merely available |
| Message throttling, kill switch | present. The kill switch bypasses the throttle, because a runaway is at its cap by definition |
| Pegged, MPL orders | present |
| Clearing, novation, a CCP | the part of it that matters here is present: collateral nets across one underlying, exactly. See below |
| Margin, leverage, liquidation | absent **by design**, and it should stay that way while collateral is exact. See below |
| Multiple venues, order routing | absent. The scoring-rule venue is selectable but not simultaneous |

Order types implemented: **limit, market, stop and stop-limit**, with GTC,
IOC, FOK and post-only, and an iceberg display size on anything that rests. A
production venue offers twenty or more.

### Why margin stays absent

Not an omission. Every contract here settles inside a known interval, which is
what makes collateral exact arithmetic rather than a value-at-risk estimate --
a short at 5,100 on a contract bounded by 10,000 can lose at most 4,900 per lot,
and nothing needs to model volatility to know it. Leverage is precisely the
decision to hold *less* than that, which replaces the subtraction with an
estimate and brings with it a liquidation engine, a margin model, and every way
of being wrong about both. It is a different exchange, and worth building
deliberately rather than by degrees.

## What is genuinely present, and correct

Not everything thin is wrong, and these have been checked rather than assumed:

- **Price-time priority matching**, differentially tested against a deliberately
  naive reference implementation across cancel-heavy random streams. That
  harness found a real depth-accounting bug that was corrupting fill-or-kill.
- **Deterministic replay.** Identical command streams produce identical event
  streams, which is the acceptance criterion any port has to meet.
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

1. ~~Validate against stylized facts~~, done, and three of four predictions
   were wrong.
2. ~~Realistic order flow~~, done. Power-law sizes, Hawkes arrivals. The cancel
   rate reaches ~60%, not the >90% of real books; see below.
3. ~~An arbitrageur~~, written, measured, and **off by default**; see below.
4. ~~Fees~~, maker-taker, off by default.
5. ~~Auctions and halts~~, done, off by default.

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

### Still open now, re-measured

Every item above is struck through, so the list was carrying no information.
These replace it. All six were measured this session and each names how.

1. **There is no C++ kernel.** Zero `.cpp`, `.hpp` or `CMakeLists.txt` files in
   the repository, against a stated stack of Python for research and C++ for
   the exchange. `tests/test_differential.py` was built as its acceptance test
   and has been hardened over roughly 1.2 million fuzzed commands, so the
   specification exists and the work is the port. This is the largest single
   item here and the one a reader will notice first.

2. **No real data has been collected.** `data/raw` holds zero files. The
   collector is written and needs an API key bound to a static IP, which is not
   something the codebase can supply itself.

3. **Nothing relists, and it is blocked on the item above rather than on
   effort.** Contracts settle correctly now, so the market drains as they
   expire: at the default 600 simulated seconds to the trading day, the
   four-week window runs out after about 4.7 hours. Listing a new series needs
   data for the new window, and the fixture spans 2026-06-17 to 2026-09-30
   against a contract window ending 2026-09-28. Two days. Recycling the fixture
   to keep the market populated would be fabricating evidence, which is the one
   thing the no-hardcoding rule exists to forbid, so this waits for the
   collector rather than being worked around.

4. **The netting flag relaxes the gate without changing the bill.**
   `Venue._portfolio_affords` runs as a fallback when the per-contract check
   refuses an order and asks the netted question, but `Account.apply_fill` keys
   collateral by symbol and always posts the gross figure for that one
   contract. Measured on seed 7 over 300 simulated seconds with it on: the
   fallback ran 2,535 times and admitted 1,301 orders the per-contract check
   had refused, and total posted collateral rose from 321,704,201 to
   341,327,876, because more positions were admitted and every one was still
   charged in full. So the released-capital figures below describe `worst_case`
   as a function, not anything this venue charges. Making the flag sound means
   posting per netting group so the gate and the ledger answer the same
   question, and that moves every capital number the project reports. Off by
   default, documented at the flag, and pinned by two tests in
   `test_netting.py`.

5. **A working order reserves nothing, so affordability can go stale.** The
   entry check is per symbol and considers positions only, so orders resting
   across several books are each individually affordable until several of them
   fill. Measured across four seeds at 300 simulated seconds: one of
   twenty-four accounts underwater on two of the four, worst 8,358 on a 40
   million account, which is 0.021 percent of its own capital, with
   conservation exactly zero throughout. `test_no_account_is_over_committed`
   asserts the invariant strictly and passes only because its fixture is short.
   The fee the check was omitting is now counted; the remaining cause is the
   absence of reservation, and closing it properly refuses orders that are
   accepted today.

6. **The option surface is too cheap in volatility.** Implied dispersion sits
   at 0.155 of the remaining distance to settlement, so the maker ends short
   calls and puts on the same strikes at its position limit. That is a
   one-directional pricing error rather than a risk outcome, and the existing
   `delta_limit` is structurally blind to it because a short straddle has
   roughly zero delta. Blocked on the same two-clocks problem that once stopped
   contracts expiring at all: the agent cannot compute a settlement horizon, so
   it cannot scale a one-step forecast error by its square root.

## Netting, which is the part of a clearing house that matters here

A CCP does two things. It novates -- becoming buyer to every seller so nobody
is exposed to anyone's default -- and it nets. The first is **already true and
by construction**: collateral here is exact, so no participant can ever owe
more than it posted, and there is no default to be protected from. Building
novation would be ceremony.

The second was missing and mattered. Collateral was charged per contract, so an
account holding a long future, a long put and a short call at one strike posted
against all three worst cases at once -- for a package that put-call parity says
cannot lose anything at any level. The arbitrageur felt it hardest, since
holding offsetting packages is its entire business.

The usual objection to portfolio margining is that it means a risk *model*, a
model is an estimate, and an estimate is exactly what this collateral is not.
That objection does not apply, for the same reason single-contract collateral
is exact: every instrument settles as a known function of a bounded scalar, so
the worst case of a portfolio is

    min over level in [0, 1] of  sum_i quantity_i * payoff_i(level)

the minimum of a piecewise-linear function of one bounded variable. It is
attained at an endpoint or a kink, every kink is known in advance -- a strike, a
threshold -- and there are a handful. Evaluating each is not an approximation of
the answer; it is the answer.

| portfolio | gross | net | released |
|---|---|---|---|
| a lone future | 46,700 | 46,700 | 0% |
| future against the same call | 100,000 | 46,000 | 54% |
| a conversion: future, long put, short call | 100,000 | **0** | 100% |
| a vertical spread | 54,000 | 500 | 99% |
| a share against its four weekly legs | 39,990 | **0** | 100% |

The zeros are the point: a conversion and a strip are riskless by identity, and
an exchange that charges for them is charging for arithmetic it can do itself.
The vertical's residual 500 is exactly the fifty-point gap between its strikes
on ten lots.

**Only same-underlying positions net.** A future on SPIKE and a future on CROW
are functions of different numbers, and netting them would need a correlation --
which would be an estimate, and the whole guarantee would be gone. That is not
a limitation to be lifted later; it is the line between arithmetic and
modelling.

Off by default so every published measurement keeps meaning what it meant. With
it on, over five minutes on seed 41, the arbitrageur attempted 1,094 trades
against 944 and was starved of capital on 281 occasions against 374.

## A ninth asset class: a claim on the second moment

`SPIKE_DISP` and `CROW_DISP` settle on how *unevenly* a Brawler performs across
the maps and modes it plays, not on how well. The metric walks exactly the same
strata as `adjusted_win_rate`, with the same shrinkage and the same coverage
gate; the only difference is which moment comes out at the end.

That makes it a different kind of claim rather than a different number. A
Brawler winning everywhere at 0.52 and one winning at 0.70 on half the maps and
0.34 on the other half have the same adjusted win rate and are not remotely the
same thing to own. Measured on the fixture, SPIKE and CROW settle within 1.2% of
each other on level and **68% apart** on dispersion -- 0.0535 against 0.0318.
Nothing else on the exchange could express that.

It joins on the same terms as everything else: a rate lives in [0, 1], so the
standard deviation of a set of rates cannot exceed 0.5, and collateral stays
arithmetic. Imputed strata count toward it and pull it *down*, which is the
honest direction -- an unmeasured cell is not evidence of variability, and a
metric that let thin data widen the spread would pay out for having none.

## Risk controls, and one that disabled the other

**Message throttling.** A cap on commands per participant per second, on a
rolling window so a burst cannot be split across a boundary and counted as two
quiet ones. Not politeness: an algorithm that malfunctions emits orders faster
than anything downstream can process, and the venue with no limit is the one
that goes down with it.

**A kill switch.** Pulls everything a participant has working and refuses it
more. Deliberately blunt, because the point of a kill switch is that it is the
one control that always works. A stopped participant may still *cancel* --
refusing that too would trap it in the orders it already has, which is the
opposite of what stopping it is for.

The first version had the throttle applied to the kill switch's own cancels. A
runaway is at its message cap by definition at the moment someone reaches for
the switch, so every cancel came back `RATE_LIMITED` while `kill` reported the
symbols as pulled and both orders stood in the book. Venue-originated commands
now bypass the participant's allowance. It was found by writing the tests, not
by running the market.

## Tiered tick tables

A tick has two jobs that pull against each other. Too fine and queue priority
is worthless -- anyone can step in front of a resting order for a hundredth of a
penny, so nobody posts size. Too coarse and the spread cannot narrow to what
the market knows. The resolution that is right at a price of 4 is wrong at
4,000.

`PIPER_WR_FUT` carries the one tiered table -- a quarter point below 4,000, a
whole point above -- so the rule is exercised rather than merely available. The
base tick stays the unit everything is represented in, because the engine
matches on integer ticks and a variable unit would make a tick index mean
different prices at different levels. The table is a rule about which prices
may be *quoted*, enforced by the venue, and agents snap their quotes onto it
conservatively: a bid rounds down and an offer rounds up, so obeying the grid
never makes a quote more aggressive than its author intended.

## Orders that hide, and orders that wait

**Iceberg orders** trade visibility for queue priority. Size is information: an
order for ten thousand lots announces what you are doing before you have done
any of it, so it is worked in slices, and each refreshed slice goes to the back
of its level behind everything that arrived while the last one was working.
That cost is what makes an iceberg a trade-off rather than simply a better
order -- a venue that refreshed in place would let one participant hold the
front of a queue indefinitely while showing a single lot.

The depth publishes the slice and the resting quantity counts the whole thing,
and both are true: one is what the market can see, the other is what is there.

**Stop orders** wait for a price before they exist, and are held off the book
while they wait. Publishing one would say exactly where the market has to go to
set off a cascade, which is the single fact its owner most wants kept quiet.

A triggered stop prints, which can trigger more stops. Nothing here prevents
that -- being able to *measure* a cascade is most of the reason to model stops
at all -- and `MatchingEngine.cascade_depth` records how many rounds each one
ran for. What is prevented is a cascade that never ends, which would be a bug
in the model rather than an event in a market.

Two things had to be got right for a stop to be a stop rather than a leak:

- **A triggered plain stop becomes a market order, which is immediate-or-cancel
  by construction here.** Carrying the stop's own time-in-force through handed
  the engine a GTC market order, which it refuses -- so the stop vanished on
  being triggered, with nothing in the tape to say so.
- **Collateral is reserved from the moment a stop is parked.** The engine
  releases a triggered stop inside its own matching, which never passes back
  through the venue's affordability check, so an unreserved stop would create a
  position the account had never been asked to cover. It acknowledges at its
  limit if it has one and its trigger otherwise, and the venue reserves against
  that. A plain stop can still fill through its trigger in a fast market --
  which is the risk its owner takes in reality -- and the collar on unpriced
  orders bounds how far through.

**The differential harness caught the bug in this.** A visible slice was
computed once when an order was constructed rather than when it joined a level,
so an order that partially filled on the way in and then rested published its
*original* size as depth. The reference matcher's book was one lot shallower
than the engine's, and that one lot was the whole of it. Icebergs themselves
are outside the harness -- the deliberately naive reference does not model them
-- so the guarantee it still gives is the one that matters for the C++ port:
ordinary matching is unchanged.

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
that it measures volume **in the canonical corpus, not in the game**, a wider
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
