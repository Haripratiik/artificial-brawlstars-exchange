/* Render every view against a real market snapshot, without a browser.
 *
 * The views are pure functions of the store, which is what makes this possible:
 * node can import them directly and check the HTML they produce. What this
 * catches is the whole class of template bugs that look fine until someone
 * opens the page — a field renamed on the server showing up as `undefined`, a
 * number arriving as a string and rendering `NaN`, an object interpolated into
 * text as `[object Object]`.
 *
 * It does not replace looking at the page. It does mean a server-side rename
 * fails a test rather than silently blanking a panel.
 *
 *     node tests/frontend/render.mjs <fixture.json>
 */

import { readFileSync } from 'node:fs';
import * as views from '../../dashboard/static/js/views.js';
import * as fmt from '../../dashboard/static/js/format.js';

const fixture = JSON.parse(readFileSync(process.argv[2] ?? '/tmp/fixture.json', 'utf8'));

const failures = [];
const check = (name, condition, detail = '') => {
  if (!condition) failures.push(`${name}${detail ? `: ${detail}` : ''}`);
};

// Build the store exactly as main.js does after a tick.
const history = {};
for (const [symbol, book] of Object.entries(fixture.snapshot.books)) {
  const base = Number(book.mark);
  // A plausible path, so the chart code actually has something to draw.
  history[symbol] = Array.from({ length: 80 }, (_, i) => base * (1 + Math.sin(i / 9) * 0.004));
}

/* The blotter only appears once the venue has sent something back, so a quiet
 * market leaves that code path unrendered. It is forced here instead: this test
 * first failed one run in three, purely because whether the bug showed up
 * depended on whether a fill happened to land during the fixture window. */
fixture.snapshot.log = [
  { t: 1_500_000_000, symbol: 'SPIKE_WR_FUT', type: 'ack',
    sequence: 1, agent_id: 'you', order_id: 1, side: 'buy', quantity: 10, price: null },
  { t: 1_600_000_000, symbol: 'SPIKE_WR_FUT', type: 'fill',
    sequence: 2, agent_id: 'you', order_id: 1, side: 'buy', quantity: 4,
    price: '4660.25', aggressor: true, remaining: 6 },
  { t: 1_700_000_000, symbol: 'SPIKE_GT48', type: 'reject',
    sequence: 3, agent_id: 'you', reason: 'insufficient_collateral', order_id: 2 },
  { t: 1_800_000_000, symbol: 'SPIKE_WR_FUT', type: 'cancel',
    sequence: 4, agent_id: 'you', order_id: 1, remaining: 6 },
  ...(fixture.snapshot.log ?? []),
];

const store = {
  view: 'markets',
  symbol: 'SPIKE_WR_FUT',
  snapshot: fixture.snapshot,
  instruments: fixture.instruments,
  session: fixture.session,
  agents: fixture.agents,
  diagnostics: fixture.diagnostics,
  depth: fixture.depth,
  history,
  side: 'buy',
  generation: fixture.snapshot.generation ?? 0,
};

const LEAKS = ['undefined', 'NaN', '[object Object]', 'null%', '$NaN'];

for (const name of ['markets', 'trade', 'portfolio', 'research', 'lab']) {
  let html;
  try {
    html = views[name](store);
  } catch (error) {
    failures.push(`${name} threw: ${error.message}`);
    continue;
  }
  check(`${name} produced HTML`, typeof html === 'string' && html.length > 200,
        `${html?.length ?? 0} chars`);
  for (const leak of LEAKS) {
    check(`${name} free of "${leak}"`, !html.includes(leak));
  }
  // Tags must balance well enough that the panel does not swallow the page.
  const open = (html.match(/<div/g) || []).length;
  const close = (html.match(/<\/div>/g) || []).length;
  check(`${name} div tags balance`, open === close, `${open} open, ${close} close`);
}

/* A view rendered with nothing yet loaded must degrade, not explode: this is
 * the state every view is in for the first second after the page opens. */
const cold = { ...store, snapshot: null, session: null, diagnostics: null, depth: null,
               agents: [], instruments: [], history: {} };
for (const name of ['markets', 'trade', 'portfolio', 'research', 'lab']) {
  try {
    const html = views[name](cold);
    check(`${name} survives an empty store`, typeof html === 'string' && html.length > 0);
  } catch (error) {
    failures.push(`${name} threw on empty store: ${error.message}`);
  }
}

/* Content that must actually reach the page. */
const trade = views.trade(store);
check('trade shows the symbol', trade.includes('SPIKE_WR_FUT'));
check('trade renders a ladder', trade.includes('lad-row'), 'no ladder rows');
check('trade renders a chart', trade.includes('<svg'), 'no chart svg');
check('trade offers post-only', trade.includes('post_only'));

const markets = views.markets(store);
for (const symbol of Object.keys(fixture.snapshot.books)) {
  check(`markets lists ${symbol}`, markets.includes(symbol));
}

const blotter = views.portfolio(store);
check('blotter renders event types', blotter.includes('reject') && blotter.includes('fill'));
check('blotter shows the symbol', blotter.includes('SPIKE_GT48'));
check('blotter shows a reject reason', blotter.includes('insufficient_collateral'));
check('blotter formats the fill price', blotter.includes('4,660.25'),
      'price not converted from ticks');

const lab = views.lab(store);
check('lab shows the seed', lab.includes('c-seed'));
check('lab lists fee schedules', lab.includes('maker-taker'));

/* Formatting edge cases — every one of these has a wrong answer that renders. */
check('price(null) is a dash', fmt.price(null) === '—', fmt.price(null));
check('price("") is a dash', fmt.price('') === '—', fmt.price(''));
check('price(0) is still zero', fmt.price(0) === '0.00', fmt.price(0));
check('money(null) is a dash', fmt.money(null) === '—', fmt.money(null));
check('money(0) is still zero', fmt.money(0) === '0.00', fmt.money(0));
check('signed(null) is a dash', fmt.signed(null) === '—', fmt.signed(null));
check('price("4660.25") formats', fmt.price('4660.25') === '4,660.25', fmt.price('4660.25'));
check('money handles millions', fmt.money(2_500_000) === '2.50M', fmt.money(2_500_000));
check('money marks negatives', fmt.money(-42).startsWith('−'), fmt.money(-42));
check('signed marks positives', fmt.signed(1.5) === '+1.50', fmt.signed(1.5));
check('clock formats minutes', fmt.clock(90e9) === '1m 30s', fmt.clock(90e9));
check('esc neutralises markup',
      fmt.esc('<img src=x onerror=1>') === '&lt;img src=x onerror=1&gt;');
check('sparkline of one point is empty', fmt.sparkline([1]) === '');
check('chart of one point degrades', fmt.priceChart([1]).includes('Waiting'));

if (failures.length) {
  console.error(`FAILED (${failures.length})`);
  failures.forEach((f) => console.error('  - ' + f));
  process.exit(1);
}
console.log('all frontend render checks passed');
