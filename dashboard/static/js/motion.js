/* Motion, kept on a short leash.
 *
 * anime.js is vendored rather than pulled from a CDN, because the page loads
 * from a local server that may not have internet, and a chart that silently
 * stops animating because jsdelivr is unreachable is worse than no animation.
 *
 * Two rules govern everything here, and both come from the interface being a
 * *trading screen* rather than a landing page:
 *
 *   * **Motion carries information or it does not happen.** A number sliding to
 *     its new value says "this changed and here is by how much", which the
 *     static digits cannot. A panel that slides for its own sake charges an
 *     attention cost on every tick and repays nothing.
 *   * **Nothing is only motion.** Every animated change also has a static cue —
 *     a colour, a sign, a label — so a reader who missed the movement, or who
 *     has motion turned off entirely, loses nothing.
 *
 * The prediction-market design literature is explicit that aggressive red/green
 * flashing on price changes creates anxiety, so the tick tint here is a brief
 * low-opacity wash rather than a strobe.
 */

import { animate, utils } from './vendor/anime.esm.js';

export { animate, utils };

/** Honoured everywhere, and re-read each time so a mid-session change applies. */
export const reducedMotion = () =>
  globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

// The house curve. Written out rather than approximated: 0.2, 0, 0, 1 is not
// the same feel as the more common 0.4, 0, 0.2, 1.
export const EASE = 'cubicBezier(0.2, 0, 0, 1)';

/**
 * Count a number to its new value instead of jumping to it.
 *
 * This is the one place a motion library earns its keep. Interpolating a value
 * over 260ms and reformatting it every frame is not something CSS can express,
 * and the design guidance for prediction markets asks for exactly this — a
 * smooth transition in the 200-300ms band rather than a sudden jump.
 *
 * The element carries its own last value, so repeated calls chase the target
 * rather than restarting from zero, and an interrupted count continues from
 * wherever it had reached.
 */
export function countTo(element, value, format = (v) => v.toFixed(2)) {
  if (!element || !Number.isFinite(value)) return;

  const previous = Number(element.dataset.value);
  element.dataset.value = String(value);

  if (!Number.isFinite(previous) || previous === value || reducedMotion()) {
    element.textContent = format(value);
    return;
  }

  const state = { n: previous };
  animate(state, {
    n: value,
    duration: 260,
    ease: EASE,
    onUpdate: () => {
      element.textContent = format(state.n);
    },
  });
}

/**
 * A staggered entrance for a set that has just appeared.
 *
 * Reserved for infrequent, staged moments — switching to a screen, a market
 * rebuild — where the sequence communicates that these are separate things.
 * Running it on every tick would be an animation nobody asked for, forty times
 * a minute.
 */
export function revealAll(elements, { delay = 34, distance = 5 } = {}) {
  const nodes = [...elements];
  if (!nodes.length || reducedMotion()) return;
  animate(nodes, {
    opacity: [0, 1],
    translateY: [distance, 0],
    duration: 300,
    ease: EASE,
    delay: (_el, i) => i * delay,
  });
}

/**
 * Acknowledge a click on the element itself.
 *
 * 0.96 exactly. Below about 0.95 the press reads as a bounce rather than a
 * button giving under a finger.
 */
export function press(element) {
  if (!element || reducedMotion()) return;
  animate(element, {
    scale: [{ to: 0.96, duration: 90 }, { to: 1, duration: 130 }],
    ease: EASE,
  });
}
