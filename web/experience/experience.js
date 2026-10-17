// Tab 1, "Live demo": film picker, player, and the spoiler-safe ask panel.
// Talks to the service through the contract in docs/web-api.md. Add ?mock=1 to run without a back end.

const params = new URLSearchParams(location.search);
const MOCK = params.has('mock') && params.get('mock') !== '0';
const LANG_KEY = 'hwa-lang';

const ICONS = {
  shield: '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z"/><path d="m8.8 12 2.3 2.3 4.2-4.6"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  book: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 1.5V4M12 20v2.5M1.5 12H4M20 12h2.5"/>',
  quote: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8.5 9.5h7M8.5 12.5h4.5"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>',
  spark: '<path d="M12 3c.8 4.6 3.4 7.2 8 8-4.6.8-7.2 3.4-8 8-.8-4.6-3.4-7.2-8-8 4.6-.8 7.2-3.4 8-8z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  send: '<path d="M4 12 20 4l-5 16-3.5-6.5z"/><path d="m11.5 13.5 4-4"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  rewind: '<path d="M11 6 4 12l7 6zM20 6l-7 6 7 6z"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  tap: '<circle cx="12" cy="12" r="3.5"/><circle cx="12" cy="12" r="8.5" opacity=".5"/>',
};

function icon(name, cls = '') {
  return `<svg class="xp-icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.spark}</svg>`;
}

/** Tiny DOM builder: h('div', {class: 'x', onclick: fn}, child, 'text'). */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

function fmt(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  const hh = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return hh ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`;
}

function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const TYPE_LABELS = {
  scene: 'Scene', event: 'Event', recap: 'Recap', shot_caption: 'Shot', character_fact: 'Character',
  object: 'Object', cue: 'Dialogue', video_window: 'Clip', look: 'Look', character: 'Character',
  shot: 'Shot', line: 'Dialogue',
};
const GATE_ROWS = [
  ['scene', 'Scenes'], ['event', 'Events'], ['cue', 'Lines of dialogue'], ['shot_caption', 'Shots described'],
  ['character_fact', 'Character facts'], ['object', 'Objects'], ['recap', 'Recaps'],
];

function toolIcon(name = '') {
  const n = name.toLowerCase();
  if (/recap|story|so_far|summary/.test(n)) return 'book';
  if (/shot|frame|tap|look|screen|bbox|who/.test(n)) return 'target';
  if (/dialog|cue|said|line|transcript|speech/.test(n)) return 'quote';
  if (/window|clip|video|watch/.test(n)) return 'film';
  if (/character|person|cast/.test(n)) return 'user';
  if (/search|find|retriev|lookup|query/.test(n)) return 'search';
  return 'spark';
}

const QUICK = [
  { key: 'miss', label: 'What did I miss?', question: 'What did I miss?', icon: 'book' },
  { key: 'who', label: 'Who is this?', question: 'Who is this?', icon: 'user', pick: 'Tap the person you mean' },
  { key: 'what', label: 'What is that?', question: 'What is that?', icon: 'target', pick: 'Tap the thing you mean' },
  { key: 'say', label: 'What did they just say?', question: 'What did they just say?', icon: 'quote' },
];

function loadStyles() {
  const href = new URL('./experience.css', import.meta.url).href;
  if (document.querySelector(`link[href="${href}"]`)) return Promise.resolve();
  return new Promise((resolve) => {
    const link = h('link', { rel: 'stylesheet', href });
    link.addEventListener('load', resolve);
    link.addEventListener('error', resolve);
    document.head.append(link);
  });
}

export async function mount(el) {
  const [, mock] = await Promise.all([loadStyles(), MOCK ? import('./mock.js') : null]);
  const app = new Experience(el, mock ? mock.mockFetch : (...a) => fetch(...a));
  app.render();
  await app.loadTitles();
}

class Experience {
  constructor(root, fetchImpl) {
    this.root = root;
    this.fetch = fetchImpl;
    this.titles = [];
    this.title = null;
    this.sessions = new Map();
    this.lang = 'en';
    try { this.lang = localStorage.getItem(LANG_KEY) === 'zh' ? 'zh' : 'en'; } catch { /* storage unavailable */ }
    this.busy = false;
    this.abort = null;
    this.pick = null;           // {question, x, y} while choosing a point on the frame
    this.lastAskP = null;
    this.gateTimer = 0;
    this.gateSeq = 0;
    this.overlayP = null;       // P at which the ring / highlight were drawn
  }

  // ---------- layout ----------

  render() {
    const r = this.root;
    r.classList.add('xp');
    r.innerHTML = '';

    const intro = h('div', { class: 'xp-intro' },
      h('div', { class: 'xp-intro__text' },
        h('p', { class: 'xp-eyebrow' }, MOCK ? 'Live demo · mock data' : 'Live demo'),
        h('h1', { class: 'xp-title' }, 'Pause anywhere. Ask anything.', h('span', {}, ' Get no spoilers.')),
        h('p', { class: 'xp-lede' },
          'The assistant only knows what the film has shown up to the moment you pause. ',
          'Everything later stays locked on the server, so it cannot leak into an answer.'),
      ),
      h('ol', { class: 'xp-howto', 'aria-label': 'How to try it' },
        h('li', {}, h('b', {}, '1'), 'Pick a film and press play'),
        h('li', {}, h('b', {}, '2'), 'Pause at any moment'),
        h('li', {}, h('b', {}, '3'), 'Ask, or tap someone on screen'),
      ),
    );

    this.cards = h('div', { class: 'xp-cards', role: 'list' },
      [0, 1, 2].map(() => h('div', { class: 'xp-card xp-card--skeleton', role: 'listitem', 'aria-hidden': 'true' },
        h('div', { class: 'xp-card__poster' }), h('div', { class: 'xp-card__body' }, h('span'), h('span')))));
    const picker = h('section', { class: 'xp-picker', 'aria-labelledby': 'xp-picker-h' },
      h('h2', { id: 'xp-picker-h', class: 'xp-section-h' }, 'Choose a film'),
      this.cards);

    // Player
    this.video = h('video', {
      class: 'xp-video', controls: true, playsinline: true, preload: 'metadata',
      'aria-label': 'Film player',
    });
    this.content = h('div', { class: 'xp-content', 'aria-hidden': 'true' });
    this.ring = h('div', { class: 'xp-ring', hidden: true });
    this.hl = h('div', { class: 'xp-hl', hidden: true }, h('span', { class: 'xp-hl__label' }));
    this.cross = h('div', { class: 'xp-cross' });
    this.content.append(this.hl, this.ring, this.cross);

    this.pickLayer = h('div', {
      class: 'xp-picklayer', tabindex: '-1', role: 'application',
      'aria-label': 'Choose a point on the frame. Use the arrow keys to move and Enter to ask. Escape cancels.',
      onpointerdown: (e) => this.onPickPointer(e),
      onpointermove: (e) => this.onPickHover(e),
      onkeydown: (e) => this.onPickKey(e),
    });
    this.pickBar = h('div', { class: 'xp-pickbar', role: 'status' },
      h('span', { class: 'xp-pickbar__msg' }),
      h('span', { class: 'xp-pickbar__nudge' }, 'Tap inside the picture'),
      h('span', { class: 'xp-pickbar__actions' },
        h('button', { type: 'button', class: 'xp-btn xp-btn--ghost', onclick: () => this.confirmPick(null) },
          'Ask about the whole frame'),
        h('button', { type: 'button', class: 'xp-btn xp-btn--ghost', onclick: () => this.cancelPick() },
          'Cancel')));
    this.pausedPill = h('button', {
      type: 'button', class: 'xp-pill', hidden: true,
      onclick: () => this.input.focus(),
    });
    this.player = h('div', { class: 'xp-player' },
      this.video, this.content, this.pickLayer, this.pickBar, this.pausedPill);

    this.nowTitle = h('span', { class: 'xp-now__title' });
    this.nowTime = h('span', { class: 'xp-now__time' });
    const now = h('div', { class: 'xp-now' }, this.nowTitle, this.nowTime);

    // Spoiler shield
    this.shieldStat = h('span', { class: 'xp-shield__stat' }, 'Pause to see what is unlocked');
    this.shieldChars = h('span', { class: 'xp-shield__chars' });
    this.trackSeen = h('div', { class: 'xp-track__seen' });
    this.trackMark = h('div', { class: 'xp-track__mark' }, h('span'));
    this.gateRows = h('div', { class: 'xp-gate-rows' });
    this.shield = h('section', { class: 'xp-shield', 'aria-labelledby': 'xp-shield-h' },
      h('div', { class: 'xp-shield__head' },
        h('span', { class: 'xp-shield__badge', html: icon('shield') }),
        h('h2', { id: 'xp-shield-h', class: 'xp-shield__title' }, 'Spoiler shield'),
        this.shieldStat),
      h('div', { class: 'xp-track', role: 'img', 'aria-label': 'Film timeline: watched part unlocked, the rest locked' },
        this.trackSeen, h('div', { class: 'xp-track__lock', html: icon('lock') }), this.trackMark),
      h('div', { class: 'xp-track__legend' },
        h('span', {}, h('i', { class: 'xp-dot xp-dot--ok' }), 'Unlocked: already on screen'),
        this.shieldChars,
        h('span', {}, h('i', { class: 'xp-dot xp-dot--locked' }), 'Locked: never sent to the model')),
      h('details', { class: 'xp-gate-details' },
        h('summary', {}, 'What the gate lets through right now'),
        h('p', { class: 'xp-muted xp-small' },
          'Counts only. The browser never receives locked content; the gate runs on the server before any search.'),
        this.gateRows));

    // Ask panel
    this.langBtns = ['en', 'zh'].map((code) => h('button', {
      type: 'button', class: 'xp-seg__btn', 'aria-pressed': String(this.lang === code), 'data-lang': code,
      'aria-label': code === 'en' ? 'Answer in English' : 'Answer in Chinese',
      title: code === 'en' ? 'Answer in English' : 'Answer in Chinese',
      onclick: () => this.setLang(code),
    }, code.toUpperCase()));
    this.lockChip = h('span', { class: 'xp-lock', title: 'Questions are answered as of this moment' });
    this.thread = h('div', { class: 'xp-thread', role: 'log', 'aria-label': 'Questions and answers' });
    this.live = h('div', { class: 'visually-hidden', 'aria-live': 'polite' });
    this.chips = QUICK.map((q) => h('button', {
      type: 'button', class: 'xp-chip', 'data-key': q.key, onclick: () => this.onQuick(q),
      html: `${icon(q.icon)}<span>${q.label}</span>${q.pick ? icon('tap', 'xp-chip__tap') : ''}`,
    }));
    this.input = h('input', {
      class: 'xp-input', type: 'text', name: 'question', maxlength: '300', autocomplete: 'off',
      placeholder: 'Ask about this moment…', 'aria-label': 'Your question',
    });
    this.sendBtn = h('button', { type: 'submit', class: 'xp-send', 'aria-label': 'Ask', html: icon('send') });
    this.form = h('form', { class: 'xp-form', onsubmit: (e) => this.onSubmit(e) }, this.input, this.sendBtn);
    this.panel = h('aside', { class: 'xp-panel', 'aria-labelledby': 'xp-panel-h' },
      h('div', { class: 'xp-panel__inner' },
        h('div', { class: 'xp-panel__head' },
          h('div', {},
            h('h2', { id: 'xp-panel-h', class: 'xp-panel__title' }, 'Ask the assistant'),
            this.lockChip),
          h('div', { class: 'xp-seg', role: 'group', 'aria-label': 'Answer language' }, this.langBtns)),
        this.thread,
        h('div', { class: 'xp-composer' },
          h('div', { class: 'xp-chips', role: 'group', 'aria-label': 'Quick questions' }, this.chips),
          this.form),
        this.live));

    this.stage = h('section', { class: 'xp-stage', 'aria-label': 'Player and assistant' },
      h('div', { class: 'xp-main' }, this.player, now, this.shield),
      this.panel);

    r.append(intro, picker, this.stage);
    r.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.pick) { this.cancelPick(); this.input.focus(); }
    });
    this.renderEmptyThread();
    this.bindVideo();
    this.updateLock();
  }

  renderEmptyThread() {
    this.thread.innerHTML = '';
    this.thread.append(h('div', { class: 'xp-empty' },
      h('div', { class: 'xp-empty__icon', html: icon('pause') }),
      h('p', { class: 'xp-empty__title' }, 'Pause the film, then ask'),
      h('p', { class: 'xp-muted' },
        'Try ', h('b', {}, 'What did I miss?'), ' a few minutes in, or ', h('b', {}, 'Who is this?'),
        ' and tap a face on screen. Answers only use what you have already seen.')));
  }

  // ---------- titles ----------

  async loadTitles() {
    try {
      const res = await this.fetch('/api/titles');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.titles = await res.json();
      if (!Array.isArray(this.titles) || !this.titles.length) throw new Error('no titles');
    } catch (err) {
      console.warn('Could not load titles', err);
      this.cards.innerHTML = '';
      this.cards.append(h('div', { class: 'xp-notice', role: 'alert' },
        h('span', { html: icon('alert') }),
        h('div', {},
          h('strong', {}, 'The film list did not load.'),
          h('p', {}, 'The service may be waking up. Give it a few seconds and try again.'),
          h('button', { type: 'button', class: 'xp-btn', onclick: () => this.loadTitles() }, 'Try again'))));
      return;
    }
    this.renderCards();
    const wanted = params.get('title');
    this.selectTitle(this.titles.find((t) => t.id === wanted || t.title === wanted) || this.titles[0], false);
  }

  renderCards() {
    this.cards.innerHTML = '';
    for (const t of this.titles) {
      const img = h('img', {
        src: `/api/titles/${encodeURIComponent(t.id)}/frames/${Math.floor(t.poster_t ?? 60)}.jpg`,
        alt: '', loading: 'lazy', decoding: 'async',
      });
      img.addEventListener('error', () => img.remove(), { once: true });
      const card = h('button', {
        type: 'button', class: 'xp-card', 'data-id': t.id, 'aria-pressed': 'false',
        onclick: () => this.selectTitle(t, true),
      },
        h('div', { class: 'xp-card__poster' }, img,
          h('span', { class: 'xp-card__dur' }, fmt(t.duration_s)),
          h('span', { class: 'xp-card__now', html: `${icon('film')}Now playing` })),
        h('div', { class: 'xp-card__body' },
          h('span', { class: 'xp-card__title' }, t.title),
          h('span', { class: 'xp-card__syn' }, t.synopsis || ''),
          h('span', { class: 'xp-card__lic' }, (t.license || '').replace(/-/g, ' '))));
      this.cards.append(h('div', { role: 'listitem' }, card));
    }
  }

  selectTitle(t, user) {
    if (!t) return;
    if (this.title && this.title.id === t.id) {
      if (user) this.player.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    this.stopStream();
    this.cancelPick();
    this.title = t;
    if (!this.sessions.has(t.id)) this.sessions.set(t.id, newId());
    this.lastAskP = null;
    for (const c of this.cards.querySelectorAll('.xp-card')) c.setAttribute('aria-pressed', String(c.dataset.id === t.id));
    this.video.src = `/api/titles/${encodeURIComponent(t.id)}/video`;
    this.video.poster = `/api/titles/${encodeURIComponent(t.id)}/frames/${Math.floor(t.poster_t ?? 60)}.jpg`;
    this.nowTitle.textContent = t.title;
    this.clearOverlay();
    this.renderEmptyThread();
    this.updateTime();
    this.updatePausedPill();
    this.refreshGate(0, true);
    if (user) this.player.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---------- video ----------

  bindVideo() {
    const v = this.video;
    v.addEventListener('loadedmetadata', () => { this.layoutContent(); this.updateTime(); });
    v.addEventListener('timeupdate', () => this.updateTime());
    v.addEventListener('play', () => {
      this.cancelPick();
      this.clearOverlay();
      this.updatePausedPill();
    });
    v.addEventListener('pause', () => {
      this.updatePausedPill();
      this.refreshGate(v.currentTime);
    });
    v.addEventListener('seeking', () => {
      if (this.overlayP != null && Math.abs(v.currentTime - this.overlayP) > 0.5) this.clearOverlay();
    });
    v.addEventListener('seeked', () => {
      this.updateTime();
      this.updatePausedPill();
      if (v.paused) this.refreshGate(v.currentTime);
    });
    v.addEventListener('ended', () => this.updatePausedPill());
    v.addEventListener('error', () => {
      if (!this.title) return;
      this.pausedPill.hidden = false;
      this.pausedPill.className = 'xp-pill xp-pill--warn';
      this.pausedPill.innerHTML = `${icon('alert')}<span>The video could not be loaded. Try another film or reload.</span>`;
    });
    new ResizeObserver(() => this.layoutContent()).observe(v);
  }

  /** Positions the overlay box on the visible picture (object-fit: contain leaves letterbox bars). */
  layoutContent() {
    const v = this.video;
    const w = v.clientWidth;
    const hh = v.clientHeight;
    const vw = v.videoWidth || 16;
    const vh = v.videoHeight || 9;
    const scale = Math.min(w / vw, hh / vh);
    const cw = vw * scale;
    const ch = vh * scale;
    Object.assign(this.content.style, {
      left: `${v.offsetLeft + (w - cw) / 2}px`, top: `${v.offsetTop + (hh - ch) / 2}px`,
      width: `${cw}px`, height: `${ch}px`,
    });
  }

  contentRect() { return this.content.getBoundingClientRect(); }

  updateTime() {
    const t = this.title;
    if (!t) return;
    const cur = this.video.currentTime || 0;
    const dur = this.video.duration || t.duration_s || 1;
    this.nowTime.textContent = `${fmt(cur)} / ${fmt(dur)}`;
    const pct = Math.min(100, (cur / dur) * 100);
    this.trackSeen.style.width = `${pct}%`;
    this.trackMark.style.left = `${pct}%`;
    this.trackMark.firstChild.textContent = fmt(cur);
    this.trackMark.firstChild.style.transform = `translateX(-${pct}%)`;
    if (!this.busy) this.updateLock();
  }

  updatePausedPill() {
    const v = this.video;
    const show = this.title && v.paused && !v.ended && v.currentTime > 0.5 && !this.pick && !this.busy;
    this.pausedPill.hidden = !show;
    if (!show) return;
    this.pausedPill.className = 'xp-pill';
    this.pausedPill.innerHTML = `${icon('pause')}<span>Paused at <b>${fmt(v.currentTime)}</b></span>` +
      '<span class="xp-pill__cta">Ask about this moment</span>';
  }

  updateLock(p) {
    const at = p ?? (this.video.currentTime || 0);
    this.lockChip.innerHTML = `${icon('lock')}<span>Answers as of <b>${fmt(at)}</b></span>`;
  }

  // ---------- spoiler gate ----------

  refreshGate(p, immediate = false) {
    clearTimeout(this.gateTimer);
    const run = async () => {
      const t = this.title;
      if (!t) return;
      const seq = ++this.gateSeq;
      try {
        const res = await this.fetch(`/api/titles/${encodeURIComponent(t.id)}/gate?p=${p.toFixed(1)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const g = await res.json();
        if (seq === this.gateSeq && this.title === t) this.renderGate(g);
      } catch (err) {
        console.warn('Gate request failed', err);
      }
    };
    if (immediate) run(); else this.gateTimer = setTimeout(run, 200);
  }

  renderGate(g) {
    const keys = new Set([...Object.keys(g.eligible || {}), ...Object.keys(g.total || {})]);
    let eligible = 0;
    let total = 0;
    for (const k of keys) {
      eligible += Number(g.eligible?.[k]) || 0;
      total += Number(g.total?.[k]) || 0;
    }
    this.setShieldCounts(eligible, Math.max(0, total - eligible), g.p);
    if (g.characters_total != null) {
      this.shieldChars.innerHTML = `${icon('user')}<span><b>${g.characters_known ?? 0}</b> of ${g.characters_total} characters introduced</span>`;
    }
    this.gateRows.innerHTML = '';
    const rows = GATE_ROWS.filter(([k]) => keys.has(k));
    for (const k of keys) if (!GATE_ROWS.some(([r]) => r === k) && Number(g.total?.[k])) rows.push([k, TYPE_LABELS[k] || k]);
    for (const [k, label] of rows) {
      const e = Number(g.eligible?.[k]) || 0;
      const n = Number(g.total?.[k]) || 0;
      const pct = n ? (e / n) * 100 : 0;
      this.gateRows.append(h('div', { class: 'xp-gate-row' },
        h('span', { class: 'xp-gate-row__label' }, label),
        h('span', { class: 'xp-gate-row__bar' }, h('i', { style: `width:${pct.toFixed(1)}%` })),
        h('span', { class: 'xp-gate-row__num' }, `${e} / ${n}`)));
    }
  }

  setShieldCounts(eligible, locked, p) {
    this.shieldStat.innerHTML =
      `<b class="xp-ok">${eligible.toLocaleString('en')}</b> facts unlocked · ` +
      `<b class="xp-locked">${locked.toLocaleString('en')}</b> still locked` +
      (p != null ? ` <span class="xp-muted">at ${fmt(p)}</span>` : '');
    this.shield.classList.remove('is-pulse');
    void this.shield.offsetWidth;
    this.shield.classList.add('is-pulse');
  }

  // ---------- asking ----------

  setLang(code) {
    this.lang = code;
    for (const b of this.langBtns) b.setAttribute('aria-pressed', String(b.dataset.lang === code));
    try { localStorage.setItem(LANG_KEY, code); } catch { /* storage unavailable */ }
  }

  onQuick(q) {
    if (this.busy || !this.title) return;
    if (q.pick) this.startPick(q);
    else this.ask(q.question, null);
  }

  onSubmit(e) {
    e.preventDefault();
    if (this.busy) { this.stopStream(); return; }
    const q = this.input.value.trim();
    if (!q || !this.title) return;
    this.input.value = '';
    this.cancelPick();
    this.ask(q, null);
  }

  startPick(q) {
    this.video.pause();
    this.clearOverlay();
    this.pick = { question: q.question, x: 0.5, y: 0.5 };
    this.player.classList.add('is-picking');
    this.pickBar.querySelector('.xp-pickbar__msg').innerHTML =
      `${icon('tap')}<span><b>${q.pick}</b> on the video</span>`;
    this.moveCross(0.5, 0.5, false);
    for (const c of this.chips) c.classList.toggle('is-active', c.dataset.key === q.key);
    this.updatePausedPill();
    this.pickLayer.focus({ preventScroll: true });
    const r = this.player.getBoundingClientRect();
    if (r.top < 0 || r.bottom > innerHeight) this.player.scrollIntoView({ behavior: 'smooth', block: 'center' });
    this.live.textContent = `${q.pick} on the video, or use the arrow keys and Enter.`;
  }

  cancelPick() {
    if (!this.pick) return;
    this.pick = null;
    this.player.classList.remove('is-picking', 'is-keyboard');
    for (const c of this.chips) c.classList.remove('is-active');
    this.updatePausedPill();
  }

  moveCross(x, y, keyboard) {
    this.pick.x = Math.min(1, Math.max(0, x));
    this.pick.y = Math.min(1, Math.max(0, y));
    this.cross.style.left = `${this.pick.x * 100}%`;
    this.cross.style.top = `${this.pick.y * 100}%`;
    this.player.classList.toggle('is-keyboard', keyboard);
  }

  pointToTap(e) {
    const r = this.contentRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  }

  onPickHover(e) {
    if (!this.pick) return;
    const p = this.pointToTap(e);
    this.player.classList.toggle('is-outside', !p);
    if (p) this.moveCross(p.x, p.y, false);
  }

  onPickPointer(e) {
    if (!this.pick) return;
    e.preventDefault();
    const p = this.pointToTap(e);
    if (!p) {
      this.live.textContent = 'That is outside the picture. Tap inside the frame.';
      this.pickBar.classList.remove('is-nudge');
      void this.pickBar.offsetWidth;
      this.pickBar.classList.add('is-nudge');
      clearTimeout(this.nudgeTimer);
      this.nudgeTimer = setTimeout(() => this.pickBar.classList.remove('is-nudge'), 1600);
      return;
    }
    this.confirmPick(p);
  }

  onPickKey(e) {
    if (!this.pick) return;
    const step = e.shiftKey ? 0.1 : 0.025;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (moves[e.key]) {
      e.preventDefault();
      this.moveCross(this.pick.x + moves[e.key][0], this.pick.y + moves[e.key][1], true);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      this.confirmPick({ x: this.pick.x, y: this.pick.y });
    } else if (e.key === 'Escape') {
      e.preventDefault();
      this.cancelPick();
      this.input.focus();
    }
  }

  confirmPick(tap) {
    if (!this.pick) return;
    const question = this.pick.question;
    this.cancelPick();
    if (tap) this.drawRing(tap);
    this.ask(question, tap);
  }

  drawRing(tap) {
    this.ring.hidden = false;
    this.ring.style.left = `${tap.x * 100}%`;
    this.ring.style.top = `${tap.y * 100}%`;
    this.ring.classList.remove('is-in');
    void this.ring.offsetWidth;
    this.ring.classList.add('is-in');
    this.overlayP = this.video.currentTime;
  }

  drawHighlight(hl) {
    const b = hl?.bbox;
    if (!Array.isArray(b) || b.length !== 4) return;
    const [ymin, xmin, ymax, xmax] = b.map(Number);
    Object.assign(this.hl.style, {
      top: `${ymin / 10}%`, left: `${xmin / 10}%`,
      height: `${Math.max(0, ymax - ymin) / 10}%`, width: `${Math.max(0, xmax - xmin) / 10}%`,
    });
    const label = this.hl.firstChild;
    label.textContent = hl.label || '';
    label.hidden = !hl.label;
    this.hl.classList.toggle('is-low', ymin < 80);
    this.hl.hidden = false;
    this.hl.classList.remove('is-in');
    void this.hl.offsetWidth;
    this.hl.classList.add('is-in');
    this.overlayP = this.video.currentTime;
  }

  clearOverlay() {
    this.ring.hidden = true;
    this.hl.hidden = true;
    this.overlayP = null;
  }

  setBusy(on) {
    this.busy = on;
    this.root.classList.toggle('is-busy', on);
    for (const c of this.chips) c.disabled = on;
    this.input.disabled = on;
    this.sendBtn.innerHTML = icon(on ? 'stop' : 'send');
    this.sendBtn.setAttribute('aria-label', on ? 'Stop' : 'Ask');
    this.sendBtn.classList.toggle('is-stop', on);
    this.updatePausedPill();
  }

  stopStream() {
    if (this.abort) this.abort.abort();
    this.abort = null;
  }

  scrollThread(force = false) {
    const t = this.thread;
    if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 160) t.scrollTop = t.scrollHeight;
  }

  /** Brings a new answer card's top into view, inside the thread and (on narrow layouts) on the page. */
  revealAnswer(card) {
    const t = this.thread;
    t.scrollTop = Math.max(0, card.offsetTop - 12);
    requestAnimationFrame(() => {
      const r = card.getBoundingClientRect();
      if (r.top > innerHeight - 120 || r.top < 0) window.scrollBy({ top: r.top - innerHeight * 0.3, behavior: 'smooth' });
    });
  }

  async ask(question, tap) {
    if (this.busy || !this.title) return;
    const v = this.video;
    v.pause();
    const p = Math.round((v.currentTime || 0) * 10) / 10;
    const title = this.title;

    if (this.thread.querySelector('.xp-empty')) this.thread.innerHTML = '';
    if (this.lastAskP != null && p < this.lastAskP - 0.5) {
      for (const turn of this.thread.querySelectorAll('.xp-turn')) turn.classList.add('is-stale');
      this.thread.append(h('div', { class: 'xp-divider', role: 'note' },
        h('span', { html: icon('rewind') }), h('span', {}, `You went back to ${fmt(p)}. New conversation, so earlier answers can't spoil it.`)));
    }
    this.lastAskP = p;
    this.updateLock(p);

    const turn = new Turn(this, { question, tap, p, title });
    this.thread.append(turn.el);
    this.scrollThread(true);
    this.setBusy(true);
    const ctrl = new AbortController();
    this.abort = ctrl;
    const started = performance.now();

    try {
      const res = await this.fetch('/api/ask', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
        body: JSON.stringify({
          title_id: title.id, p, question, lang: this.lang, session_id: this.sessions.get(title.id),
          ...(tap ? { tap: { x: +tap.x.toFixed(4), y: +tap.y.toFixed(4) } } : {}),
        }),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        let msg = '';
        try { msg = (await res.json()).message || ''; } catch { /* not JSON */ }
        if (res.status === 429) turn.fail('You are asking faster than the demo allows.', msg || 'Wait a minute, then try again.', 'slow');
        else turn.fail('The assistant could not answer.', msg || `The service returned HTTP ${res.status}.`);
        return;
      }
      await readSSE(res, (event, data) => {
        if (this.title !== title) return;
        turn.onEvent(event, data, performance.now() - started);
        if (event === 'gate') this.setShieldCounts(Number(data.eligible_total) || 0, Number(data.locked_total) || 0, data.p ?? p);
        if (event === 'answer') {
          if (data.highlight && Math.abs(v.currentTime - p) < 0.6) this.drawHighlight(data.highlight);
          this.live.textContent = `Answer: ${data.text || ''}`;
          const card = turn.el.querySelector('.xp-answer');
          if (card) this.revealAnswer(card);
        } else this.scrollThread();
      });
      turn.finish();
    } catch (err) {
      if (err.name === 'AbortError') turn.fail('Stopped.', 'You stopped this answer.', 'muted');
      else {
        console.warn('Ask failed', err);
        turn.fail('The connection dropped.', 'Check your network and try again.');
      }
    } finally {
      if (this.abort === ctrl) this.abort = null;
      this.setBusy(false);
      if (!turn.answered) this.scrollThread();
    }
  }

  retry(question, tap, p) {
    if (this.busy) return;
    if (Math.abs((this.video.currentTime || 0) - p) > 0.5) this.video.currentTime = p;
    if (tap) this.drawRing(tap);
    this.ask(question, tap);
  }

  rewindTo(t) {
    this.video.pause();
    this.video.currentTime = t;
    this.player.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

/** Reads a text/event-stream body and calls onEvent(event, data) for each message. */
async function readSSE(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  const flush = (block) => {
    let event = 'message';
    const data = [];
    for (const line of block.split(/\r?\n/)) {
      if (!line || line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    }
    if (!data.length) return;
    let parsed;
    try { parsed = JSON.parse(data.join('\n')); } catch { parsed = { raw: data.join('\n') }; }
    onEvent(event, parsed);
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let m;
    while ((m = /\r?\n\r?\n/.exec(buf))) {
      flush(buf.slice(0, m.index));
      buf = buf.slice(m.index + m[0].length);
    }
  }
  buf += decoder.decode();
  if (buf.trim()) flush(buf);
}

/** One question with its live step timeline and final answer card. */
class Turn {
  constructor(app, { question, tap, p, title }) {
    this.app = app;
    this.q = { question, tap, p };
    this.title = title;
    this.steps = new Map();
    this.answered = false;
    this.failed = false;
    this.lastMark = 0;

    this.stepList = h('ol', { class: 'xp-steps', 'aria-label': 'What the assistant did' });
    this.thinking = h('li', { class: 'xp-step is-pending xp-step--thinking' },
      h('span', { class: 'xp-step__icon' }, h('i', { class: 'xp-spinner' })),
      h('span', { class: 'xp-step__body' }, h('span', { class: 'xp-step__label' }, 'Locking the moment and checking the gate…')));
    this.stepList.append(this.thinking);
    this.slot = h('div', { class: 'xp-turn__answer' });
    this.el = h('article', { class: 'xp-turn' },
      h('div', { class: 'xp-q' },
        h('p', { class: 'xp-q__text' }, question),
        h('span', { class: 'xp-q__meta' },
          h('span', { class: 'xp-tag', html: `${icon('lock')}<span>${fmt(p)}</span>` }),
          tap ? h('span', { class: 'xp-tag', html: `${icon('tap')}<span>tapped</span>` }) : null)),
      this.stepList, this.slot);
  }

  addStep({ id, iconName, label, summary, ms, pending }) {
    const li = h('li', { class: `xp-step${pending ? ' is-pending' : ''}` },
      h('span', { class: 'xp-step__icon', html: icon(iconName) }),
      h('span', { class: 'xp-step__body' },
        h('span', { class: 'xp-step__label' }, label),
        h('span', { class: 'xp-step__summary' }, summary || '')),
      h('span', { class: 'xp-step__ms' }, ms >= 50 ? `${(ms / 1000).toFixed(1)} s` : ''));
    this.stepList.insertBefore(li, this.thinking);
    if (id) this.steps.set(id, { li, start: performance.now() });
    return li;
  }

  onEvent(event, data, elapsed) {
    switch (event) {
      case 'gate': {
        this.addStep({
          iconName: 'shield', label: `Spoiler gate locked at ${fmt(data.p ?? this.q.p)}`,
          summary: `${(Number(data.eligible_total) || 0).toLocaleString('en')} facts unlocked · ` +
            `${(Number(data.locked_total) || 0).toLocaleString('en')} kept away from the model`,
          ms: elapsed,
        }).classList.add('xp-step--gate');
        this.setThinking('Thinking…');
        break;
      }
      case 'tool': {
        const id = data.id || `s${this.steps.size}`;
        this.addStep({
          id, iconName: toolIcon(data.name), label: `${data.label || data.name || 'Working'}…`, pending: true,
        }).dataset.tool = data.name || '';
        this.setThinking(null);
        break;
      }
      case 'tool_result': {
        const s = this.steps.get(data.id);
        const li = s ? s.li : this.addStep({ iconName: toolIcon(data.name), label: data.name || 'Step' });
        li.classList.remove('is-pending');
        const label = li.querySelector('.xp-step__label');
        label.textContent = label.textContent.replace(/…$/, '');
        li.querySelector('.xp-step__summary').textContent = data.summary || 'Done';
        const ms = Number(data.duration_ms ?? data.ms) || (s ? performance.now() - s.start : 0);
        li.querySelector('.xp-step__ms').textContent = ms >= 50 ? `${(ms / 1000).toFixed(1)} s` : '';
        if (![...this.steps.values()].some((x) => x.li.classList.contains('is-pending'))) this.setThinking('Writing the answer…');
        break;
      }
      case 'answer':
        this.answered = true;
        this.setThinking(null);
        this.renderAnswer(data);
        break;
      case 'error':
        this.fail('The assistant ran into a problem.', data.message || 'Please try again.');
        break;
      default:
        break;
    }
  }

  setThinking(text) {
    this.thinking.hidden = !text;
    if (text) this.thinking.querySelector('.xp-step__label').textContent = text;
  }

  renderAnswer(a) {
    const app = this.app;
    const tid = encodeURIComponent(this.title.id);
    const guard = a.guard || {};
    const guardEl = !guard.checked
      ? h('span', { class: 'xp-badge', html: `${icon('shield')}<span>Guard not run</span>` })
      : guard.rewritten
        ? h('span', { class: 'xp-badge xp-badge--rewrite', title: 'The output guard caught something and replaced the answer',
            html: `${icon('shield')}<span>Spoiler guard rewrote this</span>` })
        : h('span', { class: 'xp-badge xp-badge--ok', title: 'Checked for real names, future facts and unrevealed names',
            html: `${icon('shield')}<span>Spoiler guard passed</span>` });

    const card = h('div', { class: 'xp-answer' });
    card.append(h('p', { class: 'xp-answer__text', lang: app.lang === 'zh' ? 'zh' : 'en' }, a.text || ''));

    if (a.highlight?.bbox) {
      const b = a.highlight.bbox.map(Number);
      const fig = h('figure', { class: 'xp-onscreen' },
        h('div', { class: 'xp-onscreen__img' },
          h('img', {
            src: `/api/titles/${tid}/frames/${Math.floor(a.highlight.frame_t ?? this.q.p)}.jpg`, alt: '', loading: 'lazy',
          }),
          h('i', {
            class: 'xp-onscreen__box',
            style: `top:${b[0] / 10}%;left:${b[1] / 10}%;height:${(b[2] - b[0]) / 10}%;width:${(b[3] - b[1]) / 10}%`,
          })),
        h('figcaption', {}, a.highlight.label ? `On screen: ${a.highlight.label}` : 'On screen'));
      card.append(fig);
    }

    const ev = (a.evidence || []).filter((e) => e && e.start_s != null);
    if (ev.length) {
      const quote = h('blockquote', { class: 'xp-evidence__quote', hidden: true });
      const chips = ev.map((e) => h('button', {
        type: 'button', class: 'xp-ev', 'aria-pressed': 'false', 'aria-label': `${TYPE_LABELS[e.type] || e.type} at ${fmt(e.start_s)}`,
        onclick: (evt) => {
          const btn = evt.currentTarget;
          const on = btn.getAttribute('aria-pressed') !== 'true';
          for (const c of chips) c.setAttribute('aria-pressed', 'false');
          btn.setAttribute('aria-pressed', String(on));
          quote.hidden = !on || !e.text;
          quote.textContent = e.text || '';
        },
      }, h('span', { class: 'xp-ev__t' }, fmt(e.start_s)), h('span', { class: 'xp-ev__type' }, TYPE_LABELS[e.type] || e.type)));
      card.append(h('div', { class: 'xp-evidence' },
        h('span', { class: 'xp-evidence__h' }, 'Based on'),
        h('div', { class: 'xp-evidence__chips' }, chips), quote));
    }

    const frames = (a.frames || []).filter((f) => f && f.t != null);
    if (frames.length) {
      card.append(h('div', { class: 'xp-frames' }, frames.map((f) => h('button', {
        type: 'button', class: 'xp-frame', title: `Rewind to ${fmt(f.t)}`,
        'aria-label': `${f.caption || fmt(f.t)}. Rewind the player to ${fmt(f.t)}`,
        onclick: () => app.rewindTo(f.t),
      },
        h('img', { src: `/api/titles/${tid}/frames/${Math.floor(f.t)}.jpg`, alt: '', loading: 'lazy' }),
        h('span', { class: 'xp-frame__cap' }, h('span', { html: icon('rewind') }), h('span', {}, f.caption || fmt(f.t)))))));
    }

    card.append(h('div', { class: 'xp-answer__foot' }, guardEl,
      a.latency_ms != null ? h('span', { class: 'xp-badge', html: `${icon('clock')}<span>${(a.latency_ms / 1000).toFixed(1)} s</span>` }) : null));
    this.slot.append(card);
  }

  fail(title, detail, kind = 'error') {
    if (this.failed || this.answered) return;
    this.failed = true;
    this.setThinking(null);
    for (const s of this.steps.values()) s.li.classList.remove('is-pending');
    const { question, tap, p } = this.q;
    this.slot.append(h('div', { class: `xp-notice xp-notice--${kind}`, role: kind === 'muted' ? null : 'alert' },
      h('span', { html: icon(kind === 'slow' ? 'clock' : kind === 'muted' ? 'stop' : 'alert') }),
      h('div', {},
        h('strong', {}, title),
        h('p', {}, detail),
        h('button', { type: 'button', class: 'xp-btn', onclick: () => this.app.retry(question, tap, p) }, 'Ask again'))));
  }

  finish() {
    this.setThinking(null);
    if (!this.answered && !this.failed) this.fail('No answer came back.', 'The stream ended early. Please try again.');
  }
}
