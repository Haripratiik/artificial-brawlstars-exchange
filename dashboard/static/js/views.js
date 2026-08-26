/* The five screens.
 *
 * Each view is a pure function from store state to HTML, plus an optional
 * `bind` that wires events after the render. Keeping them pure means the live
 * socket can re-render on every tick without any view holding its own state
 * that could drift from the market's.
 *
 * The exception is the trade ticket: an input the user is typing into must not
 * be rewritten underneath them, so it is rendered once per symbol change and
 * left alone afterwards.
 */

import { clock, cls, count, describe, esc, impliedProbability, money, percent,
         price, priceChart, signed, sparkline, walkBook } from './format.js';

/* ── markets ─────────────────────────────────────────────────────────── */

export function markets(store) {
  const { snapshot, instruments, history } = store;
  const books = snapshot?.books ?? {};
  const symbols = Object.keys(books);
  if (!symbols.length) return `<div class="view"><div class="empty">Connecting&hellip;</div></div>`;

  const cards = symbols
    .map((symbol, i) => {
      const book = books[symbol];
      const meta = instruments.find((x) => x.symbol === symbol) || {};
      const series = (history[symbol] || []).slice(-90);
      const first = series.find((v) => v != null);
      const last = series.length ? series[series.length - 1] : null;
      const change = first != null && last != null ? last - first : 0;
      const pct = first ? (change / first) * 100 : 0;
      const session = snapshot.sessions?.[symbol] ?? 'continuous';

      // A real button rather than a div with a click handler: the card is an
      // action, and a keyboard user has to be able to reach and fire it. The
      // children are spans so the markup stays valid inside a button.
      return `<button type="button" class="card" data-symbol="${esc(symbol)}"
                   data-session="${esc(session)}" style="animation-delay:${i * 28}ms"
                   aria-label="${esc(symbol)}, ${price(book.mark)}, ${signed(pct)} percent">
        <span class="row1">
          <span class="sym">${esc(symbol)}</span>
          <span class="kind">${esc(book.class ?? '')}</span>
        </span>
        <span class="price mono ${cls(change)}">${
          impliedProbability(book.mark, book.contract?.payoff) != null
            ? percent(impliedProbability(book.mark, book.contract?.payoff), 0)
            : price(book.mark)}</span>
        <span class="sub">
          <span class="${cls(change)}">${signed(change)} (${signed(pct)}%)</span>
          <span class="faint">${count(book.trades)} trades</span>
        </span>
        <span class="spark" aria-hidden="true">${sparkline(series)}</span>
      </button>`;
    })
    .join('');

  return `<div class="view"><div class="grid">${cards}</div></div>`;
}

/* ── trade ───────────────────────────────────────────────────────────── */

export function trade(store) {
  const { snapshot, instruments, symbol, history, depth } = store;
  const book = snapshot?.books?.[symbol];
  if (!book) return `<div class="view"><div class="empty">Select an instrument.</div></div>`;

  const meta = instruments.find((x) => x.symbol === symbol) || {};
  const session = snapshot.sessions?.[symbol] ?? 'continuous';
  const series = history[symbol] || [];

  return `<div class="trade">
    <div class="bar-top">${instrumentBar(symbol, book, meta, session)}</div>

    <div class="panel lad">
      <h2>Depth <em>${esc(symbol)}</em></h2>
      <div class="panel-body">${ladder(book, depth)}</div>
    </div>

    <div class="panel">
      <h2>Price <em>${series.length} pts</em></h2>
      <div class="panel-body" style="padding:6px">
        ${priceChart(series, { settlesAt: meta.settles_at ?? null, label: symbol })}
      </div>
    </div>

    <div class="panel tkt">
      <h2>Ticket</h2>
      <div class="panel-body">
        <div class="ticket" id="ticket">${ticket(symbol, book, session)}</div>
      </div>
      <h2 style="border-top:1px solid var(--rule)">Working <em>${(snapshot.orders || []).length}</em></h2>
      <div class="panel-body">${orders(snapshot)}</div>
    </div>

    <div class="panel">
      <h2>Tape</h2>
      <div class="panel-body">${tape(snapshot, symbol)}</div>
    </div>
  </div>`;
}

function instrumentBar(symbol, book, meta, session) {
  const spread = book.spread == null ? '—' : price(book.spread);
  return `<div class="instrument-bar" data-session="${esc(session)}">
    <div>
      <div class="name">${esc(symbol)}</div>
      <div class="kind mono faint">${esc(book.class ?? '')}</div>
    </div>
    <span class="badge ${esc(session)}">${esc(session.replace('_', ' '))}</span>
    <div class="stat"><b>${price(book.mark)}</b><span>mark</span></div>
    ${impliedProbability(book.mark, book.contract?.payoff) != null
      ? `<div class="stat"><b class="amber">${percent(impliedProbability(book.mark, book.contract?.payoff))}</b>
           <span>implied odds</span></div>` : ''}
    <div class="stat"><b>${spread}</b><span>spread</span></div>
    <div class="stat"><b>${count(book.trades)}</b><span>trades</span></div>
    ${meta.settles_at != null
      ? `<div class="stat"><b class="up">${price(meta.settles_at)}</b><span>settles at</span></div>` : ''}
    <div class="terms">${describe(book.contract)}
      <span class="faint mono"> &middot; expiry ${esc(book.contract?.expiry ?? '?')}
      &middot; tick ${esc(book.tick ?? '')}</span>
    </div>
    <div class="spacer"></div>
    <button type="button" class="minor" data-act="halt" data-symbol="${esc(symbol)}"
            aria-label="Halt trading in ${esc(symbol)}">Halt</button>
    <button type="button" class="minor" data-act="uncross" data-symbol="${esc(symbol)}"
            aria-label="Run the reopening auction for ${esc(symbol)}">Uncross</button>
  </div>`;
}

/**
 * The ladder. Bids and asks share a price column, so the shape of the book is
 * one vertical scan rather than two tables to compare, and each row is
 * click-to-trade at that price.
 */
function ladder(book, depth) {
  const bids = (depth?.bids ?? book.bids ?? []).map(([p, q]) => [Number(p), q]);
  const asks = (depth?.asks ?? book.asks ?? []).map(([p, q]) => [Number(p), q]);
  if (!bids.length && !asks.length) return `<div class="empty">No resting orders.</div>`;

  const byPrice = new Map();
  bids.forEach(([p, q]) => byPrice.set(p, { ...(byPrice.get(p) || {}), bid: q }));
  asks.forEach(([p, q]) => byPrice.set(p, { ...(byPrice.get(p) || {}), ask: q }));

  // Cumulative size outward from the touch. Per-level size tells you what is
  // at a price; the running total tells you what it costs to get through it,
  // which is the question anyone sizing an order is actually asking.
  const cumulativeBid = new Map();
  let runningBid = 0;
  for (const [p, q] of bids) { runningBid += q; cumulativeBid.set(p, runningBid); }
  const cumulativeAsk = new Map();
  let runningAsk = 0;
  for (const [p, q] of asks) { runningAsk += q; cumulativeAsk.set(p, runningAsk); }

  const peak = Math.max(1, runningBid, runningAsk);
  const mark = Number(book.mark);
  const rows = [...byPrice.entries()].sort((a, b) => b[0] - a[0]);
  // The row nearest the mark, so the eye lands on the middle of the book.
  let nearest = 0;
  rows.forEach(([p], i) => {
    if (Math.abs(p - mark) < Math.abs(rows[nearest][0] - mark)) nearest = i;
  });

  return `<div class="ladder">${rows
    .map(([p, side], i) => {
      // Bars show the cumulative book, so their shape is the liquidity profile
      // rather than a row-by-row sawtooth.
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

function ticket(symbol, book, session) {
  const halted = session !== 'continuous';
  const binary = book.contract?.payoff?.kind === 'binary';
  // On a binary, buying is a bet the event happens and selling is a bet it does
  // not. Prediction markets label the buttons that way rather than making the
  // reader translate, and the translation is where people make mistakes.
  const buyLabel = binary ? 'Buy Yes' : 'Buy';
  const sellLabel = binary ? 'Buy No' : 'Sell';
  return `<div class="sides">
      <button type="button" data-side="buy" aria-pressed="true">${buyLabel}</button>
      <button type="button" data-side="sell" aria-pressed="false">${sellLabel}</button>
    </div>
    <div class="row2">
      <div class="field">
        <label for="t-qty">Quantity</label>
        <input id="t-qty" type="number" min="1" step="1" value="10"
               inputmode="numeric" autocomplete="off" spellcheck="false">
      </div>
      <div class="field">
        <label for="t-tif">Time in force</label>
        <select id="t-tif">
          <option value="gtc">GTC</option>
          <option value="ioc">IOC</option>
          <option value="fok">FOK</option>
          <option value="post_only">Post only</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label for="t-px">Limit price &mdash; blank for market</label>
      <input id="t-px" type="text" inputmode="decimal" placeholder="4660.25&hellip;"
             autocomplete="off" spellcheck="false">
    </div>
    <div class="quick" role="group" aria-label="Quick size">
      ${[5, 25, 100, 250].map((n) => `<button type="button" data-qty="${n}">${n}</button>`).join('')}
    </div>
    <!-- What it costs, before you commit to it. -->
    <div class="preview" id="t-preview" aria-live="polite"></div>
    <button type="button" class="send" id="t-send" ${halted ? 'disabled' : ''}>
      ${halted ? `${esc(session.replace('_', ' '))} &mdash; orders rest` : 'Send order'}
    </button>
    <div class="row2">
      <button type="button" class="minor" data-act="cancel_all">Cancel All</button>
      <button type="button" class="minor danger" data-act="flatten">Flatten</button>
    </div>
    <div class="note">Your order joins the same queue as every algorithm here and
      travels the same latency link. Nothing about it is privileged.</div>`;
}

function orders(snapshot) {
  const rows = snapshot.orders || [];
  if (!rows.length) return `<div class="empty">No working orders.</div>`;
  return `<table><tbody>${rows
    .map(
      (o) => `<tr>
        <td>${esc(o.symbol)}</td>
        <td class="faint">#${o.order_id}</td>
        <td><button type="button" class="minor" data-act="cancel" data-order="${o.order_id}"
                aria-label="Cancel order ${o.order_id} in ${esc(o.symbol)}">Cancel</button></td>
      </tr>`
    )
    .join('')}</tbody></table>`;
}

function tape(snapshot, symbol) {
  const rows = (snapshot.tape || []).filter((t) => t.symbol === symbol).slice(0, 40);
  if (!rows.length) return `<div class="empty">No prints yet.</div>`;
  return `<table>
    <thead><tr><th>Time</th><th>Price</th><th>Size</th><th>Aggressor</th></tr></thead>
    <tbody>${rows
      .map(
        (t) => `<tr class="tape-row">
          <td class="faint">${clock(t.t)}</td>
          <td>${price(t.price)}</td>
          <td>${count(t.quantity)}</td>
          <td class="${t.side === 'buy' ? 'up' : 'down'}">${esc(t.side)}</td>
        </tr>`
      )
      .join('')}</tbody></table>`;
}

/* ── portfolio ───────────────────────────────────────────────────────── */

export function portfolio(store) {
  const s = store.snapshot;
  if (!s) return `<div class="view"><div class="empty">Connecting&hellip;</div></div>`;
  const a = s.account;
  const positions = a.positions || [];

  const stat = (label, value, klass = '') =>
    `<div class="panel" style="padding:12px">
      <div class="mono faint" style="font-size:8.5px;letter-spacing:.15em;text-transform:uppercase">${label}</div>
      <div class="mono ${klass}" style="font-size:22px;letter-spacing:-.03em;margin-top:5px">${value}</div>
    </div>`;

  return `<div class="view">
    <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(160px,1fr));margin-bottom:12px">
      ${stat('Equity', money(a.equity))}
      ${stat('Profit &amp; loss', money(a.pnl), cls(Number(a.pnl)))}
      ${stat('Free cash', money(a.free_cash))}
      ${stat('Collateral', money(a.collateral))}
    </div>

    <div class="panel" style="margin-bottom:12px">
      <h2>Positions <em>${positions.length}</em></h2>
      <div class="panel-body">${
        positions.length
          ? `<table>
              <thead><tr><th>Symbol</th><th>Qty</th><th>Avg price</th><th>Unrealised</th><th>Realised</th></tr></thead>
              <tbody>${positions
                .map(
                  (p) => `<tr data-symbol="${esc(p.symbol)}">
                    <td>${esc(p.symbol)}</td>
                    <td class="${cls(p.quantity)}">${signed(p.quantity, 0)}</td>
                    <td>${price(p.average_price)}</td>
                    <td class="${cls(Number(p.unrealized))}">${money(p.unrealized)}</td>
                    <td class="${cls(Number(p.realized))}">${money(p.realized)}</td>
                  </tr>`
                )
                .join('')}</tbody></table>`
          : `<div class="empty">Flat.</div>`
      }</div>
    </div>

    <div class="panel">
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
 * whenever anything happened — invisible in a quiet market and useless in a
 * busy one.
 */
function blotter(log) {
  if (!log || !log.length) return `<div class="empty">Nothing yet.</div>`;
  const tone = { fill: 'up', reject: 'down', cancel: 'dim', ack: 'faint' };
  return `<table>
    <thead><tr><th>Time</th><th>Event</th><th>Symbol</th><th>Side</th>
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

/* ── research ────────────────────────────────────────────────────────── */

export function research(store) {
  const { diagnostics, agents, symbol } = store;

  const rows = (diagnostics?.verdicts ?? [])
    .map(
      (v) => `<tr>
        <td style="text-align:left">${esc(v.name)}</td>
        <td>${v.value == null ? '—' : Number(v.value).toFixed(3)}</td>
        <td class="faint" style="text-align:left">${esc(v.expected)}</td>
        <td class="${v.verdict === 'as expected' ? 'verdict-ok' : 'verdict-no'}">${esc(v.verdict)}</td>
      </tr>`
    )
    .join('');

  const roster = (agents || [])
    .map(
      (a) => `<tr>
        <td style="text-align:left">${esc(a.id)}</td>
        <td class="faint" style="text-align:left">${esc(a.kind)}</td>
        <td>${count(a.fills)}</td>
        <td class="${a.rejects ? 'down' : 'faint'}">${count(a.rejects)}</td>
        <td>${a.equity == null ? '—' : money(a.equity)}</td>
      </tr>`
    )
    .join('');

  return `<div class="view">
    <div class="panel" style="margin-bottom:12px">
      <h2>Stylized facts <em>${esc(symbol)}</em></h2>
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

    <div class="note" style="margin-bottom:12px">
      These are the same estimators the research harness uses, run on the live
      price series &mdash; not a second implementation that could disagree with it.
      A verdict of &ldquo;unexpected&rdquo; is information, not a failure: three of
      four predictions made before the first run turned out wrong.
    </div>

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

/* ── lab ─────────────────────────────────────────────────────────────── */

export function lab(store) {
  const s = store.session;
  if (!s) return `<div class="view"><div class="empty">Loading&hellip;</div></div>`;
  const c = s.config;

  const schedules = Object.entries(s.fee_schedules || {})
    .map(
      ([name, f]) =>
        `<option value="${esc(name)}" ${name === c.fees ? 'selected' : ''}>
          ${esc(name)} — taker ${f.taker_bps}bp / maker ${f.maker_bps}bp
        </option>`
    )
    .join('');

  const halts = (s.halts || []).slice().reverse();

  return `<div class="view">
    <div class="two">
      <div class="panel">
        <h2>Configuration <em>gen ${s.generation}</em></h2>
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
            </div>
            <div class="field">
              <label for="c-fees">Fee schedule</label>
              <select id="c-fees">${schedules}</select>
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
            <button type="button" class="send" id="c-apply">Rebuild Market</button>
            <div class="note">A configuration change starts a <em>new</em> session
              rather than editing the running one. A population edited mid-flight
              would produce a market no seed could reproduce, and reproducibility
              is most of what makes a result here worth anything.</div>
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
              .map(
                ([sym, state]) => `<tr>
                  <td style="text-align:left">${esc(sym)}</td>
                  <td><span class="badge ${esc(state)}">${esc(state.replace('_', ' '))}</span></td>
                  <td>
                    <button type="button" class="minor" data-act="halt" data-symbol="${esc(sym)}"
                            aria-label="Halt trading in ${esc(sym)}">Halt</button>
                    <button type="button" class="minor" data-act="uncross" data-symbol="${esc(sym)}"
                            aria-label="Run the reopening auction for ${esc(sym)}">Uncross</button>
                  </td>
                </tr>`
              )
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
              <thead><tr><th>Symbol</th><th>Reason</th><th>Reference</th><th>Price</th></tr></thead>
              <tbody>${halts
                .map(
                  (h) => `<tr>
                    <td style="text-align:left">${esc(h.symbol)}</td>
                    <td style="text-align:left" class="${h.reason === 'price_band' ? 'down' : 'dim'}">${esc(h.reason)}</td>
                    <td>${h.reference ?? '—'}</td>
                    <td>${h.price ?? '—'}</td>
                  </tr>`
                )
                .join('')}</tbody></table>`
          : `<div class="empty">No halts this session.</div>`
      }</div>
    </div>
  </div>`;
}
