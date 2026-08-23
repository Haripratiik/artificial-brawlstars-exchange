# Arena Markets

**A deterministic multi-agent electronic market simulator for studying market microstructure, information aggregation, quantitative trading, and event-linked derivatives.**

Arena Markets is a financial market simulator in which quantitative funds, market makers,
arbitrageurs, and noise traders price and trade derivatives whose terminal payoffs are tied to
future *measurable outcomes in an external system*. The financial engine is generic. Its first
experimental world uses historical Brawl Stars match statistics and balance changes as the
data-generating process.

The distinction that defines the project:

> **The game determines what the contracts pay. The market determines how those payoffs are
> priced before they are known.**

Remove the external data and the contracts lose their settlement values — which is how we know
the domain is structural rather than cosmetic.

---

## Status

Early. Two of ten phases are complete; the roadmap is in [docs/PLAN.md](docs/PLAN.md).

| Phase | | |
|---|---|---|
| 0 | Data collection | **done** — snowball crawler, resumable, running |
| 1 | Contracts & deterministic settlement | **done** — 100 tests |
| 2 | Exchange kernel (Python reference) | next |
| 3 | Minimal artificial market | |
| 4 | C++ kernel, differential-tested | |
| 5 | Synthetic world, heterogeneous information | |
| 6 | Information-aggregation experiment | |
| 7 | Multi-asset & microstructure | |
| 8 | Real historical replay | |
| 9+ | Prediction-market venue, options, risk | |

Nothing here is a trading system, and no claim in this README is ahead of the code.

---

## What exists today

**A contract algebra.** An underlying is a closed three-node algebra — a single metric, a
difference, or a pinned weighted basket — so performance futures, relative-value spreads, and
class indices all settle through one mechanism rather than three.

**Deterministic settlement.** Every contract is content-addressed. The same spec against the
same dataset produces a byte-identical settlement record, and any change to the spec, the data,
or the standardization snapshot changes its digest. Settlement carries full provenance: source
digests, per-stratum diagnostics, and the achieved coverage.

**A metric that survives a biased sample.** The official API has no aggregate-statistics
endpoint, so every statistic has to be built by crawling battle logs — and no crawl of Brawl
Stars can be representative. The settlement metric therefore *standardizes* onto stratum weights
pinned before the observation window opens, so the composition of the crawl cancels out of the
settlement value. Thin strata are shrunk toward a hierarchical prior; unobserved strata shrink
fully to it rather than being dropped. This is the project's answer to its hardest data problem,
and it is enforced by
[`test_standardization_cancels_composition_drift`](tests/test_metrics.py).

**Settlement rules that are estimated, not asserted.** Nothing in a reference snapshot is a
hardcoded constant. Standardization weights, hierarchical priors, and the shrinkage strength are
all derived from data knowable at the snapshot date — the last by method of moments on a
beta-binomial, cross-checked against out-of-sample prediction error. Each snapshot is immutable
and pinned by contract, while the *series* of snapshots is re-derived as the metagame moves.

**Lookahead prevention as a structural property.** A contract published after its observation
window opens is rejected at construction. Reference snapshots must predate the windows they
settle. Agent-facing dataset access filters on when an observation *became knowable*, not on the
window it describes.

**A collector built to run unattended for months.** Rate-limited well under the API ceiling,
resumable across kills, append-only raw storage, and battle deduplication that collapses the
six copies of every 3v3 battle the API returns.

---

## Quick start

```bash
pip install -e ".[dev]"
```

```bash
python -m pytest
```

Regenerate the deterministic fixture dataset:

```bash
python tools/make_fixture_dataset.py
```

Run the collector. Full setup — API key, the IP allow-list, hosting — is in
[docs/collector.md](docs/collector.md); it takes about ten minutes and costs nothing.

```bash
python -m collectors.brawl_api --check --proxy
```

```bash
python -m collectors.brawl_api --proxy --data-dir data/raw
```

`--proxy` routes through RoyaleAPI's community proxy, so the API key allow-lists *their* fixed
address rather than yours. Supercell's IP lock then stops mattering and the collector runs
anywhere, including a laptop on a dynamic address.

---

## Layout

```
python/arena/          the generic financial engine
  contracts/           underlying algebra, payoffs, immutable specs
  settlement/          oracle protocol, deterministic settlement engine
  worlds/brawl/        world #1: metrics, canonical dataset, oracle
collectors/brawl_api/  the data collector
data/reference/        frozen standardization snapshots
data/fixtures/         deterministic synthetic fixtures for tests
docs/                  plan, economy spec, collector setup
tests/
```

- [docs/PLAN.md](docs/PLAN.md) — the ten-phase roadmap and the research that shaped it
- [docs/ECONOMY.md](docs/ECONOMY.md) — what the contract and settlement layer implements, in detail
- [docs/collector.md](docs/collector.md) — complete free setup for the data collector

The engine does not import from `worlds/`. That is what makes a second data-generating domain a
matter of implementing one protocol.

---

## Research questions

The north star is broader than which agent earns the most:

> How well can an artificial market aggregate heterogeneous information about a complex external
> system into prices, and how do market structure, latency, liquidity, and information asymmetry
> affect that process?

The first experiment measures the market's forecast against a **ladder** of baselines — best
individual agent, simple mean, precision-weighted mean, and a recalibrated aggregate. The
literature already establishes that markets beat a naive average, so only the last rung makes
the result interesting.

---

## Disclaimer

This material is unofficial and is not endorsed by Supercell. For more information see
Supercell's Fan Content Policy: www.supercell.com/fan-content-policy.

This is a non-commercial research project. All capital, positions, and PnL are simulated. There
is no deposit, withdrawal, cash-out, prize, reward, or transferable value of any kind, and none
will be added. Event-contingent instruments here are research constructs for studying
probability calibration and information aggregation.

Brawl Stars and related marks are property of their respective owners. No game artwork, audio,
or other assets are redistributed by this project.
