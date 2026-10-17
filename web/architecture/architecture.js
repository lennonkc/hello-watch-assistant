// Tab 2, "How it works": interactive pipeline diagram, Google Cloud technology map and a live spoiler-gate demo.
// Plain ES module, no dependencies. The shell calls mount(el) once, the first time the tab is shown.

import {
  REPO, SERVICES, PAPERS, LANES, NODES, GROUPS, EDGES, WIDE, NARROW, TOURS, RESULTS, REJECTED, GATE_TYPES,
  FALLBACK_TITLES,
} from './data.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const NODE = new Map(NODES.map((n) => [n.id, n]));
const SERVICE = new Map(SERVICES.map((s) => [s.id, s]));
const FAMILIES = [
  ['gemini', 'Gemini and agents'], ['run', 'Cloud Run'], ['storage', 'Cloud Storage'],
  ['secret', 'Secret Manager'], ['build', 'Build and operations'],
];
const WIDE_MIN = 700; // below this the diagram switches to the stacked phone layout
const SIDE_PANEL_MIN = 1080; // below this the detail panel becomes a bottom sheet

const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const familyVar = (family) => `var(--gc-${family})`;
const primaryFamily = (n) => (n.tech.length ? SERVICE.get(n.tech[0]).family : null);

// ── tiny DOM helpers ─────────────────────────────────────────────────

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  setProps(el, props);
  el.append(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  return el;
}

function s(tag, props = {}, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  setProps(el, props);
  el.append(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  return el;
}

function setProps(el, props) {
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k === 'text') el.textContent = v;
    else if (k === 'style') el.style.cssText = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
}

function mmss(t) {
  const sec = Math.max(0, Math.floor(t));
  return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
}

function ensureStylesheet() {
  const href = new URL('./architecture.css', import.meta.url).href;
  let link = document.querySelector(`link[data-arch-css]`);
  if (!link) {
    link = h('link', { rel: 'stylesheet', href, 'data-arch-css': '' });
    document.head.append(link);
  }
  if (link.sheet) return Promise.resolve();
  return new Promise((resolve) => {
    link.addEventListener('load', resolve, { once: true });
    link.addEventListener('error', resolve, { once: true });
    setTimeout(resolve, 1500);
  });
}

// ── mount ────────────────────────────────────────────────────────────

export function mount(el) {
  if (el.dataset.archMounted) return () => {};
  el.dataset.archMounted = '1';
  const ready = ensureStylesheet();

  const ctx = {
    selected: 'gate', // pinned node
    hover: null, // node previewed by pointer
    filter: null, // service id highlighted from the technology map
    tour: null, // { key, i }
    layout: null, // 'wide' | 'narrow'
    panel: null, // 'side' | 'sheet'
    sheetOpen: false,
    nodeEls: new Map(),
    edgeEls: [],
    cleanups: [],
  };

  const root = h('div', { class: 'arch' });
  ctx.root = root;
  root.append(
    intro(),
    flowSection(ctx),
    techMap(ctx),
    gateDemo(ctx),
    structural(),
    results(),
    research(ctx),
  );
  el.append(root);

  const ro = new ResizeObserver(() => relayout(ctx));
  ro.observe(root);
  ctx.cleanups.push(() => ro.disconnect());
  const onKey = (e) => {
    if (e.key !== 'Escape' || el.hidden) return;
    if (ctx.sheetOpen) closeSheet(ctx);
    else if (ctx.tour) endTour(ctx);
    else if (ctx.filter) setFilter(ctx, null);
  };
  document.addEventListener('keydown', onKey);
  ctx.cleanups.push(() => document.removeEventListener('keydown', onKey));

  ready.then(() => (document.fonts ? document.fonts.ready : null)).then(() => {
    ctx.layout = null; // force a render with final fonts so text fitting measures correctly
    relayout(ctx);
  });
  relayout(ctx);

  return () => ctx.cleanups.forEach((fn) => fn());
}

function relayout(ctx) {
  const width = ctx.root.clientWidth;
  if (!width) return; // hidden tab
  const layout = width >= WIDE_MIN ? 'wide' : 'narrow';
  const panel = width >= SIDE_PANEL_MIN ? 'side' : 'sheet';
  if (panel !== ctx.panel) {
    ctx.panel = panel;
    ctx.root.dataset.panel = panel;
    if (panel === 'side') ctx.sheetOpen = false;
    syncSheet(ctx);
  }
  if (layout !== ctx.layout) {
    ctx.layout = layout;
    ctx.root.dataset.layout = layout;
    renderDiagram(ctx);
  }
}

// ── intro ────────────────────────────────────────────────────────────

function intro() {
  const ideas = [
    ['Every fact has a reveal time',
      'Gemini watches each film once, offline, and writes a knowledge pack in which every row records the moment it is revealed on screen.'],
    ['Gate, then rank',
      'When you pause at P, the server first computes what is already visible. Retrieval and the agent only ever read that set.'],
    ['Guard the words, too',
      'Each answer is checked for real people’s names, future “canary” terms and character names the film hasn’t revealed yet.'],
  ];
  return h('header', { class: 'arch-intro' },
    h('p', { class: 'arch-eyebrow', text: 'How it works' }),
    h('h2', { class: 'arch-title', text: 'The model never sees what you haven’t seen.' }),
    h('p', { class: 'arch-lead', text:
      'A general chatbot knows the whole film from reviews, wikis and its training data, so “who is the old man?” at minute 12 can '
      + 'spoil the reveal at minute 80. Here spoiler safety is not an instruction to the model. It is structural: the server hands '
      + 'the agent only what is already on screen at your paused position P.' }),
    h('ol', { class: 'arch-ideas' }, ideas.map(([t, d], i) =>
      h('li', { class: 'arch-idea' },
        h('span', { class: 'arch-idea__n', 'aria-hidden': 'true', text: String(i + 1) }),
        h('strong', { text: t }), h('p', { text: d })))),
  );
}

// ── flow diagram section ─────────────────────────────────────────────

function flowSection(ctx) {
  const tourBtns = Object.entries(TOURS).map(([key, t]) =>
    h('button', { type: 'button', class: 'arch-btn', 'data-tour': key, onclick: () => startTour(ctx, key) },
      h('span', { class: 'arch-btn__icon', 'aria-hidden': 'true', text: '▶' }), t.label));

  ctx.tourBar = h('div', { class: 'arch-tourbar', hidden: true, role: 'group', 'aria-label': 'Guided tour' },
    h('span', { class: 'arch-tourbar__label' }),
    h('button', { type: 'button', class: 'arch-btn arch-btn--ghost', onclick: () => stepTour(ctx, -1), text: 'Back' }),
    h('button', { type: 'button', class: 'arch-btn', onclick: () => stepTour(ctx, 1), text: 'Next' }),
    h('button', { type: 'button', class: 'arch-btn arch-btn--ghost', onclick: () => endTour(ctx), text: 'End tour' }));

  ctx.filterBar = h('div', { class: 'arch-filterbar', hidden: true, role: 'status' },
    h('span', { class: 'arch-filterbar__label' }),
    h('button', { type: 'button', class: 'arch-btn arch-btn--ghost', onclick: () => setFilter(ctx, null), text: 'Clear' }));

  ctx.canvas = h('div', { class: 'arch-canvas' });
  ctx.detail = h('aside', { class: 'arch-detail', 'aria-label': 'Step details', tabindex: '-1' });
  ctx.detailBody = h('div', { class: 'arch-detail__body', 'aria-live': 'polite' });
  ctx.detail.append(
    h('button', { type: 'button', class: 'arch-detail__close', 'aria-label': 'Close details', onclick: () => closeSheet(ctx) },
      h('span', { 'aria-hidden': 'true', text: '×' })),
    ctx.detailBody);

  const legend = h('ul', { class: 'arch-legend', 'aria-label': 'Legend' },
    FAMILIES.map(([f, label]) => h('li', { style: `--c: ${familyVar(f)}` }, h('span', { class: 'arch-dot' }), label)),
    h('li', { class: 'arch-legend__gate' }, h('span', { class: 'arch-dot' }), 'Spoiler gate path'),
    h('li', { class: 'arch-legend__infra' }, h('span', { class: 'arch-dash' }), 'Platform support'));

  const sec = h('section', { class: 'arch-section arch-flow', 'aria-labelledby': 'arch-flow-h' },
    h('div', { class: 'arch-section__head' },
      h('h3', { id: 'arch-flow-h', text: 'Two pipelines, one gate' }),
      h('p', { text: 'Offline, Gemini turns each film into a time-stamped knowledge pack. Online, every question passes the '
        + 'spoiler gate before the agent sees anything. Select a step to see what it does and which Google technology runs it.' })),
    h('div', { class: 'arch-toolbar' }, h('div', { class: 'arch-toolbar__tours' }, tourBtns), ctx.tourBar, ctx.filterBar),
    h('div', { class: 'arch-flow__grid' },
      h('div', { class: 'arch-flow__main' }, ctx.canvas, legend),
      ctx.detail));
  renderDetail(ctx);
  return sec;
}

// Layout → { width, height, boxes: Map(id → rect), groups: Map(id → rect), lanes: [{ lane, rect }] }
function computeLayout(kind) {
  const rect = ([x, y, w, h]) => ({ x, y, w, h });
  if (kind === 'wide') {
    return {
      kind, width: WIDE.width, height: WIDE.height,
      boxes: new Map(Object.entries(WIDE.nodes).map(([id, r]) => [id, rect(r)])),
      groups: new Map(Object.entries(WIDE.groups).map(([id, r]) => [id, rect(r)])),
      lanes: Object.entries(WIDE.lanes).map(([lane, r]) => ({ lane, rect: rect(r) })),
    };
  }
  const W = NARROW.width, PAD = 16, GAP = 8, ROW_GAP = 26;
  const boxes = new Map(), groups = new Map(), lanes = [];
  let y = 8, lane = null;
  const closeLane = () => {
    if (!lane) return;
    const bottom = y - ROW_GAP + 14;
    lanes.push({ lane: lane.id, rect: { x: 4, y: lane.y, w: W - 8, h: bottom - lane.y } });
    y = bottom + 18;
    lane = null;
  };
  const placeRow = (ids, x0, width) => {
    const n = ids.length, cw = (width - GAP * (n - 1)) / n;
    const allSmall = ids.every((id) => ['infra', 'tool'].includes(NODE.get(id).kind));
    const hgt = allSmall ? 40 : NODE.get(ids[0]).id === 'pack' ? 60 : 52;
    ids.forEach((id, i) => boxes.set(id, { x: x0 + i * (cw + GAP), y, w: cw, h: hgt }));
    y += hgt + ROW_GAP;
  };
  for (const row of NARROW.rows) {
    if (row.lane) { closeLane(); lane = { id: row.lane, y }; y += 40; continue; }
    if (row.group) {
      const gy = y;
      y += 30;
      for (const r of row.rows) placeRow(r, PAD + 10, W - 2 * PAD - 20);
      const gh = y - ROW_GAP - gy + 10;
      groups.set(row.group, { x: PAD, y: gy, w: W - 2 * PAD, h: gh });
      y = gy + gh + ROW_GAP;
      continue;
    }
    if (row.length === 1 && NODE.get(row[0]).lane === 'bridge') closeLane();
    placeRow(row, PAD, W - 2 * PAD);
  }
  closeLane();
  return { kind, width: W, height: y - 10, boxes, groups, lanes };
}

function renderDiagram(ctx) {
  const L = computeLayout(ctx.layout);
  ctx.L = L;
  ctx.nodeEls.clear();
  ctx.edgeEls = [];

  const svg = s('svg', {
    class: 'arch-svg', viewBox: `0 0 ${L.width} ${L.height}`, role: 'group',
    'aria-label': 'Pipeline diagram: offline ingest on Cloud Run Jobs feeds a knowledge pack; online questions pass the spoiler gate before the ADK agent and its tools.',
  });
  const gLanes = s('g', { class: 'arch-lanes', 'aria-hidden': 'true' });
  const gGroups = s('g', { class: 'arch-groups', 'aria-hidden': 'true' });
  const gEdges = s('g', { class: 'arch-edges', 'aria-hidden': 'true' });
  const gNodes = s('g', { class: 'arch-nodes' });
  svg.append(gLanes, gGroups, gEdges, gNodes);

  for (const { lane, rect: r } of L.lanes) {
    const meta = LANES[lane];
    gLanes.append(s('g', { class: `arch-lane arch-lane--${lane}` },
      s('rect', { x: r.x, y: r.y, width: r.w, height: r.h, rx: 18 }),
      s('text', { x: r.x + 16, y: r.y + 26, class: 'arch-lane__label' },
        meta.label, L.kind === 'wide' ? s('tspan', { class: 'arch-lane__sub', dx: 10, text: meta.sub }) : null)));
  }
  for (const [id, r] of L.groups) {
    const g = GROUPS[id];
    gGroups.append(s('g', { class: 'arch-group', style: `--c: ${familyVar(g.tech)}`, 'data-group': id },
      s('rect', { x: r.x, y: r.y, width: r.w, height: r.h, rx: 14 }),
      s('text', { x: r.x + 12, y: r.y + 19, class: 'arch-group__label', text: g.label })));
  }

  // Nodes first (measuring needs them in the DOM), edges after.
  ctx.canvas.replaceChildren(svg);
  for (const n of NODES) {
    const r = L.boxes.get(n.id);
    if (r) gNodes.append(nodeEl(ctx, n, r));
  }
  for (const e of EDGES) {
    if (e.only && e.only !== L.kind) continue;
    const el = edgeEl(e, L);
    if (el) gEdges.append(el), ctx.edgeEls.push({ e, el }), decorateEdge(e, el, L);
  }
  for (const [id, r] of L.groups) {
    const label = gGroups.querySelector(`[data-group="${id}"] text`);
    fitText(label, r.w - 24);
  }
  applyState(ctx);
}

function nodeEl(ctx, n, r) {
  const fam = primaryFamily(n);
  const techNames = n.tech.map((t) => SERVICE.get(t).name).join(', ');
  const g = s('g', {
    class: `arch-node arch-node--${n.kind}`, 'data-id': n.id, tabindex: 0, role: 'button',
    style: fam ? `--c: ${familyVar(fam)}` : null,
    'aria-label': `${n.title}. ${n.sub}.${techNames ? ` Uses ${techNames}.` : ''}`,
  });
  const icon = n.kind === 'gate' || n.id === 'pack';
  const tx = r.x + (icon ? 36 : 14);
  const showSub = r.h >= 44 && r.w >= 120;
  const titleY = showSub ? r.y + r.h / 2 - 2 : r.y + r.h / 2 + 4.5;
  g.append(
    s('rect', { class: 'arch-node__box', x: r.x, y: r.y, width: r.w, height: r.h, rx: 10 }),
    s('rect', { class: 'arch-node__bar', x: r.x, y: r.y + 8, width: 3.5, height: r.h - 16, rx: 1.75 }),
  );
  if (n.kind === 'gate') g.append(shieldIcon(r.x + 12, r.y + r.h / 2 - 10));
  if (n.id === 'pack') g.append(cylinderIcon(r.x + 13, r.y + r.h / 2 - 10));
  const title = s('text', { class: 'arch-node__title', x: tx, y: titleY, text: n.title });
  g.append(title);
  let sub = null;
  if (showSub) {
    sub = s('text', { class: 'arch-node__sub', x: tx, y: titleY + 15, text: n.sub });
    g.append(sub);
  }
  ctx.nodeEls.set(n.id, g);
  // fit after insertion (getComputedTextLength needs layout)
  queueMicrotask(() => {
    fitText(title, r.x + r.w - tx - 10, n.short);
    if (sub) fitText(sub, r.x + r.w - tx - 10);
  });

  g.addEventListener('click', () => select(ctx, n.id, { user: true }));
  g.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(ctx, n.id, { user: true }); }
  });
  g.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') preview(ctx, n.id); });
  g.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') preview(ctx, null); });
  g.addEventListener('focus', () => { if (ctx.panel === 'side') preview(ctx, n.id); });
  g.addEventListener('blur', () => preview(ctx, null));
  return g;
}

function shieldIcon(x, y) {
  return s('path', {
    class: 'arch-node__icon', transform: `translate(${x} ${y})`,
    d: 'M10 1.5 17.5 4.5v5.2c0 4.6-3.2 8-7.5 9.3C5.7 17.7 2.5 14.3 2.5 9.7V4.5z M6.5 10l2.5 2.5 4.5-5',
  });
}

function cylinderIcon(x, y) {
  return s('path', {
    class: 'arch-node__icon', transform: `translate(${x} ${y})`,
    d: 'M3 4.5c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3zm0 0v11c0 1.7 3.1 3 7 3s7-1.3 7-3v-11M3 10c0 1.7 3.1 3 7 3s7-1.3 7-3',
  });
}

function fitText(el, maxW, short) {
  if (!el || maxW <= 0) return;
  let len;
  try { len = el.getComputedTextLength(); } catch { return; }
  if (!len || len <= maxW) return;
  if (short) {
    el.textContent = short;
    if (el.getComputedTextLength() <= maxW) return;
  }
  let text = el.textContent;
  while (text.length > 2 && el.getComputedTextLength() > maxW) {
    text = text.slice(0, -1);
    el.textContent = `${text.trimEnd()}…`;
  }
}

// ── edges ────────────────────────────────────────────────────────────

const DIR = { right: [1, 0], left: [-1, 0], bottom: [0, 1], top: [0, -1] };

function sidePoint(r, side) {
  if (side === 'right') return [r.x + r.w, r.y + r.h / 2];
  if (side === 'left') return [r.x, r.y + r.h / 2];
  if (side === 'bottom') return [r.x + r.w / 2, r.y + r.h];
  return [r.x + r.w / 2, r.y];
}

function autoSides(a, b) {
  if (b.x >= a.x + a.w - 2) return ['right', 'left'];
  if (b.x + b.w <= a.x + 2) return ['left', 'right'];
  if (b.y >= a.y + a.h - 2) return ['bottom', 'top'];
  return ['top', 'bottom'];
}

function rectOf(L, id) {
  return L.boxes.get(id) || L.groups.get(id);
}

function edgePath(e, L) {
  const a = rectOf(L, e.from), b = rectOf(L, e.to);
  if (!a || !b) return null;
  const route = e.route?.[L.kind];
  if (route === 'gutter-left' || route === 'gutter-right') {
    const left = route === 'gutter-left';
    const gx = left ? 8 : L.width - 8;
    const ax = left ? a.x : a.x + a.w, bx = left ? b.x : b.x + b.w;
    const ay = a.y + a.h / 2, by = b.y + Math.min(b.h / 2, 40);
    const rr = 8, sx = left ? 1 : -1;
    return `M${ax},${ay} H${gx + sx * rr} Q${gx},${ay} ${gx},${ay + rr} V${by - rr} Q${gx},${by} ${gx + sx * rr},${by} H${bx}`;
  }
  const [sa, sb] = e.sides?.[L.kind] || autoSides(a, b);
  const p0 = sidePoint(a, sa), p3 = sidePoint(b, sb);
  const horiz = DIR[sa][1] === 0 && DIR[sb][1] === 0;
  const vert = DIR[sa][0] === 0 && DIR[sb][0] === 0;
  if (horiz) {
    const lo = Math.max(a.y, b.y) + 10, hi = Math.min(a.y + a.h, b.y + b.h) - 10;
    if (hi >= lo) p0[1] = p3[1] = (lo + hi) / 2;
  } else if (vert) {
    const lo = Math.max(a.x, b.x) + 14, hi = Math.min(a.x + a.w, b.x + b.w) - 14;
    if (hi >= lo) p0[0] = p3[0] = (lo + hi) / 2;
  }
  const dx = Math.abs(p3[0] - p0[0]), dy = Math.abs(p3[1] - p0[1]);
  const k = (side) => Math.max(18, 0.5 * (DIR[side][0] ? dx : dy));
  const [d0, d3] = [DIR[sa], DIR[sb]];
  const p1 = [p0[0] + d0[0] * k(sa), p0[1] + d0[1] * k(sa)];
  const p2 = [p3[0] + d3[0] * k(sb), p3[1] + d3[1] * k(sb)];
  const f = (p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`;
  return `M${f(p0)} C${f(p1)} ${f(p2)} ${f(p3)}`;
}

function edgeEl(e, L) {
  const d = edgePath(e, L);
  if (!d) return null;
  const kind = e.kind || 'flow';
  return s('g', { class: `arch-edge arch-edge--${kind}`, 'data-from': e.from, 'data-to': e.to },
    s('path', { class: 'arch-edge__line', d }),
    kind !== 'infra' ? s('path', { class: 'arch-edge__flow', d }) : null);
}

// Arrowheads and labels need the path's geometry, so they are added once the edge is in the DOM.
function decorateEdge(e, el, L) {
  const path = el.querySelector('.arch-edge__line');
  const len = path.getTotalLength();
  if (!len) return;
  const head = (atEnd) => {
    const tip = path.getPointAtLength(atEnd ? len : 0);
    const back = path.getPointAtLength(atEnd ? Math.max(0, len - 6) : Math.min(len, 6));
    const ang = Math.atan2(tip.y - back.y, tip.x - back.x);
    const sz = 7, w = 4;
    const bx = tip.x - Math.cos(ang) * sz, by = tip.y - Math.sin(ang) * sz;
    const px = -Math.sin(ang) * w, py = Math.cos(ang) * w;
    return s('path', {
      class: 'arch-edge__head',
      d: `M${tip.x.toFixed(1)},${tip.y.toFixed(1)} L${(bx + px).toFixed(1)},${(by + py).toFixed(1)} L${(bx - px).toFixed(1)},${(by - py).toFixed(1)}Z`,
    });
  };
  el.append(head(true));
  if (e.both) el.append(head(false));
  if (e.label && !e.route?.[L.kind]) {
    const mid = path.getPointAtLength(len / 2);
    const text = s('text', { class: 'arch-edge__label', x: mid.x, y: mid.y + 3.5, 'text-anchor': 'middle', text: e.label });
    const bg = s('rect', { class: 'arch-edge__labelbg', rx: 8 });
    el.append(bg, text);
    const w = text.getComputedTextLength() + 14;
    setProps(bg, { x: mid.x - w / 2, y: mid.y - 8.5, width: w, height: 17 });
  }
}

// ── selection, preview, filter, tour ─────────────────────────────────

function select(ctx, id, { user = false } = {}) {
  ctx.selected = id;
  ctx.hover = null;
  if (user && ctx.tour && TOURS[ctx.tour.key].steps[ctx.tour.i] !== id) endTour(ctx, { keepSelection: true });
  if (ctx.panel === 'sheet' && user) openSheet(ctx);
  renderDetail(ctx);
  applyState(ctx);
}

function preview(ctx, id) {
  if (ctx.panel !== 'side') return;
  if (ctx.hover === id) return;
  ctx.hover = id;
  renderDetail(ctx);
  applyState(ctx);
}

function shownId(ctx) {
  return ctx.hover || ctx.selected;
}

function endpointMatches(id, pred) {
  if (GROUPS[id]) return NODES.some((n) => n.group === id && pred(n));
  return pred(NODE.get(id));
}

function applyState(ctx) {
  const shown = shownId(ctx);
  const svg = ctx.canvas.querySelector('svg');
  if (!svg) return;
  svg.classList.toggle('has-filter', !!ctx.filter);
  svg.style.setProperty('--f', ctx.filter ? familyVar(SERVICE.get(ctx.filter).family) : null);
  const uses = (n) => n.tech.includes(ctx.filter);
  for (const [id, g] of ctx.nodeEls) {
    const n = NODE.get(id);
    g.classList.toggle('is-selected', id === ctx.selected);
    g.classList.toggle('is-shown', id === shown);
    g.classList.toggle('is-match', !!ctx.filter && uses(n));
    g.setAttribute('aria-pressed', String(id === ctx.selected));
  }
  const touches = (end) => end === shown || (!!GROUPS[end] && NODE.get(shown)?.group === end);
  for (const { e, el } of ctx.edgeEls) {
    el.classList.toggle('is-active', touches(e.from) || touches(e.to));
    el.classList.toggle('is-match', !!ctx.filter && endpointMatches(e.from, uses) && endpointMatches(e.to, uses));
  }
  for (const g of svg.querySelectorAll('.arch-group')) {
    const id = g.dataset.group;
    g.classList.toggle('is-match', !!ctx.filter && NODES.some((n) => n.group === id && uses(n)));
  }
}

function renderDetail(ctx) {
  const n = NODE.get(shownId(ctx));
  if (!n) return;
  const lane = n.lane === 'bridge' ? 'Shared by both pipelines' : `${LANES[n.lane].label} · ${LANES[n.lane].sub}`;
  const chips = n.tech.length
    ? h('ul', { class: 'arch-chips', 'aria-label': 'Google technology' }, n.tech.map((t) => {
      const sv = SERVICE.get(t);
      return h('li', { class: 'arch-chip', style: `--c: ${familyVar(sv.family)}` }, h('span', { class: 'arch-dot' }), sv.name);
    }))
    : h('p', { class: 'arch-detail__muted', text: 'Runs in the viewer’s browser.' });
  const [ins, outs] = n.io;
  const tour = ctx.tour ? TOURS[ctx.tour.key] : null;
  ctx.detailBody.replaceChildren(...[
    h('p', { class: 'arch-eyebrow', text: tour ? `${tour.label} · step ${ctx.tour.i + 1} of ${tour.steps.length}` : lane }),
    h('h4', { class: `arch-detail__title${n.kind === 'tool' ? ' is-mono' : ''}`, text: n.title }),
    h('p', { class: 'arch-detail__sub', text: n.sub }),
    chips,
    h('p', { class: 'arch-detail__does', text: n.does }),
    n.safety ? h('p', { class: 'arch-callout' }, h('strong', { text: 'Spoiler safety. ' }), n.safety) : null,
    h('div', { class: 'arch-io' },
      h('div', {}, h('h5', { text: 'In' }), h('ul', {}, ins.map((t) => h('li', { text: t })))),
      h('span', { class: 'arch-io__arrow', 'aria-hidden': 'true', text: '→' }),
      h('div', {}, h('h5', { text: 'Out' }), h('ul', {}, outs.map((t) => h('li', { text: t }))))),
    n.papers ? h('div', { class: 'arch-detail__refs' }, h('h5', { text: 'Research' }),
      h('ul', {}, n.papers.map((p) => h('li', { text: PAPERS[p] })))) : null,
    n.code ? h('div', { class: 'arch-detail__refs' }, h('h5', { text: 'In the repository' }),
      h('ul', { class: 'arch-code' }, n.code.map((c) =>
        h('li', {}, h('a', { href: REPO + c, target: '_blank', rel: 'noopener', text: c }))))) : null,
  ].filter(Boolean));
}

function openSheet(ctx) {
  ctx.sheetOpen = true;
  syncSheet(ctx);
}

function closeSheet(ctx) {
  if (!ctx.sheetOpen) return;
  ctx.sheetOpen = false;
  syncSheet(ctx);
  ctx.nodeEls.get(ctx.selected)?.focus({ preventScroll: true });
}

function syncSheet(ctx) {
  ctx.detail.dataset.open = String(ctx.panel === 'side' || ctx.sheetOpen);
  ctx.root.classList.toggle('has-sheet', ctx.panel === 'sheet' && ctx.sheetOpen);
}

function setFilter(ctx, id) {
  ctx.filter = ctx.filter === id ? null : id;
  for (const b of ctx.root.querySelectorAll('[data-service]')) {
    b.setAttribute('aria-pressed', String(b.dataset.service === ctx.filter));
  }
  if (ctx.filter) {
    const sv = SERVICE.get(ctx.filter);
    const count = NODES.filter((n) => n.tech.includes(ctx.filter)).length;
    ctx.filterBar.querySelector('.arch-filterbar__label').replaceChildren(
      h('span', { class: 'arch-dot', style: `--c: ${familyVar(sv.family)}` }),
      `Highlighting ${count} step${count === 1 ? '' : 's'} that use `, h('strong', { text: sv.name }));
    ctx.filterBar.hidden = false;
    const first = NODES.find((n) => n.tech.includes(ctx.filter));
    revealNode(ctx, first?.id);
  } else {
    ctx.filterBar.hidden = true;
  }
  applyState(ctx);
}

function revealNode(ctx, id) {
  const target = id ? ctx.nodeEls.get(id) : null;
  if (!target) return;
  const r = target.getBoundingClientRect();
  if (r.top >= 70 && r.bottom <= innerHeight - 20) return;
  target.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
}

function startTour(ctx, key) {
  ctx.tour = { key, i: 0 };
  ctx.tourBar.hidden = false;
  showTourStep(ctx);
}

function stepTour(ctx, delta) {
  if (!ctx.tour) return;
  const steps = TOURS[ctx.tour.key].steps;
  ctx.tour.i = Math.min(steps.length - 1, Math.max(0, ctx.tour.i + delta));
  showTourStep(ctx);
}

function showTourStep(ctx) {
  const { key, i } = ctx.tour;
  const steps = TOURS[key].steps;
  ctx.tourBar.querySelector('.arch-tourbar__label').textContent = `${TOURS[key].label}: step ${i + 1} of ${steps.length}`;
  const [back, next] = ctx.tourBar.querySelectorAll('button');
  back.disabled = i === 0;
  next.disabled = i === steps.length - 1;
  ctx.selected = steps[i];
  ctx.hover = null;
  if (ctx.panel === 'sheet') openSheet(ctx);
  renderDetail(ctx);
  applyState(ctx);
  if (ctx.panel === 'side') revealNode(ctx, steps[i]);
}

function endTour(ctx, { keepSelection = false } = {}) {
  ctx.tour = null;
  ctx.tourBar.hidden = true;
  if (!keepSelection) renderDetail(ctx);
}

// ── Google Cloud technology map ─────────────────────────────────────

function techMap(ctx) {
  const groups = [...new Set(SERVICES.map((sv) => sv.group))];
  return h('section', { class: 'arch-section', 'aria-labelledby': 'arch-tech-h' },
    h('div', { class: 'arch-section__head' },
      h('h3', { id: 'arch-tech-h', text: 'Google Cloud technology map' }),
      h('p', { text: 'Twelve Google services, each with one job. Select a service to highlight every step that uses it.' })),
    h('div', { class: 'arch-techmap' }, groups.map((gname) =>
      h('div', { class: 'arch-techmap__group' },
        h('h4', { class: 'arch-techmap__title', text: gname }),
        h('ul', {}, SERVICES.filter((sv) => sv.group === gname).map((sv) => {
          const count = NODES.filter((n) => n.tech.includes(sv.id)).length;
          return h('li', {}, h('button', {
            type: 'button', class: 'arch-service', 'data-service': sv.id, 'aria-pressed': 'false',
            style: `--c: ${familyVar(sv.family)}`, onclick: () => setFilter(ctx, sv.id),
          },
          h('span', { class: 'arch-service__name' }, h('span', { class: 'arch-dot' }), sv.name),
          h('span', { class: 'arch-service__role', text: sv.role }),
          h('span', { class: 'arch-service__count', text: `${count} step${count === 1 ? '' : 's'}` })));
        }))))));
}

// ── spoiler gate demo ────────────────────────────────────────────────

function gateDemo(ctx) {
  const st = {
    titles: [], title: null, p: 0, live: false, models: new Map(), cache: new Map(),
    cells: new Map(), counts: new Map(), timer: 0, abort: null,
  };
  const picker = h('div', { class: 'arch-seg', role: 'radiogroup', 'aria-label': 'Film' });
  const slider = h('input', { type: 'range', class: 'arch-range', min: 0, max: 100, step: 1, value: 0, id: 'arch-p',
    'aria-describedby': 'arch-p-help' });
  const readout = h('output', { class: 'arch-readout', for: 'arch-p' });
  const marks = h('div', { class: 'arch-range__marks', 'aria-hidden': 'true' });
  const visible = h('strong', { class: 'arch-stat__value is-ok' });
  const locked = h('strong', { class: 'arch-stat__value is-locked' });
  const chars = h('strong', { class: 'arch-stat__value' });
  const phase = h('p', { class: 'arch-gate__phase', role: 'status' });
  const wall = h('div', { class: 'arch-wall' });
  const source = h('p', { class: 'arch-gate__source' });

  const sec = h('section', { class: 'arch-section arch-gate', 'aria-labelledby': 'arch-gate-h' },
    h('div', { class: 'arch-section__head' },
      h('h3', { id: 'arch-gate-h', text: 'Try the gate' }),
      h('p', { text: 'Drag the playback position. Each item in the knowledge pack unlocks only once its reveal time, plus a '
        + 'small safety margin, is behind you. Locked items are not hidden from the answer; they never reach the agent at all.' })),
    h('div', { class: 'arch-gate__panel' },
      h('div', { class: 'arch-gate__controls' },
        picker,
        h('div', { class: 'arch-gate__slider' },
          h('label', { for: 'arch-p', class: 'arch-gate__label' }, 'Paused at ', readout),
          h('div', { class: 'arch-range__wrap' }, slider, marks),
          h('p', { id: 'arch-p-help', class: 'arch-gate__help', text: 'Use the arrow keys for one-second steps.' }))),
      h('div', { class: 'arch-gate__stats' },
        h('div', { class: 'arch-stat' }, visible, h('span', { text: 'items the agent can read' })),
        h('div', { class: 'arch-stat' }, locked, h('span', { text: 'items locked in the future' })),
        h('div', { class: 'arch-stat' }, chars, h('span', { text: 'characters introduced so far' }))),
      phase, wall, source));

  const render = (g) => {
    const elig = GATE_TYPES.reduce((a, [k]) => a + (g.eligible[k] || 0), 0);
    const total = GATE_TYPES.reduce((a, [k]) => a + (g.total[k] || 0), 0);
    visible.textContent = String(elig);
    locked.textContent = String(total - elig);
    chars.textContent = `${g.characters_known} / ${g.characters_total}`;
    for (const [k] of GATE_TYPES) {
      const row = st.cells.get(k);
      if (!row) continue;
      const n = g.eligible[k] || 0, tot = g.total[k] || 0;
      if (row.cells.length !== tot) buildRow(k, tot);
      const r = st.cells.get(k);
      const prev = st.counts.get(k) ?? 0;
      r.cells.forEach((c, i) => {
        const on = i < n;
        c.classList.toggle('is-on', on);
        c.classList.toggle('is-new', on && i >= prev && prev !== -1);
      });
      st.counts.set(k, n);
      r.num.textContent = `${n} / ${tot}`;
      r.strip.setAttribute('aria-label', `${n} of ${tot} ${r.label.toLowerCase()} unlocked`);
    }
    const t = st.title;
    if (t.story_start_s != null && st.p < t.story_start_s) phase.textContent = 'Opening titles: the story has not started, so only the title can be discussed.';
    else if (t.story_end_s != null && st.p >= t.story_end_s) phase.textContent = 'End credits: “what did I miss?” stops at the last story moment, and the credits frame is not inspected.';
    else phase.textContent = '';
  };

  const buildRow = (k, tot) => {
    const row = st.cells.get(k);
    const cells = Array.from({ length: tot }, () => h('span', { class: 'arch-cell' }));
    row.strip.replaceChildren(...cells);
    row.cells = cells;
    st.counts.set(k, -1);
  };

  const buildWall = () => {
    st.cells.clear();
    st.counts.clear();
    wall.replaceChildren(...GATE_TYPES.map(([k, label]) => {
      const num = h('span', { class: 'arch-wall__num' });
      const strip = h('div', { class: 'arch-wall__cells', role: 'img' });
      st.cells.set(k, { label, num, strip, cells: [] });
      return h('div', { class: 'arch-wall__row' },
        h('div', { class: 'arch-wall__meta' }, h('span', { text: label }), num), strip);
    }));
  };

  const setSource = () => {
    source.replaceChildren(st.live
      ? h('span', {}, h('span', { class: 'arch-dot is-ok' }),
        'Live counts from this film’s knowledge pack. The endpoint returns numbers only; no content leaves the server.')
      : h('span', {}, h('span', { class: 'arch-dot is-locked' }),
        'Illustrative data: the live service is not reachable from this page, so these counts are simulated.'));
  };

  const query = () => {
    const t = st.title, p = st.p;
    if (!st.live) { render(synthGate(st, t, p)); return; }
    const key = `${t.id}:${p}`;
    if (st.cache.has(key)) { render(st.cache.get(key)); return; }
    clearTimeout(st.timer);
    st.timer = setTimeout(async () => {
      st.abort?.abort();
      st.abort = new AbortController();
      try {
        const g = await fetchJSON(`/api/titles/${encodeURIComponent(t.id)}/gate?p=${p}`, st.abort.signal);
        st.cache.set(key, g);
        if (st.title === t && st.p === p) render(g);
      } catch (err) {
        if (err.name === 'AbortError') return;
        st.live = false;
        setSource();
        render(synthGate(st, t, p));
      }
    }, 80);
  };

  const pick = (t) => {
    st.title = t;
    for (const b of picker.children) {
      const on = b.dataset.id === t.id;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    const dur = Math.floor(t.duration_s);
    slider.max = String(dur);
    st.p = Math.round((t.story_start_s ?? 0) + 0.45 * ((t.story_end_s ?? dur) - (t.story_start_s ?? 0)));
    slider.value = String(st.p);
    const mark = (at, text) => {
      const pct = (100 * at) / dur;
      const shift = pct < 10 ? '-6px' : pct > 90 ? 'calc(-100% + 6px)' : '-50%';
      return h('span', { style: `left: ${pct}%; --shift: ${shift}`, text });
    };
    marks.replaceChildren(
      t.story_start_s != null ? mark(t.story_start_s, 'story starts') : null,
      t.story_end_s != null ? mark(t.story_end_s, 'credits') : null);
    buildWall();
    update();
  };

  const update = () => {
    const dur = Math.floor(st.title.duration_s);
    readout.textContent = mmss(st.p);
    slider.setAttribute('aria-valuetext', `${mmss(st.p)} of ${mmss(dur)}`);
    slider.style.setProperty('--fill', `${(100 * st.p) / Math.max(1, dur)}%`);
    query();
  };

  slider.addEventListener('input', () => { st.p = Number(slider.value); update(); });

  const buildPicker = () => {
    picker.replaceChildren(...st.titles.map((t, i) => h('button', {
      type: 'button', role: 'radio', class: 'arch-seg__btn', 'data-id': t.id, 'aria-checked': String(i === 0),
      onclick: () => pick(t),
      onkeydown: (e) => {
        const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (!d) return;
        e.preventDefault();
        const j = (st.titles.indexOf(t) + d + st.titles.length) % st.titles.length;
        pick(st.titles[j]);
        picker.children[j].focus();
      },
    }, t.title)));
  };

  // Prefer live data; fall back to simulated counts when the API is not there (e.g. the standalone preview).
  (async () => {
    try {
      const titles = await fetchJSON('/api/titles', undefined, 4000);
      if (!Array.isArray(titles) || !titles.length) throw new Error('no titles');
      const rank = (t) => { const i = FALLBACK_TITLES.findIndex((f) => f.title === t.title); return i < 0 ? 99 : i; };
      st.titles = titles.slice().sort((a, b) => rank(a) - rank(b));
      st.live = true;
    } catch {
      st.titles = FALLBACK_TITLES.map((t) => ({ ...t, story_start_s: 0.03 * t.duration_s, story_end_s: 0.9 * t.duration_s }));
      st.live = false;
    }
    setSource();
    buildPicker();
    pick(st.titles[0]);
  })();

  ctx.cleanups.push(() => { clearTimeout(st.timer); st.abort?.abort(); });
  return sec;
}

async function fetchJSON(url, signal, timeoutMs = 6000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  signal?.addEventListener('abort', () => ac.abort(), { once: true });
  try {
    const res = await fetch(url, { signal: ac.signal, headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    if (signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// Simulated reveal times for the offline preview: deterministic per title, no film content.
function synthGate(st, t, p) {
  if (!st.models.has(t.id)) {
    let seed = [...t.title].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
    const rnd = () => {
      seed = (seed + 0x6d2b79f5) >>> 0;
      let x = seed;
      x = Math.imul(x ^ (x >>> 15), x | 1);
      x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
    const d = t.duration_s, a = t.story_start_s, b = t.story_end_s;
    const per = { scene: d / 75, event: d / 26, cue: d / 8, shot_caption: d / 5, recap: d / 85, character_fact: d / 40, object: d / 70 };
    const margin = { scene: 3, event: 3, cue: 1, shot_caption: 3, recap: 3, character_fact: 1, object: 2 };
    const times = {};
    for (const [k] of GATE_TYPES) {
      times[k] = Array.from({ length: Math.max(1, Math.round(per[k])) }, () => a + rnd() * (b - a) + margin[k]).sort((x, y) => x - y);
    }
    const chars = Array.from({ length: 7 }, () => a + rnd() ** 1.6 * (b - a)).sort((x, y) => x - y);
    st.models.set(t.id, { times, chars });
  }
  const m = st.models.get(t.id);
  const eligible = {}, total = {};
  for (const [k] of GATE_TYPES) {
    total[k] = m.times[k].length;
    eligible[k] = m.times[k].filter((x) => x <= p).length;
  }
  return { p, eligible, total, characters_known: m.chars.filter((x) => x + 2 <= p).length, characters_total: m.chars.length };
}

// ── why structural ───────────────────────────────────────────────────

function structural() {
  const flow = (items, bad) => h('ol', { class: `arch-mini${bad ? ' is-bad' : ''}` },
    items.map(([t, tone]) => h('li', { class: tone ? `is-${tone}` : null, text: t })));
  return h('section', { class: 'arch-section', 'aria-labelledby': 'arch-why-h' },
    h('div', { class: 'arch-section__head' },
      h('h3', { id: 'arch-why-h', text: 'Why the gate is structural' }),
      h('p', { text: 'The order of two operations decides whether a bug is an annoyance or a spoiler.' })),
    h('div', { class: 'arch-compare' },
      h('article', { class: 'arch-card arch-card--bad' },
        h('p', { class: 'arch-eyebrow', text: 'Typical RAG' }),
        h('h4', { text: 'Rank the whole film, then filter' }),
        flow([['Whole film'], ['Rank by similarity'], ['Top results, future included', 'locked'], ['Metadata filter'], ['Model']], true),
        h('p', { text: 'Future scenes are in the candidate list. If the filter is misconfigured or silently skipped, they go straight into the prompt, and the answer still looks fine.' })),
      h('article', { class: 'arch-card arch-card--good' },
        h('p', { class: 'arch-eyebrow', text: 'Hello Watch Assistant' }),
        h('h4', { text: 'Gate at P, then rank inside the gate' }),
        flow([['Whole film'], ['Spoiler gate at P', 'ok'], ['Eligible set only', 'ok'], ['Rank by similarity'], ['Model']]),
        h('p', { text: 'Ranking runs on a set that contains nothing after P. There is no future item in memory to leak, so a ranking bug can only make an answer worse — never a spoiler. 0 violations in 120 checked results.' }))),
    h('h4', { class: 'arch-subhead', text: 'Rejected on purpose' }),
    h('ul', { class: 'arch-rejected' }, REJECTED.map((r) =>
      h('li', { class: 'arch-card' }, h('strong', { text: r.title }), h('p', { text: r.why })))));
}

// ── results ──────────────────────────────────────────────────────────

function results() {
  return h('section', { class: 'arch-section', 'aria-labelledby': 'arch-results-h' },
    h('div', { class: 'arch-section__head' },
      h('h3', { id: 'arch-results-h', text: 'Measured on the demo films' }),
      h('p', { text: 'Three open Blender films processed end to end. Spot checks on Sintel unless noted.' })),
    h('ul', { class: 'arch-results' }, RESULTS.map((r) =>
      h('li', { class: 'arch-result' },
        h('span', { class: 'arch-result__value' }, r.value, r.unit ? h('small', { text: ` ${r.unit}` }) : null),
        h('span', { class: 'arch-result__label', text: r.label })))));
}

// ── research ─────────────────────────────────────────────────────────

function research(ctx) {
  return h('section', { class: 'arch-section', 'aria-labelledby': 'arch-research-h' },
    h('div', { class: 'arch-section__head' },
      h('h3', { id: 'arch-research-h', text: 'Research it builds on' }),
      h('p', { text: 'Training-free techniques from recent movie-understanding work, applied with Gemini. Select one to see where it is used.' })),
    h('ul', { class: 'arch-papers' }, Object.entries(PAPERS).map(([key, title]) => {
      const used = NODES.filter((n) => n.papers?.includes(key));
      return h('li', { class: 'arch-card' },
        h('p', { class: 'arch-paper__title', text: title }),
        h('p', { class: 'arch-paper__used' }, 'Used in ', used.map((n, i) => [
          i ? ', ' : null,
          h('button', { type: 'button', class: 'arch-link', onclick: () => {
            select(ctx, n.id, { user: true });
            revealNode(ctx, n.id);
          }, text: n.title }),
        ])));
    })));
}
