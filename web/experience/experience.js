// Tab 1, "Live demo": film switcher, player with its own controls, the gate line, and the ask panel where answers
// stream in sentence by sentence. Contract: docs/web-api.md.
// Deep links: ?title=<id>&t=<seconds>&q=<text>&lang=en|zh opens that film paused at t with the question filled in;
// &ask=1 also sends it. Unknown ids fall back to the first film.

const params = new URLSearchParams(location.search);
const LANG_KEY = 'hwa-lang';
const BENCH_URL = 'https://github.com/lennonkc/hello-watch-assistant/blob/main/bench/README.md';
const TEASER = { id: 'F0LyLRiYJ7I', title: 'Hello Watch Assistant demo walkthrough' };
const GATE_EVERY_S = 5;          // while playing, refresh the gate line this often (film time)

/** Every request carries the preview cookie (see /api/unlock). */
const api = (url, opts = {}) => fetch(url, { credentials: 'same-origin', ...opts });

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
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  send: '<path d="M4 12 20 4l-5 16-3.5-6.5z"/><path d="m11.5 13.5 4-4"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  rewind: '<path d="M11 6 4 12l7 6zM20 6l-7 6 7 6z"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  tap: '<circle cx="12" cy="12" r="3.5"/><circle cx="12" cy="12" r="8.5" opacity=".5"/>',
  volume: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  mute: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="m16 9.5 5 5M21 9.5l-5 5"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  shrink: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  chart: '<path d="M4 20V11M10 20V5M16 20v-6M2.5 20h19"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
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

const num = (n) => (Number(n) || 0).toLocaleString('en');

function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

// Everything the live demo shows, in the language picked with the EN / ZH switch (the answer language). A question's
// card keeps the language it was asked in. Server-written labels (steps, frame captions) come in that language too.
const STR = {
  en: {
    types: {
      scene: 'Scene', event: 'Event', recap: 'Recap', shot_caption: 'Shot', character_fact: 'Character',
      object: 'Object', cue: 'Dialogue', video_window: 'Clip', look: 'Look', character: 'Character',
      shot: 'Shot', line: 'Dialogue',
    },
    gateRows: {
      scene: 'Scenes', event: 'Events', cue: 'Lines of dialogue', shot_caption: 'Shots described',
      character_fact: 'Character facts', object: 'Objects', recap: 'Recaps',
    },
    chooseFilm: 'Choose a film', playFilm: 'Play the film', preview: 'Preview quality',
    nowPlaying: 'Now playing', changeFilm: 'Change film',
    wholeFrame: 'Whole frame', cancel: 'Cancel',
    gateChecking: 'Checking what the assistant can see…', details: 'Details',
    gateLine: (e, l) => `Assistant can see <b class="xp-ok">${e}</b> moments · <b class="xp-locked">${l}</b> still locked`,
    gateNothing: (l) => `Nothing unlocked yet · <b class="xp-locked">${l}</b> moments locked until they play`,
    gateChars: (k, n) => `${k} of ${n} characters introduced`,
    gateNote: (t) => `Counts only, at ${t}. Locked items stay on the server; the gate runs there before any lookup or search.`,
    panelTitle: 'Ask the assistant', quick: 'Quick questions', answerLang: 'Answer language',
    placeholder: 'Ask about this moment…', yourQuestion: 'Your question', ask: 'Ask', stop: 'Stop',
    tapNote: 'Your tap goes with the next question.', clear: 'Clear',
    emptyHead: 'Pause anywhere, then ask. ',
    emptyBody: 'To ask about someone, tap them on the paused picture first. Answers use what the film has shown up to that moment.',
    lock: (t) => `Answers as of <b>${t}</b>`, lockTitle: 'Questions are answered as of this moment',
    hintTap: 'Tap someone on screen to ask about them', outside: 'That is outside the picture. Tap inside it.',
    outsideLive: 'That is outside the picture.', videoError: 'The video could not be loaded. Try another film or reload.',
    pointSet: 'Point set. Ask who or what it is.', pickLive: (q) => `${q} on the picture, or use the arrow keys and Enter.`,
    wentBack: (t) => `You went back to ${t}. The answers below are from later in the film, so the assistant leaves them out now.`,
    answerLive: 'Answer: ', couldNot: 'The assistant could not answer.', http: (n) => `The service returned HTTP ${n}.`,
    budgetTotal: ['The demo has used up today’s question budget.', 'It resets within a day. The player and the gate line keep working meanwhile.'],
    budgetIp: ['You have reached today’s question limit for this demo.', 'Thanks for trying it this much. It resets within a day.'],
    tooMany: ['Too many questions right now.', 'Wait a minute, then try again.'],
    listFailed: ['The film list did not load.', 'The service may be waking up. Give it a few seconds and try again.'],
    tryAgain: 'Try again',
    // one question's card
    tapped: 'tapped', steps: 'What the assistant did', locking: 'Locking the moment and checking the gate…',
    gateStep: (t) => `Spoiler gate at ${t}`, gateSummary: (e, l) => `${e} moments unlocked · ${l} kept from the model`,
    lookingUp: 'Looking things up…', working: 'Working', step: 'Step', done: 'Done', writing: 'Writing the answer…',
    lookups: (n) => (n === 1 ? '1 lookup' : `${n} lookups`), traceDone: (t, look) => `Gate at ${t} + ${look}`,
    basedOn: 'Based on', evAt: (type, t) => `${type} at ${t}`, rewindTo: (t) => `Rewind to ${t}`,
    frameAria: (cap, t) => `${cap}. Rewind the player to ${t}`,
    guardNotRun: 'Guard not run', guardReplaced: 'Guard replaced the draft',
    guardRemoved: (n) => `Guard removed ${n} sentence${n > 1 ? 's' : ''}`,
    guardCaught: 'The output guard caught something in the draft', guardOk: 'Passed the output guard',
    guardOkTitle: 'Checked for unrevealed names, later plot terms and real people’s names',
    timing: (f, d) => `first words ${f} s · done ${d} s`, secs: (d) => `${d} s`,
    cutBadge: 'Cut short', cutTitle: 'The answer ran out of time; the sentences shown are complete and checked',
    askAgain: 'Ask again',
    cutError: ['The answer was cut short.', 'Something went wrong partway through.'],
    problem: ['The assistant ran into a problem.', 'Please try again.'],
    stoppedCut: ['Stopped.', 'You stopped this answer partway through.'], stopped: ['Stopped.', 'You stopped this answer.'],
    droppedCut: ['The answer was cut short.', 'The connection dropped. Check your network and ask again.'],
    dropped: ['The connection dropped.', 'Check your network and try again.'],
    endedCut: ['The answer was cut short.', 'The stream ended before the answer was complete.'],
    ended: ['No answer came back.', 'The stream ended early. Please try again.'],
    pause: 'Quick pause: the demo takes a few questions a minute.',
    askIn: (t) => `You can ask again in ${t}.`, askNow: 'You can ask again now.',
  },
  zh: {
    types: {
      scene: '场景', event: '事件', recap: '回顾', shot_caption: '镜头', character_fact: '角色',
      object: '物件', cue: '台词', video_window: '片段', look: '外观', character: '角色', shot: '镜头', line: '台词',
    },
    gateRows: {
      scene: '场景', event: '事件', cue: '台词', shot_caption: '已描述的镜头', character_fact: '角色信息',
      object: '物件', recap: '回顾',
    },
    chooseFilm: '选择影片', playFilm: '播放影片', preview: '预览画质',
    nowPlaying: '正在播放', changeFilm: '换一部',
    wholeFrame: '整个画面', cancel: '取消',
    gateChecking: '正在查看助手能看到什么…', details: '详情',
    gateLine: (e, l) => `助手能看到 <b class="xp-ok">${e}</b> 条内容 · <b class="xp-locked">${l}</b> 条仍然锁着`,
    gateNothing: (l) => `还没有解锁任何内容 · <b class="xp-locked">${l}</b> 条内容播到才解锁`,
    gateChars: (k, n) => `已出场 ${k} / ${n} 个角色`,
    gateNote: (t) => `只显示数量，截至 ${t}。锁住的内容留在服务器上；每次查找或搜索之前，都先在服务器上过这道闸。`,
    panelTitle: '向助手提问', quick: '快捷问题', answerLang: '回答语言',
    placeholder: '问问这一刻…', yourQuestion: '你的问题', ask: '提问', stop: '停止',
    tapNote: '你点的位置会和下一个问题一起发送。', clear: '清除',
    emptyHead: '随时暂停，然后提问。',
    emptyBody: '想问某个人，先在暂停的画面上点一下他。回答只用到影片播到这一刻为止展示过的内容。',
    lock: (t) => `回答截至 <b>${t}</b>`, lockTitle: '问题按这一刻来回答',
    hintTap: '点一下画面里的人，就能问关于他的问题', outside: '那里在画面外面，请点画面里面。',
    outsideLive: '那里在画面外面。', videoError: '视频没能加载。换一部影片或刷新页面试试。',
    pointSet: '位置已选好，问问那是谁或是什么。', pickLive: (q) => `${q}，或用方向键加回车。`,
    wentBack: (t) => `你退回到了 ${t}。下面的回答来自影片更后面的位置，所以助手现在不会用到它们。`,
    answerLive: '回答：', couldNot: '助手没能回答。', http: (n) => `服务返回了 HTTP ${n}。`,
    budgetTotal: ['今天的演示提问额度已经用完了。', '一天之内会重置。播放器和闸门信息照常可用。'],
    budgetIp: ['你今天在这个演示里的提问次数已经用完了。', '谢谢你这么认真地试用！一天之内会重置。'],
    tooMany: ['现在提问太频繁了。', '请等一分钟再试。'],
    listFailed: ['影片列表没能加载。', '服务可能正在启动，过几秒再试一次。'],
    tryAgain: '再试一次',
    tapped: '已点选', steps: '助手做了什么', locking: '正在锁定这一刻并检查闸门…',
    gateStep: (t) => `${t} 的防剧透闸门`, gateSummary: (e, l) => `已解锁 ${e} 条 · ${l} 条不给模型看`,
    lookingUp: '正在查找…', working: '处理中', step: '步骤', done: '完成', writing: '正在写回答…',
    lookups: (n) => `${n} 次查找`, traceDone: (t, look) => `${t} 的闸门 + ${look}`,
    basedOn: '依据', evAt: (type, t) => `${t} 的${type}`, rewindTo: (t) => `倒回到 ${t}`,
    frameAria: (cap, t) => `${cap}。把播放器倒回到 ${t}`,
    guardNotRun: '未经输出检查', guardReplaced: '输出检查替换了草稿',
    guardRemoved: (n) => `输出检查删掉了 ${n} 句`,
    guardCaught: '输出检查在草稿里发现了问题', guardOk: '通过输出检查',
    guardOkTitle: '已检查：尚未揭晓的名字、后面剧情的词、真人姓名',
    timing: (f, d) => `首句 ${f} 秒 · 完成 ${d} 秒`, secs: (d) => `${d} 秒`,
    cutBadge: '被截断', cutTitle: '回答超时了；显示出来的句子都完整并且经过了检查',
    askAgain: '再问一次',
    cutError: ['回答被中断了。', '中途出了点问题。'],
    problem: ['助手遇到了问题。', '请再试一次。'],
    stoppedCut: ['已停止。', '你中途停止了这个回答。'], stopped: ['已停止。', '你停止了这个回答。'],
    droppedCut: ['回答被中断了。', '连接断开了。检查一下网络再问一次。'],
    dropped: ['连接断开了。', '检查一下网络再试一次。'],
    endedCut: ['回答被中断了。', '回答还没写完，连接就结束了。'],
    ended: ['没有收到回答。', '连接提前结束了，请再试一次。'],
    pause: '稍等一下：这个演示每分钟只接受几个问题。',
    askIn: (t) => `${t} 后可以再问。`, askNow: '现在可以再问了。',
  },
};
const strings = (lang) => STR[lang] || STR.en;
const GATE_ROWS = ['scene', 'event', 'cue', 'shot_caption', 'character_fact', 'object', 'recap'];

function toolIcon(name = '') {
  const n = name.toLowerCase();
  if (/recap|story|so_far|summary/.test(n)) return 'book';
  if (/shot|frame|tap|look|screen|bbox|who/.test(n)) return 'target';
  if (/dialog|cue|said|line|transcript|speech/.test(n)) return 'quote';
  if (/window|clip|video|watch/.test(n)) return 'film';
  if (/search|find|retriev|lookup|query/.test(n)) return 'search';
  return 'spark';
}

// The four one-tap questions. `pick`: ask the viewer to point at something first (unless they already tapped).
const QUICK = [
  { key: 'who', label: 'Who is this?', zh: '这是谁？', icon: 'user', pick: 'Tap the person you mean', pickZh: '点一下你说的那个人' },
  { key: 'miss', label: 'What did I miss?', zh: '我错过了什么？', icon: 'book' },
  { key: 'say', label: 'What did they just say?', zh: '他们刚才说了什么？', icon: 'quote' },
  { key: 'what', label: 'What is that?', zh: '那是什么？', icon: 'target', pick: 'Tap the thing you mean', pickZh: '点一下你说的那个东西' },
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
  await loadStyles();
  const app = new Experience(el);
  app.render();
  await app.loadTitles();
}

class Experience {
  constructor(root) {
    this.root = root;
    this.titles = [];
    this.title = null;
    this.sessions = new Map();
    this.lang = 'en';
    try { this.lang = localStorage.getItem(LANG_KEY) === 'zh' ? 'zh' : 'en'; } catch { /* storage unavailable */ }
    if (['en', 'zh'].includes(params.get('lang'))) this.lang = params.get('lang');
    this.busy = false;
    this.abort = null;
    this.pick = null;           // {question, x, y} while choosing a point for a "who / what" chip
    this.tap = null;            // {x, y, p}: the viewer's tap on the paused picture, sent with the next question
    this.at = null;             // position to open at (deep link), until the video has seeked there
    this.lastAskP = null;
    this.gateTimer = 0;
    this.gateSeq = 0;
    this.gateP = null;
    this.gateShown = null;
    this.hlP = null;            // P whose answer drew the highlight box
    this.cooldownUntil = 0;     // per-minute limit: no request before this (ms since epoch)
    this.cooldownNotice = null;
  }

  /** The page's strings in the current language. */
  get s() { return strings(this.lang); }

  // ---------- layout ----------

  render() {
    const r = this.root;
    r.classList.add('xp');
    r.innerHTML = '';

    // Film picker: one card per film above the stage (the cards tilt toward the pointer, see web/shared/motion.js).
    this.films = h('div', { class: 'xp-cards', role: 'group' },
      [0, 1, 2].map(() => h('div', { class: 'xp-cards__item', 'aria-hidden': 'true' },
        h('span', { class: 'xp-card xp-card--skeleton' },
          h('span', { class: 'xp-card__poster' }), h('span', { class: 'xp-card__body' }, h('span'), h('span'))))));
    this.pickerHead = h('h2', { id: 'xp-picker-h', class: 'xp-section-h' });
    this.picker = h('section', { class: 'xp-picker', 'aria-labelledby': 'xp-picker-h' }, this.pickerHead, this.films);

    // Above the player: which film is on, and a way back up to the cards.
    this.nowTitle = h('span', { class: 'xp-now__title' });
    this.nowTags = h('span', { class: 'xp-now__tags' });
    this.changeBtn = h('button', {
      type: 'button', class: 'xp-now__change', onclick: () => this.scrollTo(this.picker),
    }, h('span', { html: icon('film') }), h('span', {}));
    this.now = h('div', { class: 'xp-now' }, this.nowTitle, this.nowTags, this.changeBtn);

    // Player: the picture (video + overlays + tap layer) above its own controls, so the whole paused picture can
    // take a tap and nothing of the native controls sits on top of it.
    this.video = h('video', { class: 'xp-video', playsinline: true, preload: 'metadata', 'aria-label': 'Film' });
    this.content = h('div', { class: 'xp-content', 'aria-hidden': 'true' });
    this.ring = h('div', { class: 'xp-ring', hidden: true });
    this.hl = h('div', { class: 'xp-hl', hidden: true }, h('span', { class: 'xp-hl__label' }));
    this.cross = h('div', { class: 'xp-cross' });
    this.content.append(this.hl, this.ring, this.cross);
    this.tapLayer = h('div', {
      class: 'xp-taplayer', tabindex: '-1', role: 'application',
      'aria-label': 'Paused picture. Choose a point with the arrow keys and press Enter. Escape cancels.',
      onclick: (e) => this.onPictureClick(e),
      onpointermove: (e) => this.onPickHover(e),
      onkeydown: (e) => this.onPickKey(e),
    });
    this.hint = h('div', { class: 'xp-hint', hidden: true, 'aria-hidden': 'true' });
    this.bigPlay = h('button', {
      type: 'button', class: 'xp-bigplay', html: icon('play'), onclick: () => this.video.play(),
    });
    this.wholeBtn = h('button', { type: 'button', class: 'xp-btn xp-btn--ghost', onclick: () => this.confirmPick(null) });
    this.cancelBtn = h('button', { type: 'button', class: 'xp-btn xp-btn--ghost', onclick: () => this.cancelPick() });
    this.pickBar = h('div', { class: 'xp-pickbar', role: 'status' },
      h('span', { class: 'xp-pickbar__msg' }), h('span', { class: 'xp-pickbar__actions' }, this.wholeBtn, this.cancelBtn));
    this.screen = h('div', { class: 'xp-screen' },
      this.video, this.content, this.tapLayer, this.hint, this.bigPlay, this.pickBar);

    this.playBtn = h('button', { type: 'button', class: 'xp-ctl', onclick: () => this.togglePlay() });
    this.scrub = h('input', {
      type: 'range', class: 'xp-scrub', min: '0', max: '1', step: '0.1', value: '0',
      'aria-label': 'Position in the film',
      oninput: () => this.onScrub(),
      onchange: () => { this.scrubbing = false; },
    });
    this.timeEl = h('span', { class: 'xp-time' }, '00:00 / 00:00');
    this.muteBtn = h('button', { type: 'button', class: 'xp-ctl', onclick: () => { this.video.muted = !this.video.muted; } });
    this.fsBtn = h('button', { type: 'button', class: 'xp-ctl', onclick: () => this.toggleFullscreen() });
    this.controls = h('div', { class: 'xp-controls' },
      this.playBtn, h('div', { class: 'xp-scrub__wrap' }, this.scrub), this.timeEl, this.muteBtn, this.fsBtn);
    this.player = h('div', { class: 'xp-player', onkeydown: (e) => this.onPlayerKey(e) }, this.screen, this.controls);

    // The gate line: what the assistant can see at this position, live.
    this.gateText = h('span', { class: 'xp-gate__text' });
    this.gateMoreLabel = h('span', {});
    this.gateMore = h('button', {
      type: 'button', class: 'xp-gate__more', 'aria-expanded': 'false', 'aria-controls': 'xp-gate-rows',
      onclick: () => this.toggleGateRows(),
    }, this.gateMoreLabel, h('span', { html: icon('down') }));
    this.gateRows = h('div', { class: 'xp-gate__rows', id: 'xp-gate-rows', hidden: true });
    this.gate = h('div', { class: 'xp-gate' },
      h('div', { class: 'xp-gate__line' },
        h('span', { class: 'xp-gate__badge', html: icon('shield') }), this.gateText, this.gateMore),
      this.gateRows);

    // Ask panel: the composer on top, the newest answer right under it.
    this.langBtns = ['en', 'zh'].map((code) => h('button', {
      type: 'button', class: 'xp-seg__btn', 'aria-pressed': String(this.lang === code), 'data-lang': code,
      'aria-label': code === 'en' ? 'Answer in English' : 'Answer in Chinese',
      title: code === 'en' ? 'Answer in English' : 'Answer in Chinese',
      onclick: () => this.setLang(code),
    }, code.toUpperCase()));
    this.lockChip = h('span', { class: 'xp-lock' });
    this.chips = QUICK.map((q) => h('button', {
      type: 'button', class: 'xp-chip', 'data-key': q.key, onclick: () => this.onQuick(q),
    }));
    this.input = h('input', {
      class: 'xp-input', type: 'text', name: 'question', maxlength: '300', autocomplete: 'off', enterkeyhint: 'send',
    });
    this.sendBtn = h('button', { type: 'submit', class: 'xp-send', html: icon('send') });
    this.form = h('form', { class: 'xp-form', onsubmit: (e) => this.onSubmit(e) }, this.input, this.sendBtn);
    this.tapNoteText = h('span', {});
    this.tapClear = h('button', { type: 'button', class: 'xp-tapnote__clear', onclick: () => this.clearTap() });
    this.tapNote = h('div', { class: 'xp-tapnote', hidden: true },
      h('span', { html: icon('tap') }), this.tapNoteText, this.tapClear);
    this.thread = h('div', { class: 'xp-thread', 'aria-label': 'Answers' });
    this.live = h('div', { class: 'visually-hidden', 'aria-live': 'polite' });
    this.panelTitle = h('h2', { id: 'xp-panel-h', class: 'xp-panel__title' });
    this.langSeg = h('div', { class: 'xp-seg', role: 'group' }, this.langBtns);
    this.chipGroup = h('div', { class: 'xp-chips', role: 'group' }, this.chips);
    this.panel = h('aside', { class: 'xp-panel', 'aria-labelledby': 'xp-panel-h' },
      h('div', { class: 'xp-panel__head' },
        h('div', { class: 'xp-panel__titles' }, this.panelTitle, this.lockChip), this.langSeg),
      h('div', { class: 'xp-composer' }, this.chipGroup, this.form, this.tapNote),
      this.thread, this.live);

    this.stage = h('section', { class: 'xp-stage', 'aria-labelledby': 'xp-stage-h' },
      h('h2', { id: 'xp-stage-h', class: 'visually-hidden' }, 'Live demo: pause the film and ask about it'),
      h('div', { class: 'xp-main' }, this.now, this.player, this.gate),
      this.panel);

    r.append(this.renderHero(), this.picker, this.stage, this.renderAbout());
    r.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.pick) { this.cancelPick(); this.input.focus(); }
    });
    if (params.get('q')) this.input.value = params.get('q').slice(0, 300);
    this.applyLang();
    this.renderEmptyThread();
    this.bindVideo();
    this.updateControls();
    this.updateLock();
  }

  /** Puts the page's own words in the current language (cards already in the thread keep theirs). */
  applyLang() {
    const s = this.s;
    this.stage.setAttribute('lang', this.lang === 'zh' ? 'zh-CN' : 'en');
    this.films.setAttribute('aria-label', s.chooseFilm);
    this.pickerHead.textContent = s.chooseFilm;
    this.changeBtn.lastChild.textContent = s.changeFilm;
    for (const b of this.films.querySelectorAll('.xp-card__now')) b.textContent = s.nowPlaying;
    this.bigPlay.setAttribute('aria-label', s.playFilm);
    this.wholeBtn.textContent = s.wholeFrame;
    this.cancelBtn.textContent = s.cancel;
    this.gateMoreLabel.textContent = s.details;
    if (this.gateCounts) { this.gateShown = null; this.setGateCounts(...this.gateCounts); } else this.gateText.textContent = s.gateChecking;
    if (this.gateData) this.renderGate(this.gateData, false);
    this.panelTitle.textContent = s.panelTitle;
    this.lockChip.title = s.lockTitle;
    this.langSeg.setAttribute('aria-label', s.answerLang);
    this.chipGroup.setAttribute('aria-label', s.quick);
    QUICK.forEach((q, i) => { this.chips[i].innerHTML = `${icon(q.icon)}<span></span>`; this.chips[i].lastChild.textContent = this.lang === 'zh' ? q.zh : q.label; });
    this.input.placeholder = s.placeholder;
    this.input.setAttribute('aria-label', s.yourQuestion);
    this.sendBtn.setAttribute('aria-label', this.busy ? s.stop : s.ask);
    this.tapNoteText.textContent = s.tapNote;
    this.tapClear.textContent = s.clear;
    if (this.thread.querySelector('.xp-empty')) this.renderEmptyThread();
    for (const b of this.root.querySelectorAll('.xp-badge--preview')) b.textContent = s.preview;
    if (!this.hint.hidden && !this.hint.classList.contains('is-warn')) { this.hint.hidden = true; this.updateHint(); }
    this.updateLock();
  }

  /** First screen: what this is, the three steps, and the walkthrough video beside them. */
  renderHero() {
    const steps = [
      ['film', 'Pick a film', 'and press play'],
      ['pause', 'Pause anywhere', 'the story locks there'],
      ['tap', 'Ask or tap', 'someone on screen'],
    ];
    return h('section', { class: 'xp-hero', 'aria-labelledby': 'xp-hero-h' },
      h('div', { class: 'xp-hero__copy' },
        h('p', { class: 'xp-eyebrow' }, 'Live demo'),
        h('h1', { id: 'xp-hero-h', class: 'xp-title xp-title--hero' }, 'Pause anywhere. Ask anything.',
          h('span', {}, ' Spoilers stay locked.')),
        h('p', { class: 'xp-lede' },
          'The assistant is only given what the film has shown up to the moment you pause. Everything later stays ' +
          'locked on the server, and every answer is checked before you see it.'),
        h('ol', { class: 'xp-howto', 'aria-label': 'How to try it' },
          steps.map(([ic, head, sub], i) => h('li', {},
            h('span', { class: 'xp-howto__icon', html: icon(ic) }),
            h('span', { class: 'xp-howto__text' }, h('small', {}, `Step ${i + 1}`), h('b', {}, head), h('span', {}, sub))))),
        h('div', { class: 'xp-hero__cta' },
          h('button', { type: 'button', class: 'xp-cta', onclick: () => this.scrollTo(this.picker) },
            'Try it now', h('span', { html: icon('down') })),
          h('a', { class: 'xp-cta xp-cta--ghost', href: `https://www.youtube.com/watch?v=${TEASER.id}`, target: '_blank', rel: 'noopener' },
            h('span', { html: icon('play') }), 'Watch on YouTube'))),
      this.renderTeaser());
  }

  /** Smooth scroll to a section, below the sticky header (scroll-margin-top in the stylesheet). */
  scrollTo(el, smooth = true) {
    const still = !smooth || matchMedia('(prefers-reduced-motion: reduce)').matches;
    el?.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'start' });
  }

  /** Below the demo: how answers are kept to what the viewer has seen, and where that is measured. */
  renderAbout() {
    const points = [
      ['lock', 'Time-gated retrieval, before the model',
        'When you ask, the server works out what the film has shown up to your paused moment. Lookups and search run ' +
        'only inside that set, so later scenes, lines and names are not part of what the model is given.'],
      ['shield', 'An output guard, after it',
        'Gemini may still know a well-known film from its training. Every sentence of an answer is checked for names ' +
        'the film has not revealed yet, terms from later in the plot and real people’s names before it is shown; a ' +
        'sentence that fails is dropped.'],
      ['chart', 'Measured, not promised',
        'Spoiler leaks, accuracy and speed are measured on a fixed set of questions at fixed positions in each film.'],
    ];
    return h('section', { class: 'xp-about', 'aria-labelledby': 'xp-about-h' },
      h('div', { class: 'xp-about__head' },
        h('div', {},
          h('p', { class: 'xp-eyebrow' }, 'How it works'),
          h('h2', { id: 'xp-about-h', class: 'xp-title' }, 'Locked before the model.', h('span', {}, ' Checked after it.'))),
        h('p', { class: 'xp-about__links' },
          h('a', { class: 'xp-cta', href: BENCH_URL, target: '_blank', rel: 'noopener' },
            h('span', { html: icon('chart') }), 'Benchmark scorecard'),
          h('a', { class: 'xp-cta xp-cta--ghost', href: '#architecture' }, 'Architecture'))),
      h('ul', { class: 'xp-points' }, points.map(([ic, head, body]) => h('li', {},
        h('span', { class: 'xp-points__icon', html: icon(ic) }),
        h('b', {}, head), h('span', {}, body)))));
  }

  /** Demo video. A local copy of its cover until clicked, so YouTube is only contacted when the viewer asks for it. */
  renderTeaser() {
    const frame = h('div', { class: 'xp-teaser__frame' });
    const play = () => {
      const src = `https://www.youtube-nocookie.com/embed/${TEASER.id}?autoplay=1&rel=0&modestbranding=1&playsinline=1`;
      frame.replaceChildren(h('iframe', {
        src, title: TEASER.title, allow: 'autoplay; encrypted-media; picture-in-picture; fullscreen',
        referrerpolicy: 'strict-origin-when-cross-origin',
      }));
      this.video?.pause();
    };
    frame.append(h('button', { type: 'button', class: 'xp-teaser__poster', onclick: play, 'aria-label': `Play video: ${TEASER.title} (YouTube)` },
      h('img', { src: new URL('../brand/teaser.jpg', import.meta.url).href, alt: '', decoding: 'async', fetchpriority: 'high' }),
      h('span', { class: 'xp-teaser__play', html: icon('play') })));
    return h('figure', { class: 'xp-teaser' }, frame,
      h('figcaption', { class: 'xp-teaser__cap' },
        h('b', {}, 'See it in action'), h('span', {}, 'A walkthrough of this demo · plays from YouTube')));
  }

  renderEmptyThread() {
    this.thread.replaceChildren(h('div', { class: 'xp-empty' },
      h('span', { class: 'xp-empty__icon', html: icon('pause') }),
      h('p', { class: 'xp-empty__title' }, this.s.emptyHead.trim().replace(/[.。]$/, '')),
      h('p', { class: 'xp-empty__body' }, this.s.emptyBody)));
  }

  // ---------- titles ----------

  async loadTitles() {
    try {
      const res = await api('/api/titles');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.titles = await res.json();
      if (!Array.isArray(this.titles) || !this.titles.length) throw new Error('no titles');
    } catch (err) {
      console.warn('Could not load titles', err);
      this.films.replaceChildren(h('div', { class: 'xp-notice', role: 'alert' },
        h('span', { html: icon('alert') }),
        h('div', {},
          h('strong', {}, this.s.listFailed[0]),
          h('p', {}, this.s.listFailed[1]),
          h('button', { type: 'button', class: 'xp-btn', onclick: () => this.loadTitles() }, this.s.tryAgain))));
      return;
    }
    this.renderFilms();
    const wanted = params.get('title');
    const match = wanted && this.titles.find((t) => t.id === wanted || t.title === wanted);
    const t0 = Number(params.get('t'));
    this.selectTitle(match || this.titles[0], {
      at: Number.isFinite(t0) && t0 > 0 ? t0 : null,
      ask: Boolean(match) && params.get('ask') === '1' && Boolean(params.get('q')),
    });
    // A deep link to a film or a question opens on the player, not on the introduction above it.
    if (match || params.get('q')) requestAnimationFrame(() => this.scrollTo(this.stage, false));
  }

  renderFilms() {
    const s = this.s;
    this.films.replaceChildren(...this.titles.map((t) => {
      const img = h('img', { src: frameUrl(t.id, t.poster_t ?? 60), alt: '', loading: 'lazy', decoding: 'async' });
      img.addEventListener('error', () => img.remove(), { once: true });
      const card = h('button', {
        type: 'button', class: 'xp-card', 'data-id': t.id, 'aria-pressed': 'false',
        onclick: () => this.selectTitle(t, { scroll: true }),
      },
        h('span', { class: 'xp-card__poster' }, img,
          t.duration_s ? h('span', { class: 'xp-card__dur' }, fmt(t.duration_s)) : null,
          h('span', { class: 'xp-card__now' }, s.nowPlaying)),
        h('span', { class: 'xp-card__body' },
          h('span', { class: 'xp-card__title' }, t.title),
          t.synopsis ? h('span', { class: 'xp-card__syn' }, t.synopsis) : null,
          h('span', { class: 'xp-card__tags' },
            t.license ? h('span', { class: 'xp-card__lic' }, String(t.license).replace(/-/g, ' ')) : null,
            t.playback === 'preview' ? h('span', { class: 'xp-badge xp-badge--preview' }, s.preview) : null)));
      return h('div', { class: 'xp-cards__item' }, card);
    }));
  }

  selectTitle(t, { at = null, ask = false, scroll = false } = {}) {
    if (!t) return;
    if (this.title && this.title.id === t.id) {
      if (scroll) this.scrollTo(this.stage);
      return;
    }
    if (scroll) this.scrollTo(this.stage);
    this.stopStream();
    this.cancelPick();
    this.clearTap();
    this.title = t;
    if (!this.sessions.has(t.id)) this.sessions.set(t.id, newId());
    this.lastAskP = null;
    this.at = at != null ? Math.min(at, t.duration_s || at) : null;
    for (const b of this.films.querySelectorAll('.xp-card')) b.setAttribute('aria-pressed', String(b.dataset.id === t.id));
    this.nowTitle.textContent = t.title;
    this.nowTags.replaceChildren(
      t.license ? h('span', { class: 'xp-card__lic' }, String(t.license).replace(/-/g, ' ')) : '',
      t.playback === 'preview' ? h('span', { class: 'xp-badge xp-badge--preview' }, this.s.preview) : '');
    this.player.classList.toggle('is-fresh', this.at == null);
    this.video.poster = frameUrl(t.id, this.at ?? t.poster_t ?? 60);
    // Until the video's metadata arrives (slow network), take the picture's shape from the poster frame, so the box
    // already has the film's aspect and a tap maps to the right point.
    this.posterDims = null;
    const im = new Image();
    im.onload = () => {
      if (this.title !== t || this.video.videoWidth) return;
      this.posterDims = [im.naturalWidth, im.naturalHeight];
      this.player.style.setProperty('--xp-ar', `${im.naturalWidth} / ${im.naturalHeight}`);
      this.layoutContent();
    };
    im.src = this.video.poster;
    this.video.src = `/api/titles/${encodeURIComponent(t.id)}/video`;
    if (this.at != null) {
      const v = this.video;
      const go = () => { v.currentTime = this.at; };
      if (v.readyState >= 1) go(); else v.addEventListener('loadedmetadata', go, { once: true });
    }
    // &ask=1: send right away. The question is asked at the deep link's position whether or not the video can play
    // here (position() reports `at` until the player has seeked), so it never waits on the video.
    if (ask) setTimeout(() => this.onSubmit(), 0);
    this.clearOverlay();
    this.renderEmptyThread();
    this.updateControls();
    this.updateLock();
    this.refreshGate(this.at ?? 0, true);
  }

  // ---------- player ----------

  /** The position questions are asked at: where the video is, or where a deep link is taking it. */
  position() {
    const v = this.video;
    return this.at != null && (v.readyState < 1 || v.seeking) ? this.at : (v.currentTime || 0);
  }

  bindVideo() {
    const v = this.video;
    v.addEventListener('loadedmetadata', () => {
      if (v.videoWidth && v.videoHeight) this.player.style.setProperty('--xp-ar', `${v.videoWidth} / ${v.videoHeight}`);
      this.scrub.max = String(v.duration || this.title?.duration_s || 1);
      this.layoutContent();
      this.updateControls();
    });
    v.addEventListener('timeupdate', () => {
      this.updateControls();
      if (!v.paused && !v.seeking && Math.abs(v.currentTime - (this.gateP ?? -1e9)) >= GATE_EVERY_S) {
        this.refreshGate(v.currentTime, true);
      }
    });
    v.addEventListener('play', () => {
      this.player.classList.remove('is-fresh');
      this.cancelPick();
      this.clearTap();
      this.clearOverlay();
      this.updateControls();
    });
    v.addEventListener('pause', () => { this.updateControls(); this.refreshGate(v.currentTime); });
    v.addEventListener('seeking', () => {
      if (this.tap && Math.abs(v.currentTime - this.tap.p) > 0.3) this.clearTap();
      if (this.hlP != null && Math.abs(v.currentTime - this.hlP) > 0.3) this.clearOverlay();
      this.updateControls();
    });
    v.addEventListener('seeked', () => { this.at = null; this.updateControls(); this.refreshGate(v.currentTime); });
    v.addEventListener('volumechange', () => this.updateControls());
    v.addEventListener('ended', () => this.updateControls());
    v.addEventListener('error', () => {
      if (!this.title || !v.getAttribute('src')) return;
      this.showHint(this.s.videoError, true);
    });
    new ResizeObserver(() => this.layoutContent()).observe(v);
    for (const ev of ['fullscreenchange', 'webkitfullscreenchange']) {
      document.addEventListener(ev, () => { this.updateControls(); this.layoutContent(); });
    }
  }

  /** Positions the overlay box on the visible picture (object-fit: contain leaves letterbox bars). */
  layoutContent() {
    const v = this.video;
    const w = v.clientWidth;
    const hh = v.clientHeight;
    const vw = v.videoWidth || this.posterDims?.[0] || 16;
    const vh = v.videoHeight || this.posterDims?.[1] || 9;
    const scale = Math.min(w / vw, hh / vh);
    const cw = vw * scale;
    const ch = vh * scale;
    Object.assign(this.content.style, {
      left: `${v.offsetLeft + (w - cw) / 2}px`, top: `${v.offsetTop + (hh - ch) / 2}px`,
      width: `${cw}px`, height: `${ch}px`,
    });
  }

  togglePlay() {
    const v = this.video;
    if (v.paused || v.ended) v.play().catch(() => {}); else v.pause();
  }

  toggleFullscreen() {
    const d = document;
    if (d.fullscreenElement || d.webkitFullscreenElement) {
      (d.exitFullscreen || d.webkitExitFullscreen).call(d)?.catch?.(() => {});
      return;
    }
    const el = this.player;
    const req = el.requestFullscreen || el.webkitRequestFullscreen;
    if (req) req.call(el)?.catch?.(() => {});
    else if (this.video.webkitEnterFullscreen) this.video.webkitEnterFullscreen();     // iPhone: native player only
  }

  onScrub() {
    this.scrubbing = true;
    const t = Number(this.scrub.value);
    this.video.currentTime = t;
    this.updateControls(t);
    this.refreshGate(t);
  }

  onPlayerKey(e) {
    if (e.target !== this.player && e.target !== this.tapLayer && e.target !== this.playBtn) return;
    if (this.pick) return;
    if (e.key === ' ' || e.key === 'k') { e.preventDefault(); this.togglePlay(); }
  }

  updateControls(at) {
    const v = this.video;
    const playing = !v.paused && !v.ended;
    const cur = at ?? this.position();
    const dur = v.duration || this.title?.duration_s || 0;
    const fs = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
    const key = `${playing}${v.muted}${fs}`;
    if (this.ctlKey !== key) {                         // icons only change with the state (timeupdate fires 4×/s)
      this.ctlKey = key;
      this.playBtn.innerHTML = icon(playing ? 'pause' : 'play', playing ? '' : 'xp-icon--fill');
      this.playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
      this.muteBtn.innerHTML = icon(v.muted ? 'mute' : 'volume');
      this.muteBtn.setAttribute('aria-label', v.muted ? 'Unmute' : 'Mute');
      this.fsBtn.innerHTML = icon(fs ? 'shrink' : 'expand');
      this.fsBtn.setAttribute('aria-label', fs ? 'Exit full screen' : 'Full screen');
    }
    this.timeEl.textContent = `${fmt(cur)} / ${fmt(dur)}`;
    if (!this.scrubbing) this.scrub.value = String(cur);
    this.scrub.setAttribute('aria-valuetext', `${fmt(cur)} of ${fmt(dur)}`);
    this.scrub.style.setProperty('--pct', `${dur ? Math.min(100, (cur / dur) * 100) : 0}%`);
    this.player.classList.toggle('is-paused', !playing);
    this.bigPlay.hidden = playing || !this.player.classList.contains('is-fresh');
    this.updateHint();
    if (!this.busy) this.updateLock();
  }

  updateHint() {
    const v = this.video;
    const show = this.title && v.paused && !this.pick && !this.busy && !this.tap && !this.bigPlayVisible()
      && this.position() > 0.5;
    if (!show) { if (!this.hint.classList.contains('is-warn')) this.hint.hidden = true; return; }
    if (!this.hint.hidden && !this.hint.classList.contains('is-warn')) return;
    this.showHint(this.s.hintTap);
  }

  bigPlayVisible() { return !this.bigPlay.hidden; }

  showHint(text, warn = false) {
    this.hint.hidden = false;
    this.hint.classList.toggle('is-warn', warn);
    this.hint.innerHTML = `${icon(warn ? 'alert' : 'tap')}<span></span>`;
    this.hint.lastChild.textContent = text;
  }

  updateLock(p) {
    const at = p ?? this.position();
    this.lockChip.innerHTML = `${icon('lock')}<span>${this.s.lock(fmt(at))}</span>`;
  }

  // ---------- the gate line ----------

  refreshGate(p, immediate = false) {
    clearTimeout(this.gateTimer);
    this.gateP = p;
    const run = async () => {
      const t = this.title;
      if (!t) return;
      const seq = ++this.gateSeq;
      try {
        const res = await api(`/api/titles/${encodeURIComponent(t.id)}/gate?p=${p.toFixed(1)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const g = await res.json();
        if (seq === this.gateSeq && this.title === t) this.renderGate(g);
      } catch (err) {
        console.warn('Gate request failed', err);
      }
    };
    if (immediate) run(); else this.gateTimer = setTimeout(run, 200);
  }

  renderGate(g, counts = true) {
    const s = this.s;
    this.gateData = g;
    const keys = new Set([...Object.keys(g.eligible || {}), ...Object.keys(g.total || {})]);
    let eligible = 0;
    let total = 0;
    for (const k of keys) {
      eligible += Number(g.eligible?.[k]) || 0;
      total += Number(g.total?.[k]) || 0;
    }
    if (counts) this.setGateCounts(eligible, Math.max(0, total - eligible));
    const rows = GATE_ROWS.filter((k) => keys.has(k)).map((k) => [k, s.gateRows[k]]);
    for (const k of keys) if (!GATE_ROWS.includes(k) && Number(g.total?.[k])) rows.push([k, s.types[k] || k]);
    this.gateRows.replaceChildren(
      ...rows.map(([k, label]) => {
        const e = Number(g.eligible?.[k]) || 0;
        const n = Number(g.total?.[k]) || 0;
        return h('div', { class: 'xp-gate-row' },
          h('span', { class: 'xp-gate-row__label' }, label),
          h('span', { class: 'xp-gate-row__bar' }, h('i', { style: `width:${n ? ((e / n) * 100).toFixed(1) : 0}%` })),
          h('span', { class: 'xp-gate-row__num' }, `${e} / ${n}`));
      }),
      g.characters_total != null
        ? h('p', { class: 'xp-gate__chars' }, s.gateChars(g.characters_known ?? 0, g.characters_total))
        : null,
      h('p', { class: 'xp-gate__note' }, s.gateNote(fmt(g.p))));
  }

  setGateCounts(eligible, locked) {
    const key = `${eligible}/${locked}`;
    if (this.gateShown === key) return;
    this.gateShown = key;
    this.gateCounts = [eligible, locked];
    this.gateText.innerHTML = eligible ? this.s.gateLine(num(eligible), num(locked)) : this.s.gateNothing(num(locked));
    this.gate.classList.remove('is-pulse');
    void this.gate.offsetWidth;
    this.gate.classList.add('is-pulse');
  }

  toggleGateRows() {
    const open = this.gateRows.hidden;
    this.gateRows.hidden = !open;
    this.gateMore.setAttribute('aria-expanded', String(open));
  }

  // ---------- tapping the picture ----------

  pointToTap(e) {
    const r = this.content.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) return null;
    return { x, y };
  }

  onPictureClick(e) {
    const v = this.video;
    if (this.pick) {
      const p = this.pointToTap(e);
      if (p) this.confirmPick(p); else this.nudge();
      return;
    }
    if (!this.title) return;
    if (!v.paused && !v.ended) { v.pause(); return; }
    if (this.bigPlayVisible()) { v.play().catch(() => {}); return; }
    const p = this.pointToTap(e);
    if (!p) { this.nudge(); return; }
    this.setTap(p);
  }

  nudge() {
    this.showHint(this.s.outside, true);
    this.live.textContent = this.s.outsideLive;
    clearTimeout(this.nudgeTimer);
    this.nudgeTimer = setTimeout(() => { this.hint.classList.remove('is-warn'); this.updateHint(); }, 1600);
  }

  setTap(p) {
    this.tap = { x: p.x, y: p.y, p: this.position() };
    this.clearOverlay();
    this.drawRing(p);
    this.tapNote.hidden = false;
    for (const c of this.chips) c.classList.toggle('is-ready', c.dataset.key === 'who' || c.dataset.key === 'what');
    this.live.textContent = this.s.pointSet;
    this.updateHint();
  }

  clearTap() {
    this.tap = null;
    this.tapNote.hidden = true;
    for (const c of this.chips) c.classList.remove('is-ready');
    this.ring.hidden = true;
    this.updateHint();
  }

  /** The tap to send with a question at p: only if it was made at that moment. */
  tapAt(p) { return this.tap && Math.abs(this.tap.p - p) < 0.3 ? { x: this.tap.x, y: this.tap.y } : null; }

  startPick(q) {
    this.video.pause();
    this.clearOverlay();
    this.pick = { question: q.question, x: 0.5, y: 0.5 };
    this.player.classList.add('is-picking');
    this.pickBar.querySelector('.xp-pickbar__msg').innerHTML = `${icon('tap')}<span><b>${q.pick}</b></span>`;
    this.moveCross(0.5, 0.5, false);
    for (const c of this.chips) c.classList.toggle('is-active', c.dataset.key === q.key);
    this.updateHint();
    this.tapLayer.focus({ preventScroll: true });
    const r = this.player.getBoundingClientRect();
    if (r.top < 0 || r.bottom > innerHeight) this.player.scrollIntoView({ behavior: 'smooth', block: 'center' });
    this.live.textContent = this.s.pickLive(q.pick);
  }

  cancelPick() {
    if (!this.pick) return;
    this.pick = null;
    this.player.classList.remove('is-picking', 'is-keyboard', 'is-outside');
    for (const c of this.chips) c.classList.remove('is-active');
    this.updateHint();
  }

  moveCross(x, y, keyboard) {
    this.pick.x = Math.min(1, Math.max(0, x));
    this.pick.y = Math.min(1, Math.max(0, y));
    this.cross.style.left = `${this.pick.x * 100}%`;
    this.cross.style.top = `${this.pick.y * 100}%`;
    this.player.classList.toggle('is-keyboard', keyboard);
  }

  onPickHover(e) {
    if (!this.pick || e.pointerType === 'touch') return;
    const p = this.pointToTap(e);
    this.player.classList.toggle('is-outside', !p);
    if (p) this.moveCross(p.x, p.y, false);
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
    if (tap) this.setTap(tap);
    this.ask(question, tap);
  }

  drawRing(tap) {
    this.ring.hidden = false;
    this.ring.style.left = `${tap.x * 100}%`;
    this.ring.style.top = `${tap.y * 100}%`;
    this.ring.classList.remove('is-in');
    void this.ring.offsetWidth;
    this.ring.classList.add('is-in');
  }

  /** Draws the server's box, only while the player still shows the moment it belongs to. */
  drawHighlight(hl, p) {
    const b = hl?.bbox;
    const v = this.video;
    if (!Array.isArray(b) || b.length !== 4 || !v.paused || Math.abs(this.position() - p) > 0.6) return;
    const [ymin, xmin, ymax, xmax] = b.map(Number);
    Object.assign(this.hl.style, {
      top: `${ymin / 10}%`, left: `${xmin / 10}%`,
      height: `${Math.max(0, ymax - ymin) / 10}%`, width: `${Math.max(0, xmax - xmin) / 10}%`,
    });
    const label = this.hl.firstChild;
    label.textContent = hl.label || '';
    label.hidden = !hl.label;
    this.hl.classList.toggle('is-low', ymin < 80);
    if (this.hl.hidden || this.hlKey !== b.join()) {
      this.hl.hidden = false;
      this.hl.classList.remove('is-in');
      void this.hl.offsetWidth;
      this.hl.classList.add('is-in');
    }
    this.hlKey = b.join();
    this.hlP = p;
  }

  clearHighlight() {
    this.hl.hidden = true;
    this.hlP = null;
    this.hlKey = null;
  }

  clearOverlay() {
    this.clearHighlight();
    if (!this.tap) this.ring.hidden = true;
  }

  // ---------- asking ----------

  setLang(code) {
    this.lang = code;
    for (const b of this.langBtns) b.setAttribute('aria-pressed', String(b.dataset.lang === code));
    try { localStorage.setItem(LANG_KEY, code); } catch { /* storage unavailable */ }
    this.applyLang();
  }

  onQuick(q) {
    if (this.busy || !this.title) return;
    const zh = this.lang === 'zh';
    const question = zh ? q.zh : q.label;
    if (!q.pick) { this.ask(question, null); return; }
    const tap = this.tapAt(this.position());
    if (tap) this.ask(question, tap);
    else this.startPick({ ...q, question, pick: zh ? q.pickZh : q.pick });
  }

  onSubmit(e) {
    e?.preventDefault();
    if (this.busy) { this.stopStream(); return; }
    const q = this.input.value.trim();
    if (!q || !this.title) return;
    this.cancelPick();
    if (this.ask(q, this.tapAt(this.position()))) this.input.value = '';
  }

  setBusy(on) {
    this.busy = on;
    this.root.classList.toggle('is-busy', on);
    for (const c of this.chips) c.disabled = on;
    this.input.disabled = on;
    this.sendBtn.innerHTML = icon(on ? 'stop' : 'send');
    this.sendBtn.setAttribute('aria-label', on ? this.s.stop : this.s.ask);
    this.sendBtn.classList.toggle('is-stop', on);
    this.updateHint();
  }

  stopStream() {
    if (this.abort) this.abort.abort();
    this.abort = null;
  }

  /** Starts a question. Returns false if it was not sent (busy, or waiting out the per-minute limit). */
  ask(question, tap) {
    if (this.busy || !this.title) return false;
    if (Date.now() < this.cooldownUntil && this.cooldownNotice?.isConnected) {
      this.cooldownNotice.classList.remove('is-shake');
      void this.cooldownNotice.offsetWidth;
      this.cooldownNotice.classList.add('is-shake');
      return false;
    }
    this.run(question, tap);
    return true;
  }

  async run(question, tap) {
    const v = this.video;
    v.pause();
    const p = Math.round(this.position() * 10) / 10;
    const title = this.title;
    const lang = this.lang;

    this.thread.querySelector('.xp-empty')?.remove();
    if (this.lastAskP != null && p < this.lastAskP - 0.5) {
      for (const turn of this.thread.querySelectorAll('.xp-turn')) turn.classList.add('is-stale');
      this.thread.prepend(h('div', { class: 'xp-divider', role: 'note' },
        h('span', { html: icon('rewind') }),
        h('span', {}, this.s.wentBack(fmt(p)))));
    }
    this.lastAskP = p;
    this.updateLock(p);

    const turn = new Turn(this, { question, tap, p, title, lang });
    this.thread.prepend(turn.el);
    this.thread.scrollTop = 0;
    this.revealTurn(turn.el);
    this.setBusy(true);
    const ctrl = new AbortController();
    this.abort = ctrl;
    const started = performance.now();

    try {
      const res = await api('/api/ask', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
        body: JSON.stringify({
          title_id: title.id, p, question, lang, session_id: this.sessions.get(title.id),
          ...(tap ? { tap: { x: +tap.x.toFixed(4), y: +tap.y.toFixed(4) } } : {}),
        }),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        let body = {};
        try { body = await res.json(); } catch { /* not JSON */ }
        if (res.status === 429) this.limited(turn, body, Number(res.headers.get('retry-after')));
        else turn.fail(turn.s.couldNot, body.message || body.detail || turn.s.http(res.status));
        return;
      }
      await readSSE(res, (event, data) => {
        if (this.title !== title) return;
        turn.onEvent(event, data, performance.now() - started);
        if (event === 'gate') this.setGateCounts(Number(data.eligible_total) || 0, Number(data.locked_total) || 0);
        else if (event === 'highlight') this.drawHighlight(data, p);
        else if (event === 'answer') {
          // The answer's box is the truthful one: null when the text is not about the tapped entity.
          if (data.highlight) this.drawHighlight(data.highlight, p);
          else if (this.hlP === p) this.clearHighlight();
          this.live.textContent = `${turn.s.answerLive}${data.text || ''}`;
        }
      });
      turn.finish();
    } catch (err) {
      if (err.name === 'AbortError') turn.stopped();
      else {
        console.warn('Ask failed', err);
        turn.dropped();
      }
    } finally {
      if (this.abort === ctrl) this.abort = null;
      this.setBusy(false);
    }
  }

  /** HTTP 429: say which limit was hit; for the per-minute one, count down to when asking works again. */
  limited(turn, body, retryAfter) {
    const wait = Math.max(1, Math.round(Number(body.retry_after_s) || retryAfter || 60));
    if (body.limit === 'per_minute') {
      this.cooldownUntil = Date.now() + wait * 1000;
      this.cooldownNotice = turn.countdown(this.cooldownUntil);
    } else if (body.limit === 'daily' && body.scope === 'total') {
      turn.fail(...turn.s.budgetTotal, 'slow', false);
    } else if (body.limit === 'daily') {
      turn.fail(...turn.s.budgetIp, 'slow', false);
    } else {
      turn.fail(turn.s.tooMany[0], turn.lang === 'en' && body.message ? body.message : turn.s.tooMany[1], 'slow');
    }
  }

  /** On narrow layouts the panel is below the player: make sure the new turn's top is on screen. */
  revealTurn(el) {
    requestAnimationFrame(() => {
      const r = el.getBoundingClientRect();
      if (r.top > innerHeight - 140) window.scrollBy({ top: r.top - innerHeight + 220, behavior: 'smooth' });
    });
  }

  retry(question, tap, p) {
    if (this.busy) return;
    if (Math.abs(this.position() - p) > 0.5) this.video.currentTime = p;
    if (tap) this.setTap(tap);
    this.ask(question, tap);
  }

  rewindTo(t) {
    this.video.pause();
    this.video.currentTime = t;
    this.player.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function frameUrl(id, t) { return `/api/titles/${encodeURIComponent(id)}/frames/${Math.floor(Number(t) || 0)}.jpg`; }

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

/** One question: the step trace (collapsed to one line once text arrives), the streamed answer, its sources. */
class Turn {
  constructor(app, { question, tap, p, title, lang }) {
    this.app = app;
    this.q = { question, tap, p };
    this.title = title;
    this.lang = lang;
    this.s = strings(lang);
    const s = this.s;
    this.steps = new Map();
    this.nSteps = 0;
    this.lookups = 0;
    this.streamed = '';
    this.answered = false;
    this.failed = false;

    this.stepList = h('ol', { class: 'xp-steps', 'aria-label': s.steps });
    this.traceLabel = h('span', { class: 'xp-trace__label' }, s.locking);
    this.trace = h('details', { class: 'xp-trace is-working', open: true },
      h('summary', {}, h('i', { class: 'xp-spinner', 'aria-hidden': 'true' }), this.traceLabel),
      this.stepList);
    this.text = h('p', { class: 'xp-answer__text', lang: lang === 'zh' ? 'zh' : 'en' });
    this.card = h('div', { class: 'xp-answer is-streaming', hidden: true }, this.text);
    this.slot = h('div', { class: 'xp-turn__notes' });
    this.el = h('article', { class: 'xp-turn', lang: lang === 'zh' ? 'zh-CN' : 'en' },
      h('div', { class: 'xp-q' },
        h('p', { class: 'xp-q__text' }, question),
        h('span', { class: 'xp-q__meta' },
          h('span', { class: 'xp-tag', html: `${icon('lock')}<span>${fmt(p)}</span>` }),
          tap ? h('span', { class: 'xp-tag', html: `${icon('tap')}<span>${s.tapped}</span>` }) : null)),
      this.trace, this.card, this.slot);
  }

  addStep({ id, iconName, label, summary, pending }) {
    const li = h('li', { class: `xp-step${pending ? ' is-pending' : ''}` },
      h('span', { class: 'xp-step__icon', html: icon(iconName) }),
      h('span', { class: 'xp-step__body' },
        h('span', { class: 'xp-step__label' }, label),
        h('span', { class: 'xp-step__summary' }, summary || '')),
      h('span', { class: 'xp-step__ms' }));
    this.stepList.append(li);
    this.nSteps += 1;
    if (id) this.steps.set(id, { li, start: performance.now() });
    return li;
  }

  onEvent(event, data, elapsed) {
    const s = this.s;
    switch (event) {
      case 'gate':
        this.addStep({
          iconName: 'shield', label: s.gateStep(fmt(data.p ?? this.q.p)),
          summary: s.gateSummary(num(data.eligible_total), num(data.locked_total)),
        }).classList.add('xp-step--gate');
        this.setLabel(s.lookingUp);
        break;
      case 'tool': {
        const id = data.id || `s${this.nSteps}`;
        this.addStep({ id, iconName: toolIcon(data.name), label: `${data.label || data.name || s.working}…`, pending: true });
        this.lookups += 1;
        this.setLabel(`${data.label || data.name || s.working}…`);
        break;
      }
      case 'tool_result': {
        const st = this.steps.get(data.id);
        const li = st ? st.li : this.addStep({ iconName: toolIcon(data.name), label: data.name || s.step });
        li.classList.remove('is-pending');
        const label = li.querySelector('.xp-step__label');
        label.textContent = label.textContent.replace(/…$/, '');
        li.querySelector('.xp-step__summary').textContent = data.summary || s.done;
        const ms = st ? performance.now() - st.start : 0;
        li.querySelector('.xp-step__ms').textContent = ms >= 50 ? s.secs((ms / 1000).toFixed(1)) : '';
        if (!this.streamed && !this.pending()) this.setLabel(s.writing);
        break;
      }
      case 'delta':
        this.addText(data.text || '');
        break;
      case 'answer':
        this.answered = true;
        this.finalise(data, elapsed);
        break;
      case 'error':
        if (this.streamed) this.cutShort(...s.cutError);
        else this.fail(s.problem[0], this.lang === 'en' && data.message ? data.message : s.problem[1]);
        break;
      default:
        break;
    }
    this.syncTrace();
  }

  pending() { return [...this.steps.values()].some((x) => x.li.classList.contains('is-pending')); }

  setLabel(text) { this.traceLabel.textContent = text; }

  /** Once the answer starts, the trace folds into one line so it never pushes the answer down. */
  syncTrace() {
    const working = !this.answered && !this.failed && (this.pending() || !this.streamed);
    this.trace.classList.toggle('is-working', working);
    if (this.streamed || this.answered || this.failed) {
      if (!this.folded) { this.trace.open = false; this.folded = true; }
      const busy = [...this.steps.values()].filter((x) => x.li.classList.contains('is-pending')).pop();
      this.setLabel(busy ? busy.li.querySelector('.xp-step__label').textContent
        : this.s.traceDone(fmt(this.q.p), this.s.lookups(this.lookups)));
    }
  }

  addText(text) {
    if (!text) return;
    this.card.hidden = false;
    this.streamed += text;
    this.text.append(h('span', { class: 'xp-delta' }, text));
  }

  finalise(a, elapsed) {
    const app = this.app;
    const s = this.s;
    const tid = encodeURIComponent(this.title.id);
    this.card.hidden = false;
    this.card.classList.remove('is-streaming');
    if ((a.text || '') !== this.streamed) this.text.textContent = a.text || '';

    const ev = (a.evidence || []).filter((e) => e && e.start_s != null);
    if (ev.length) {
      const quote = h('blockquote', { class: 'xp-evidence__quote', hidden: true });
      const chips = ev.map((e) => h('button', {
        type: 'button', class: 'xp-ev', 'aria-pressed': 'false', 'aria-label': s.evAt(s.types[e.type] || e.type, fmt(e.start_s)),
        onclick: (evt) => {
          const btn = evt.currentTarget;
          const on = btn.getAttribute('aria-pressed') !== 'true';
          for (const c of chips) c.setAttribute('aria-pressed', 'false');
          btn.setAttribute('aria-pressed', String(on));
          quote.hidden = !on || !e.text;
          quote.textContent = e.text || '';
        },
      }, h('span', { class: 'xp-ev__t' }, fmt(e.start_s)), h('span', { class: 'xp-ev__type' }, s.types[e.type] || e.type)));
      this.card.append(h('div', { class: 'xp-evidence' },
        h('span', { class: 'xp-evidence__h' }, s.basedOn),
        h('div', { class: 'xp-evidence__chips' }, chips), quote));
    }

    const frames = (a.frames || []).filter((f) => f && f.t != null);
    if (frames.length) {
      this.card.append(h('div', { class: 'xp-frames' }, frames.map((f) => h('button', {
        type: 'button', class: 'xp-frame', title: s.rewindTo(fmt(f.t)),
        'aria-label': s.frameAria(f.caption || fmt(f.t), fmt(f.t)),
        onclick: () => app.rewindTo(f.t),
      },
        h('img', { src: `/api/titles/${tid}/frames/${Math.floor(f.t)}.jpg`, alt: '', loading: 'lazy' }),
        h('span', { class: 'xp-frame__cap' }, h('span', { html: icon('rewind') }), h('span', {}, f.caption || fmt(f.t)))))));
    }

    const guard = a.guard || {};
    const dropped = Number(guard.dropped) || 0;
    const guardEl = !guard.checked
      ? h('span', { class: 'xp-badge', html: `${icon('shield')}<span>${s.guardNotRun}</span>` })
      : guard.rewritten
        ? h('span', {
          class: 'xp-badge xp-badge--rewrite', title: s.guardCaught,
          html: `${icon('shield')}<span>${dropped ? s.guardRemoved(dropped) : s.guardReplaced}</span>`,
        })
        : h('span', {
          class: 'xp-badge xp-badge--ok', title: s.guardOkTitle,
          html: `${icon('shield')}<span>${s.guardOk}</span>`,
        });
    const t = a.timing || {};
    const total = t.total_ms ?? a.latency_ms ?? elapsed;
    const timing = t.first_text_ms != null && t.first_text_ms < total - 150
      ? s.timing((t.first_text_ms / 1000).toFixed(1), (total / 1000).toFixed(1))
      : s.secs((total / 1000).toFixed(1));
    if (a.cut_off) this.card.classList.add('is-cut');
    this.card.append(h('div', { class: 'xp-answer__foot' }, guardEl,
      a.cut_off ? h('span', {
        class: 'xp-badge xp-badge--rewrite', title: s.cutTitle,
        html: `${icon('clock')}<span>${s.cutBadge}</span>`,
      }) : null,
      total != null ? h('span', { class: 'xp-badge', html: `${icon('clock')}<span>${timing}</span>` }) : null));
  }

  notice(kind, title, detail, retry = true) {
    const { question, tap, p } = this.q;
    const box = h('div', { class: `xp-notice xp-notice--${kind}`, role: kind === 'muted' ? null : 'alert' },
      h('span', { html: icon(kind === 'slow' ? 'clock' : kind === 'muted' ? 'stop' : 'alert') }),
      h('div', {},
        h('strong', {}, title),
        detail ? h('p', {}, detail) : null,
        retry ? h('button', { type: 'button', class: 'xp-btn', onclick: () => this.app.retry(question, tap, p) }, this.s.askAgain) : null));
    this.slot.append(box);
    return box;
  }

  fail(title, detail, kind = 'error', retry = true) {
    if (this.failed || this.answered) return null;
    this.failed = true;
    for (const s of this.steps.values()) s.li.classList.remove('is-pending');
    this.syncTrace();
    return this.notice(kind, title, detail, retry);
  }

  /** Text already shown stays; say that it is incomplete. */
  cutShort(title, detail, kind = 'error') {
    if (this.failed || this.answered) return;
    this.failed = true;
    this.card.classList.remove('is-streaming');
    this.card.classList.add('is-cut');
    for (const s of this.steps.values()) s.li.classList.remove('is-pending');
    this.syncTrace();
    this.notice(kind, title, detail);
  }

  stopped() {
    if (this.streamed) this.cutShort(...this.s.stoppedCut, 'muted');
    else this.fail(...this.s.stopped, 'muted');
  }

  dropped() {
    if (this.streamed) this.cutShort(...this.s.droppedCut);
    else this.fail(...this.s.dropped);
  }

  /** Per-minute limit: a live countdown, then the retry button. */
  countdown(until) {
    const box = this.fail(this.s.pause, ' ', 'slow', false);
    if (!box) return null;
    const p = box.querySelector('p');
    const tick = () => {
      const left = Math.ceil((until - Date.now()) / 1000);
      if (left > 0 && box.isConnected) {
        p.textContent = this.s.askIn(fmt(left).replace(/^00:/, '0:'));
        setTimeout(tick, 1000 - (Date.now() % 1000) + 5);
        return;
      }
      p.textContent = this.s.askNow;
      const { question, tap, p: at } = this.q;
      if (box.isConnected && !box.querySelector('button')) {
        box.lastChild.append(h('button', { type: 'button', class: 'xp-btn', onclick: () => this.app.retry(question, tap, at) }, this.s.askAgain));
      }
    };
    tick();
    return box;
  }

  finish() {
    if (this.answered || this.failed) return;
    if (this.streamed) this.cutShort(...this.s.endedCut);
    else this.fail(...this.s.ended);
  }
}
