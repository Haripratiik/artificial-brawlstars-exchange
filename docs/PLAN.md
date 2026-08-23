# Arena Markets — Attack Plan

**Status:** planning, revised after the August 2026 feasibility research pass
**Stack:** Python (research, agents, data, experiments, modeling) · C++ (exchange kernel, performance-critical simulation)

---

## Part I — What the research changed

The kickoff spec is sound. Five findings change *how* we sequence it.

### 1. There is no aggregate-statistics endpoint. The data layer is the critical path.

The official Supercell API exposes players, clubs, brawlers, rankings, event rotation, and
**battle logs capped at the last 25 battles per player**. There is no "win rate by brawler by
map" endpoint anywhere. Every meta statistic in this project has to be *manufactured*:

```
rankings  ->  player tags  ->  battlelog (25 each)  ->  dedupe  ->  stratified aggregates
```

Two operational constraints on top:

- **API keys are IP-locked.** A key is bound to the IP addresses declared when it was created,
  so on a home connection it breaks whenever the address changes. **Solved, and at no cost:**
  RoyaleAPI operate a free community proxy (`bsproxy.royaleapi.dev`), so the key allow-lists
  *their* fixed address instead of yours. Supercell then only ever sees the proxy, and the
  collector's own IP stops mattering. No VPS, no static IP, no key-regeneration hack.
- **Rate limit is roughly 10 requests/second per key**, throttled with HTTP 429.

The rate limit is the good news: ~10 req/s is ~860k requests/day. At 25 battles per battlelog
response, even with heavy overlap between polls, a single well-behaved collector can plausibly
accumulate on the order of 10^5–10^6 *deduplicated* battles per day.

**Consequence: the collector's value is proportional to wall-clock time, and nothing else in
the project is.** It must start on day one and then be left alone. Everything else must be
built so it is *never blocked* waiting for it.

### 2. Brawl Time Ninja is a bootstrap reference, not an ingestion source.

- Its repository now states development moved to a private repo — the methodology is no longer
  readable, and there is no license to rely on.
- The site returns HTTP 403 to automated fetchers.
- Its own documentation says statistics come from *its visitors*, "who are usually better than
  the average" — the sample is explicitly **not** representative of the player base.
- Its "adjusted win rate" is documented as the share of battles a brawler "wins or ranks high"
  (which generalizes across Showdown-style modes), with a **Bayesian average** interpolation for
  low-pick brawlers.

Use it the way the spec already suggests: occasional *manual* CSV export to sanity-check our own
aggregates and to shape priors. Never automated ingestion, never redistribution.

The visitor-bias point is worth keeping: it means "adjusted win rate" is only well defined
*relative to a stated population*. Our contracts must name the population explicitly, which the
settlement schema already forces.

### 3. The Fan Content Policy has teeth on exactly one of our instrument families.

The policy requires this **exact** notice:

> This material is unofficial and is not endorsed by Supercell. For more information see
> Supercell's Fan Content Policy: www.supercell.com/fan-content-policy.

and lists **gambling** among prohibited content, alongside a non-commercial requirement and a
ban on implying endorsement.

This does not threaten the project — simulated capital, no cash-out, research framing — but it
does re-rank the instruments. **Esports match-outcome binaries are the single highest-risk
family** (they look exactly like sports betting to a casual reader), and they are also the ones
with the smallest sample and the weakest connection to the battle dataset. Defer them
indefinitely. Brawler-statistic contracts carry the research and carry less risk.

### 4. The market-making adaptation the spec flagged as an open problem now has a published answer.

The spec notes Avellaneda–Stoikov "will need adaptation because Arena Markets contracts have
bounded / settling payoffs." *Optimal Market Making in Prediction Markets* (arXiv 2607.17991,
July 2026) does precisely that adaptation:

- model a latent belief process `L_t` and set `p_t = f(L_t)` with logistic `f`, so price stays
  in (0,1) by construction rather than by clamping;
- price evolves as `dp_t = ς(t, p_t) dW_t` with **state-dependent volatility that vanishes at
  the boundaries** — a 0.02 contract simply cannot move like a 0.50 contract;
- add a **terminal settlement penalty** `Φ(p_T, q_T) = −γ_T · q_T² · p_T(1−p_T)`, the variance
  of the settlement value of inventory still held at resolution. This term has no analogue in
  Avellaneda–Stoikov and is the whole difference;
- no closed-form reservation price; optimal quotes come from the value function numerically.

We get a principled, citable market maker instead of an improvised one.

### 5. The obvious version of Experiment 1 has a baseline that is too weak to be interesting.

"Does the market beat its constituent agents?" is nearly free to win against the *worst* agent
or a naive average. The literature is sharper than that: Atanasov et al. (*Management Science*)
find prediction markets beat the **simple mean** of forecasters, but **lose** to prediction polls
once those are aggregated properly — temporal decay, performance weighting, and recalibration.

So the experiment must be run against a **ladder** of baselines:

| Baseline | Why it's there |
|---|---|
| Best single agent (ex post) | Upper bound on any individual |
| Simple mean of agent beliefs | The weak baseline the literature already beat |
| Precision-weighted mean | Uses each agent's own uncertainty |
| Recalibrated / extremized aggregate | The one that actually beats markets in the literature |

If the market only beats rows 1–2, that is a known result. If it beats row 4, that is a finding.
Designing this in from the start is the difference between a project and a *result*.

---

## Part II — The design idea this points to

The single most useful consequence of the above:

> **Model information heterogeneity as different sample sizes drawn from the same
> data-generating process.**

Instead of hand-tuning "agent A gets Gaussian noise with σ=0.03", give agent *j* a sample of
`n_j` battles from the true stratum-level DGP. Then:

- its posterior over the win rate is an exact Beta/Normal update — no arbitrary noise model;
- information quality has a **unit**: one battle observed;
- "how much is faster/better information worth?" becomes a measurable dollar-per-battle number;
- ground truth `p*` is known exactly, so Brier score, calibration, and *resolution* can be
  decomposed properly rather than estimated from one realization;
- it maps directly onto Kyle-style informed-vs-noise structure, and onto the real collector
  (an agent's `n_j` is literally "how much of the crawl has this fund seen").

This is why the synthetic world is not a shortcut. For the core information-aggregation
question it is **strictly better instrumentation** than real data, because real data gives you
one realization and no `p*`. Real historical replay then serves as the external-validity test,
which is exactly the right division of labour — and conveniently it is also the part that has
to wait for the collector anyway.

A second structural property worth exploiting, specific to these contracts:

> **Volatility has a known term structure.** A win-rate future settling over window
> `[T₀, T₁]` decomposes at time *t* into an already-realized part and a still-unknown part:
> `W_T = (n_seen/n_total)·W_seen + (n_left/n_total)·W_future`.
> The conditional variance shrinks *deterministically* as the window fills.

Equities do not do this. It gives the market maker an analytic σ(t), gives options a predictable
IV decay whose violations are informative, and gives a clean convergence trade near expiry. It
is a genuinely distinctive feature of the asset class this project invents.

---

## Part III — Two tracks

```
TRACK A  (data)     day 1 ─────────────────────────────────────────────>  accrues forever
                    low effort, high latency, unblocks real replay only

TRACK B  (engine)   day 1 ──> contracts ──> exchange ──> agents ──> experiments
                    all effort, zero dependency on Track A
```

They meet at a single seam: a `World` protocol that yields (a) timestamped observations and
(b) settlement truth. `SyntheticWorld` and `BrawlReplayWorld` both implement it. The exchange,
the agents, and the experiment harness never know which one is underneath.

---

## Part IV — Phases

Each phase has an exit test. Do not start the next phase until it passes.

### Phase 0 — Start the clock (days) — **DONE, pending an API key**
Track A only, then leave it running.
- Register an API key allow-listing the proxy address, not your own. No hosting decision needed.
- Collector: rankings → player tags → battlelogs → append-only gzip JSONL, battles verbatim.
- Snowball expansion through participant tags; SQLite frontier + dedupe index; restart-safe.
- **Exit:** collector has run unattended for 72h and the deduplicated battle count is growing.

Setup is in [collector.md](collector.md) — about ten minutes, zero cost. A laptop is a fine
host; an always-on machine is strictly better only because it collects while you sleep.

### Phase 1 — The economy (Python) — **DONE**
Contracts and settlement before any exchange, per the spec's Milestone 0.
Implemented in detail in [ECONOMY.md](ECONOMY.md), including the open judgment calls.
- Canonical metric definition, written down and frozen (stratified/standardized win rate with
  pinned reference weights; explicit population; Bayesian shrinkage for low samples).
- Contract spec model, content-addressed by digest; underlying algebra (single / difference /
  basket) so futures, spreads, and indices come from one mechanism.
- Deterministic settlement engine: min sample size, missing-data policy, provenance, tick
  quantization.
- **Exit:** same contract + same dataset ⇒ byte-identical settlement digest, and a mutated
  reference-weight file changes that digest.

### Phase 2 — Exchange kernel, Python reference
Deliberately in Python first.
- Price-time priority book, limit/market/cancel/replace, partial fills, deterministic sequence
  numbers, trade tape, L2 snapshots.
- Discrete-event kernel: priority queue, agent wakeups, pairwise latency matrix + latency noise
  (ABIDES' design, which is itself modeled on NASDAQ ITCH/OUCH messaging).
- Property tests: no crossed book, conservation of quantity, replay determinism under seed.
- **Exit:** adversarial matching-engine suite green; seeded run reproduces bit-for-bit.

### Phase 3 — Minimal artificial market
- ~hundreds of noise agents, one fundamental agent, one inventory-skew market maker.
- One instrument: a linear brawler-performance future.
- **Exit:** a full session produces plausible order flow, trades, inventory, and PnL that
  survives eyeballing.

### Phase 4 — C++ kernel, differential-tested against Phase 2
This is where the C++ half lands, and the ordering is the point: the Python engine becomes the
**correctness oracle**, so the C++ port is validated rather than merely written.
- C++20 core, `scikit-build-core` + CMake, **nanobind** bindings (vs pybind11: ~4× faster
  compiles, ~5× smaller binaries, ~10× lower call overhead, and one `ndarray` type that works
  across NumPy/JAX/PyTorch — which matters given the JAX ambitions later).
- Toolchain: MSVC Build Tools 14.50 / VS 2026 — defaults to C++20 and ships CMake 4.1.1, so
  it is one install. *(Neither a compiler nor CMake is currently on this machine.)*
- Differential test: identical order streams through both engines must produce identical tapes.
- **Exit:** engines agree on randomized order streams; C++ is meaningfully faster on a
  throughput benchmark.

### Phase 5 — Synthetic world + heterogeneous information
- `World` protocol; `SyntheticWorld` with a stratum-level DGP calibrated to whatever real data
  exists by then.
- Agent information = `n_j` sampled battles. No-lookahead information interface.
- **Exit:** an agent's forecast error scales as `1/√n_j` as theory demands.

### Phase 6 — Experiment 1, done properly
- Market vs the **full baseline ladder** (best agent / simple mean / precision-weighted /
  recalibrated-extremized).
- Brier decomposition into calibration + resolution + uncertainty against known `p*`.
- **Exit:** a defensible result with error bars, including the case where the market *loses*.

### Phase 7 — Multi-asset and microstructure
Spreads, class indices, cross-market arbitrage, stat-arb; then latency tiers, maker/taker fees,
queue position, adverse-selection diagnostics.

### Phase 8 — Real historical replay
Only now, once Track A has accrued enough. Same harness, `BrawlReplayWorld`, one real patch as
the shock. This is the external-validity result.

### Phase 9+ — Prediction-market venue (LMSR vs CLOB), options and event vol, margin and
liquidation cascades, dashboard. In that order.

---

## Part V — Standing decisions

| Decision | Choice | Reason |
|---|---|---|
| First instrument | Linear brawler win-rate future | Continuous, connects to largest dataset |
| Esports match binaries | Deferred indefinitely | Weakest data, highest policy risk |
| API IP lock | RoyaleAPI proxy, allow-list theirs | Removes the constraint for free; no VPS needed |
| Collector host | Any always-on machine; laptop to start | Proxy decouples hosting from the IP lock |
| Bindings | nanobind | Faster builds, smaller binaries, JAX-ready ndarray |
| Build backend | scikit-build-core + CMake | Modern, not setuptools |
| Engine order | Python reference → C++ port | Free correctness oracle for a Python-first dev |
| BTN data | Manual export, bootstrap only | Closed source, unclear license, blocks bots |
| Repo identity | `arena-markets` | Generic engine; Brawl is world #1 |
| Disclaimer | Exact Fan Content Policy wording | Required verbatim |

---

## References

- Supercell Brawl Stars API — https://developer.brawlstars.com/
- Supercell Fan Content Policy — https://supercell.com/en/fan-content-policy/
- Byrd, Hybinette & Balch, *ABIDES* — https://arxiv.org/abs/1904.12066
- *Optimal Market Making in Prediction Markets* (2026) — https://arxiv.org/html/2607.17991v1
- Avellaneda & Stoikov (2008) — https://doi.org/10.1080/14697680701381228
- Kyle (1985) — https://www.jstor.org/stable/1913210
- Atanasov et al., *Distilling the Wisdom of Crowds* — https://pubsonline.informs.org/doi/10.1287/mnsc.2015.2374
- Frey et al., *JAX-LOB* (2023) — https://arxiv.org/abs/2308.13289
- nanobind benchmarks — https://nanobind.readthedocs.io/en/latest/benchmark.html
