# Arena Markets

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-745%20passing-2ea44f?style=flat)
![Asset classes](https://img.shields.io/badge/asset%20classes-9-0A0A0A?style=flat)
![Collateral](https://img.shields.io/badge/collateral-exact%2C%20not%20modelled-8b5cf6?style=flat)
![Determinism](https://img.shields.io/badge/replay-bit%20identical-0ea5e9?style=flat)

> A complete electronic exchange, built to answer one question with evidence rather than opinion: **does a market aggregate dispersed information better than the agents trading inside it?** Nine asset classes clear against a real settlement source, every position is collateralised by arithmetic instead of a risk model, and money conservation is exactly zero rather than approximately zero.

The exchange is finished and runs end to end: a discrete-event kernel with per-agent latency, a price-time matching engine carrying the order types real venues actually list, opening and closing auctions, circuit breakers, a clearing house, settlement against Brawl Stars battle statistics, and a browser front end any user can log into and trade on.

The research result is a negative. **The market does not beat its own agents.** It reproduces an unweighted average of their beliefs, and loses by a factor of 2.2 to those same beliefs weighted by evidence. A logarithmic scoring rule with none of an order book's structural limits lands in the identical place (p = 0.97), which rules out the explanation everyone reaches for first.

The headline finding is a negative, and the [errata](#what-building-it-corrected) are the most useful part of the repository.

**No real money and no real securities.** The underlyings are public game statistics, every participant is simulated, and nothing here connects to a venue.

---

## Highlights

- **Collateral is arithmetic, not a risk model.** Every instrument settles as a known function of one bounded scalar, so a portfolio's worst case is the minimum of a piecewise-linear function of a single bounded variable. That minimum sits at an endpoint or a kink, every kink is known in advance, and there are a handful of them. Evaluating each is not an approximation of the answer, it is the answer. Measured on real packages: **100% of collateral released on a conversion, 99% on a vertical spread, 100% on a share against its weekly strip.** See [netting.py](python/arena/portfolio/netting.py).
- **Money conserves exactly.** All cash is integer minor units at a scale of 1,000,000, so `conservation_check()` returns integer zero after every trade, fee, auction and settlement, in every test and every experiment trial. Not a tolerance. Zero.
- **The market ties the simple mean and loses to precision weighting.** 200 paired trials scored against a *known* true probability, which no field study can do. Squared error to truth: market **0.03108**, simple mean 0.02957 (p = 0.59, indistinguishable), precision-weighted 0.01436 (**market 2.2x worse**, p < 0.0001), best agent chosen with hindsight 0.00272. See [Findings](#findings).
- **It is not the order book.** The obvious inference from Experiment 1 was that a limit order book aggregates by capital because it needs a counterparty for every trade. Experiment 2 built an LMSR venue, which always quotes and needs no counterparty, and it lands in the same place. That inference was wrong, and it is left standing in the write-up rather than quietly rewritten.
- **Concentrating information makes the market worse.** Holding the total fixed at 10,319 battles and moving it all into a single trader raises market error **2.7x**, because one agent has one balance sheet. Dispersed information reaches the price better than concentrated information, which is the opposite of the usual intuition about an informed monopolist.
- **Nine asset classes on one matching engine.** Futures, binaries, calls, puts, calendar spreads, an index, commodities, equities and a volatility contract. 28 listed instruments, all sharing one collateral rule, because they all reduce to a bounded payoff function.

---

## Findings

### Experiment 1: the market against its own agents

Eight informed agents see log-spaced battle counts from 50 to 5,000, form Beta posteriors, and forecast the Beta-Binomial tail, which is exactly Bayes-optimal for what each has seen. They trade on the real matching engine against a market maker and eight noise traders, with heterogeneous latency. Paired by trial, Benjamini-Hochberg corrected across the four comparisons.

| Baseline | Baseline error | Market minus baseline | 95% CI | p (adj) | Verdict |
|---|---|---|---|---|---|
| Best single agent (hindsight) | 0.00272 | **+0.02836** | [+0.023, +0.034] | <0.0001 | market loses |
| Simple mean | 0.02957 | +0.00151 | [-0.002, +0.006] | 0.59 | **no difference** |
| Precision-weighted | 0.01436 | **+0.01672** | [+0.012, +0.022] | <0.0001 | market loses |
| Extremized log-odds | 0.02980 | +0.00128 | [-0.003, +0.006] | 0.59 | **no difference** |

Three ablations rule out the easy explanations. Removing noise traders entirely changes nothing (0.02818, every comparison unchanged). Raising position limits five-fold changes nothing (0.03152). Error by session length is flat from 300 seconds onward, so the market has converged and is not short of time.

What survives: every agent carries the same base size, the same position limit and the same cash, so the 50-battle agent pushes the price about as hard as the 5,000-battle agent. **The market has no channel through which evidence quality reaches the price.** The precision-weighted baseline has exactly that channel by construction, and wins with it.

The market is healthy while it loses, which matters, because otherwise the result would be a fact about a broken simulation: 2,360 trades per trial, two-sided 99.9% of the time, reliability 0.0098, conservation exact in every trial.

### Experiment 2: is it the mechanism?

An LMSR venue has none of the limitations blamed in Experiment 1. It always quotes, it needs no counterparty, and it has no market maker anchoring price to its own reference. It reaches the same answer, p = 0.97. The limitation is not the order book. Full write-up in [experiments/information_aggregation/](experiments/information_aggregation/README.md).

---

## What building it corrected

Fourteen defects found by running the code against itself rather than by reading it. Every one of them made the market look healthier than it was, which is the pattern worth internalising. Several were invisible on any single instrument and appeared only when two were compared.

1. **A mark price that ignored the standing book.** `mark()` returned the last trade, so an aggressive buy that swept every offer on the book reported the price *falling*. It is clamped into the touch now, and skips crossed books.
2. **Two units, one number, no error.** `settles_at` was stored in ticks while every price around it was in contract units. A future marking at 4,663 reported a settlement of 18,677, and nothing raised. Found by opening the web page and reading it, not by any of the 700 tests.
3. **Every agent held one view per contract instead of one per underlying.** The same trader valued the 4,650 call at 119.03 and the 4,600 call at 36.67, which is free money at settlement. Fixed with per-underlying keys and common random numbers across the chain.
4. **Unfilled market-on-open orders survived the auction** at their sentinel prices. Trades printed at -4,611,686,018,427,387,904 and the fee ledger reached 4.8e22. The fix needed both a book removal and a terminal status, because a tombstone that is invisible is still matchable.
5. **The venue paid maker rebates on both sides of its own auction,** every side of which it was itself. Exchange revenue came out at -1,251.
6. **Order ids are unique per book, not per venue.** An agent tracking its working orders in a flat dictionary believed it had 8 outstanding and had 123 in a single book. Keyed by `(symbol, order_id)` now.
7. **Auction fills were never delivered to the agents that made them.** 362 of 494 position pairs disagreed between the venue's books and the agents' own. Routing the uncross through the same dispatch path as continuous trading took that to 2.
8. **Fixing that revealed an accidental rate limiter.** The order-tracking bug had been suppressing requotes, and without it the market emitted 1.6M events per simulated minute. Requote-on-change plus conflated market data brought it to 317K, and the suite from 38.8s to 11.0s.
9. **Conflation that dropped updates instead of delaying them** deadlocked the market. A trial that traded 2,039 times traded 0. Hold-and-flush on the next wakeup is the whole difference between conflation and data loss.
10. **Collaring limit orders locked the book.** A price collar protects an order that did not name a price, and a limit order has already named one. Applying it to both produced 2,492 limit states in five minutes and a future quoting 9,267 against a settlement of 4,669.
11. **A volatility estimate that never decayed without prints.** Makers quoted the 4,700 call at 153 against an intrinsic value of 100 for six straight minutes, accumulated to their position limits, and the bid vanished at 17 of 19 sampled moments. Volatility now decays on a 60-second half-life whether or not anything trades.
12. **A kill switch disabled by its own rate limiter.** The control you reach for when a participant is running away was itself rate limited, and a runaway participant is at its message cap by definition. It also reported success while doing nothing at all.
13. **A quoting rule that turned spread width into price.** Clamping the *centre* of a two-sided quote into the settlement range keeps the bid legal and makes the mid a function of the half-spread, which widens with inventory. Two calls both worth nothing marked at 1.63 and 68.38, purely because one book carried a heavier position. That is an inverted chain, manufactured by the quoting rule rather than by any view.
14. **A differential harness caught an iceberg publishing its original size.** The visible slice was computed when the order was constructed, so a partially filled resting order kept advertising depth it no longer had.

---

## Architecture

Two languages with one division of labour: Python for research, agents, data and experiments, C++ reserved for the exchange kernel and performance-critical simulation. The Python exchange is the reference implementation, and the specification any port has to match.

| Layer | Module | What it holds |
|---|---|---|
| Time | [sim/](python/arena/sim/) | Discrete-event kernel in integer nanoseconds, per-agent latency, FIFO per ordered pair. Same seed, same bytes. |
| Matching | [exchange/](python/arena/exchange/) | Price-time book. Limit, market, IOC, FOK, stop, stop-limit, iceberg, pegged, minimum-quantity, post-only. Stop cascades are iterative and depth-bounded. |
| Contracts | [contracts/](python/arena/contracts/) | Payoff algebra over a bounded underlying: linear, call, put, binary. The bounded range is what makes collateral exact. |
| Venue | [market/](python/arena/market/) | Listing rules, tiered tick tables, maker-taker fees, opening and closing auctions, LULD circuit breakers, a participant kill switch, message throttles. |
| Clearing | [portfolio/](python/arena/portfolio/) | Integer minor units, per-account cash and positions, exact portfolio netting evaluated at kinks. |
| Agents | [agents/](python/arena/agents/) | Market makers, an option surface maker, Bayesian informed traders, noise, flow, and an arbitrageur that enforces parity and index identities. |
| Settlement | [settlement/](python/arena/settlement/) | The oracle boundary, deterministic settlement, and the proof that cash and positions both close out to zero. |
| World | [worlds/brawl/](python/arena/worlds/brawl/) | Stratified win-rate estimation with shrinkage, battle volume, stratum dispersion. Every metric is bounded by construction. |
| Research | [research/](python/arena/research/) | Trial harness, manifests carrying seeds and code digests, aggregation baselines, stylized-fact diagnostics. |
| Front end | [dashboard/](dashboard/) | The exchange as a website: accounts, order entry, depth, tape, positions and settlement, in vanilla ES modules with no build step. |

---

## Running it

```bash
pip install -e .
```

```bash
python -m pytest -q
```

The exchange as a website, with signed session cookies and a per-user account:

```bash
python -m dashboard.server
```

The experiment, which writes a manifest with seeds and code digests so that a run reproduces byte for byte:

```bash
python experiments/information_aggregation/run.py --quick
```

---

## Design commitments

These are the rules the code was held to, and most of the errata above are cases where breaking one produced a market that looked right.

- **No hardcoding.** Nothing here nudges a price, a spread or a distribution toward what a real market would show. Every observed regularity has to fall out of the mechanism or not appear at all. A market tuned to look real has stopped being evidence about anything.
- **Collateral is exact or it is not collateral.** No VaR, no correlation estimates, no margin models. Positions on different underlyings do not net, because netting them would require a correlation, and a correlation is an estimate. That is not a limitation to be fixed later, it is the line between arithmetic and modelling.
- **Determinism is a test, not an aspiration.** The same seed and the same manifest give a bit-identical result digest, which is what makes a paired ablation a comparison rather than a pair of anecdotes.
- **Negative results get reported.** The information-aggregation result was pre-committed in the plan before the run, and the wrong inference drawn from Experiment 1 is left in the write-up beside the experiment that refuted it.

## What is deliberately absent

- **Margin and leverage.** Each of them replaces an exact subtraction with an estimate of what a position is worth before it settles. That trade is available and has not been taken.
- **Multiple venues and order routing.** One venue, so that price formation is the only thing being measured.
- **Sophisticated market-making and buy-side firms.** Last on the list on purpose. Venue correctness first and participants second, because a clever agent on a broken exchange measures the exchange.

## Documentation

- [docs/PLAN.md](docs/PLAN.md), the phase plan and what each phase had to prove before it closed
- [docs/GAPS.md](docs/GAPS.md), an audit of this exchange against real venue mechanics, including what is still missing
- [docs/ECONOMY.md](docs/ECONOMY.md), where value comes from and why every contract is bounded
- [experiments/information_aggregation/](experiments/information_aggregation/README.md), the full experimental write-up
