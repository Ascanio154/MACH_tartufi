// Read our 5 agents' logs fast from the team console API.
// Use: open https://projectnandaitaly.ai/team (signed in), open DevTools console,
// paste this file, then e.g.:
//   await logs()                         // last 40 lines of every agent
//   await logs('booking', 'confirm')     // booking centre lines matching "confirm" (server-side filter)
//   await logs('all', 'N-2026-11-02-890923000053')   // one citizen's need across all agents
//   await journey('N-2026-11-02-890923000053')       // the journey trace + verdict
// The server filters with ?q=, so a search is much lighter than loading the whole log.

const AGENTS = {
  booking: 'agent:h3n6kfd18r3g',
  gp: 'agent:bmrcsmqq6at2',
  hospital: 'agent:xtj1dk9x3qej',
  specialist: 'agent:q18es2d82f8r',
  consultorio: 'agent:pxvd2kk2yx5j',
};

async function _get(path, ms = 60000) {
  const c = new AbortController();
  const timer = setTimeout(() => c.abort(), ms);
  try {
    const r = await fetch(path, { credentials: 'include', signal: c.signal });
    const text = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${text.slice(0, 200)}`);
    try { return JSON.parse(text); } catch { return text; }
  } finally { clearTimeout(timer); }
}

function _rows(body) {
  if (Array.isArray(body)) return body;
  if (body && typeof body === 'object') {
    for (const k of ['entries', 'logs', 'lines', 'items', 'records', 'data']) {
      if (Array.isArray(body[k])) return body[k];
    }
  }
  return typeof body === 'string' ? body.split('\n') : [body];
}

// Response: {lines: [{time, level, message, agent_id}]}, NEWEST FIRST, only the last ~150 lines
// (~15 s of a busy agent). Lines carry no message text: use them for errors/liveness;
// use journey() for what was actually said.
const _NOISE = /GET \/health|HTTP Request: POST https:\/\/relay|^relay (ack|renew|history)$|^final ack sent$/;

function _line(e) {
  if (typeof e === 'string') return e;
  const at = e.time || e.ts || e.at || '';
  const msg = e.message || e.msg || e.text || JSON.stringify(e);
  return `${String(at).slice(11, 19)} ${e.level || ''} ${msg}`.trim();
}

async function logs(who = 'all', q = '', tail = 40, { noise = false } = {}) {
  const names = who === 'all' ? Object.keys(AGENTS) : [who];
  const out = await Promise.all(names.map(async (name) => {
    const params = new URLSearchParams({ agent_id: AGENTS[name] || name });
    // Lines carry need_id/msg_id: filter citizens locally (server-side q= is very slow).
    const byNeed = /^N-\d{4}-/.test(q);
    if (q && !byNeed) params.set('q', q);
    try {
      let all = _rows(await _get(`/v1/logs?${params}`, 180000));
      if (byNeed) all = all.filter((e) => e.need_id === q);
      const rows = noise ? all : all.filter((e) => !_NOISE.test(e.message || ''));
      const bad = all.filter((e) => /error|warn|critical/i.test(e.level || '') || /Traceback|Error|Exception/.test(e.message || ''));
      const runs = all.filter((e) => e.message === 'brain done').length;
      const span = all.length ? `${String(all.at(-1).time).slice(11, 19)}..${String(all[0].time).slice(11, 19)}` : '';
      return `== ${name}: ${all.length} lines ${span}, ${runs} brain runs, ${bad.length} errors${q ? `, q=${q}` : ''}\n`
        + rows.slice(0, tail).reverse().map(_line).join('\n');
    } catch (err) {
      return `== ${name}: ${err}`;
    }
  }));
  const text = out.join('\n\n');
  console.log(text);
  return text;
}

async function journey(needId) {
  const body = await _get(`/v1/team/journeys/${encodeURIComponent(needId)}`);
  console.log(body);
  return body;
}
