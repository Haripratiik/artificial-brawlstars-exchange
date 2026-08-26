/* main.js under a stub DOM.
 *
 * The controller had no coverage at all, which is awkward for the file that
 * owns the socket, the render loop and every event binding. A full DOM
 * emulator would be a heavy dependency for a project with no build step, so
 * this stubs the handful of APIs main.js actually touches — enough to catch a
 * load-time error, and enough to assert the property that matters:
 *
 *   snapshots arrive at 20Hz, and the expensive subtree must NOT be rebuilt
 *   twenty times a second.
 *
 * That is not only about CPU. Replacing the subtree destroys focus, scroll and
 * text selection, so a rebuild on every message makes the panel impossible to
 * use with a keyboard.
 *
 *     node tests/frontend/controller.mjs <fixture.json>
 */

import { readFileSync } from 'node:fs';

const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));

const failures = [];
const check = (name, ok, detail = '') => {
  if (!ok) failures.push(`${name}${detail ? `: ${detail}` : ''}`);
};

/* ── the smallest DOM that main.js will accept ────────────────────────── */

let innerHTMLWrites = 0;

function makeNode(id = '') {
  const node = {
    id,
    dataset: {},
    style: {},
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    children: [],
    childNodes: [],
    scrollTop: 0,
    textContent: '',
    get innerHTML() { return ''; },
    set innerHTML(_v) { innerHTMLWrites += 1; },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {},
    append() {}, replaceChildren() {}, replaceWith() {}, remove() {},
    focus() {}, closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    contains: () => false,
    value: '',
    checked: false,
  };
  return node;
}

const nodes = new Map();
for (const id of ['main', 'nav', 'watchlist', 'clock', 'events', 'equity', 'pnl',
                  'health', 'health-text', 'speed', 'speed-label', 'toaster']) {
  nodes.set(id, makeNode(id));
}

globalThis.document = {
  getElementById: (id) => nodes.get(id) ?? makeNode(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeNode(),
  addEventListener() {},
  activeElement: null,
  body: makeNode('body'),
};
globalThis.CSS = { escape: (s) => String(s) };
globalThis.location = { protocol: 'http:', host: 'localhost:8000', href: 'http://localhost:8000/', search: '' };
globalThis.history = { replaceState() {} };
globalThis.confirm = () => true;
globalThis.fetch = async () => ({ ok: true, json: async () => ({ instruments: [], agents: [] }) });

let sockets = [];
globalThis.WebSocket = class {
  static OPEN = 1;
  constructor() { this.readyState = 1; sockets.push(this); }
  send() {}
  close() {}
};

// Frames are driven by hand so the throttle can be measured rather than waited on.
let clockMs = 0;
const pending = [];
globalThis.requestAnimationFrame = (fn) => { pending.push(fn); return pending.length; };
function runFrame(advanceMs = 16) {
  clockMs += advanceMs;
  const due = pending.splice(0, pending.length);
  due.forEach((fn) => fn(clockMs));
}

globalThis.setInterval = () => 0;   // the periodic REST refreshes are not the subject
globalThis.setTimeout = (fn) => { void fn; return 0; };
globalThis.clearTimeout = () => {};

/* ── load ─────────────────────────────────────────────────────────────── */

try {
  await import('../../dashboard/static/js/main.js');
} catch (error) {
  console.error(`FAILED: main.js threw on load: ${error.message}`);
  process.exit(1);
}

check('the controller opened a socket', sockets.length === 1, `${sockets.length}`);
const socket = sockets[0];
check('the socket has a message handler', typeof socket.onmessage === 'function');

/* ── the render loop ──────────────────────────────────────────────────── */

const snapshot = { ...fixture.snapshot };
innerHTMLWrites = 0;

// One second of traffic at the real cadence: twenty snapshots, sixty frames.
for (let tick = 0; tick < 20; tick += 1) {
  socket.onmessage({ data: JSON.stringify({ ...snapshot, clock: tick * 5e7 }) });
  runFrame(16);
  runFrame(16);
  runFrame(18);
}

// Panels redraw on a ~120ms cadence, so a second of traffic is roughly eight
// rebuilds, not twenty. The exact figure depends on frame alignment; what is
// being asserted is that the two rates are decoupled at all.
check('panels are not rebuilt on every message',
      innerHTMLWrites < 20,
      `${innerHTMLWrites} rebuilds for 20 messages`);
check('panels are still being rebuilt',
      innerHTMLWrites > 0,
      'nothing rendered at all');

if (failures.length) {
  console.error(`FAILED (${failures.length})`);
  failures.forEach((f) => console.error('  - ' + f));
  process.exit(1);
}
console.log(`controller checks passed (${innerHTMLWrites} rebuilds for 20 messages)`);
