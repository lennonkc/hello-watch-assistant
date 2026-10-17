// Motion layer for every tab: entrances, tab and theme transitions, count-ups, the ask flow, film-card tilt and the
// architecture diagram. It only watches the DOM the app already builds (Mutation/IntersectionObserver) and animates
// on top of it, so no app logic depends on it. Plain ES module, no dependencies.
// Contract: everything is skipped under prefers-reduced-motion; animations use opacity, transform and the individual
// translate/rotate/scale properties, mostly as partial keyframes ("from" only) so they settle on the page's own styles.

const root = document.documentElement;
const RM = matchMedia('(prefers-reduced-motion: reduce)');
// ?motion=0 turns the layer off (static screenshots, debugging).
const OFF = new URLSearchParams(location.search).get('motion') === '0';
let reduced = RM.matches;

function setReduced(on) {
  on ||= OFF;
  reduced = on;
  root.classList.toggle('hwa-m', !on);
  if (on) for (const el of document.querySelectorAll('[data-hwa-reveal]')) el.removeAttribute('data-hwa-reveal');
}
setReduced(RM.matches);
RM.addEventListener('change', (e) => setReduced(e.matches));

// ---------- helpers ----------

let tokens = null;
function tk() {
  if (tokens) return tokens;
  const cs = getComputedStyle(root);
  const v = (name, fb) => cs.getPropertyValue(name).trim() || fb;
  const ms = (name, fb) => {
    const s = v(name, '');
    if (!s) return fb;
    const n = parseFloat(s);
    return Number.isFinite(n) ? (s.endsWith('ms') ? n : n * 1000) : fb;
  };
  tokens = {
    out: v('--ease-out', 'cubic-bezier(.2,.8,.2,1)'),
    spring: v('--ease-spring', 'cubic-bezier(.34,1.4,.5,1)'),
    fast: ms('--dur-fast', 160), dur: ms('--dur', 280), slow: ms('--dur-slow', 600),
  };
  return tokens;
}

/** el.animate() that is a no-op under reduced motion. */
function play(el, frames, opts = {}) {
  if (reduced || !el?.animate) return null;
  try {
    return el.animate(frames, { duration: 420, easing: tk().out, fill: 'backwards', ...opts });
  } catch {
    return null;
  }
}

/** Animates from the given values to whatever the element's own styles say (partial keyframes). */
function from(el, props, opts) { return play(el, [{ ...props, offset: 0 }], opts); }

const raf = () => new Promise((r) => requestAnimationFrame(r));
const below = (el, frac = 0.92) => el.getBoundingClientRect().top > innerHeight * frac;
// Phones get shorter travel and quicker reveals.
const narrow = () => innerWidth < 700;

// ---------- number tweens ----------

const NUM_RE = /(\d[\d,]*(?:\.\d+)?)/;
const tweenVals = new Map(); // key -> numbers currently on screen
const tweenRuns = new Map(); // key -> run token

function firstNumText(el) {
  const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) if (/\d/.test(n.data)) return n;
  return null;
}

/**
 * Tweens every number in the first numeric text node of el from what was last shown under `key` (or from zeros)
 * to the text that is there now. The final text is restored exactly. Writes go to the text node's data, so
 * childList observers never see them.
 */
function tweenNumbers(el, key, { start, delay = 0, dur = 650 } = {}) {
  const node = el && firstNumText(el);
  if (!node) return;
  const target = node.data;
  const parts = target.split(NUM_RE);
  const nums = [], specs = [];
  for (let i = 1; i < parts.length; i += 2) {
    nums.push(parseFloat(parts[i].replace(/,/g, '')));
    specs.push({ comma: parts[i].includes(','), dec: (parts[i].split('.')[1] || '').length });
  }
  const prev = start ?? tweenVals.get(key);
  const a = prev && prev.length === nums.length ? prev.slice() : nums.map(() => 0);
  tweenRuns.set(key, null);
  if (reduced || a.every((x, i) => x === nums[i])) { tweenVals.set(key, nums); return; }
  const run = {};
  tweenRuns.set(key, run);
  const fmt = (x, sp) => (sp.comma
    ? x.toLocaleString('en', { minimumFractionDigits: sp.dec, maximumFractionDigits: sp.dec })
    : x.toFixed(sp.dec));
  const write = (vals) => {
    node.data = parts.map((p, i) => (i % 2 ? fmt(vals[(i - 1) / 2], specs[(i - 1) / 2]) : p)).join('');
  };
  write(a);
  const t0 = performance.now() + delay;
  const step = (now) => {
    if (tweenRuns.get(key) !== run) return;
    if (!node.isConnected) { tweenRuns.delete(key); return; }
    const t = Math.min(1, Math.max(0, (now - t0) / dur));
    const k = 1 - (1 - t) ** 4;
    const cur = a.map((x, i) => x + (nums[i] - x) * k);
    tweenVals.set(key, cur);
    if (t < 1) { write(cur); requestAnimationFrame(step); return; }
    node.data = target;
    tweenVals.set(key, nums);
    tweenRuns.delete(key);
  };
  requestAnimationFrame(step);
}

// ---------- tab ink ----------

function setupTabInk() {
  const bar = document.querySelector('.tabs');
  if (!bar) return;
  const ink = document.createElement('span');
  ink.className = 'hwa-tab-ink';
  ink.setAttribute('aria-hidden', 'true');
  bar.prepend(ink);
  let last = null;
  const place = (animate) => {
    const sel = bar.querySelector('[role="tab"][aria-selected="true"]');
    if (!sel) return;
    const br = bar.getBoundingClientRect(), sr = sel.getBoundingClientRect();
    const r = { x: sr.left - br.left - bar.clientLeft, y: sr.top - br.top - bar.clientTop, w: sr.width, h: sr.height };
    Object.assign(ink.style, { width: `${r.w}px`, height: `${r.h}px`, transform: `translate(${r.x}px, ${r.y}px)` });
    if (animate && last && Math.abs(last.x - r.x) > 1) {
      // Stretch across both tabs, then settle on the new one.
      const lo = Math.min(last.x, r.x), span = Math.max(last.x + last.w, r.x + r.w) - lo;
      play(ink, [
        { transform: `translate(${last.x}px, ${r.y}px)`, width: `${last.w}px` },
        { transform: `translate(${lo}px, ${r.y}px)`, width: `${span}px`, offset: 0.42 },
        { transform: `translate(${r.x}px, ${r.y}px)`, width: `${r.w}px` },
      ], { duration: 520, easing: tk().out, fill: 'none' });
    }
    last = r;
  };
  new MutationObserver(() => place(true)).observe(bar, { subtree: true, attributes: true, attributeFilter: ['aria-selected'] });
  new ResizeObserver(() => place(false)).observe(bar);
  document.fonts?.ready.then(() => place(false));
  place(false);
}

// ---------- view transitions: tab + theme ----------

const tabNames = () => [...document.querySelectorAll('[role="tab"][data-tab]')].map((t) => t.dataset.tab);
const selectedTab = () => document.querySelector('[role="tab"][aria-selected="true"]')?.dataset.tab ?? null;

function transition(fn, kind) {
  const run = () => { try { fn(); } catch (err) { console.error(err); } };
  if (reduced || !document.startViewTransition || document.visibilityState !== 'visible') { run(); return; }
  if (kind === 'theme') themeTransition(run);
  else if (kind === 'tab') tabTransition(run);
  else run();
}

function tabTransition(run) {
  const names = tabNames();
  const before = names.indexOf(selectedTab());
  let dir = 0;
  root.classList.add('hwa-vt-tab');
  const vt = document.startViewTransition(() => {
    run();
    dir = Math.sign(names.indexOf(selectedTab()) - before);
  });
  vt.ready.then(() => {
    root.animate({ opacity: [1, 0], transform: ['none', `translateX(${-dir * (narrow() ? 16 : 28)}px)`] }, {
      duration: 200, easing: 'cubic-bezier(.4,0,1,1)', fill: 'both', pseudoElement: '::view-transition-old(root)',
    });
  }).catch(() => {});
  vt.finished.finally(() => root.classList.remove('hwa-vt-tab'));
}

function themeTransition(run) {
  const btn = document.getElementById('theme-toggle');
  const r = btn?.getBoundingClientRect();
  const x = r ? r.left + r.width / 2 : innerWidth - 40;
  const y = r ? r.top + r.height / 2 : 32;
  const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  root.classList.add('hwa-vt-theme', 'hwa-theme-swap');
  const vt = document.startViewTransition(run);
  vt.ready.then(() => {
    root.animate({ clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] }, {
      duration: 620, easing: tk().out, pseudoElement: '::view-transition-new(root)',
    });
  }).catch(() => {});
  vt.finished.finally(() => root.classList.remove('hwa-vt-theme', 'hwa-theme-swap'));
}

function setupThemeIcon() {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  new MutationObserver((recs) => {
    for (const r of recs) {
      if (r.target.hasAttribute('hidden') || r.oldValue === null) continue;
      from(r.target, { rotate: '-120deg', scale: 0.4, opacity: 0 }, { duration: 560, easing: tk().spring });
    }
  }).observe(btn, { subtree: true, attributes: true, attributeFilter: ['hidden'], attributeOldValue: true });
  const mark = document.querySelector('.brand__mark');
  document.querySelector('.brand')?.addEventListener('pointerenter', (e) => {
    if (e.pointerType !== 'mouse') return;
    play(mark, [{ rotate: '0deg', scale: 1 }, { rotate: '-10deg', scale: 1.08, offset: 0.4 }, { rotate: '0deg', scale: 1 }],
      { duration: 640, fill: 'none' });
  });
}

// ---------- panel entrance + scroll reveal ----------

const contentOf = (panel) => panel.querySelector(':scope > .xp-intro, :scope > .arch');
const hostOf = (panel) => (panel.querySelector(':scope > .arch') || panel);

const REVEAL_SEL = [
  '.arch-section__head', '.arch-toolbar', '.arch-flow__main', '.arch-techmap__group', '.arch-gate__panel',
  '.arch-compare > *', '.arch-subhead', '.arch-rejected > li', '.arch-results > li', '.arch-papers > li',
].join(',');

const revealIO = new IntersectionObserver((entries) => {
  let k = 0;
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    revealIO.unobserve(e.target);
    reveal(e.target, k++ * (e.target instanceof SVGElement ? 35 : 70));
  }
}, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });

function reveal(el, delay = 0) {
  if (!el.hasAttribute('data-hwa-reveal')) return;
  from(el, { opacity: 0, translate: '0 18px' }, { duration: 620, delay });
  el.removeAttribute('data-hwa-reveal');
  if (el.matches('.arch-results > li')) countResult(el, delay + 120);
}

// The detail bottom sheet is position:fixed inside the flow section; a transformed or faded ancestor would become
// its containing block and hide it. Nothing that contains it is ever hidden or translated.
const holdsSheet = (el) => !!el.querySelector?.('.arch-detail');

function markReveal(el) {
  if (reduced || document.visibilityState === 'hidden' || el.hasAttribute('data-hwa-reveal') || holdsSheet(el)) return;
  el.setAttribute('data-hwa-reveal', '');
  revealIO.observe(el);
}

/** Staggered entrance of a panel's top-level blocks. Blocks below the fold reveal on scroll instead. */
function enterPanel(panel, dir = 0) {
  if (reduced) return;
  const blocks = [...hostOf(panel).children].filter((el) => !el.hidden);
  let i = 0;
  for (const el of blocks) {
    if (el.hasAttribute('data-hwa-reveal')) continue;
    if (below(el)) {
      const parts = el.matches('.arch > *') ? el.querySelectorAll(REVEAL_SEL) : [];
      if (parts.length) parts.forEach(markReveal); else markReveal(el);
      continue;
    }
    const delay = i++ * 70;
    const dx = dir * (narrow() ? 14 : 24);
    if (holdsSheet(el)) {
      // Entrance on the parts instead, so no transform ever sits on the sheet's ancestors.
      el.querySelectorAll(':scope > .arch-section__head, :scope > .arch-toolbar, .arch-flow__main').forEach((part, j) =>
        from(part, { opacity: 0, translate: `${dx}px 14px` }, { duration: 620, delay: delay + j * 70 }));
    } else {
      from(el, { opacity: 0, translate: `${dx}px 14px` }, { duration: 620, delay });
    }
    el.querySelectorAll('.xp-howto > li, .arch-ideas > li').forEach((li, j) =>
      from(li, { opacity: 0, translate: '0 10px' }, { duration: 520, delay: delay + 160 + j * 70 }));
    if (el.matches('.arch > *') && !el.matches('.arch-intro')) {
      // A section that starts on screen but runs past the fold: reveal its lower parts on scroll.
      el.querySelectorAll(REVEAL_SEL).forEach((part) => { if (below(part)) markReveal(part); });
    }
  }
}

async function stylesReady(panel) {
  if (!panel.querySelector(':scope > .arch')) return;
  const link = document.querySelector('link[data-arch-css]');
  if (!link || link.sheet) return;
  await new Promise((r) => {
    link.addEventListener('load', r, { once: true });
    link.addEventListener('error', r, { once: true });
    setTimeout(r, 1200);
  });
}

const entered = new WeakSet();
function watchMount(panel) {
  const check = async () => {
    const c = contentOf(panel);
    if (!c || entered.has(c)) return;
    entered.add(c);
    await stylesReady(panel);
    await raf();
    enterPanel(panel, 0);
    if (c.matches('.arch')) setupArch(c);
  };
  new MutationObserver(check).observe(panel, { childList: true });
  check();
}

let curTab = null;
document.addEventListener('hwa:tabchange', (e) => {
  const tab = e.detail?.tab;
  const prev = curTab;
  curTab = tab;
  if (!prev || prev === tab) return;
  const names = tabNames();
  const panel = document.getElementById(`panel-${tab}`);
  const c = panel && contentOf(panel);
  if (c && entered.has(c)) enterPanel(panel, Math.sign(names.indexOf(tab) - names.indexOf(prev)));
});

// ---------- live demo tab ----------

function revealWords(p, delay) {
  const text = p.textContent;
  if (reduced || !text.trim() || text.length > 6000) return 0;
  let segs;
  if (typeof Intl.Segmenter === 'function') {
    segs = [...new Intl.Segmenter(p.lang || 'en', { granularity: 'word' }).segment(text)].map((s) => s.segment);
  } else {
    segs = text.split(/(\s+)/).filter(Boolean);
  }
  // Glue punctuation onto the word before it.
  const units = [];
  for (const s of segs) {
    const last = units[units.length - 1];
    if (/\S/.test(s) && !/[\p{L}\p{N}]/u.test(s) && last && /\S$/.test(last)) units[units.length - 1] = last + s;
    else units.push(s);
  }
  const words = units.filter((s) => /\S/.test(s)).length;
  const step = Math.min(24, (narrow() ? 450 : 680) / Math.max(1, words));
  const frag = document.createDocumentFragment();
  let k = 0, lastSpan = null;
  for (const s of units) {
    if (!/\S/.test(s)) { frag.append(s); continue; }
    const span = document.createElement('span');
    span.className = 'hwa-w';
    span.textContent = s;
    span.style.animationDelay = `${Math.round(delay + k++ * step)}ms`;
    frag.append(span);
    lastSpan = span;
  }
  p.replaceChildren(frag);
  let done = false;
  const unwrap = () => { if (!done) { done = true; p.textContent = text; } };
  lastSpan?.addEventListener('animationend', unwrap, { once: true });
  setTimeout(unwrap, delay + k * step + 1200);
  return k * step;
}

function animAnswer(card) {
  from(card, { opacity: 0, translate: '0 14px', scale: 0.985 }, { duration: 560 });
  const text = card.querySelector('.xp-answer__text');
  const wordsMs = text ? revealWords(text, 140) : 0;
  [...card.children].filter((c) => c !== text).forEach((c, i) =>
    from(c, { opacity: 0, translate: '0 10px' }, { duration: 480, delay: 200 + wordsMs * 0.55 + i * 80 }));
  const box = card.querySelector('.xp-onscreen__box');
  if (box) from(box, { opacity: 0, scale: 1.25 }, { duration: 620, delay: 360 + wordsMs * 0.55, easing: tk().spring });
}

function animStep(li, delay = 0) {
  from(li, { opacity: 0, translate: '-12px 0' }, { duration: 420, delay });
}

function stepDone(li) {
  play(li.querySelector('.xp-step__icon'), [{ scale: 0.6 }, { scale: 1.16, offset: 0.55 }, { scale: 1 }],
    { duration: 460, fill: 'none' });
  const sum = li.querySelector('.xp-step__summary');
  if (sum?.textContent) from(sum, { opacity: 0, translate: '0 -3px' }, { duration: 320 });
}

function ripple(host, left, top, delay) {
  if (reduced || !host) return;
  const el = document.createElement('span');
  el.className = 'hwa-ripple';
  Object.assign(el.style, { left, top });
  host.append(el);
  const a = play(el, [{ scale: 0.6, opacity: 0.9 }, { scale: 3.2, opacity: 0 }],
    { duration: 820, delay, easing: 'cubic-bezier(.1,.6,.3,1)', fill: 'both' });
  if (a) a.onfinish = () => el.remove(); else el.remove();
}

// The app re-adds one-shot classes with remove() + reflow + add(); remove() of an absent class still writes the
// attribute, so one burst can produce two "added" records. Animate once per burst.
const lastFired = new WeakMap();
function once(el, gap = 80) {
  const now = performance.now();
  if (now - (lastFired.get(el) ?? -1e9) < gap) return false;
  lastFired.set(el, now);
  return true;
}

const added = (r, cls) => r.target.classList.contains(cls) && !(r.oldValue || '').split(/\s+/).includes(cls);
const removed = (r, cls) => !r.target.classList.contains(cls) && (r.oldValue || '').split(/\s+/).includes(cls);

function onXp(records) {
  const cards = [];
  for (const r of records) {
    const t = r.target;
    if (r.type === 'childList') {
      if (t.classList?.contains('xp-shield__stat')) {
        t.querySelectorAll('b').forEach((b) => tweenNumbers(b, `shield:${b.className}`));
      } else if (t.classList?.contains('xp-shield__chars')) {
        tweenNumbers(t.querySelector('b'), 'shield:chars');
      } else if (t.classList?.contains('xp-gate-rows')) {
        gateRows(t);
      } else if (t.matches?.('.xp-step--thinking .xp-step__label')) {
        from(t, { opacity: 0 }, { duration: 260 });
      } else if (t.classList?.contains('xp-evidence__quote') && !t.hidden && r.addedNodes.length) {
        from(t, { opacity: 0, translate: '0 -4px' }, { duration: 300 });
      }
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue;
        const c = n.classList;
        if (c.contains('xp-turn')) {
          from(n.querySelector('.xp-q'), { opacity: 0, scale: 0.92, translate: '0 10px' }, { duration: 520, easing: tk().spring });
          n.querySelectorAll('.xp-step').forEach((li, i) => animStep(li, 160 + i * 60));
        } else if (c.contains('xp-step')) animStep(n);
        else if (c.contains('xp-answer')) animAnswer(n);
        else if (c.contains('xp-divider')) from(n, { opacity: 0, scale: '0.6 1' }, { duration: 520 });
        else if (c.contains('xp-empty')) from(n, { opacity: 0, translate: '0 8px' }, { duration: 520 });
        else if (c.contains('xp-notice')) {
          from(n, { opacity: 0, translate: '0 8px' }, { duration: 380 });
          if (c.contains('xp-notice--error') || (!c.contains('xp-notice--muted') && !c.contains('xp-notice--slow'))) {
            play(n, [{ translate: '0 0' }, { translate: '-5px 0' }, { translate: '5px 0' }, { translate: '-3px 0' }, { translate: '0 0' }],
              { duration: 420, delay: 340, fill: 'none' });
          }
        } else if (n.querySelector?.(':scope > .xp-card:not(.xp-card--skeleton)')) cards.push(n);
      }
    } else if (r.attributeName === 'class') {
      if (removed(r, 'is-pending') && t.classList.contains('xp-step')) stepDone(t);
      else if (added(r, 'is-pulse') && t.classList.contains('xp-shield') && once(t)) {
        play(t.querySelector('.xp-shield__badge'), [{ scale: 1 }, { scale: 1.2, offset: 0.35 }, { scale: 1 }], { duration: 640, fill: 'none' });
      } else if (added(r, 'is-in') && t.classList.contains('xp-ring') && once(t)) {
        from(t, { scale: 2.2, opacity: 0 }, { duration: 560, easing: tk().spring });
        ripple(t.parentElement, t.style.left, t.style.top, 120);
        ripple(t.parentElement, t.style.left, t.style.top, 320);
      } else if (added(r, 'is-in') && t.classList.contains('xp-hl') && once(t)) {
        from(t, { opacity: 0, scale: 1.08 }, { duration: 560 });
        from(t.querySelector('.xp-hl__label'), { opacity: 0, translate: '0 6px' }, { duration: 420, delay: 240 });
      } else if (added(r, 'is-picking') && t.classList.contains('xp-player') && once(t)) {
        from(t.querySelector('.xp-picklayer'), { opacity: 0 }, { duration: 300 });
        from(t.querySelector('.xp-cross'), { scale: 0.3, opacity: 0 }, { duration: 460, easing: tk().spring });
      } else if (t.classList.contains('xp-send') && (added(r, 'is-stop') || removed(r, 'is-stop'))) {
        from(t.querySelector('svg'), { rotate: '-90deg', scale: 0.4, opacity: 0 }, { duration: 420, easing: tk().spring });
      }
    } else if (r.attributeName === 'hidden' && !t.hidden && r.oldValue !== null) {
      if (t.classList.contains('xp-pill')) {
        from(t, { opacity: 0, scale: 0.9, translate: '0 -6px' }, { duration: 460, easing: tk().spring });
      } else if (t.classList.contains('xp-step--thinking')) from(t, { opacity: 0 }, { duration: 300 });
      else if (t.classList.contains('xp-evidence__quote')) from(t, { opacity: 0, translate: '0 -4px' }, { duration: 320 });
    } else if (r.attributeName === 'open' && t.open && t.classList.contains('xp-gate-details')) {
      t.querySelectorAll('.xp-gate-row__bar i').forEach((i, j) =>
        from(i, { width: '0%' }, { duration: 700, delay: 60 + j * 50 }));
    }
  }
  if (cards.length) {
    cards.forEach((li, i) => {
      from(li, { opacity: 0, translate: '0 18px', scale: 0.97 }, { duration: 620, delay: i * 90 });
      addGlare(li.querySelector('.xp-card'));
    });
  }
}

const gateWidths = new Map();
function gateRows(box) {
  const open = box.closest('details')?.open;
  for (const row of box.querySelectorAll('.xp-gate-row')) {
    const label = row.querySelector('.xp-gate-row__label')?.textContent || '';
    tweenNumbers(row.querySelector('.xp-gate-row__num'), `gate:${label}`, { dur: 500 });
    const bar = row.querySelector('.xp-gate-row__bar i');
    if (!bar) continue;
    const w = bar.style.width;
    const prev = gateWidths.get(label);
    gateWidths.set(label, w);
    if (open && prev != null && prev !== w) from(bar, { width: prev }, { duration: 520 });
  }
}

// Film-card tilt: rotate on the card, parallax translate on the poster image, glare follows the pointer.
const tilts = new Map();
let tiltFrame = 0;

function addGlare(card) {
  const poster = card?.querySelector('.xp-card__poster');
  if (!poster || poster.querySelector('.hwa-glare')) return;
  const g = document.createElement('i');
  g.className = 'hwa-glare';
  g.setAttribute('aria-hidden', 'true');
  poster.append(g);
}

function tiltLoop() {
  tiltFrame = 0;
  for (const [card, s] of tilts) {
    s.x += (s.tx - s.x) * 0.18;
    s.y += (s.ty - s.y) * 0.18;
    const ang = Math.min(6, Math.hypot(s.x, s.y) * 12); // the corner under the pointer presses in, up to 6 degrees
    const img = card.querySelector('.xp-card__poster img');
    if (!s.on && Math.abs(s.x) < 0.002 && Math.abs(s.y) < 0.002) {
      card.style.rotate = '';
      if (img) img.style.translate = '';
      tilts.delete(card);
      continue;
    }
    card.style.rotate = ang > 0.01 ? `${s.y.toFixed(4)} ${(-s.x).toFixed(4)} 0 ${ang.toFixed(2)}deg` : '';
    if (img) img.style.translate = `${(-s.x * 12).toFixed(2)}px ${(-s.y * 8).toFixed(2)}px`;
    card.style.setProperty('--hwa-gx', `${((s.x + 0.5) * 100).toFixed(1)}%`);
    card.style.setProperty('--hwa-gy', `${((s.y + 0.5) * 100).toFixed(1)}%`);
  }
  if (tilts.size) tiltFrame = requestAnimationFrame(tiltLoop);
}

function setupTilt(panel) {
  const kick = () => { if (!tiltFrame) tiltFrame = requestAnimationFrame(tiltLoop); };
  panel.addEventListener('pointermove', (e) => {
    if (reduced || e.pointerType === 'touch') return;
    const card = e.target.closest?.('.xp-card');
    if (!card || card.classList.contains('xp-card--skeleton')) return;
    const r = card.getBoundingClientRect();
    let s = tilts.get(card);
    if (!s) tilts.set(card, (s = { x: 0, y: 0, tx: 0, ty: 0, on: true }));
    s.tx = Math.min(0.5, Math.max(-0.5, (e.clientX - r.left) / r.width - 0.5));
    s.ty = Math.min(0.5, Math.max(-0.5, (e.clientY - r.top) / r.height - 0.5));
    s.on = true;
    card.classList.add('hwa-tilting');
    kick();
  }, { passive: true });
  panel.addEventListener('pointerout', (e) => {
    const card = e.target.closest?.('.xp-card');
    if (!card || card.contains(e.relatedTarget)) return;
    card.classList.remove('hwa-tilting');
    const s = tilts.get(card);
    if (s) { s.tx = 0; s.ty = 0; s.on = false; kick(); }
  });
}

// ---------- how-it-works tab ----------

function countResult(li, delay) {
  tweenNumbers(li.querySelector('.arch-result__value'), li, { start: null, delay, dur: 900 });
}

function edgeDraw(line, dur, delay) {
  if (!line || line.closest('.arch-edge--infra')) return null;
  let len = 0;
  try { len = line.getTotalLength(); } catch { /* not rendered */ }
  if (!len) return null;
  const d = `${len.toFixed(1)} ${len.toFixed(1)}`;
  return play(line, [{ strokeDasharray: d, strokeDashoffset: len }, { strokeDasharray: d, strokeDashoffset: 0 }],
    { duration: dur, delay });
}

let drawStart = 0;
function drawDiagram(svg) {
  if (reduced || !svg) return;
  drawStart = performance.now();
  if (svg.closest('.arch')?.dataset.layout === 'narrow') {
    // The stacked phone diagram is taller than the screen: no stroke drawing, parts fade up as they scroll in.
    svg.querySelectorAll('.arch-lane, .arch-group, .arch-node, .arch-edge').forEach(markReveal);
    return;
  }
  svg.querySelectorAll('.arch-lane').forEach((g, i) => from(g, { opacity: 0 }, { duration: 600, delay: i * 90 }));
  svg.querySelectorAll('.arch-group').forEach((g, i) => from(g, { opacity: 0 }, { duration: 500, delay: 160 + i * 60 }));
  const nodes = [...svg.querySelectorAll('.arch-node')];
  const step = Math.min(40, 700 / Math.max(1, nodes.length));
  nodes.forEach((g, i) => from(g, { opacity: 0, translate: '0 10px' }, { duration: 520, delay: 200 + i * step }));
  const edgeT0 = 260 + nodes.length * step * 0.5;
  svg.querySelectorAll('.arch-edge').forEach((g, i) => {
    const delay = edgeT0 + i * 28;
    if (!edgeDraw(g.querySelector('.arch-edge__line'), 720, delay)) from(g, { opacity: 0 }, { duration: 500, delay });
    g.querySelectorAll('.arch-edge__head, .arch-edge__flow, .arch-edge__label, .arch-edge__labelbg')
      .forEach((el) => from(el, { opacity: 0 }, { duration: 360, delay: delay + 520 }));
  });
}

function pulseNode(g, color, delay = 0) {
  const box = g.querySelector('.arch-node__box');
  if (reduced || !box) return;
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  for (const a of ['x', 'y', 'width', 'height', 'rx']) rect.setAttribute(a, box.getAttribute(a));
  rect.setAttribute('class', 'hwa-pulse');
  if (color) rect.style.stroke = color;
  g.insertBefore(rect, box);
  const a = play(rect, [{ strokeWidth: 2, opacity: 0.75 }, { strokeWidth: 16, opacity: 0 }],
    { duration: 720, delay, easing: 'cubic-bezier(.1,.6,.3,1)', fill: 'both' });
  if (a) a.onfinish = () => rect.remove(); else rect.remove();
}

function onArch(records) {
  const freshSvgs = new Set();
  const cellsByStrip = new Map();
  let selected = false, sheetOpened = false;
  const pending = [];
  for (const r of records) {
    const t = r.target;
    if (r.type === 'childList') {
      for (const n of r.addedNodes) if (n.nodeName === 'svg') freshSvgs.add(n);
      if (t.classList?.contains('arch-canvas') && freshSvgs.size && drawStart && performance.now() - drawStart < 1600) {
        drawDiagram(t.querySelector('svg'));
      }
      if (t.classList?.contains('arch-stat__value')) tweenNumbers(t, t, { dur: 520 });
      if (t.classList?.contains('arch-detail__body')) pending.push(() => detailSwap(t, selected, sheetOpened));
      continue;
    }
    if (freshSvgs.has(t.ownerSVGElement)) continue;
    if (r.attributeName === 'data-open' && t.classList.contains('arch-detail') && t.dataset.open === 'true'
      && r.oldValue !== 'true' && t.closest('.arch')?.dataset.panel === 'sheet') {
      sheetOpened = true;
    } else if (r.attributeName === 'class') {
      if (added(r, 'is-new') && t.classList.contains('arch-cell')) {
        const list = cellsByStrip.get(t.parentElement) || [];
        list.push(t);
        cellsByStrip.set(t.parentElement, list);
      } else if (added(r, 'is-selected') && t.classList.contains('arch-node')) {
        selected = true;
        pulseNode(t);
      } else if (added(r, 'is-match') && t.classList.contains('arch-node')) {
        pending.push((i) => pulseNode(t, 'var(--f)', i * 40));
      } else if (added(r, 'is-active') && t.classList.contains('arch-edge')) {
        pending.push(() => { if (selected) edgeDraw(t.querySelector('.arch-edge__line'), 420, 0); });
      }
    } else if (r.attributeName === 'hidden' && !t.hidden && r.oldValue !== null
      && (t.classList.contains('arch-tourbar') || t.classList.contains('arch-filterbar'))) {
      from(t, { opacity: 0, translate: '0 -6px' }, { duration: 340 });
    }
  }
  pending.forEach((fn, i) => fn(i));
  for (const cells of cellsByStrip.values()) {
    const step = Math.min(14, 320 / cells.length);
    cells.forEach((c, j) => play(c, [{ scale: 0.4 }, { scale: 1.35, offset: 0.6 }, { scale: 1 }],
      { duration: 440, delay: j * step }));
  }
}

function detailSwap(body, strong, sheetOpened) {
  if (strong) {
    // When the bottom sheet is sliding up, let it arrive before its content settles.
    const t0 = sheetOpened ? 260 : 0;
    [...body.children].forEach((c, i) => from(c, { opacity: 0, translate: '0 8px' }, { duration: 380, delay: t0 + i * 35 }));
  } else {
    from(body, { opacity: 0.35 }, { duration: 180, easing: 'ease-out' });
  }
}

function setupArch(arch) {
  const canvas = arch.querySelector('.arch-canvas');
  if (canvas) {
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      io.disconnect();
      (document.fonts ? document.fonts.ready : Promise.resolve()).then(raf).then(() => drawDiagram(canvas.querySelector('svg')));
    }, { threshold: 0.2 });
    io.observe(canvas);
  }
  // Results above the fold at mount never pass through reveal(); count them when they first show.
  const rio = new IntersectionObserver((entries) => {
    entries.forEach((e, i) => {
      if (!e.isIntersecting) return;
      rio.unobserve(e.target);
      if (!e.target.hasAttribute('data-hwa-reveal')) countResult(e.target, i * 80);
    });
  }, { threshold: 0.4 });
  arch.querySelectorAll('.arch-results > li').forEach((li) => rio.observe(li));
}

// Never print or save a page with unrevealed sections.
addEventListener('beforeprint', () => {
  for (const el of document.querySelectorAll('[data-hwa-reveal]')) { revealIO.unobserve(el); el.removeAttribute('data-hwa-reveal'); }
});

// ---------- boot ----------

function boot() {
  setupTabInk();
  setupThemeIcon();
  curTab = selectedTab();
  const xp = document.getElementById('panel-experience');
  const ar = document.getElementById('panel-architecture');
  const opts = { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'hidden', 'open'], attributeOldValue: true };
  if (xp) {
    watchMount(xp);
    setupTilt(xp);
    new MutationObserver(onXp).observe(xp, opts);
  }
  if (ar) {
    watchMount(ar);
    new MutationObserver(onArch).observe(ar, { ...opts, attributeFilter: [...opts.attributeFilter, 'data-open'] });
  }
}

window.hwaMotion = {
  transition,
  /** Sets el's text and tweens its numbers from what was shown before. */
  count(el, text, key = el) { if (!el) return; el.textContent = String(text); tweenNumbers(el, key); },
  reveal: markReveal,
  get reduced() { return reduced; },
};

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
else boot();
