/* Boot, socket, store, router.
 *
 * One WebSocket carries the live market at 20Hz; everything slow-moving
 * (contract terms, the agent roster, diagnostics, session configuration) comes
 * over REST and is refreshed on a much lazier timer. Pushing all of it down the
 * socket would mean re-serialising static contract specs twenty times a second
 * to say nothing new.
 *
 * The store is a plain object and the views are pure functions of it, so a tick
 * is just: merge, re-render. The one thing deliberately *not* re-rendered on a
 * tick is the order ticket — rewriting an input while somebody is typing into
 * it is the fastest way to make a trading screen unusable.
 */

import { clock, count, money, price, signed, cls } from './format.js';
import { lab, markets, portfolio, research, trade } from './views.js';

const store = {
  view: 'markets',
  symbol: null,
  snapshot: null,
  instruments: [],
  session: null,
  agents: [],
  diagnostics: null,
  depth: null,
  history: {},          // symbol -> array of mids, built client-side from ticks
  side: 'buy',
  generation: 0,
};

const VIEWS = { markets, trade, portfolio, research, lab };
const main = document.getElementById('main');
const HISTORY_POINTS = 400;

/* ── socket ──────────────────────────────────────────────────────────── */

let socket = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.ack) return acknowledge(payload.ack);

    // A rebuild resets every series: the old prices belong to a market that no
    // longer exists, and splicing them onto the new one would draw a jump that
    // never happened.
    if (payload.generation !== store.generation) {
      store.generation = payload.generation;
      store.history = {};
      refreshSlow();
    }

    store.snapshot = payload;
    recordHistory(payload);
    if (!store.symbol) store.symbol = Object.keys(payload.books)[0] ?? null;
    render();
  };

  socket.onclose = () => {
    toast('Disconnected — retrying', true);
    setTimeout(connect, 1200);
  };
}

function recordHistory(payload) {
  for (const [symbol, book] of Object.entries(payload.books || {})) {
    const series = (store.history[symbol] ||= []);
    const value = Number(book.mark);
    if (Number.isFinite(value)) series.push(value);
    if (series.length > HISTORY_POINTS) series.splice(0, series.length - HISTORY_POINTS);
  }
}

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function acknowledge(ack) {
  if (ack.ok === false) toast(ack.error ?? 'Rejected', true);
  else if (ack.speed === undefined) toast('Accepted');
}

/* ── slow data ───────────────────────────────────────────────────────── */

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

async function refreshSlow() {
  try {
    const [instruments, session, agents] = await Promise.all([
      json('/api/instruments'),
      json('/api/session'),
      json('/api/agents'),
    ]);
    store.instruments = instruments.instruments;
    store.session = session;
    store.agents = agents.agents;
    render();
  } catch (failure) {
    // A slow-data hiccup must not take the live view down with it.
    console.warn('slow refresh failed', failure);
  }
}

async function refreshSymbol() {
  if (!store.symbol) return;
  const symbol = store.symbol;
  try {
    const [depth, diagnostics] = await Promise.all([
      json(`/api/book/${encodeURIComponent(symbol)}?levels=18`),
      json(`/api/diagnostics/${encodeURIComponent(symbol)}`),
    ]);
    // Guard against a slow response landing after the user moved on.
    if (store.symbol !== symbol) return;
    store.depth = depth;
    store.diagnostics = diagnostics;
    render();
  } catch (failure) {
    console.warn('symbol refresh failed', failure);
  }
}

/* ── render ──────────────────────────────────────────────────────────── */

let ticketSignature = null;

function render() {
  renderHeader();
  renderWatchlist();

  const view = VIEWS[store.view] ?? markets;
  const signature = `${store.view}:${store.symbol}`;

  // Re-rendering the trade view wholesale on every tick would blow away the
  // ticket inputs mid-keystroke, so the ticket is patched around instead.
  if (store.view === 'trade' && ticketSignature === signature) {
    const fresh = document.createElement('div');
    fresh.innerHTML = view(store);
    const keep = main.querySelector('#ticket');
    const incoming = fresh.querySelector('#ticket');
    if (keep && incoming) incoming.replaceWith(keep);
    main.replaceChildren(...fresh.childNodes);
  } else {
    main.innerHTML = view(store);
    ticketSignature = signature;
  }
  bind();
}

function renderHeader() {
  const s = store.snapshot;
  if (!s) return;
  document.getElementById('clock').textContent = clock(s.clock);
  document.getElementById('events').textContent = count(s.events);
  document.getElementById('equity').textContent = money(s.account.equity);

  const pnl = document.getElementById('pnl');
  pnl.textContent = money(s.account.pnl);
  pnl.className = `mono ${cls(Number(s.account.pnl))}`;

  const conserved = Number(s.conservation) === 0;
  const health = document.getElementById('health');
  health.classList.toggle('bad', !conserved);
  document.getElementById('health-text').textContent = conserved
    ? 'conserved'
    : `leak ${s.conservation}`;
}

function renderWatchlist() {
  const s = store.snapshot;
  if (!s) return;
  const list = document.getElementById('watchlist');

  list.innerHTML = Object.entries(s.books)
    .map(([symbol, book]) => {
      const series = (store.history[symbol] || []).slice(-60);
      const first = series.find((v) => v != null);
      const last = series.length ? series[series.length - 1] : null;
      const change = first != null && last != null ? last - first : 0;
      const pct = first ? (change / first) * 100 : 0;
      return `<div class="watch" data-symbol="${symbol}"
                   aria-current="${symbol === store.symbol}">
        <span class="sym">${symbol}</span>
        <span class="px ${cls(change)}">${price(book.mark)}</span>
        <span class="cls">${book.class ?? ''}</span>
        <span class="chg ${cls(change)}">${signed(pct)}%</span>
        <span class="spark">${sparklineFor(series)}</span>
      </div>`;
    })
    .join('');
}

function sparklineFor(series) {
  // Imported lazily through views' formatter to keep one implementation.
  return series.length > 1 ? sparkInline(series) : '';
}

function sparkInline(values) {
  const pts = values.filter((v) => Number.isFinite(v));
  if (pts.length < 2) return '';
  const lo = Math.min(...pts);
  const hi = Math.max(...pts);
  const span = hi - lo || 1;
  const step = 100 / (pts.length - 1);
  const d = pts
    .map((v, i) => `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${(14 - ((v - lo) / span) * 12).toFixed(1)}`)
    .join('');
  const rising = pts[pts.length - 1] >= pts[0];
  return `<svg class="chart" viewBox="0 0 100 16" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="${rising ? 'var(--up)' : 'var(--down)'}"
          stroke-width="1" vector-effect="non-scaling-stroke"/>
  </svg>`;
}

/* ── events ──────────────────────────────────────────────────────────── */

function bind() {
  main.querySelectorAll('[data-symbol]').forEach((node) => {
    if (node.dataset.act) return;              // control buttons carry a symbol too
    node.addEventListener('click', () => select(node.dataset.symbol));
  });

  main.querySelectorAll('.lad-row').forEach((row) => {
    row.addEventListener('click', () => {
      const input = document.getElementById('t-px');
      if (input) input.value = row.dataset.price;
    });
  });

  main.querySelectorAll('.sides button').forEach((button) => {
    button.addEventListener('click', () => {
      store.side = button.dataset.side;
      main.querySelectorAll('.sides button').forEach((b) =>
        b.setAttribute('aria-pressed', String(b.dataset.side === store.side)));
    });
  });

  const sendButton = document.getElementById('t-send');
  if (sendButton) sendButton.addEventListener('click', submitOrder);

  main.querySelectorAll('[data-act]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      act(button.dataset.act, button.dataset);
    });
  });

  const apply = document.getElementById('c-apply');
  if (apply) apply.addEventListener('click', rebuild);
}

function submitOrder() {
  const quantity = Number(document.getElementById('t-qty').value);
  const raw = document.getElementById('t-px').value.trim();
  send({
    action: 'submit',
    symbol: store.symbol,
    side: store.side,
    quantity,
    price: raw === '' ? null : raw,
    tif: document.getElementById('t-tif').value,
  });
}

async function act(action, data) {
  if (action === 'cancel') return send({ action: 'cancel', order_id: Number(data.order) });
  if (action === 'cancel_all') return send({ action: 'cancel_all' });
  if (action === 'flatten') return send({ action: 'flatten' });

  if (action === 'halt' || action === 'uncross') {
    try {
      const result = await json(
        `/api/session/${encodeURIComponent(data.symbol)}/${action}`, { method: 'POST' });
      toast(result.ok ? `${data.symbol} ${result.session}` : result.error, !result.ok);
      refreshSlow();
    } catch (failure) {
      toast(String(failure), true);
    }
  }
}

async function rebuild() {
  const band = document.getElementById('c-band').value.trim();
  const body = {
    seed: Number(document.getElementById('c-seed').value),
    flow_traders: Number(document.getElementById('c-flow').value),
    fees: document.getElementById('c-fees').value,
    price_band: band === '' ? null : Number(band),
    arbitrageur: document.getElementById('c-arb').checked,
    speed: store.snapshot?.speed ?? 1,
  };
  try {
    const result = await json('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast(`Rebuilt — generation ${result.generation}`);
    store.history = {};
    refreshSlow();
  } catch (failure) {
    toast(String(failure), true);
  }
}

function select(symbol) {
  if (!symbol || symbol === store.symbol) {
    if (symbol) store.view = 'trade';
  } else {
    store.symbol = symbol;
    store.depth = null;
    store.diagnostics = null;
    store.view = 'trade';
    refreshSymbol();
  }
  syncNav();
  render();
}

function syncNav() {
  document.querySelectorAll('#nav button').forEach((b) =>
    b.setAttribute('aria-current', String(b.dataset.view === store.view)));
}

document.getElementById('nav').addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  store.view = button.dataset.view;
  syncNav();
  if (store.view === 'research') refreshSymbol();
  if (store.view === 'lab') refreshSlow();
  render();
});

document.getElementById('watchlist').addEventListener('click', (event) => {
  const row = event.target.closest('.watch');
  if (row) select(row.dataset.symbol);
});

const speed = document.getElementById('speed');
speed.addEventListener('input', () => {
  document.getElementById('speed-label').textContent = `${Number(speed.value).toFixed(1)}×`;
  send({ action: 'speed', value: Number(speed.value) });
});

/* ── toast ───────────────────────────────────────────────────────────── */

let toastTimer = null;
function toast(message, bad = false) {
  document.querySelector('.toast')?.remove();
  const node = document.createElement('div');
  node.className = `toast${bad ? ' bad' : ''}`;
  node.textContent = message;
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 2600);
}

/* ── go ──────────────────────────────────────────────────────────────── */

connect();
refreshSlow();
setInterval(refreshSlow, 8000);
setInterval(refreshSymbol, 2500);
