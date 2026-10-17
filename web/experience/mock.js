// Offline stand-in for the JSON / SSE endpoints in docs/web-api.md, enabled with ?mock=1.
// Video and frame URLs are not mocked: they still go to the real paths.
// Every answer here is placeholder text; nothing describes the films' stories.
// Add "#429" to a question to simulate rate limiting, "#error" for a failed stream, "#rewrite" for a guarded answer.

const TITLES = [
  {
    id: 'c4c02a81-ae5c-4a91-aef1-eea196f91c17', title: 'Sintel', duration_s: 888, story_start_s: 18, story_end_s: 840,
    poster_t: 120, license: 'CC-BY-3.0', attribution: 'Sintel (c) Blender Foundation | durian.blender.org',
    synopsis: 'A lone traveller crosses snowfields and markets on a personal quest.',
  },
  {
    id: '9e9cbbc5-02d4-46b1-a1b3-f1a47e7e50fe', title: 'Tears of Steel', duration_s: 734, story_start_s: 10,
    story_end_s: 640, poster_t: 200, license: 'CC-BY-3.0',
    attribution: 'Tears of Steel (c) Blender Foundation | mango.blender.org',
    synopsis: 'A small team in a future Amsterdam tries something risky to set things right.',
  },
  {
    id: '79400f81-eef7-4ebf-ad77-7aea38346b20', title: 'Elephants Dream', duration_s: 654, story_start_s: 8,
    story_end_s: 560, poster_t: 90, license: 'CC-BY-2.5',
    attribution: 'Elephants Dream (c) 2006 Blender Foundation / Netherlands Media Art Institute | orange.blender.org',
    synopsis: 'Two travellers explore a strange, endless machine and see it very differently.',
  },
];

const TOTALS = { scene: 22, event: 61, cue: 104, recap: 10, shot_caption: 175, character_fact: 18, object: 14 };
const CHARACTERS_TOTAL = 7;

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const id = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(id); reject(new DOMException('Aborted', 'AbortError')); });
});

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function gate(title, p) {
  const span = Math.max(1, title.story_end_s - title.story_start_s);
  const frac = Math.min(1, Math.max(0, (p - title.story_start_s) / span));
  const eligible = {};
  for (const [k, n] of Object.entries(TOTALS)) eligible[k] = Math.floor(n * frac ** (k === 'recap' ? 1.2 : 0.95));
  return {
    p, eligible, total: { ...TOTALS },
    characters_known: Math.round(CHARACTERS_TOTAL * Math.min(1, frac * 1.6)), characters_total: CHARACTERS_TOTAL,
  };
}

function plan(question, tap) {
  const q = question.toLowerCase();
  if (tap) {
    return [
      ['current_shot', 'Looking at the paused frame', 'Shot 41 · 2 people on screen · tap matches the one on the left'],
      ['character_card', 'Checking what the film has revealed about them', 'Named on screen earlier · 3 facts unlocked'],
    ];
  }
  if (q.includes('miss')) {
    return [
      ['story_so_far', 'Reading the story so far', '1 recap + 6 events, up to the paused moment'],
      ['search_moments', 'Searching unlocked moments', '5 matches from scenes and events'],
    ];
  }
  if (q.includes('say') || q.includes('said')) {
    return [['recent_dialogue', 'Listening to the last lines', '3 lines in the last 30 s · 2 speakers']];
  }
  return [
    ['search_moments', 'Searching unlocked moments', '5 matches from scenes, shots and objects'],
    ['near_window', 'Re-watching the last 45 seconds', '45 s clip ending at the paused moment'],
  ];
}

function sse(write) {
  const encoder = new TextEncoder();
  return (event, data) => write(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
}

function ask(body, signal) {
  const title = TITLES.find((t) => t.id === body.title_id) || TITLES[0];
  const p = Number(body.p) || 0;
  const question = String(body.question || '');
  if (question.includes('#429')) return json({ message: 'Too many questions from this address. Try again in a minute.' }, 429);

  const stream = new ReadableStream({
    async start(controller) {
      const send = sse((chunk) => controller.enqueue(chunk));
      const started = performance.now();
      try {
        const g = gate(title, p);
        const eligible = Object.values(g.eligible).reduce((a, b) => a + b, 0);
        const total = Object.values(g.total).reduce((a, b) => a + b, 0);
        await sleep(250, signal);
        send('gate', { p, eligible_total: eligible, locked_total: total - eligible });
        if (question.includes('#error')) {
          await sleep(600, signal);
          send('error', { message: 'The model did not respond in time.' });
          send('done', {});
          controller.close();
          return;
        }
        let i = 0;
        for (const [name, label, summary] of plan(question, body.tap)) {
          const id = `t${++i}`;
          send('tool', { id, name, label, args: { p } });
          await sleep(500 + Math.random() * 700, signal);
          send('tool_result', { id, name, summary });
        }
        await sleep(700, signal);
        const at = Math.max(0, Math.floor(p));
        const back = (s) => Math.max(0, at - s);
        const zh = body.lang === 'zh';
        const text = zh
          ? `(Mock answer in Chinese) Placeholder text for ${fmt(p)}; the real service writes this answer in Chinese.`
          : `(Mock answer) This is placeholder text for ${fmt(p)}. The real service answers only from what the film has shown before this moment.`;
        send('answer', {
          text,
          evidence: [
            { type: 'scene', start_s: back(60), end_s: back(38), text: 'Placeholder scene summary.' },
            { type: 'event', start_s: back(25), end_s: back(20), text: 'Placeholder event.' },
            { type: 'line', start_s: back(9), end_s: back(7), text: 'Placeholder line of dialogue.' },
          ],
          frames: [back(52), back(22), back(4)].map((t) => ({ t, caption: `Earlier: ${fmt(t)}` })),
          highlight: body.tap
            ? {
                bbox: box(body.tap.y * 1000, body.tap.x * 1000),
                frame_t: at,
                label: question.toLowerCase().includes('what') ? 'the object' : 'the person you tapped',
              }
            : null,
          guard: { checked: true, rewritten: question.includes('#rewrite') },
          latency_ms: Math.round(performance.now() - started),
        });
        send('done', {});
        controller.close();
      } catch (err) {
        controller.error(err);
      }
    },
  });
  return new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

function box(cy, cx) {
  const clamp = (v) => Math.round(Math.min(1000, Math.max(0, v)));
  return [clamp(cy - 260), clamp(cx - 110), clamp(cy + 260), clamp(cx + 110)];
}

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

/** Same shape as fetch(); handles the JSON / SSE endpoints and defers everything else to the network. */
export async function mockFetch(input, init = {}) {
  const url = new URL(typeof input === 'string' ? input : input.url, location.href);
  const path = url.pathname;
  if (path === '/api/titles') { await sleep(200); return json(TITLES); }
  const g = path.match(/^\/api\/titles\/([^/]+)\/gate$/);
  if (g) {
    const title = TITLES.find((t) => t.id === g[1]);
    if (!title) return json({ message: 'Unknown title' }, 404);
    await sleep(120);
    return json(gate(title, Number(url.searchParams.get('p')) || 0));
  }
  if (path === '/api/ask' && (init.method || 'GET').toUpperCase() === 'POST') {
    return ask(JSON.parse(init.body || '{}'), init.signal);
  }
  return fetch(input, init);
}
