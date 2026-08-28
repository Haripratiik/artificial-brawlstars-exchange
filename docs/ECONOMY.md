# The Economy: what Phase 1 actually implements

A precise description of the contract and settlement layer, written to be argued with. The last
section lists every judgment call I made that could reasonably have gone the other way.

---

## 1. Layering

```
  arena.contracts     what a contract IS          generic, no Brawl knowledge
  arena.settlement    how a contract SETTLES      generic, talks to an Oracle protocol
  arena.worlds.brawl  what the numbers MEAN       all domain knowledge lives here
```

The engine never imports from `worlds/`. `settle()` takes a `ContractSpec` and anything
satisfying the `Oracle` protocol. That is the seam a second data-generating world plugs into, and
it is enforced by the import graph rather than by convention.

---

## 2. Data model

### `AggregateRow`: one cell of the derived table

A row is one **(brawler, stratum, sub-window)** cell. Settlement never needs an individual
battle, so the settlement path reads aggregates. Raw battles are still kept by the collector;
this is the derived layer above them.

```
observed_at       when this aggregate became KNOWABLE
window_start      \  the period the counts describe, half-open [start, end)
window_end        /
brawler_id
mode_id, map_id, trophy_bucket     -> the stratum
brawler_battles   appearances of THIS brawler
brawler_wins
stratum_battles   distinct battles in the stratum
stratum_slots     total brawler appearances by ANYONE in the stratum
source_id
```

`brawler_battles` is the win-rate denominator; `stratum_slots` is the use-rate denominator.
Storing both avoids reconstructing either by self-joining the table.

**`observed_at` is not `window_end`.** It is when the aggregate could first have been computed,
after the battles were played *and* collected. Conflating the two is the easiest way to leak the
future into a replay, so the constructor rejects `observed_at < window_end` outright.

Validated at construction: timezone-awareness, window ordering, `wins <= battles`,
non-negativity, and `brawler_battles <= stratum_slots`.

### `StratumKey` = (mode, map, trophy_bucket)

The finest grain contracts can filter on, and the grain standardization weights are defined over.
That is why the derived table is built at exactly this grain.

---

## 3. The metric

Three metrics live in a registry; a contract naming anything else fails at resolution rather than
silently measuring something adjacent.

### Notation

For brawler *i* over window *W*, rows are collapsed into one cell per stratum *s*:

- n_s = Σ `brawler_battles`, w_s = Σ `brawler_wins`, k_s = Σ `stratum_slots`
- ω_s = standardization weight from the pinned snapshot (0 if out of universe)
- m_mode(s) = neutral win rate for the mode (the shrinkage target)
- κ = prior strength, in pseudo-battles

**Sub-windows are pooled by summing counts, not by averaging ratios.** Every battle carries equal
weight regardless of which week it fell in. Averaging weekly rates would let a quiet week count
as much as a busy one.

### `raw_win_rate`

```
raw = (Σ_s w_s) / (Σ_s n_s)
```

Unstandardized and composition-dependent. Kept deliberately, because "standardization changed
the answer by X" is itself a result worth reporting.

### `adjusted_win_rate`: the settlement metric

Two corrections that fix different problems, applied in this order:

**Step 1, shrink (fixes within-stratum noise).**

```
p̂_s = (w_s + κ · m_mode(s)) / (n_s + κ)
```

A cell with 9 battles and 6 wins is not evidence of a 67% win rate. With κ = 250 that cell
reports 0.512, not 0.667.

**Step 2, standardize (fixes between-stratum composition).**

The metric walks **the snapshot's strata**, not the observed ones. Let E = strata with evidence
(n_s ≥ `min_stratum_battles`).

```
coverage = (Σ_{s∈E} ω_s) / (Σ_{all s} ω_s)
adjusted = (Σ_{s} ω_s · p̂_s) / (Σ_{s} ω_s)     with p̂_s = m_s for s ∉ E
```

If `coverage < min_strata_coverage`, raise `InsufficientEvidence`: a metric that is mostly prior
is not a measurement, however well-formed it looks.

**Missing strata are imputed, not dropped.** A stratum with no data enters with n=0 and therefore
shrinks entirely to its prior, same formula, no special case. The alternative, dropping and
renormalizing over survivors, implicitly assumes missing strata resemble observed ones; but strata
go missing precisely because they are unpopular, rare, or high-trophy, which is close to the
opposite of missing-at-random. Partial pooling is the standard poststratification answer to empty
cells, and full shrinkage to the prior is exactly that. `DROP_AND_RENORMALIZE` remains available
for comparison, is part of the contract's digest, and is not recommended.

`use_rate` is the deliberate exception: it always drops and renormalizes, because imputing zero
would assert the brawler is never picked there, and unlike a win rate there is no mechanically
pinned baseline to shrink toward.

### `adjusted_win_rate_lift`: the recommended settlement metric

```
lift = Σ_s ω_s (p̂_s − m_s)
```

Zero means "exactly average wherever it was played."

**Why it should carry the flagship contract.** Mode baselines are unequal and pinned by the rules
(0.500 for 3v3, 0.450 for Solo Showdown). So a plain adjusted rate has a neutral point of
`Σ ω_s m_s`, a number that moves whenever the *mode mix* moves. Supercell rotates maps and modes
continuously, so a contract on the plain rate would have its par level drift with the rotation, and
a trader holding it would be partly pricing the event schedule rather than the brawler.

Subtracting each stratum's baseline removes that. A brawler who is exactly average scores 0 under
any weighting and any rotation, so the contract isolates what it claims to measure. It also demotes
the slots-versus-battles question: the basis still scales each stratum's contribution, but can no
longer move the neutral point.

On the fixture the difference is stark. Across the patch, the absolute future moves 4838 → 4669,
mixing Spike's nerf with the mode composition. The lift moves **+110.75 → −58.25**, Spike crossing
cleanly from above-average to below.

The neutral point is computed inside `adjusted_win_rate` over exactly the strata that contributed
and exposed as the `neutral_level` diagnostic, so the subtraction is exact under either
missing-strata policy rather than reconstructed approximately afterwards.

The order matters. Shrink first, each stratum's estimate is improved using its own evidence.
Then weight, composition is imposed from outside.

**Why this is the answer to the project's hardest problem.** There is no aggregate-stats endpoint,
so every statistic is built by crawling battle logs, and no crawl of Brawl Stars can be
representative. A raw rate would move whenever the crawler's reach moved, and a contract settling
on it would price the crawler as much as the game. Standardizing onto weights pinned *before the
window opens* makes composition drift cancel. Proven by
`test_standardization_cancels_composition_drift`: two crawls of the same underlying game, one
low-trophy-heavy and one high-trophy-heavy, differ by >0.05 raw and <0.02 adjusted.

### `use_rate`

```
use = (Σ_{s∈S′} ω_s · (n_s / k_s)) / (Σ_{s∈S′} ω_s)
```

Denominated in **slots**, not battles: a 3v3 battle offers six slots, so a brawler picked by both
teams every game would otherwise show a use rate above 1.

**No shrinkage.** A use rate of zero in a well-sampled stratum is a real observation, nobody
picked it, not the noise artifact a zero win rate would be. Shrinking it would destroy signal.

### Reported sample size

`sample_size` counts battles in **contributing strata only**. Excluded strata are reported
separately as `battles_observed`. This matters because the engine checks `min_sample_size`
against it: counting excluded evidence would let a contract clear its evidential bar on data the
metric deliberately threw away. (In the fixture that gap is ~22,000 battles out of ~271,000.)

---

## 3b. What the game's rules pin exactly

Most of a metagame is empirical. A few things are not, and separating them out turns out to matter
more than anything else in this layer.

A battlelog names **every** participant, not just the player whose log it is. So for any battle we
observe, we observe the full outcome distribution across its slots:

| Mode | Slots | Win | Draw | Lose | Pooled rate |
|---|---:|---:|---:|---:|---:|
| 3v3 (all objectives) | 6 | 3 | n/a | 3 | **0.500** |
| Solo Showdown | 10 | ranks 1–4 | rank 5 | 6–10 | **0.450** |
| Duo Showdown | 10 (5 teams) | top 2 teams | 3rd team | 4th–5th | **0.500** |

Pooled over *all* brawlers, the win rate is arithmetic, not an estimate.

Solo and Duo do **not** share a baseline, which is easy to assume and wrong: both seat ten
players, but Solo draws exactly one slot while Duo draws a whole team of two.

### The half-draw convention

Draws are scored as half a win. This is not a preference, it is the only scoring under which a
mode's pooled rate is independent of its draw rate. Scoring a draw as a loss gives 3v3 a pooled
rate of `(1−d)/2`, so a brawler's measured performance would move when the metagame turned more
defensive *even though the brawler had not changed*. A settlement metric may not have that
property. Draws are therefore stored as their own column, and the scoring convention lives in the
metric rather than in the collector.

### Two consequences

**Mode priors are taken from the rules, not fitted.** An exact constant beats any estimate of the
same quantity, and it means a thin or skewed estimation window cannot drag the shrinkage target
around. `use_mechanical=False` falls back to the observed rate, which is how the gap below is
measured.

**The gap is a free, exact correctness check.** Recompute the pooled rate from real data and
compare. Over a corpus covering every brawler it must be ~0; a material gap means participants are
being dropped from battles, battles are being double-counted, or draws are being mis-scored.
Nothing else in this project catches those failure modes. `build_reference.py` reports it and
flags gaps above 0.02. (On the deliberately partial fixture the gap is large, and should be.)

---

## 4. The reference snapshot: everything in it is estimated

**Nothing is hardcoded.** Weights, priors, and shrinkage strength are all derived from data that
was knowable at `as_of`. See `arena/worlds/brawl/estimation.py`.

The apparent tension, a settlement rule must be *frozen*, but must not be *arbitrary*, resolves
by separating the individual snapshot from the series. Each snapshot is immutable and pinned by
exactly one contract; the series is re-derived as the game moves. `ref-2026S09` and `ref-2026S10`
differ, and neither can ever be edited. `save_reference` refuses to overwrite an existing file for
exactly this reason.

### Weights: share of observed play volume

```
omega_s = volume_s / sum_j volume_j
```

So "adjusted win rate" means *the rate this brawler would post if the game were played in the
proportions it was actually played in during the estimation window*, a claim anyone can check.

`volume` is slots by default, battles optionally, and **the choice matters**. A Showdown battle
offers ten brawler slots against a team mode's six, so slot-weighting gives Showdown roughly 10/6
the weight per battle, and Showdown's baseline win rate is ~0.41 against ~0.52, so the setting
visibly moves the settlement value. Slots is the default because a win rate is measured per
appearance and a brawler occupies one slot. The choice is recorded in the snapshot's provenance,
not buried in code.

One implementation trap worth naming: `stratum_slots` describes the *stratum* and repeats
identically on every brawler's row for it. Summing naively would scale each weight by how many
brawlers the crawl happened to observe there, making the weights a function of coverage, the
exact thing standardization exists to remove. Deduplication is tested.

### Priors: hierarchical, estimated per stratum

The mode-level rate is what the game's structure mechanically pins. On the fixture the estimator
recovers **showdown 0.4152** and **gemGrab 0.5244** with nobody telling it that Showdown scores
"top four of ten". The stratum-level rate then captures real deviation and is partially pooled
back toward its mode, so a thin stratum borrows strength instead of inventing a baseline.

`prior_for()` falls back to the mode when a stratum has no prior of its own, which is what
happens for a map added to rotation after the snapshot was frozen.

### Prior strength: method of moments

Modelling cell *c* as `theta_c ~ Beta(mean m_c, strength kappa)` and `x_c ~ Binomial(n_c, theta_c)`,
with `A_c = m_c(1 - m_c)`:

```
E[(p_hat_c - m_c)^2] = (A_c / (kappa + 1)) * (1 + kappa / n_c)

  =>   kappa = (sum A_c - S) / (S - sum A_c/n_c),    S = sum (p_hat_c - m_c)^2
```

Closed form, no grid search, no held-out windows. It degrades correctly: if the observed spread of
cell rates is fully explained by binomial noise the denominator collapses and kappa diverges,
meaning "trust none of this variation". Clamped to [1, 100000], and a clamp is reported because it
means the estimation window was uninformative.

**Validated against your out-of-sample approach.** `sweep_prior_strength` fits on the first half of
the estimation window and scores weighted MSE on the second. On the fixture, method of moments
gives **kappa = 355** while the out-of-sample optimum is **250**, with the MSE curve nearly flat
between them (7.24e-5 vs 7.25e-5). The closed form lands essentially at the predictive optimum,
which is the evidence that the beta prior is not misspecified. A test asserts the two stay within
a factor of five.

### Provenance

The snapshot records method, `as_of`, weight basis, lookback, pooling strength, rows and cells
used, and the digest of the dataset it was fitted on, all covered by the snapshot digest, so two
snapshots fitted differently are different snapshots even if their numbers coincide.

A separate `file_digest` records the bytes on disk. It is deliberately *not* part of the content:
"which data produced these weights" and "which file did I read" are different facts, and an
earlier version of this code conflated them.

---

## 5. The contract

### `MetricRef`: a fully qualified measurable quantity

```python
MetricRef(metric="adjusted_win_rate", subject="SPIKE",
          modes=("ALL",), maps=("ALL",), trophy_buckets=("ALL",))
```

Filters must be sorted, duplicate-free, and never mix `"ALL"` with explicit values, so two
contracts naming the same universe compare and hash equal regardless of how they were written.
`"ALL"` is spelled out rather than represented by an empty tuple, so a truncated spec file cannot
silently widen a contract's universe.

### `Underlying`: a closed three-node algebra

| Node | Meaning | Instrument family |
|---|---|---|
| `Single(ref)` | one metric | performance future |
| `Difference(a, b)` | a − b | relative-value spread |
| `Basket(((leg, w), ...))` | Σ wᵢ · legᵢ | class / meta index |

Closed on purpose. Settlement must be auditable years later, and an algebra with three
constructors can be reasoned about exhaustively, unlike an expression string evaluated from
YAML, which is both a correctness and a security problem.

`atoms()` returns deduplicated, sorted `MetricRef`s, so each metric is resolved exactly once and
resolution order does not depend on how the tree was nested. `Basket.evaluate` accumulates in
canonical-shape order, because float addition is not associative and reordering legs in a config
file must not move the last bit of a settlement value.

### `Payoff`

| | |
|---|---|
| `Linear(scale, offset)` | `scale·level + offset` |
| `Binary(comparison, threshold, payout)` | `payout` if the comparison holds, else 0 |

`Linear(scale=10_000)` moves a rate in [0,1] onto a price grid with useful tick resolution: a
0.5537 win rate settles at 5537, so one tick is a quotable amount rather than a rounding artifact.

Nothing enforces a 0–1 price range on binaries. Whether their price is readable as a probability
is a question this project exists to *test*, not an identity to assume.

Options are absent by design, an option is a function of a *traded future*, not of the raw
metric, so it belongs to a later phase and a different module.

### `ContractSpec`: immutable, content-addressed

```python
ContractSpec(contract_id, underlying, payoff, window, policy,
             reference_id, published_at, tick_size="0.01", lot_size=1, metadata=())
```

Two invariants enforced at construction, both lookahead bugs that would otherwise be invisible:

1. **`published_at <= window.start`**: a market written after its outcome has begun forming is
   lookahead by construction.
2. **`reference_id` is required**: standardization must be pinned; it cannot be left implicit.

(The snapshot's own `as_of <= window.start` is a documented requirement, currently checked by
convention rather than by code, see §9.)

`spec_digest` is `sha256` over canonical JSON of the whole spec.

### `DataPolicy`: the evidential bar

| Field | Guards |
|---|---|
| `min_sample_size` | the metric as a whole |
| `min_stratum_battles` | per-cell reliability |
| `min_strata_coverage` | the metric's *composition* |
| `missing_data_policy` | `VOID` only, currently |

The last two exist because a standardized rate computed from three well-sampled strata out of
twenty-seven is not the quantity the contract named, even if the total battle count looks fine.

---

## 6. Settlement

`settle(spec, oracle)`, one function, deliberately small enough to verify by reading.

```
1. reject if oracle.reference_id != spec.reference_id      -> raises ReferenceMismatch
2. for each atom in spec.atoms()  (canonical order):
       resolve via oracle                                  -> MetricUnavailable ⇒ VOID
       if sample_size < policy.min_sample_size              -> VOID
3. level = spec.underlying.evaluate(resolved values)
4. raw   = spec.payoff.apply(level)
5. value = quantize_to_tick(raw, spec.tick_size)            -> ROUND_HALF_EVEN
```

Steps 2 and 5 are the ones people skip. Skipping 2 settles on evidence the contract declared
insufficient; skipping 5 prints a closing value the exchange cannot represent.

**Errors vs. voids.** A VOID says *the world did not supply enough evidence*, a normal outcome,
recorded with a reason. A raise says *the experiment is wired up wrong*. `ReferenceMismatch` is a
raise, because silently voiding a config error would hide it behind a plausible market outcome.

**Void records keep partial evidence.** A spread whose second leg fails still records the first
leg's resolution. "Voided because Crow's sample was thin" is far more useful than "voided".

### `SettlementResult`

Carries `contract_id`, `spec_digest`, status, settlement value (`Decimal`, or `None` if void),
underlying level, every `MetricResolution` with source digests and diagnostics, and a void reason.
`result_digest` covers all of it.

**No wall-clock timestamp.** Determinism means identical inputs produce byte-identical output; a
`computed_at` field would break that for nothing. *When* a settlement ran belongs in the run
manifest, that is about the experiment. The record is about the world.

---

## 7. Determinism

Four mechanisms in `arena/determinism.py`:

| | |
|---|---|
| `canonical_json` | sorted keys, no whitespace, rejects NaN and unserializable types |
| `digest` | `sha256:` over canonical JSON |
| `stable_sum` | fixed accumulation order, smallest magnitude first |
| `quantize_to_tick` | Decimal at 60-digit precision, `ROUND_HALF_EVEN` |

Half-even rather than half-up because repeated settlements under half-up drift systematically
upward. `quantize_to_tick(5537.125, "0.25") == 5537`, not 5537.5.

Verified: same spec + same data ⇒ identical `result_digest`; changing the payoff, tick size,
policy, contract id, reference id, or dataset each changes a digest.

---

## 8. Lookahead prevention

Four independent channels, each with a named test:

| Channel | Mechanism |
|---|---|
| Contract written after the fact | `published_at <= window.start`, enforced in `ContractSpec.__post_init__` |
| Snapshot fitted on the window it settles | `reference_as_of <= window.start`, enforced in `settle()`, raises `ReferenceLookahead` |
| Snapshot fitted on uncollected data | estimator filters on `observed_at <= as_of` **and** `window_end <= as_of` |
| Aggregate knowable before it happened | `observed_at >= window_end`, enforced in `AggregateRow` |
| Agent seeing uncollected data | `dataset.visible_at(t)` filters on `observed_at`, never on window bounds |

The last is the subtle one, and it lives at the *dataset* layer on purpose: an agent cannot forget
to apply it, because it never receives the full dataset. The research harness is the deliberate
exception, it evaluates the market against outcomes the market could not have known, which is the
entire point of measuring forecast error.

A lookahead bug does not crash and does not produce implausible numbers. It produces *better*
results. That is the worst possible failure mode, because nothing about the output invites
suspicion.

---

## 9. Judgment calls

### Resolved

| # | Was | Now |
|---|---|---|
| 1 | Weights hand-authored | Estimated as share of observed play volume, from a lookback strictly before `as_of`. Basis (slots/battles) is explicit and recorded |
| 2 | κ = 250, a guess | Method of moments, closed form. Validated out-of-sample: 355 vs an empirical optimum of 250 on a flat curve |
| 4 | Drop missing strata and renormalize | Imputed from prior via full shrinkage. No missing-at-random assumption |
| 5 | Shrink toward a mode-level prior | Hierarchical: stratum-level priors partially pooled toward mode-level |
| 12 | `as_of ≤ window.start` by convention | Enforced in `settle()`, raises `ReferenceLookahead` |

| 3 | Clustering unaccounted for | `design_effect` parameter divides each cell's count in the κ fit. Default 1.0 (independence), see below |
| n/a | Weight basis unresolved | Made second-order by the **lift** metric, which fixes the neutral point at 0 under any basis |
| n/a | Draws silently scored as losses | Stored separately, scored as half a win, which makes mode baselines draw-rate invariant |

### Still open

**3. The design effect is a parameter, not yet a measurement.** The correction is in place but
defaults to 1.0, which asserts independence, wrong, but honest, since DEFF cannot be estimated
without real clustered data.

The direction of the bias is worth stating precisely, because it is not obvious. The crawler
fetches a player's last 25 battles at once, so those share a pilot and therefore a skill level;
matchmaking correlates opponents further. Clustering inflates true sampling variance above the
binomial formula by `DEFF = 1 + (m−1)·ICC`. Understating the binomial component makes the residual
look like real between-cell variation, which inflates the between term, which makes **κ come out
too small**, so the metric shrinks thin cells too little and is noisier than it reports.

The collector already stores what's needed: every battle records `surfaced_by`, the player whose
log produced it. Estimating ICC by clustering on that is a first-real-data task.

**Note on within-battle dependence:** the six slots of one battle are *perfectly* dependent,
exactly three win. But that constrains the aggregate across brawlers, not a single brawler's rate,
where each battle contributes about one observation. The clustering that actually bites a single
brawler's rate is by pilot, not by battle.

**6. Rows must fit entirely inside the window; partial overlap is excluded, not pro-rated.**
Honest (a row is an already-aggregated count, and splitting one assumes uniform distribution
within it) but wasteful. The cleaner fix is upstream: have the collector emit aggregates on a grid
aligned to contract windows.

**7. Sub-window pooling has no per-period weighting.** If the crawler was down for three days of a
28-day window, those days simply contribute fewer battles. Usually right, but it silently
underweights any period the collector missed, which is not random either.

**8. `use_rate` doesn't shrink at all.** Defensible for zeros, but a use rate computed from six
slots is still noise. Shrinking toward the cross-brawler mean might be better, at the cost of the
clean "zero is real" property.

**9. `min_sample_size` applies per-atom, uniformly.** For a spread, both legs face the same bar
regardless of how sensitive the payoff is to each. Arguably the bar should reflect the contract's
sensitivity.

**10. `VOID` is the only missing-data policy.** No fallback waterfall to a wider universe, no
partial settlement. Conservative, and I think correct to start, but real exchanges do have
fallbacks.

**11. Basket weights need not sum to 1.** Deliberate, an index can be a sum rather than a mean,
but it means payoff scale and weight normalization interact in a way that is easy to get wrong.

**13. No YAML round-trip.** Specs are only constructible in Python, so they cannot be authored
outside code or diffed readably in git. Cheap to add; I left it out to keep the settlement core
dependency-free.

**14. Mixed float64 / Decimal.** The metric is float64 throughout, converted to Decimal only at
tick quantization. IEEE-754 `+`, `*`, `/` are deterministic across platforms in a fixed order, so
I believe this is sound, but it is an assumption worth stating rather than burying.

---

## 10. Test coverage (100 tests)

| File | Covers |
|---|---|
| `test_metrics.py` (18) | standardization cancelling drift, shrinkage, imputation vs renormalization, coverage semantics, prior fallback, slot denominators, sample-size accounting, order independence |
| `test_settlement.py` (23) | determinism, tick grid, provenance, digest sensitivity, every void path, reference mismatch and lookahead, all three instrument families, leg-order invariance |
| `test_contracts.py` (25) | lookahead invariants, visibility semantics, spec validation, determinism primitives |
| `test_estimation.py` (18) | no-lookahead filtering, weight derivation and slot deduplication, prior recovery and pooling, κ boundary behaviour, out-of-sample agreement, snapshot immutability |
| `test_collector.py` (16) | battle identity across log orderings, dedup, snowball tag extraction, frontier priority, restart durability, 403 diagnosis |

Three are load-bearing:

- `test_standardization_cancels_composition_drift`, if it fails, contracts are pricing the
  crawler rather than the game.
- `test_snapshot_dated_after_the_window_opens_is_rejected`, the lookahead channel that makes
  results *better*, and therefore the one nobody would catch by inspection.
- `test_out_of_sample_sweep_agrees_with_the_closed_form`, if κ drifts far from the predictive
  optimum, the beta prior is misspecified and the shrinkage is not trustworthy.
