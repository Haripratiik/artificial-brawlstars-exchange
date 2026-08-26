/* The screens.
 *
 * Each view is a pure function from store state to HTML. Keeping them pure
 * means the socket can re-render without any view holding state that could
 * drift from the market's. The one exception is the order ticket, which holds
 * live input and is moved across a re-render rather than rebuilt.
 *
 * The information order on the trade screen is not arbitrary. Prediction-market
 * design guidance is consistent about it, and this interface previously had it
 * backwards:
 *
 *   1. what the contract is, and the odds
 *   2. **how it resolves** — above the fold, not buried
 *   3. the price history
 *   4. recent trades
 *   5. the order book, *collapsed by default*
 *   6. contract specifications
 *
 * Leading with a depth ladder is right for an operator and wrong for everyone
 * else: it was the most technical thing on the page and it was the first thing
 * you saw, while the resolution rules were squeezed into a strip above it —
 * which is the documented anti-pattern, burying the terms beneath the pricing.
 */

import {
  clock, cls, count, describe, esc, impliedProbability, money, percent,
  price, priceChart, signed, sparkline,
} from './format.js';

/* ── markets ─────────────────────────────────────────────────────────── */

/**
 * Asset classes, in the words a person would use.
 *
 * The venue's own vocabulary is precise and unhelpful for browsing: "event" is
 * a prediction market and "call"/"put" are options. Grouping under these makes
 * the shape of the exchange visible, which a flat grid of seven tiles does not
 * — you could not tell, looking at the old page, that this venue lists
 * prediction markets and options at all.
 */
const CLASS_GROUPS = [
  ['event', 'Prediction Markets', 'Pays a fixed amount if the outcome happens.'],
  ['future', 'Futures', 'Settles at the measured rate itself.'],
  ['call', 'Options', 'The right to what lies past a strike.'],
  ['put', 'Options', 'The right to what lies past a strike.'],
  ['equity', 'Shares', 'Pays out every week it is alive, then expires. Worth the payments that are left.'],
  ['commodity', 'Commodities', 'An amount delivered in one week, not a rate. Each week trades separately.'],
  ['spread', 'Spreads', 'One Brawler priced against another.'],
  ['index', 'Indices', 'A weighted basket of several Brawlers.'],
];

const GROUP_ORDER = ['Prediction Markets', 'Futures', 'Shares', 'Commodities',
                     'Options', 'Spreads', 'Indices', 'Other'];

export function groupOf(assetClass) {
  return CLASS_GROUPS.find(([key]) => key === assetClass)?.[1] ?? 'Other';
}

function groupBlurb(title) {
  return CLASS_GROUPS.find(([, name]) => name === title)?.[2] ?? '';
}


/**
 * Discovery. A card carries the question, the odds, activity and time left,
 * and one action.
 *
 * Deliberately *not* on a card: order-book depth, tick size, settlement bounds,
 * spec digests. Those are specifications, and putting them on a browsing
 * surface is the fastest way to make an exchange feel like a database viewer.
 */
export function markets(store) {
  const { snapshot, instruments, history } = store;
  const books = snapshot?.books ?? {};
  const symbols = Object.keys(books).filter((s) => matches(s, books[s], store.query));
  if (!symbols.length) {
    return `<div class="view"><div class="onboard">
      <h2>${store.query ? 'Nothing matches that' : 'Connecting to the exchange&hellip;'}</h2>
      <p>${store.query
        ? `No market matches &ldquo;${esc(store.query)}&rdquo;. Try a ticker, a Brawler, or a class like future or event.`
        : 'Contracts here settle on measured Brawl Stars statistics.'}</p>
    </div></div>`;
  }

  const card = (symbol) => {
      const book = books[symbol];
      const meta = instruments.find((x) => x.symbol === symbol) || {};
      const series = (history[symbol] || []).slice(-90);
      const first = series.find((v) => v != null);
      const last = series.length ? series[series.length - 1] : null;
      const change = first != null && last != null ? last - first : 0;
      const pct = first ? (change / first) * 100 : 0;
      const session = snapshot.sessions?.[symbol] ?? 'continuous';
      const odds = impliedProbability(book.mark, book.contract?.payoff);
      const headline = odds != null ? percent(odds, 0) : price(book.mark);

      return `<button type="button" class="card" data-symbol="${esc(symbol)}"
                   data-region="card:${esc(symbol)}"
                   data-session="${esc(session)}"
                   aria-label="${esc(symbol)}, ${headline}, ${signed(pct)} percent">
        <span class="card-top">
          <span class="sym">${esc(symbol)}</span>
          ${session !== 'continuous'
            ? `<span class="badge ${esc(session)}">${esc(session.replace('_', ' '))}</span>`
            : `<span class="kind">${esc(book.class ?? '')}</span>`}
        </span>

        <span class="question">${question(book.contract)}</span>

        <span class="card-figure">
          <span class="price mono ${cls(change)}">${headline}</span>
          <span class="chg mono ${cls(change)}">${signed(pct)}%</span>
        </span>

        <span class="spark" aria-hidden="true">${sparkline(series)}</span>

        <span class="card-foot">
          <span>${count(book.trades)} trades</span>
          <span>${expiry(book.contract, meta)}</span>
        </span>
      </button>`;
  };

  const buckets = new Map();
  for (const symbol of symbols) {
    const title = groupOf(books[symbol].class);
    (buckets.get(title) ?? buckets.set(title, []).get(title)).push(symbol);
  }

  const sections = GROUP_ORDER.filter((t) => buckets.has(t))
    .map((title) => `<section class="class-block">
      <div class="class-head">
        <h2>${esc(title)}</h2>
        <span>${esc(groupBlurb(title))}</span>
        <b class="mono">${buckets.get(title).length}</b>
      </div>
      <div class="grid">${buckets.get(title).map(card).join('')}</div>
    </section>`)
    .join('');

  return `<div class="view">${sections}</div>`;
}

/**
 * Does this market match what someone typed?
 *
 * Searches the ticker, the asset class and the plain-language question, because
 * those are the three things a person might have in mind. Matching only the
 * ticker would mean you had to already know the ticker, which defeats the
 * point of searching.
 */
export function matches(symbol, book, query) {
  const needle = (query ?? '').trim().toLowerCase();
  if (!needle) return true;
  const haystack = [
    symbol,
    book?.class ?? '',
    question(book?.contract),
    subjectName(book?.contract?.underlying),
  ].join(' ').toLowerCase();
  return needle.split(/\s+/).every((word) => haystack.includes(word));
}

/** The contract as a question, which is how a person holds it in their head. */
export function question(contract) {
  const p = contract?.payoff;
  if (!p) return '';
  const subject = esc(subjectName(contract?.underlying));
  // Only a single-Brawler contract reads naturally with the metric attached.
  // A basket or a difference already names what it measures, and forcing the
  // metric in produced "the Assassin index's win rate" and "SPIKE vs CROW's
  // adjusted win rate" -- grammatical wreckage on the most-read line of the
  // whole exchange.
  const simple = contract?.underlying?.kind === 'single';
  const what = simple ? `${subject}'s ${esc(metricName(contract.underlying))}` : subject;

  if (p.kind === 'binary') {
    const direction = String(p.comparison).includes('>') ? 'above' : 'below';
    return `Will ${what} finish ${direction} ${p.threshold}?`;
  }
  // For a delivery contract the week *is* the contract: the same deliverable in
  // a different week is a different instrument, which is what gives a commodity
  // its term structure. Saying "where it settles" would hide the only thing
  // distinguishing one rung from the next.
  // A share is worth the stream, so the stream is the question. Saying
  // "where it settles" would be the one number that is always zero.
  const stream = contract?.distribution;
  if (stream) {
    return `A share of ${what}, paid out over ${count(stream.periods)} weeks`;
  }
  if (contract?.underlying?.ref?.kind === 'quantity') {
    return `How many thousand battles ${subject} plays, delivered ${esc(week(contract))}`;
  }
  if (p.kind === 'call') return `${what} above ${p.strike} at settlement`;
  if (p.kind === 'put') return `${what} below ${p.strike} at settlement`;
  return `Where ${what} settles`;
}

/**
 * The Brawler a contract is written on.
 *
 * The metric reference arrives under `ref`, not `metric` -- reading the wrong
 * key made every question on the exchange read "Will the metric finish above
 * 0.48?", which is a contract nobody could identify. It failed silently
 * because the fallback was a plausible English phrase rather than an error.
 */
/** "week to 7 Sep", from a contract's expiry. */
function week(contract) {
  const raw = contract?.expiry;
  if (!raw) return 'that week';
  const when = new Date(raw);
  if (Number.isNaN(when.getTime())) return 'that week';
  return `week to ${when.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}`;
}

function subjectName(underlying) {
  if (!underlying) return 'the metric';
  if (underlying.kind === 'single') return underlying.ref?.subject ?? 'the metric';
  if (underlying.kind === 'difference') {
    return `the gap between ${subjectName(underlying.left)} and ${subjectName(underlying.right)}`;
  }
  if (underlying.kind === 'basket') return 'the Assassin index';
  return 'the metric';
}

/** "adjusted win rate", from "adjusted_win_rate". */
function metricName(underlying) {
  const raw = underlying?.ref?.metric ?? underlying?.left?.ref?.metric;
  return raw ? String(raw).replace(/_/g, ' ') : 'win rate';
}

function expiry(contract, meta) {
  const raw = contract?.expiry ?? meta.expiry;
  if (!raw) return '';
  const days = Math.round((new Date(raw) - Date.now()) / 86_400_000);
  if (!Number.isFinite(days)) return esc(String(raw));
  if (days < 0) return 'settled';
  if (days === 0) return 'settles today';
  return `${days}d left`;
}

/* ── trade ───────────────────────────────────────────────────────────── */

export function trade(store) {
  const { snapshot, instruments, symbol, history, depth } = store;
  const book = snapshot?.books?.[symbol];
  if (!book) {
    return `<div class="view"><div class="onboard">
      <h2>Pick a market</h2><p>Choose a contract from the list to start trading.</p>
    </div></div>`;
  }

  const meta = instruments.find((x) => x.symbol === symbol) || {};
  const session = snapshot.sessions?.[symbol] ?? 'continuous';
  const series = history[symbol] || [];
  const position = (snapshot.account?.positions ?? []).find((p) => p.symbol === symbol);

  return `<div class="market">
    <div class="market-main">
      <div data-region="head">${contractHead(symbol, book, meta, session)}</div>
      <div data-region="resolution">${resolution(book, meta)}</div>

      <div class="panel" data-region="chart">
        <h2>Price <em>${series.length} points</em></h2>
        <div class="panel-body chart-body">
          ${priceChart(series, {
            settlesAt: store.reveal ? (meta.settles_at ?? null) : null,
            label: symbol,
          })}
        </div>
      </div>

      <div class="panel" data-region="tape">
        <h2>Recent Trades</h2>
        <div class="panel-body">${tape(snapshot, symbol)}</div>
      </div>

      <div class="panel" data-region="counterparties">
        <h2>Who Filled You <em>${esc(symbol)}</em></h2>
        <div class="panel-body">${counterparties(snapshot, symbol)}</div>
      </div>

      <details class="panel drop" data-region="book">
        <summary><h2>Order Book</h2><span class="hint">depth at every price</span></summary>
        <div class="panel-body">${ladder(book, depth)}</div>
      </details>

      <details class="panel drop" data-region="spec">
        <summary><h2>Contract Specification</h2><span class="hint">the exact terms</span></summary>
        <div class="panel-body">${specification(book)}</div>
      </details>
    </div>

    <aside class="market-side">
      <div class="panel tkt" data-region="ticket">
        <h2>Trade</h2>
        <div class="panel-body">
          <div class="ticket" id="ticket">${ticket(symbol, book, session)}</div>
        </div>
      </div>

      <div class="panel" data-region="position">
        <h2>Your Position</h2>
        <div class="panel-body">${positionCard(position)}</div>
      </div>

      <div class="panel" data-region="orders">
        <h2>Working Orders <em>${(snapshot.orders || []).length}</em></h2>
        <div class="panel-body">${orders(snapshot)}</div>
      </div>
    </aside>
  </div>`;
}

function contractHead(symbol, book, meta, session) {
  const odds = impliedProbability(book.mark, book.contract?.payoff);
  return `<header class="contract" data-session="${esc(session)}">
    <div class="contract-id">
      <span class="sym mono">${esc(symbol)}</span>
      <span class="badge ${esc(session)}">${esc(session.replace('_', ' '))}</span>
      <span class="kind">${esc(book.class ?? '')}</span>
    </div>
    <h1 class="question">${question(book.contract)}</h1>
    <div class="contract-figures">
      ${odds != null
        ? `<div class="stat big"><b class="mono amber">${percent(odds)}</b>
             <span>Implied Odds</span></div>`
        : ''}
      <div class="stat big"><b class="mono">${price(book.mark)}</b><span>Last</span></div>
      <div class="stat"><b class="mono">${book.spread == null ? '—' : price(book.spread)}</b><span>Spread</span></div>
      <div class="stat"><b class="mono">${count(book.trades)}</b><span>Trades</span></div>
      <div class="stat"><b class="mono">${expiry(book.contract, meta)}</b><span>Expiry</span></div>
    </div>
  </header>`;
}

/**
 * How this contract resolves, stated plainly and placed above the fold.
 *
 * Burying resolution rules beneath the price is the documented anti-pattern,
 * and it is the one that matters most: a contract whose terms nobody can read
 * is not a market, it is a slot machine with a chart attached.
 */
function resolution(book, meta) {
  const p = book.contract?.payoff ?? {};
  const settles = meta.settles_at;
  const bounds = (book.bounds ?? []).map((b) => price(b)).join(' and ');
  // A share settles at nothing and is worth the stream, so the range is what
  // it can be *worth*, and the revealed number is what it pays out in total.
  const stream = book.contract?.distribution;
  return `<section class="resolution">
    <h2>How This Resolves</h2>
    <p>${describe(book.contract)}</p>
    <ul>
      <li><span>Measured over</span>
          <b>the observation window ending ${esc(book.contract?.expiry ?? 'expiry')}</b></li>
      <li><span>${stream ? 'Can be worth' : 'Settles between'}</span>
          <b class="mono">${bounds || '—'}</b></li>
      ${stream
        ? `<li><span>Pays</span><b>${count(stream.periods)} times, the last on
             ${esc(stream.last)}</b></li>`
        : ''}
      ${p.kind === 'binary'
        ? `<li><span>Pays</span><b class="mono">${price(p.payout)} if it happens, ${price(0)} if not</b></li>`
        : ''}
    </ul>
    ${settles != null
      ? `<details class="spoiler">
           <summary>Reveal what this is worth</summary>
           <p>${stream
             ? `This share pays out <b class="mono up">${price(settles)}</b> in total
                across its life.`
             : `This contract settles at <b class="mono up">${price(settles)}</b>.`}</p>
           <p class="note">Only a simulation can tell you this, and printing it on
             the page turns a prediction market into a countdown: there is nothing
             left to discover and nothing to disagree about. Kept behind a click so
             the market can do its job, and available because being able to check
             the price against the truth is the whole point of building one.</p>
         </details>`
      : ''}
  </section>`;
}

function specification(book) {
  const rows = [
    ['Contract', book.contract?.id],
    ['Class', book.class],
    ['Tick size', book.tick],
    [book.contract?.distribution ? 'Value range' : 'Settlement range',
     (book.bounds ?? []).join(' … ')],
    ['Expiry', book.contract?.expiry],
    ['Spec digest', book.contract?.digest],
  ];
  return `<table><tbody>${rows
    .map(([label, value]) => `<tr>
      <td style="text-align:left" class="faint">${esc(label)}</td>
      <td>${value == null || value === '' ? '—' : esc(String(value))}</td>
    </tr>`)
    .join('')}</tbody></table>`;
}

/**
 * The ladder, now behind a disclosure.
 *
 * Bids and asks share a price column so the shape of the book is one vertical
 * scan; the bars show *cumulative* size, because that is what answers the
 * question anyone sizing an order is actually asking — what it costs to get
 * through a level. Each row fills the ticket at its price.
 */
function ladder(book, depth) {
  const bids = (depth?.bids ?? book.bids ?? []).map(([p, q]) => [Number(p), q]);
  const asks = (depth?.asks ?? book.asks ?? []).map(([p, q]) => [Number(p), q]);
  if (!bids.length && !asks.length) return `<div class="empty">No resting orders.</div>`;

  const byPrice = new Map();
  bids.forEach(([p, q]) => byPrice.set(p, { ...(byPrice.get(p) || {}), bid: q }));
  asks.forEach(([p, q]) => byPrice.set(p, { ...(byPrice.get(p) || {}), ask: q }));

  const cumulativeBid = new Map();
  let runningBid = 0;
  for (const [p, q] of bids) { runningBid += q; cumulativeBid.set(p, runningBid); }
  const cumulativeAsk = new Map();
  let runningAsk = 0;
  for (const [p, q] of asks) { runningAsk += q; cumulativeAsk.set(p, runningAsk); }

  const peak = Math.max(1, runningBid, runningAsk);
  const mark = Number(book.mark);
  const rows = [...byPrice.entries()].sort((a, b) => b[0] - a[0]);
  let nearest = 0;
  rows.forEach(([p], i) => {
    if (Math.abs(p - mark) < Math.abs(rows[nearest][0] - mark)) nearest = i;
  });

  return `<div class="ladder">${rows
    .map(([p, side], i) => {
      const bidW = ((cumulativeBid.get(p) || 0) / peak) * 46;
      const askW = ((cumulativeAsk.get(p) || 0) / peak) * 46;
      return `<button type="button" class="lad-row ${i === nearest ? 'at-mark' : ''}"
                   data-price="${p}"
                   aria-label="Price ${price(p)}, ${side.bid || 0} bid, ${side.ask || 0} offered">
        <span class="bar bid" style="width:${bidW.toFixed(1)}%" aria-hidden="true"></span>
        <span class="bar ask" style="width:${askW.toFixed(1)}%" aria-hidden="true"></span>
        <span class="bidq">${side.bid ? count(side.bid) : ''}</span>
        <span class="px">${price(p)}</span>
        <span class="askq">${side.ask ? count(side.ask) : ''}</span>
      </button>`;
    })
    .join('')}</div>`;
}

/**
 * The ticket, in two layers.
 *
 * Everything a first-time trader needs is visible: which side, how many, and
 * what it costs against what it pays. Limit price, time in force and post-only
 * are real and reachable, but they sit behind a disclosure — a form that opens
 * on "time in force" has already lost most of the people looking at it.
 */
function ticket(symbol, book, session) {
  const halted = session !== 'continuous';
  const binary = book.contract?.payoff?.kind === 'binary';
  return `<div class="sides">
      <button type="button" data-side="buy" aria-pressed="true">
        ${binary ? 'Yes' : 'Buy'}<em>${binary ? 'it happens' : 'go long'}</em>
      </button>
      <button type="button" data-side="sell" aria-pressed="false">
        ${binary ? 'No' : 'Sell'}<em>${binary ? 'it does not' : 'go short'}</em>
      </button>
    </div>

    <div class="field">
      <label for="t-qty">Contracts</label>
      <input id="t-qty" type="number" min="1" step="1" value="10"
             inputmode="numeric" autocomplete="off" spellcheck="false">
    </div>
    <div class="quick" role="group" aria-label="Quick size">
      ${[5, 25, 100, 250].map((n) => `<button type="button" data-qty="${n}">${n}</button>`).join('')}
    </div>

    <div class="preview" id="t-preview" aria-live="polite"></div>

    <button type="button" class="send" id="t-send" ${halted ? 'disabled' : ''}>
      ${halted ? `${esc(session.replace('_', ' '))} &mdash; orders will rest` : 'Place Order'}
    </button>

    <details class="advanced">
      <summary>Advanced</summary>
      <div class="field">
        <label for="t-px">Limit price &mdash; blank trades at market</label>
        <input id="t-px" type="text" inputmode="decimal" placeholder="4660.25&hellip;"
               autocomplete="off" spellcheck="false">
      </div>
      <div class="field">
        <label for="t-tif">Time in force</label>
        <select id="t-tif">
          <option value="gtc">Good till cancelled</option>
          <option value="ioc">Immediate or cancel</option>
          <option value="fok">Fill or kill</option>
          <option value="post_only">Post only</option>
        </select>
      </div>
      <div class="row2">
        <button type="button" class="minor" data-act="cancel_all">Cancel All</button>
        <button type="button" class="minor danger" data-act="flatten">Flatten</button>
      </div>
      <div class="row2">
        <button type="button" class="minor" data-act="halt" data-symbol="${esc(symbol)}"
                aria-label="Halt trading in ${esc(symbol)}">Halt</button>
        <button type="button" class="minor" data-act="uncross" data-symbol="${esc(symbol)}"
                aria-label="Run the reopening auction for ${esc(symbol)}">Uncross</button>
      </div>
    </details>

    <p class="note">Your order joins the same queue as every algorithm here and
      travels the same latency link. Nothing about it is privileged.</p>`;
}

/**
 * The other side of your own fills, by name.
 *
 * "Is anything actually on the other side of this?" is a fair question to ask
 * of a simulated exchange, and a roster of agents does not answer it. This
 * does: every fill here names the participant that took it.
 */
function counterparties(snapshot, symbol) {
  const rows = (snapshot.counterparties ?? []).filter((c) => c.symbol === symbol);
  if (!rows.length) {
    return `<div class="empty">Once you trade, the participants who took the
      other side appear here by name.</div>`;
  }

  // Aggregated per counterparty and side. One order that swept a book produced
  // twenty near-identical rows -- "buy 30 at 0.06 from mm-1" over and over --
  // which is a log, not an answer. What a person wants to know is who they are
  // trading against and at what average, and that is four numbers.
  const totals = new Map();
  for (const fill of rows) {
    const key = `${fill.counterparty}|${fill.side}`;
    const acc = totals.get(key) ?? { ...fill, quantity: 0, notional: 0, fills: 0 };
    acc.quantity += fill.quantity;
    acc.notional += fill.quantity * Number(fill.price);
    acc.fills += 1;
    totals.set(key, acc);
  }

  const summary = [...totals.values()].sort((a, b) => b.quantity - a.quantity);
  return `<table>
    <thead><tr><th>Counterparty</th><th>Side</th><th>Contracts</th>
               <th>Avg price</th><th>Fills</th></tr></thead>
    <tbody>${summary
      .map((c) => `<tr>
        <td class="mono" style="text-align:left">${esc(c.counterparty)}</td>
        <td class="${c.side === 'buy' ? 'up' : 'down'}" style="text-align:left">${esc(c.side)}</td>
        <td>${count(c.quantity)}</td>
        <td>${price(c.notional / c.quantity)}</td>
        <td class="faint">${count(c.fills)}</td>
      </tr>`)
      .join('')}</tbody></table>`;
}

function positionCard(position) {
  if (!position || position.quantity === 0) {
    return `<div class="empty">No position in this contract.</div>`;
  }
  return `<div class="pos">
    <div><span>Contracts</span><b class="mono ${cls(position.quantity)}">${signed(position.quantity, 0)}</b></div>
    <div><span>Average price</span><b class="mono">${price(position.average_price)}</b></div>
    <div><span>Unrealised</span><b class="mono ${cls(Number(position.unrealized))}">${money(position.unrealized)}</b></div>
    <div><span>Realised</span><b class="mono ${cls(Number(position.realized))}">${money(position.realized)}</b></div>
  </div>`;
}

function orders(snapshot) {
  const rows = snapshot.orders || [];
  if (!rows.length) return `<div class="empty">Nothing working.</div>`;
  return `<table><tbody>${rows
    .map((o) => `<tr>
      <td style="text-align:left">${esc(o.symbol)}</td>
      <td class="faint">#${o.order_id}</td>
      <td><button type="button" class="minor" data-act="cancel" data-order="${o.order_id}"
            aria-label="Cancel order ${o.order_id} in ${esc(o.symbol)}">Cancel</button></td>
    </tr>`)
    .join('')}</tbody></table>`;
}

function tape(snapshot, symbol) {
  const rows = (snapshot.tape || []).filter((t) => t.symbol === symbol).slice(0, 30);
  if (!rows.length) return `<div class="empty">No trades yet.</div>`;
  return `<table>
    <thead><tr><th>Time</th><th>Price</th><th>Size</th><th>Taker</th></tr></thead>
    <tbody>${rows
      .map((t) => `<tr>
        <td class="faint">${clock(t.t)}</td>
        <td>${price(t.price)}</td>
        <td>${count(t.quantity)}</td>
        <td class="${t.side === 'buy' ? 'up' : 'down'}">${esc(t.side)}</td>
      </tr>`)
      .join('')}</tbody></table>`;
}

/* ── portfolio ───────────────────────────────────────────────────────── */

export function portfolio(store) {
  const s = store.snapshot;
  if (!s) return `<div class="view"><div class="onboard"><h2>Connecting&hellip;</h2></div></div>`;
  const a = s.account;
  const positions = a.positions || [];

  const figure = (label, value, klass = '') =>
    `<div class="panel figure"><span>${label}</span><b class="mono ${klass}">${value}</b></div>`;

  return `<div class="view">
    <div class="figures" data-region="figures">
      ${figure('Account Value', money(a.equity))}
      ${figure('Profit &amp; Loss', money(a.pnl), cls(Number(a.pnl)))}
      ${figure('Available to Trade', money(a.free_cash))}
      ${figure('Held as Collateral', money(a.collateral))}
    </div>

    <div class="panel" data-region="positions">
      <h2>Positions <em>${positions.length}</em></h2>
      <div class="panel-body">${
        positions.length
          ? `<table>
              <thead><tr><th>Contract</th><th>Qty</th><th>Avg</th><th>Unrealised</th><th>Realised</th></tr></thead>
              <tbody>${positions
                .map((p) => `<tr data-symbol="${esc(p.symbol)}">
                  <td>${esc(p.symbol)}</td>
                  <td class="${cls(p.quantity)}">${signed(p.quantity, 0)}</td>
                  <td>${price(p.average_price)}</td>
                  <td class="${cls(Number(p.unrealized))}">${money(p.unrealized)}</td>
                  <td class="${cls(Number(p.realized))}">${money(p.realized)}</td>
                </tr>`)
                .join('')}</tbody></table>`
          : `<div class="empty">You are flat. Pick a market to place your first trade.</div>`
      }</div>
    </div>

    <div class="panel" data-region="activity">
      <h2>Activity <em>${(s.log || []).length}</em></h2>
      <div class="panel-body">${blotter(s.log)}</div>
    </div>
  </div>`;
}

/**
 * The blotter: one row per private event the venue sent back.
 *
 * These arrive as structured events, not sentences. An earlier version
 * interpolated the whole object into a cell, which rendered `[object Object]`
 * whenever anything happened.
 */
function blotter(log) {
  if (!log || !log.length) return `<div class="empty">Nothing yet.</div>`;
  const tone = { fill: 'up', reject: 'down', cancel: 'dim', ack: 'faint' };
  return `<table>
    <thead><tr><th>Time</th><th>Event</th><th>Contract</th><th>Side</th>
               <th>Qty</th><th>Price</th><th>Detail</th></tr></thead>
    <tbody>${log
      .map((e) => {
        const detail = e.reason
          ? esc(e.reason)
          : e.remaining != null && e.type !== 'ack'
            ? `${count(e.remaining)} left`
            : '';
        return `<tr>
          <td class="faint">${clock(e.t)}</td>
          <td class="${tone[e.type] ?? 'dim'}" style="text-align:left">${esc(e.type ?? '?')}</td>
          <td style="text-align:left">${esc(e.symbol ?? '')}</td>
          <td class="${e.side === 'buy' ? 'up' : e.side === 'sell' ? 'down' : 'faint'}">${esc(e.side ?? '')}</td>
          <td>${e.quantity == null ? '' : count(e.quantity)}</td>
          <td>${e.price == null ? '' : price(e.price)}</td>
          <td class="faint" style="text-align:left">${detail}</td>
        </tr>`;
      })
      .join('')}</tbody></table>`;
}

/* ── research (operator) ─────────────────────────────────────────────── */

export function research(store) {
  const { diagnostics, agents, symbol } = store;

  const rows = (diagnostics?.verdicts ?? [])
    .map((v) => `<tr>
      <td style="text-align:left">${esc(v.name)}</td>
      <td>${v.value == null ? '—' : Number(v.value).toFixed(3)}</td>
      <td class="faint" style="text-align:left">${esc(v.expected)}</td>
      <td class="${v.verdict === 'as expected' ? 'verdict-ok' : 'verdict-no'}">${esc(v.verdict)}</td>
    </tr>`)
    .join('');

  const roster = (agents || [])
    .map((a) => `<tr>
      <td style="text-align:left">${esc(a.id)}</td>
      <td style="text-align:left">${esc(a.role ?? a.kind)}
        <span class="faint mono" style="margin-left:6px">${esc(a.kind)}</span></td>
      <td>${count(a.fills)}</td>
      <td class="${a.rejects ? 'down' : 'faint'}">${count(a.rejects)}</td>
      <td>${a.equity == null ? '—' : money(a.equity)}</td>
    </tr>`)
    .join('');

  return `<div class="view">
    <div class="panel" style="margin-bottom:12px">
      <h2>Stylized Facts <em>${esc(symbol ?? '')}</em></h2>
      <div class="panel-body">${
        diagnostics?.pending
          ? `<div class="empty">Collecting observations&hellip; (${diagnostics.observations ?? 0} so far)</div>`
          : rows
            ? `<table>
                <thead><tr><th>Statistic</th><th>Value</th><th>Expected</th><th>Verdict</th></tr></thead>
                <tbody>${rows}</tbody></table>`
            : `<div class="empty">No diagnostics yet.</div>`
      }</div>
    </div>

    <p class="note" style="margin-bottom:12px">The same estimators the research
      harness uses, run on the live price series &mdash; not a second
      implementation that could disagree with it. A verdict of
      &ldquo;unexpected&rdquo; is information, not a failure.</p>

    <div class="panel">
      <h2>Participants <em>${(agents || []).length}</em></h2>
      <div class="panel-body">${
        roster
          ? `<table>
              <thead><tr><th>Agent</th><th>Kind</th><th>Fills</th><th>Rejects</th><th>Equity</th></tr></thead>
              <tbody>${roster}</tbody></table>`
          : `<div class="empty">Loading&hellip;</div>`
      }</div>
    </div>
  </div>`;
}

/* ── lab (operator) ──────────────────────────────────────────────────── */

export function lab(store) {
  const s = store.session;
  if (!s) return `<div class="view"><div class="empty">Loading&hellip;</div></div>`;
  const c = s.config;

  const schedules = Object.entries(s.fee_schedules || {})
    .map(([name, f]) => `<option value="${esc(name)}" ${name === c.fees ? 'selected' : ''}>
        ${esc(name)} — taker ${f.taker_bps}bp / maker ${f.maker_bps}bp
      </option>`)
    .join('');

  const halts = (s.halts || []).slice().reverse();

  return `<div class="view">
    <p class="note" style="margin-bottom:12px">Operator controls. Changing any of
      these starts a <em>new</em> session rather than editing the running one
      &mdash; a population edited mid-flight would produce a market no seed could
      reproduce, and reproducibility is most of what makes a result here worth
      anything.</p>

    <div class="two">
      <div class="panel">
        <h2>Configuration <em>generation ${s.generation}</em></h2>
        <div class="panel-body">
          <div class="controls">
            <div class="row2">
              <div class="field">
                <label for="c-seed">Seed</label>
                <input id="c-seed" type="number" value="${c.seed}"
                       inputmode="numeric" autocomplete="off" spellcheck="false">
              </div>
              <div class="field">
                <label for="c-flow">Flow traders</label>
                <input id="c-flow" type="number" min="0" max="24" value="${c.flow_traders}"
                       inputmode="numeric" autocomplete="off" spellcheck="false">
              </div>
              <div class="field">
                <label for="c-makers">Market makers</label>
                <input id="c-makers" type="number" min="1" max="8" value="${c.makers ?? 3}"
                       inputmode="numeric" autocomplete="off" spellcheck="false">
              </div>
            </div>
            <div class="field">
              <label for="c-fees">Fee schedule</label>
              <select id="c-fees">${schedules}</select>
            </div>
            <div class="field">
              <label for="c-mechanism">Mechanism</label>
              <select id="c-mechanism">
                <option value="book" ${c.mechanism === 'scoring-rule' ? '' : 'selected'}>
                  Order book &mdash; every contract</option>
                <option value="scoring-rule" ${c.mechanism === 'scoring-rule' ? 'selected' : ''}>
                  Scoring rule &mdash; event contracts only</option>
              </select>
              <p class="note">A logarithmic scoring rule is defined on a partition of
                outcomes, so it can quote a coin flip and not a future. On it the venue
                is the market maker and subsidises the market instead of profiting from
                it. Experiment 2 compared the two over 200 paired trials and found the
                mechanism explains none of the difference in what a market learns.</p>
            </div>
            <div class="field">
              <label for="c-band">Price band &mdash; blank for none</label>
              <input id="c-band" type="text" inputmode="decimal"
                     value="${c.price_band ?? ''}" placeholder="0.10&hellip;"
                     autocomplete="off" spellcheck="false">
            </div>
            <label class="check">
              <input id="c-arb" type="checkbox" ${c.arbitrageur ? 'checked' : ''}>
              Cross-instrument arbitrageur
            </label>
            <label class="check">
              <input id="c-auction" type="checkbox" ${c.opening_auction === false ? '' : 'checked'}>
              Open with a call auction
            </label>
            <label class="check">
              <input id="c-surface" type="checkbox" ${c.surface === false ? '' : 'checked'}>
              Price options off one distribution
            </label>
            <button type="button" class="send" id="c-apply">Rebuild Market</button>
          </div>
        </div>
      </div>

      <div>
        <div class="panel" style="margin-bottom:12px">
          <h2>Venue</h2>
          <div class="panel-body">
            <table><tbody>
              <tr><td style="text-align:left">Taker fee</td><td>${s.fees.taker_bps} bp</td></tr>
              <tr><td style="text-align:left">Maker fee</td><td>${s.fees.maker_bps} bp</td></tr>
              <tr><td style="text-align:left">Fees collected</td><td>${money(Number(s.fees_collected) / 1e6)}</td></tr>
              <tr><td style="text-align:left">Price band</td><td>${s.price_band ?? '—'}</td></tr>
              <tr><td style="text-align:left">Uptime</td><td>${(s.uptime || 0).toFixed(0)}s</td></tr>
            </tbody></table>
          </div>
        </div>

        <div class="panel">
          <h2>Sessions</h2>
          <div class="panel-body">
            <table><tbody>${Object.entries(s.sessions || {})
              .map(([sym, state]) => `<tr>
                <td style="text-align:left">${esc(sym)}</td>
                <td><span class="badge ${esc(state)}">${esc(state.replace('_', ' '))}</span></td>
                <td>
                  <button type="button" class="minor" data-act="halt" data-symbol="${esc(sym)}"
                          aria-label="Halt trading in ${esc(sym)}">Halt</button>
                  <button type="button" class="minor" data-act="uncross" data-symbol="${esc(sym)}"
                          aria-label="Run the reopening auction for ${esc(sym)}">Uncross</button>
                </td>
              </tr>`)
              .join('')}</tbody></table>
          </div>
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:12px">
      <h2>Halts <em>${halts.length}</em></h2>
      <div class="panel-body">${
        halts.length
          ? `<table>
              <thead><tr><th>Contract</th><th>Reason</th><th>Reference</th><th>Price</th></tr></thead>
              <tbody>${halts
                .map((h) => `<tr>
                  <td style="text-align:left">${esc(h.symbol)}</td>
                  <td style="text-align:left" class="${h.reason === 'price_band' ? 'down' : 'dim'}">${esc(h.reason)}</td>
                  <td>${h.reference ?? '—'}</td>
                  <td>${h.price ?? '—'}</td>
                </tr>`)
                .join('')}</tbody></table>`
          : `<div class="empty">No halts this session.</div>`
      }</div>
    </div>
  </div>`;
}
