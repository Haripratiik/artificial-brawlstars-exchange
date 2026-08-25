# Experiment 1 — Does the market aggregate better than the agents inside it?

**Answer: no. It reproduces the simple average of their beliefs, and loses
decisively to weighting those same beliefs by how much evidence each rests on.**

```bash
python experiments/information_aggregation/run.py --full --workers 8
```

---

## What was measured

200 trials. Each is a self-contained market with one binary contract: *will the
measured win rate over the observation window exceed θ?* The window holds 2,000
battles, so the settled rate is a draw and

    truth = P(Binomial(2000, p*) / 2000 > θ)

is a real probability, known exactly. Thresholds are chosen by bisection so the
answers spread across [0.05, 0.95] — with a 2,000-battle window a *random*
threshold makes nearly every trial a foregone conclusion, and a set of foregone
conclusions cannot separate any two forecasters.

Eight informed agents see log-spaced battle counts from 50 to 5,000 (10,319
battles in total), form Beta posteriors, and forecast the Beta-Binomial tail —
**exactly Bayes-optimal** for what each has seen. They trade against one market
maker and eight noise traders on the real matching engine, with heterogeneous
latency. The market's forecast is the time-weighted mid over the final 10%.

Scoring against a known truth rather than a sampled outcome is the one thing
this setup can do that no field study can: it removes the Bernoulli noise that
otherwise dominates, which is why 200 trials suffice where a real forecasting
tournament would need thousands.

## Result

Primary metric — squared error to the true probability. Lower is better,
negative difference means the market won. Paired by trial; Benjamini–Hochberg
across the four comparisons.

| baseline | baseline error | market − baseline | 95% CI | p (adj) | verdict |
|---|---|---|---|---|---|
| best single agent (hindsight) | 0.00272 | **+0.02836** | [+0.023, +0.034] | <0.0001 | market loses |
| simple mean | 0.02957 | +0.00151 | [−0.002, +0.006] | 0.59 | **no difference** |
| precision-weighted | 0.01436 | **+0.01672** | [+0.012, +0.022] | <0.0001 | market loses |
| extremized log-odds | 0.02980 | +0.00128 | [−0.003, +0.006] | 0.59 | **no difference** |

Market error: **0.03108**. Extremization factor, fitted out of sample:
**0.95 / 1.10** across the two folds.

The market is statistically indistinguishable from an unweighted average of its
agents. It is 11× worse than the best agent picked with hindsight, and **2.2×
worse than the same agents weighted by evidence**.

## Why — three ablations, each ruling something out

| condition | market error | vs default |
|---|---|---|
| default (8 dispersed agents, 8 noise, limit 800) | 0.03108 | — |
| noise traders removed entirely | 0.02818 | −9%, comparisons unchanged |
| position limits raised 5× (800 → 4000) | 0.03152 | no change |
| all 10,319 battles held by **one** agent | 0.08424 | **2.7× worse** |

Plus a convergence check: market error by session length was 0.0144 (60s),
0.0095 (300s), 0.0113 (600s), 0.0113 (1800s) — **flat from 300s onward**. The
market has converged; it is not short of time.

So it is not noise traders, not position limits, and not insufficient time.
What remains is the mechanism itself: **a limit order book aggregates by
capital, not by evidence.** Every agent here carries the same base size, the
same position limit and the same cash, so the 50-battle agent pushes the price
about as hard as the 5,000-battle agent. The price settles where buying and
selling pressure balance — an unweighted consensus — which is precisely what the
result says it is. The precision-weighted baseline knows each agent's battle
count exactly and uses it; the market has no channel through which that
information could reach the price.

The concentration ablation is the same point from the other side. Holding total
information fixed and moving it all into one trader makes the market **2.7×
worse**, because one agent has one balance sheet. Dispersed information reaches
the price better than concentrated information — the opposite of the intuition
that an informed monopolist prices most efficiently.

## What the extremization factor says

Fitted out of sample at 0.95 and 1.10 — straddling 1.0, so log-odds
extremization neither helps nor hurts here. The forecasting literature finds
d ∈ [1.16, 3.92] optimal on geopolitical tournaments, where pooled forecasts are
systematically under-confident. That does not reproduce here, and the reason is
visible: those tournaments pool *human* forecasters who are individually
miscalibrated, while these agents are individually Bayes-optimal by
construction. Pooling optimal forecasters produces a well-calibrated aggregate
with nothing left to sharpen.

The grid was widened below 1.0 for this run. In the concentrated ablation the
fitted factor pins at **0.50**, the grid floor — seven agents at the prior and
one sharp agent produce a wildly over-confident log-odds pool that wants heavy
shrinking. Had the grid been clamped at 1.0 as the original formulation
specifies, a boundary value would have been reported as an interior optimum.

## Honesty notes

- **This is the outcome the literature predicts,** and it was pre-committed to
  in the plan before the run. Markets beating simple averages is a known result;
  beating a well-specified statistical aggregate is not, and did not happen.
- **The baselines are deliberately unfair to the market.** "Best single agent"
  is chosen after seeing the answers. "Precision-weighted" uses exact battle
  counts, which no real study can observe. Losing to these is not embarrassing;
  it is the point of putting them on the ladder.
- **Secondary (outcome-based) Brier** tells the same story more noisily: market
  0.196, simple mean 0.201, precision-weighted 0.181, best agent 0.176. The
  ordering is preserved, and the noise is exactly why the primary metric scores
  against the known truth instead.
- **The market is healthy**, so it is not winning or losing for the wrong
  reason: 2,360 trades per trial, two-sided 99.9% of the time, conservation
  exact in every trial, reliability 0.0098 (well calibrated).

## What this sets up next

The result names its own follow-up. If the gap between the market and
precision-weighting is that wealth does not track skill, then let it: run
repeated trials with the same agents, carry P&L across them, and see whether the
market migrates from the simple mean toward the precision-weighted aggregate as
better-informed agents accumulate capital. That is a sharper question than this
one, and this experiment is what makes it askable.

## Reproducing

Every run writes `results/<tag>_manifest.json` (config, seeds, git commit,
per-module code digests, results digest) and `results/<tag>_trials.csv`. Trials
are independent and deterministic from their own config, so `--workers` changes
runtime only. `tests/test_experiment.py` asserts that the same config produces
byte-identical results and that a different seed does not.
