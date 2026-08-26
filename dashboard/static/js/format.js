/* Formatting, and the two chart primitives.
 *
 * Charts are hand-built SVG rather than a library. A price line and a sparkline
 * are perhaps forty lines each, and a charting dependency would bring its own
 * colour scheme, its own fonts and its own opinions about axes — all of which
 * would have to be fought back to match the terminal. Owning them is cheaper
 * than overriding them.
 */

export const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/**
 * Absent, as distinct from zero.
 *
 * `Number(null)` and `Number('')` are both 0, which is finite, so a missing
 * mark rendered as "0.00" — a contract nobody had quoted looked like a
 * contract worth nothing. On a screen people trade from, those must never be
 * the same glyph.
 */
const missing = (v) => v == null || v === '';

/*
 * Formatters are built once and reused.
 *
 * `toLocaleString` constructs an `Intl.NumberFormat` on every call, and this
 * runs inside a render loop that fires twenty times a second across a book, a
 * ladder, a tape and a watchlist — thousands of constructions a second to
 * produce a few hundred numbers.
 *
 * They are pinned to en-US rather than the viewer's locale, which is a
 * deliberate departure. A price ladder rendered with a decimal comma does not
 * read differently, it reads *wrongly*: 4.660,25 alongside 4,660.25 is a
 * correctness hazard on a screen people trade from. Locale is honoured for
 * language, not for the radix of a price.
 */
const NUM = new Map();
function formatter(dp) {
  let f = NUM.get(dp);
  if (!f) {
    f = new Intl.NumberFormat('en-US', {
      minimumFractionDigits: dp,
      maximumFractionDigits: dp,
    });
    NUM.set(dp, f);
  }
  return f;
}
const INTEGER = new Intl.NumberFormat('en-US');

/** Prices arrive as decimal strings so the server never rounds for us. */
export function price(value, dp = 2) {
  if (missing(value)) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return formatter(dp).format(n);
}

export function money(value) {
  if (missing(value)) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  const sign = n < 0 ? '−' : '';
  const abs = Math.abs(n);
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`;
  return `${sign}${abs.toFixed(2)}`;
}

export function signed(value, dp = 2) {
  if (missing(value)) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(dp)}`;
}

export const cls = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : 'dim');

/** Nanoseconds of simulated time, as a session clock. */
export function clock(ns) {
  const s = Number(ns) / 1e9;
  if (s < 60) return `${s.toFixed(2)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(Math.floor(s % 60)).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function count(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e4) return `${(v / 1e3).toFixed(0)}k`;
  return INTEGER.format(v);
}

/** Human-readable contract terms, from the spec the server publishes. */
export function describe(contract) {
  if (!contract) return '';
  const p = contract.payoff || {};
  const u = contract.underlying || {};
  const subject = subjectOf(u);
  switch (p.kind) {
    case 'binary':
      return `Pays ${p.payout} if ${subject} ${esc(p.comparison)} ${p.threshold}, else 0.`;
    case 'call':
      return `Call on ${subject}, strike ${p.strike}, scaled ${p.scale}.`;
    case 'put':
      return `Put on ${subject}, strike ${p.strike}, scaled ${p.scale}.`;
    case 'linear':
      return `Settles at ${p.scale} &times; ${subject}${p.offset ? ` + ${p.offset}` : ''}.`;
    default:
      return `${esc(p.kind || 'contract')} on ${subject}.`;
  }
}

function subjectOf(u) {
  if (!u) return 'the metric';
  if (u.kind === 'single') return `<code>${esc(u.metric?.subject ?? '?')}</code>`;
  if (u.kind === 'difference') return `(${subjectOf(u.left)} − ${subjectOf(u.right)})`;
  if (u.kind === 'basket') {
    return `basket(${(u.legs || []).map((l) => `${l.weight}·${subjectOf(l.leg)}`).join(' + ')})`;
  }
  return 'the metric';
}

/* ── charts ──────────────────────────────────────────────────────────── */

/** A bare sparkline: no axes, no labels, just the shape of the last N points. */
export function sparkline(values, { width = 200, height = 34 } = {}) {
  const pts = values.filter((v) => v != null && Number.isFinite(v));
  if (pts.length < 2) return '';
  const lo = Math.min(...pts);
  const hi = Math.max(...pts);
  const span = hi - lo || 1;
  const step = width / (pts.length - 1);
  const y = (v) => height - 2 - ((v - lo) / span) * (height - 4);

  const d = pts.map((v, i) => `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join('');
  const rising = pts[pts.length - 1] >= pts[0];
  const stroke = rising ? 'var(--up)' : 'var(--down)';
  const fill = rising ? 'var(--up-sunk)' : 'var(--down-sunk)';

  return `<svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <path d="${d}L${width},${height}L0,${height}Z" fill="${fill}"/>
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.25"
          vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
  </svg>`;
}

/**
 * The price panel: a line with a gridded, labelled price axis.
 *
 * Drawn at a fixed internal size and stretched, except for text and strokes,
 * which use non-scaling units so a wide panel does not produce stretched
 * letterforms — the failure that makes hand-rolled SVG charts look amateur.
 */
export function priceChart(values, { settlesAt = null, label = '' } = {}) {
  const pts = values.filter((v) => v != null && Number.isFinite(v));
  if (pts.length < 2) {
    return `<div class="empty">Waiting for prices&hellip;</div>`;
  }
  const W = 1000;
  const H = 320;
  const padL = 4;
  const padR = 62;
  const padT = 12;
  const padB = 18;

  let lo = Math.min(...pts);
  let hi = Math.max(...pts);
  if (settlesAt != null && Number.isFinite(settlesAt)) {
    lo = Math.min(lo, settlesAt);
    hi = Math.max(hi, settlesAt);
  }
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.002 || 1;
  lo -= pad;
  hi += pad;
  const span = hi - lo || 1;

  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const x = (i) => padL + (i / (pts.length - 1)) * plotW;
  const y = (v) => padT + (1 - (v - lo) / span) * plotH;

  const d = pts.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
  const rising = pts[pts.length - 1] >= pts[0];
  const stroke = rising ? 'var(--up)' : 'var(--down)';
  const fill = rising ? 'var(--up-sunk)' : 'var(--down-sunk)';

  let grid = '';
  const LINES = 5;
  for (let i = 0; i <= LINES; i += 1) {
    const v = lo + (span * i) / LINES;
    const gy = y(v).toFixed(1);
    grid += `<line x1="${padL}" x2="${W - padR}" y1="${gy}" y2="${gy}"
                   stroke="var(--rule)" stroke-width="1" vector-effect="non-scaling-stroke"/>
             <text x="${W - padR + 7}" y="${gy}" dy="3.5" fill="var(--ink-faint)"
                   font-family="IBM Plex Mono, monospace" font-size="11">${v.toFixed(2)}</text>`;
  }

  // The settlement value is the thing this market is trying to discover, so it
  // is drawn as a target rather than left implicit.
  let target = '';
  if (settlesAt != null && Number.isFinite(settlesAt)) {
    const ty = y(settlesAt).toFixed(1);
    target = `<line x1="${padL}" x2="${W - padR}" y1="${ty}" y2="${ty}"
                    stroke="var(--amber)" stroke-width="1" stroke-dasharray="5 4"
                    vector-effect="non-scaling-stroke" opacity="0.85"/>
              <text x="${padL + 6}" y="${ty}" dy="-5" fill="var(--amber)"
                    font-family="IBM Plex Mono, monospace" font-size="11">settles ${settlesAt.toFixed(2)}</text>`;
  }

  const last = pts[pts.length - 1];
  const ly = y(last).toFixed(1);

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
               role="img" aria-label="${esc(label)} price history">
    ${grid}
    <path d="${d}L${x(pts.length - 1).toFixed(1)},${H - padB}L${padL},${H - padB}Z" fill="${fill}"/>
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.5"
          vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    ${target}
    <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${ly}" r="3" fill="${stroke}"/>
  </svg>`;
}
