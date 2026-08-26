# Setting up the collector

Complete setup, start to finish, at zero cost.

The collector is the only part of Arena Markets whose value depends on wall-clock time. Every
other phase can be rebuilt in an afternoon; the corpus can only be *accumulated*. So the goal of
this document is to get you collecting today, not to get you the perfect deployment eventually.

**Time to first battle stored: about 10 minutes.**

---

## The constraint, and why it turns out not to matter

Supercell API keys are locked to the IP addresses you declare when you create the key. On a home
connection that address changes without warning, and when it does the key stops working. This is
the single biggest operational annoyance in the whole project, and it is why most guides tell you
to rent a server with a static IP.

You do not need to. RoyaleAPI run a free community proxy for the Supercell APIs. You allow-list
**their** fixed address instead of yours, and route requests through them:

```
your machine  ──►  bsproxy.royaleapi.dev  ──►  api.brawlstars.com
                   (45.79.218.79)              sees the proxy's IP, not yours
```

Because Supercell only ever sees the proxy's address, your own IP becomes irrelevant. The
collector runs anywhere — laptop, home server, Raspberry Pi, any free cloud VM — and keeps
working when your address changes.

### Is routing through someone else's proxy safe?

You are handing a third party a credential, so the question deserves a straight answer rather than
a reassurance.

**What the proxy can see:** your API key, and which player tags you look up. The key travels in an
`Authorization` header, and the proxy has to read it to forward the request.

**What that key can actually do:** read public Brawl Stars game data. That is the entire scope.
It cannot touch your Supercell account, cannot write anything, cannot see private data, cannot
spend anything, and cannot be used to log in. It is also allow-listed to the proxy's own IP, so
even a leaked key only works from there.

**Worst realistic case:** someone else burns your rate limit. You revoke the key at
<https://developer.brawlstars.com/>, create another, and carry on. There is no lasting harm
available.

**Who runs it:** RoyaleAPI, a long-standing and named community operator behind RoyaleAPI.com.
Not an anonymous endpoint.

Sensible hygiene: create a key **used only for this**, never reuse it elsewhere, and revoke it if
anything looks odd.

**If you would rather not**, the direct route needs no third party at all:

```bash
python -m collectors.brawl_api --check
```

Allow-list your own public IP instead of the proxy's. The cost is that a home IP changes without
warning, and when it does the collector stops with a 403. That is not silent — `--check` reports
it, **and prints your current public IP** so fixing it is a copy-paste into the portal rather than
a hunt. On a typical home connection expect to do that every few weeks.

Both routes are supported and you can switch at any time by adding or dropping `--proxy` and
editing the key's allow-list to match. Nothing else changes.

---

## Part 1 — Get collecting (10 minutes)

### Step 1: Create the API key

1. Go to **<https://developer.brawlstars.com/>** and sign up. It is free and needs no payment
   details. You will need to verify your email.
2. Log in, then open **My Account → Create New Key**.
3. Fill it in:

   | Field | Value |
   |---|---|
   | Name | `arena-markets` |
   | Description | `Research data collection` |
   | **Allowed IP Addresses** | **`45.79.218.79`** |

   > **This is the step everything hinges on.** Enter the proxy's address, **not** your own.
   > The portal will helpfully pre-fill your current IP — delete it and replace it. If you leave
   > your own address in, every request will fail with a 403.

4. Click **Create Key**, then copy the token. It is long (a JWT, several hundred characters).
   You can view it again later, but copy it now.

### Step 2: Install

From the repository root:

```bash
pip install -e ".[collect,dev]"
```

### Step 3: Set the key

**Windows (PowerShell), current session:**

```powershell
$env:BRAWL_API_KEY = "paste-your-key-here"
```

**Windows, permanently:**

```powershell
[Environment]::SetEnvironmentVariable("BRAWL_API_KEY", "paste-your-key-here", "User")
```

**macOS / Linux:**

```bash
export BRAWL_API_KEY="paste-your-key-here"
```

Watch for a trailing newline or a truncated paste — both produce a confusing 403. The check in
the next step will tell you if that happened.

### Step 4: Verify before you commit to anything

```bash
python -m collectors.brawl_api --check --proxy
```

Success looks like:

```
endpoint  https://bsproxy.royaleapi.dev/v1
route     RoyaleAPI proxy
key must allow-list  45.79.218.79 (the proxy)

OK -- authenticated, 91 brawlers returned.
The key and IP configuration are correct. Seed the frontier next:
  python -m collectors.brawl_api --seed-only --proxy
```

If it fails, the error tells you *which* of the two possible problems you have — see
[troubleshooting](#troubleshooting) below. Do not proceed until this passes.

### Step 5: Seed the frontier

```bash
python -m collectors.brawl_api --seed-only --proxy --data-dir data/raw
```

This walks 60 country leaderboards and registers every player it finds. Expect roughly
8,000–12,000 players and about a minute of runtime.

Seeding across many countries rather than just the global board is deliberate: the trophy
threshold to appear on a small country's leaderboard is far lower than the global one, so a broad
seed spans a much wider skill range at identical request cost.

### Step 6: Start collecting

```bash
python -m collectors.brawl_api --proxy --data-dir data/raw
```

It runs until you stop it. Ctrl-C is safe at any point — all state is durable, and restarting
resumes exactly where it left off.

Expect log lines like:

```
2026-08-23T17:22:14Z INFO    batch 10 | 200 players | +3841 battles | +892 tags | frontier 14203 (2000 crawled) | corpus 38102 battles
```

**That's it. You are collecting.** Everything below is optional improvement.

---

## Part 2 — Running it 24/7 (also free)

The laptop setup above only collects while your machine is awake. Since the proxy removed the IP
constraint, *any* always-on machine works — you are no longer forced into a cloud provider with a
static IP.

In rough order of how little effort they take:

### Closing your laptop

Nothing breaks and nothing is lost. The collector's state is a SQLite file plus append-only
shards, so suspending, killing, or rebooting costs you the *hours* you were asleep and nothing
else — restart and it resumes from exactly the frontier position it had reached.

What it will not do is start itself. Use the supervisor so you never have to think about it:

```bash
python tools/run_collector.py -- --proxy --data-dir data/raw
```

It restarts the collector whenever it exits, backs off if something is genuinely broken instead of
spinning, and **stops outright** on the two failures a restart cannot fix (missing key, rejected
key) rather than hammering the API with a bad credential.

Then add a startup entry so it runs whenever the machine is awake:

- **Windows** — Task Scheduler → Create Task → Trigger "At log on" → Action: your `python.exe`,
  arguments `tools/run_collector.py -- --proxy`, Start in: the repo directory.
- **macOS** — a `launchd` plist with `RunAtLoad`.
- **Linux** — the systemd unit below already does it.

**The honest caveat:** coverage gaps from a sleeping laptop are *not* missing-at-random. They
correlate with your timezone, and therefore with which regions were playing. That biases the crawl
in a way standardization does not fully remove, because it shifts which *players* you reach rather
than only which strata. It is a real argument for an always-on host eventually. It is not an
argument for delaying the start — a gappy corpus that exists beats a perfect one that does not.

### Option A — A machine you already own

A spare laptop, a home server, a Raspberry Pi, or just leaving your desktop on. Genuinely the
simplest choice, with no signup, no credit card, and no capacity lottery. The workload is tiny:
under 8 requests/second of network traffic and a few hundred MB of RAM.

On Linux, run it under systemd — see the [unit file](#systemd-unit) below. On Windows, Task
Scheduler with "run whether user is logged on or not" works.

### Option B — Oracle Cloud Always Free

The only major cloud whose free tier is permanent rather than a 12-month trial.

**Two warnings before you start:**

1. **Signup requires a credit card for identity verification.** It is not charged on an Always
   Free account, but if "no card at all" is a hard requirement, use Option A.
2. **Do not chase the ARM instance.** The 2 OCPU / 12 GB Ampere A1 shape is what everyone wants
   and it is nearly always out of capacity — provisioning it reliably means upgrading to
   Pay-As-You-Go, which is a real billing account with real risk of charges. Oracle also
   [halved that allowance in June 2026 without announcing it](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/).

   **Use the AMD shape instead: `VM.Standard.E2.1.Micro`.** You get two of them free forever,
   they are almost always available, and 1/8 OCPU with 1 GB RAM is ample for a network-bound
   crawler. This sidesteps the entire capacity problem.

Setup:

1. Sign up at <https://cloud.oracle.com/>, choosing a home region near you.
2. **Compute → Instances → Create Instance.**
3. Under **Image and shape → Change shape**, pick **Ampere?** No — pick **Specialty and previous
   generation → `VM.Standard.E2.1.Micro`**. Confirm it shows the *Always Free eligible* badge.
4. Choose an Ubuntu image, upload or generate an SSH key, and create.
5. SSH in and set it up:

```bash
sudo apt update && sudo apt install -y python3-pip git
git clone <your-repo-url> arena-markets && cd arena-markets
pip install -e ".[collect]"
```

6. Set the key, verify, and seed exactly as in Part 1.

Because you are using the proxy, you do **not** need a reserved public IP and you do **not** need
to touch the key's allow-list again. That is the whole point.

### Option C — Google Cloud free tier

A single `e2-micro` in `us-west1`, `us-central1`, or `us-east1` is always-free and works fine.
Same caveat on the credit card. Same setup steps.

### systemd unit

```ini
# /etc/systemd/system/arena-collector.service
[Unit]
Description=Arena Markets Brawl collector
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/arena-markets
Environment=BRAWL_API_KEY=REPLACE_ME
Environment=PYTHONPATH=/home/ubuntu/arena-markets
ExecStart=/usr/bin/python3 -m collectors.brawl_api --proxy --data-dir data/raw
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now arena-collector
sudo journalctl -u arena-collector -f
```

### Migrating between hosts

The entire collector state is one SQLite file plus append-only gzip shards, so moving hosts or
merging a laptop's head start into a server is a copy:

```bash
rsync -av data/raw/ user@host:~/arena-markets/data/raw/
```

Back this up somewhere periodically. Free cloud instances do occasionally get reclaimed, and the
corpus is the one thing in this project you cannot regenerate.

---

## Why collect at all, when stats sites already exist?

A fair challenge, and the answer has two halves.

### There is no public API that serves the statistics

This was checked rather than assumed. [BrawlAPI](https://brawlapi.com/) — the most open source
available, requiring no key, no token, and imposing no rate limit — serves **only static reference
data**: the brawler roster, the map catalog, game modes, icons, and raw game config. Its `/v1/maps`
endpoint returns empty `stats` and `teamStats` arrays. Its own documentation is explicit that it
does not serve win rates, pick rates, or meta analytics.

The sites that *do* have those numbers — Brawl Time Ninja, Brawlify, BrawlMeta, BrawlVision —
compute them internally from their own crawls and expose them through their web UIs, not through
an API. BrawlMeta describes doing exactly what this collector does: storing match history hourly,
because the official API only returns the last 25 games.

So "just use an existing API" is not an option that exists. The remaining alternative is scraping a
stats site's web pages.

### On scraping — a fair question, and a more nuanced answer

"It is a public page" is a reasonable starting point, and largely right on the narrow legal
question: courts have generally held that accessing publicly available data is not unauthorized
access. Reading public pages for research is normal and lawful in many contexts.

The complications are not really about legality:

- **Public accessibility is not a licence to redistribute.** Whatever we do privately, a public
  research repository that republishes another site's dataset is a different act, and the one this
  project actually needs to do.
- **Brawl Time Ninja returns HTTP 403 to automated fetchers.** That is an explicit signal, and
  routing around it is a different act from viewing a page in a browser.
- **They already built the front door.** The dashboard has an *Export CSV* button. Using the export
  path they provided is unambiguous, requires no evasion, and gets the same numbers.
- **Layout fragility.** A scraper breaks silently when markup changes, and a settlement metric that
  silently changes definition is the specific failure this whole layer exists to prevent.

But the decisive objection is technical rather than any of those: **their pages do not carry what
we need anyway.** A stats site shows current win rates. Replay needs per-observation "knowable at"
timestamps. You could recover those by snapshotting the site repeatedly and stamping each
snapshot — but that *is* building a collector, just one with more legal ambiguity and less control
over the population definition.

The recommendation is therefore not "scraping is wrong", it is: **use the export button for
bootstrapping and validation, and run the collector for the longitudinal record.** Manual export
is periodic, low-effort, and gets you exactly the calibration data that is genuinely useful.

### Even a perfect stats API would not settle contracts

This is the deeper reason, and it is specific to this project rather than a general preference.

A stats site tells you **what is true now**. Historical replay needs **what was knowable then** —
every observation tagged with when it became available, so an agent standing at time *t* can be
given exactly the information that existed at *t* and nothing more. Strip that out and the
no-lookahead guarantee this whole layer is built around becomes unenforceable, and every research
result becomes unfalsifiable.

Three consequences follow:

- **Provenance.** A settlement must be able to name the bytes it came from. Brawl Time Ninja moved
  to a private repository, so its methodology is no longer readable — if it silently changed how
  "adjusted win rate" is computed, every past settlement derived from it would become
  indefensible, with no way to detect that it had happened.
- **Population.** Brawl Time Ninja states its data comes from *its visitors*, "usually better than
  the average". That is a fine population; it is simply not a stated one, and our contracts have to
  name the population they measure.
- **Licensing.** A public research artifact cannot be built on data we have no right to
  redistribute.

### What third-party data is genuinely good for

Not nothing. An occasional **manual** CSV export from Brawl Time Ninja is a legitimate way to
sanity-check our own aggregates and shape priors, and it is what the roadmap recommends. Manual
export, never automated ingestion, never redistribution.

And BrawlAPI is worth adopting for what it *does* serve: a free, keyless, authoritative roster of
all ~107 brawlers and ~1,239 maps. That is exactly the reference data needed to define strata, and
to run the mechanical-baseline check, which is only meaningful over a corpus covering every
brawler.

### And the honest caveat

Brawl Time Ninja reports a sample on the order of hundreds of millions of battles. A single
well-behaved collector accrues perhaps 10^5–10^6 per day. We will not catch up, and should not try.

That is survivable because **the core research does not need to.** The flagship experiment asks
whether a market aggregates information better than its constituent agents, and answering it
requires knowing the *true* probability — which a synthetic, calibrated world provides exactly and
real data never can. Real replay is the external-validity test that comes afterwards, and it needs
a defensible corpus, not the largest one.

## Checking on it

```bash
python -m collectors.brawl_api --status --data-dir data/raw
```

```
players known    : 184,220
players crawled  : 96,441
battles stored   : 2,317,905
  battlelogs_fetched   96,441
  battles_new          2,317,905
  fetch_errors         41
  players_missing      212
```

What the numbers should do over time:

| Signal | Healthy | What it means if not |
|---|---|---|
| `battles_new` per battlelog | starts near 25, declines | If it hits ~0, the frontier has saturated — **raise** `--recrawl-hours`, don't lower it |
| `players known` | climbs, then plateaus at `max_frontier` | Flat early means the snowball isn't expanding; check for fetch errors |
| `fetch_errors` | near zero | Climbing steadily means throttling — lower `--rate` |
| `players_missing` | small and slow | Deleted accounts. Normal |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 ... as invalid` / "not an IP problem" | Key is wrong, truncated, or was deleted | Re-copy the key. Check for a trailing newline |
| `403 ... allow-list 45.79.218.79` | You allow-listed your own IP instead of the proxy's | Edit the key at developer.brawlstars.com, replace your IP with `45.79.218.79` |
| `403` when running **without** `--proxy` | Your IP changed, or the key targets the proxy | Add `--proxy`. That is the whole reason it exists |
| `BRAWL_API_KEY is not set` | Env var not exported, or set in a different shell | Re-export in the shell you're running from |
| Many `fetch_errors`, slow progress | Rate limiting | `--rate 5` |
| "frontier exhausted; sleeping" repeatedly | Everything is inside its re-crawl interval | Normal on a small frontier. Re-seed or lower `--recrawl-hours` |

---

## Reference

```
--proxy                 route via RoyaleAPI (allow-list 45.79.218.79, not your IP)
--check                 verify key + IP configuration, then exit
--seed-only             populate the frontier from leaderboards, then exit
--status                print corpus statistics, then exit
--data-dir PATH         where state lives (default: data/raw)
--rate FLOAT            requests/second (default 7.5; the API ceiling is ~10)
--concurrency INT       in-flight battlelog fetches (default 4)
--batch-size INT        players per batch (default 200)
--recrawl-hours FLOAT   minimum age before refetching a player (default 12)
-v/--verbose            debug logging
```

## Storage, and keeping it small

Raw battles are not cheap. Measured on realistic records:

| Rate | Raw JSONL (gzipped) | After a year |
|---|---|---|
| 100k battles/day | 0.68 GB/month | 8 GB |
| 500k battles/day | 3.40 GB/month | **41 GB** |
| 1M battles/day | 6.80 GB/month | **82 GB** |

That is a real problem on a laptop, and the answer is to roll raw up into the aggregates
settlement actually reads, then prune:

```bash
python tools/rollup.py --data-dir data/raw --out data/derived --prune-after-days 7
```

Measured end to end on 60,000 synthetic battles: a 1.93 MB raw shard becomes a 68 KB aggregate
file — **28x smaller**, and that is before compressing the CSV.

The reason it collapses so far is that **aggregate size is bounded by brawlers × strata, not by
battle count**. Ten times the battles produce the same number of rows, just with bigger counts in
them. So aggregates grow with *time*, not with *volume*: on the order of a few MB per month
whatever the crawl rate, indefinitely affordable.

Steady state at 500k/day with a 7-day raw buffer is roughly **0.8 GB of raw plus a few MB per
month of aggregates**, instead of 41 GB a year.

Run the rollup on a schedule (weekly is plenty) alongside the collector.

**Pruning is opt-in and never deletes a shard it did not just roll up successfully.** Raw battles
are evidence — a settlement months from now may need to name the bytes it came from — so keeping a
buffer means a normalization bug stays recoverable. Trading that auditability for disk is your
call, not a default. `--prune-after-days 0` (the default) deletes nothing.

### The mechanical check runs on every rollup

```
mechanical baseline check:
  brawlBall        0.5000   vs 0.5000  gap +0.0000   ok
  gemGrab          0.5000   vs 0.5000  gap +0.0000   ok
```

Because a battlelog names every participant of a 3v3, the pooled win rate across all brawlers
*must* be 0.500. A gap means dropped participants, double-counted battles, or — the nasty one — the
log owner's team perspective inverted, which would corrupt half the corpus while leaving every
number looking perfectly plausible.

Showdown is excluded from the verdict. The API reports a `rank` for the log owner only, so we can
score one slot in ten, and that one belongs to a player the crawler chose to visit. Its pooled rate
reflects crawled-player skill rather than the game's 0.450.

**Practical consequence: write the first contracts on 3v3 modes.** Showdown win rates rest on a
tenth of the evidence plus a selection effect nobody has modelled. Use rate is fine in both, since
every occupied slot is observed whether or not its placement is.

### What it stores

```
data/raw/
  collector.db              SQLite: player frontier + battle dedupe index
  battlelogs/
    2026-08-23.jsonl.gz     append-only, one gzip shard per day
```

Each line of a shard is one battle, **verbatim as the API returned it**, wrapped with its identity
key, the fetch time, and the tag of the player whose log surfaced it. Battles are stored once even
though the API returns each of them in up to six different players' logs.

Nothing in `data/raw/` is committed to git. What *is* committed is everything needed to reproduce
a result except the crawl itself: the collection code, the frozen reference snapshots, the
contract specs, and the deterministic fixtures.

### Rate limits and politeness

The documented ceiling is about 10 requests/second per key, throttled with HTTP 429. The default
here is 7.5, and the client backs off with full jitter on 429 and 5xx. There is no reason to push
closer to the line: the binding constraint on corpus growth is how fast *players play*, not how
fast we can ask. 404s are never retried — a deleted tag will not come back, and retrying it spends
budget the rest of the crawl needs.

The proxy is a free community service. Staying well under the limit is the rent.

## Could we skip the crawl and take the numbers from someone else?

Asked properly, and answered: **no.** Surveyed August 2026, probing endpoints rather
than reading marketing copy.

The requirement is specific, and most sources fail it on the first clause: win rates by
**brawler x mode x map x trophy bucket**, with **sample sizes** so precision is knowable,
as a **historical series** so a contract can settle on a measured window rather than on
whatever the number happens to be today.

| Source | Has win rates? | Historical? | Sample sizes? | Reachable? |
|---|---|---|---|---|
| Official Supercell API | no — per-player only | n/a | n/a | yes, via the proxy |
| Brawlify / BrawlAPI | **no, static metadata only** | n/a | n/a | yes, free, no key |
| Brawl Time Ninja | yes | probably | probably | **no — authenticated** |
| BrawlStats, BrawlVision, Brawl Planet, Brawl Data | yes, on screen | no | no | **no — 403 / 429 / reset** |
| Kaggle battle-log dumps | computable from raw | **frozen 2021 / 2023** | yes | account needed |

What the probes actually returned:

* `api.brawlify.com` and `brawlapi.com` are **static metadata** — brawlers, 1,239 maps,
  68 game modes, icons. Free, no key, no rate limit, CORS open. The documentation says
  plainly that it carries no competitive statistics. Genuinely useful, but for the
  *universe definition* rather than the metric: it is the authoritative list of what
  strata exist, which the reference snapshot needs anyway.
* `cube.brawltime.ninja/cubejs-api/v1/meta` returns `{"error":"Authorization header
  isn't set"}`. So Brawl Time Ninja computes exactly these aggregates behind a **Cube.js**
  backend — and it is closed. Its own site API answers in **tRPC** envelopes, undocumented,
  and its source repository has gone private. The data exists and is not on offer.
* The other meta sites answered 403, 429, or a reset connection to anything that was not
  a browser. They are consumer websites, not data providers, and scraping them would be
  taking a service rather than using one.
* The official API's per-player battlelog returns roughly the **last 25 battles**. Third
  parties confirm it from the other side: the ones publishing hourly meta stats say they
  poll continuously *because* of that ceiling. Everyone computing these numbers is
  running the crawl we are describing.

### The one genuinely useful finding

The Kaggle dumps are **raw battle logs**, which is the right shape — the aggregation is
ours to do either way. They are stale (2021 and 2023), so they cannot settle a 2026
contract. But everything in this repository currently runs on `fixture-synthetic-v1`,
and a real hundred-thousand-battle log would put the **normalizer, the mode mechanics,
the shrinkage and the standardization** in front of real data for the first time — no
API key, no crawl, no waiting.

That is worth doing before the collector ever runs, because it separates two failures
that would otherwise arrive together: "the metric layer is wrong" and "the crawl is
wrong". Validating against a stale corpus answers the first one on its own.

### What this does not change

The crawl is still the only path to what the contracts actually settle on, and it is
still the only part of this project with a real clock on it. A battlelog holds about
25 battles; a window not collected in August 2026 cannot be collected in October.
