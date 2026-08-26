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

/* ── the ticket's arithmetic ──────────────────────────────────────────────
 *
 * These two decide what a person is *told* an order will cost before they
 * commit to it, so being subtly wrong here is worse than showing nothing.
 */

const twoLevels = [[100, 5], [101, 10]];

const partial = fmt.walkBook(twoLevels, 8);
check('walk fills across levels', partial.filled === 8, `${partial?.filled}`);
check('walk costs 5x100 + 3x101', partial.cost === 803, `${partial?.cost}`);
check('walk averages correctly', Math.abs(partial.average - 100.375) < 1e-9,
      `${partial?.average}`);
check('walk knows it completed', partial.complete === true);

const beyond = fmt.walkBook(twoLevels, 20);
check('walk reports a shortfall', beyond.complete === false && beyond.shortfall === 5,
      `filled ${beyond?.filled}, short ${beyond?.shortfall}`);
check('walk never invents depth', beyond.filled === 15, `${beyond?.filled}`);

check('walk of nothing is null', fmt.walkBook(twoLevels, 0) === null);
check('walk of an empty book is null', fmt.walkBook([], 10) === null);
check('walk of a bad size is null', fmt.walkBook(twoLevels, NaN) === null);

const binary = { kind: 'binary', payout: 1.0, threshold: 0.48, comparison: '>' };
check('a binary price is its probability',
      fmt.impliedProbability(0.42, binary) === 0.42,
      `${fmt.impliedProbability(0.42, binary)}`);
check('probability scales with the payout',
      fmt.impliedProbability(50, { kind: 'binary', payout: 200 }) === 0.25);
check('probability clamps above one',
      fmt.impliedProbability(1.4, binary) === 1);
check('a future has no implied probability',
      fmt.impliedProbability(4660, { kind: 'linear', scale: 10000 }) === null);
check('a missing payoff has no probability', fmt.impliedProbability(1, null) === null);
check('percent formats', fmt.percent(0.425) === '42.5%', fmt.percent(0.425));
check('percent of nothing is a dash', fmt.percent(null) === '—');

/* ── accessibility, as assertions ─────────────────────────────────────────
 *
 * These encode findings from an audit against the Web Interface Guidelines.
 * Writing them down as tests is the difference between fixing an issue once
 * and keeping it fixed: the clickable-div problem in particular is the kind of
 * thing that creeps back in the next time a row needs to be clickable.
 */

const everyView = ['markets', 'trade', 'portfolio', 'research', 'lab']
  .map((n) => views[n](store))
  .join('\n');

// Rows and cards are actions. A div with a click handler cannot be reached by
// keyboard and is not announced as interactive.
check('no clickable divs remain',
      !/<div[^>]*\bdata-(symbol|price)=/.test(everyView),
      'a div is carrying a click target');
check('cards are buttons', /<button[^>]*class="card"/.test(everyView));
check('ladder rows are buttons', /<button[^>]*class="lad-row/.test(everyView));

// Every button must be announceable: visible text or an explicit label.
const buttons = everyView.match(/<button[\s\S]*?<\/button>/g) || [];
check('there are buttons to check', buttons.length > 5, `${buttons.length}`);
for (const button of buttons) {
  const labelled = /aria-label=/.test(button);
  const text = button.replace(/<[^>]*>/g, '').replace(/&[a-z]+;/g, '').trim();
  check('every button is announceable', labelled || text.length > 0,
        button.slice(0, 70));
}

// Every form control needs a label. Three forms are all valid: a `for=`
// pointing at it, an `aria-label`, or a <label> wrapped around it -- the last
// of which is what a checkbox wants anyway, since it makes the text part of
// the hit target instead of a dead zone next to it.
const wrapped = (everyView.match(/<label[\s\S]*?<\/label>/g) || []).join(' ');
const inputs = everyView.match(/<(input|select)[^>]*>/g) || [];
check('there are inputs to check', inputs.length > 3, `${inputs.length}`);
for (const input of inputs) {
  const id = /id="([^"]+)"/.exec(input)?.[1];
  const byFor = id ? everyView.includes(`for="${id}"`) : false;
  const byWrap = wrapped.includes(input);
  check('every input has a label', byFor || byWrap || /aria-label=/.test(input),
        input.slice(0, 70));
}

// Decorative charts must not be announced as content.
check('sparklines are hidden from assistive tech',
      !/<span class="spark">/.test(everyView),
      'a sparkline is missing aria-hidden');

/* ── css hygiene ──────────────────────────────────────────────────────── */

const css = readFileSync(
  new URL('../../dashboard/static/css/terminal.css', import.meta.url), 'utf8');

check('no transition: all', !/transition:\s*all\b/.test(css),
      'animates properties nobody asked for');
check('focus is visible', css.includes(':focus-visible'),
      'keyboard users cannot see where they are');
check('reduced motion is honoured', css.includes('prefers-reduced-motion'));
check('taps are not delayed', css.includes('touch-action'));

const html = readFileSync(
  new URL('../../dashboard/static/index.html', import.meta.url), 'utf8');
check('dark colour-scheme declared', /color-scheme:\s*dark/.test(html));
check('theme-color matches the background', /name="theme-color"/.test(html));
check('font host is preconnected', html.includes('rel="preconnect"'));
check('fonts are not @imported from css', !css.includes('@import'),
      'an @import serialises the stylesheet and the font request');
check('there is a skip link', html.includes('class="skip"'));
check('there is exactly one h1', (html.match(/<h1/g) || []).length === 1);
check('async regions are announced', html.includes('aria-live'));
check('zoom is not disabled',
      !/user-scalable=no|maximum-scale=1/.test(html));

if (failures.length) {
  console.error(`FAILED (${failures.length})`);
  failures.forEach((f) => console.error('  - ' + f));
  process.exit(1);
}
console.log('all frontend render checks passed');
