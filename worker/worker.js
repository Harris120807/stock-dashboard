// Stock Dashboard API proxy — Cloudflare Worker
//
// Sits between the public dashboard page and Finnhub so that:
//  - the Finnhub API key lives server-side (never in the page source)
//  - responses are edge-cached per symbol/query, so upstream usage is bounded
//    by ticker count × cache windows, not by visitor count — no user-facing
//    rate limits needed
//
// Routes (GET):
//   /quote?symbol=X    -> finnhub /quote        (cache 60s)
//   /metric?symbol=X   -> finnhub /stock/metric (cache 10 min; metric=all forced)
//   /search?q=...      -> finnhub /search       (cache 24h)
//
// Public read-only API (scores are this dashboard's own derived metrics —
// raw vendor market data is deliberately NOT re-served here):
//   /api                 -> endpoint docs
//   /api/scores          -> all covered stocks: scores, position, watchlist flags
//   /api/scores/:ticker  -> one stock incl. score breakdown + daily score history
//   /api/watchlist       -> current buy/sell watchlist
// Backed by the claude/state JSON files, edged-cached ~5 min.
//
// Admin API (2026-07-24, owner-only — Bearer ADMIN_KEY, responses never cached):
//   GET  /admin/ping        -> key check for the admin page login
//   GET  /admin/config      -> kill-switch flags   POST -> update (KV `CONFIG`)
//   POST /admin/add-tickers -> dispatch add-tickers.yml with the form input
//   GET  /admin/stats?days= -> traffic from Analytics Engine (needs Worker
//                              secret CF_ANALYTICS_TOKEN with Account Analytics:Read)
// Every request logs an anonymous data point (route group + country) to the
// Analytics Engine dataset `stockdash_traffic` (binding TRAFFIC).
// Kill-switches: livePrices gates /prices, fullRefresh gates /refresh,
// stockRefresh gates /quote + /metric (/search stays open — the requests page
// depends on it). Flags live in KV (binding CONFIG, key `flags`), read with a
// 60s cache — a toggle takes effect within ~a minute everywhere.

const STATE_RAW = 'https://raw.githubusercontent.com/Harris120807/stock-dashboard/claude/state/';
const API_TTL = 300;
const SITE = 'https://valuetally.com/';

const TTL = { quote: 60, metric: 600, search: 86400 };
const ALLOWED = { quote: ['symbol'], metric: ['symbol'], search: ['q'] };
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': '*',
  'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
};

function json(obj, status, ttl) {
  const cc = ttl === 0 ? 'no-store' : 'public, max-age=' + (ttl || API_TTL);
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': cc, ...CORS },
  });
}

const DEFAULT_FLAGS = { livePrices: true, fullRefresh: true, stockRefresh: true };

async function getFlags(env) {
  try {
    const raw = await env.CONFIG?.get('flags', { cacheTtl: 60 });
    return raw ? { ...DEFAULT_FLAGS, ...JSON.parse(raw) } : { ...DEFAULT_FLAGS };
  } catch (e) {
    return { ...DEFAULT_FLAGS };   // config unreachable -> fail open, site keeps working
  }
}

// Timing-safe-enough key check: compare SHA-256 digests, not the strings.
async function adminAuthed(req, env) {
  if (!env.ADMIN_KEY) return false;
  const given = (req.headers.get('Authorization') || '').replace(/^Bearer\s+/i, '');
  if (!given) return false;
  const dig = async s => new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([dig(given), dig(env.ADMIN_KEY)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

async function stateJson(file, ctx) {
  const cacheKey = new Request('https://cache.internal/state/' + file);
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) return hit.json();
  const r = await fetch(STATE_RAW + file);
  if (!r.ok) throw new Error('state fetch failed: ' + file);
  const body = await r.text();
  ctx.waitUntil(cache.put(cacheKey, new Response(body, { headers: { 'Cache-Control': 'public, max-age=' + API_TTL } })));
  return JSON.parse(body);
}

const slim = (d, watch) => ({
  ticker: d.ticker, name: d.name, sector: d.sector, currency: d.currency || 'USD',
  valueScore: d.valueScore ?? null, indicatorScore: d.indicatorScore ?? null,
  combinedScore: d.combinedScore ?? null, position: d.position || null,
  absoluteScore: d.absoluteScore ?? null, absLabel: d.absLabel || null,
  watchlist: watch.buy.includes(d.ticker) ? 'buy' : watch.sell.includes(d.ticker) ? 'sell' : null,
});

const API_META = {
  name: 'ValueTally scores API',
  site: SITE,
  endpoints: {
    '/api/scores': 'all covered stocks with value/indicator/combined scores, position and watchlist flag',
    '/api/scores/{ticker}': 'one stock incl. score breakdown and daily combined-score history',
    '/api/watchlist': 'current buy/sell watchlist (top/bottom 3 by combined score)',
  },
  notes: 'Scores are this dashboard’s own derived metrics, recomputed roughly hourly on weekday market hours; responses are cached ~5 minutes. Underlying market data: Finnhub & Yahoo Finance. Mechanical screen — not investment advice.',
};

async function handleApi(path, ctx) {
  if (path === 'api') return json(API_META, 200, 86400);
  const watch = await stateJson('watchlist-state.json', ctx);
  if (path === 'api/watchlist') {
    return json({ updatedAt: watch.updatedAt, buy: watch.buy, sell: watch.sell });
  }
  const records = await stateJson('last-data.json', ctx);
  if (path === 'api/scores') {
    return json({ updatedAt: watch.updatedAt, count: records.length, stocks: records.map(d => slim(d, watch)) });
  }
  const m = path.match(/^api\/scores\/([A-Za-z0-9.\-]{1,12})$/);
  if (m) {
    const sym = m[1].toUpperCase();
    const d = records.find(x => x.ticker === sym || (x.adr && x.adr === sym));
    if (!d) return json({ error: 'unknown ticker', hint: 'see /api/scores for coverage' }, 404);
    let scoreHistory = null;
    try {
      // long history is sharded per ticker (state/history/{T}.json, 2026-07-22)
      const h = await stateJson('history/' + d.ticker.replace(/\//g, '_') + '.json', ctx);
      if (h && h.st && h.st.length) {
        scoreHistory = h.st.map((dn, i) => ({ date: new Date(dn * 86400000).toISOString().slice(0, 10), combinedScore: h.s[i] }));
      }
    } catch (e) { /* history unavailable — serve without it */ }
    return json({ updatedAt: watch.updatedAt, ...slim(d, watch), scoreBreakdown: d.scoreBreakdown || null, absBreakdown: d.absBreakdown || null, scoreHistory });
  }
  return json({ error: 'not found', hint: 'see /api for endpoints' }, 404);
}

// Public "full refresh" trigger: kicks the hourly-refresh workflow via a
// server-side GitHub token (fine-grained PAT, Actions-only on this repo).
// No Worker-side cooldown (owner decision 2026-07-22) — the workflow's own
// 3-min dedup step is the only rate control, so rapid re-triggers boot a
// runner that skips itself rather than double-publishing.
const GH_REPO = 'Harris120807/stock-dashboard';
const GH_WF = 'hourly-refresh.yml';

// Live prices: ONE batched Yahoo spark call for the whole universe (native
// symbols, so EU rows work — no CORS problem server-side), edge-cached 30s.
// Upstream cost is ~2 calls/min globally regardless of visitor count.
const YA_UA = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' };

async function handlePrices(ctx) {
  const cache = caches.default;
  const key = new Request('https://cache.internal/live-prices');
  const hit = await cache.match(key);
  if (hit) return new Response(hit.body, hit);
  const records = await stateJson('last-data.json', ctx);
  const syms = records.map(d => d.ticker);
  // spark rejects >20 symbols per request (400) — chunk and fetch in parallel
  const chunks = [];
  for (let i = 0; i < syms.length; i += 20) chunks.push(syms.slice(i, i + 20));
  const results = await Promise.all(chunks.map(ch =>
    fetch('https://query1.finance.yahoo.com/v8/finance/spark?symbols=' +
          ch.map(encodeURIComponent).join(',') + '&range=1d&interval=15m', { headers: YA_UA })
      .then(r => r.ok ? r.json() : null).catch(() => null)));
  const by = {};
  for (const q of results) {
    for (const [sym, v] of Object.entries(q || {})) {
      if (!v || !Array.isArray(v.close)) continue;
      const closes = v.close.filter(x => x !== null && x !== undefined);
      const p = closes.length ? closes[closes.length - 1] : null;
      const prev = v.chartPreviousClose ?? v.previousClose ?? null;
      by[sym] = { p, c: (p && prev) ? Math.round((p / prev - 1) * 10000) / 100 : null };
    }
  }
  if (!Object.keys(by).length) return json({ error: 'quote source unavailable' }, 502, 10);
  const res = json({ updatedAt: new Date().toISOString(), byTicker: by }, 200, 30);
  ctx.waitUntil(cache.put(key, res.clone()));
  return res;
}

const ACCOUNT_ID = 'e3f3a97cb1349addb9ec089f9383d12d';
const AE_DATASET = 'stockdash_traffic';

async function handleAdmin(route, req, env, ctx) {
  if (!(await adminAuthed(req, env))) return json({ error: 'unauthorized' }, 401, 0);

  if (route === 'admin/ping') return json({ ok: true }, 200, 0);

  if (route === 'admin/config') {
    const flags = await getFlags(env);
    if (req.method === 'GET') return json(flags, 200, 0);
    if (req.method === 'POST') {
      if (!env.CONFIG) return json({ error: 'config storage not provisioned' }, 503, 0);
      let body;
      try { body = await req.json(); } catch (e) { return json({ error: 'bad json' }, 400, 0); }
      for (const k of Object.keys(DEFAULT_FLAGS)) {
        if (typeof body[k] === 'boolean') flags[k] = body[k];
      }
      await env.CONFIG.put('flags', JSON.stringify(flags));
      return json({ ...flags, note: 'takes effect everywhere within ~60s' }, 200, 0);
    }
  }

  if (route === 'admin/add-tickers' && req.method === 'POST') {
    if (!env.GH_TOKEN) return json({ error: 'not configured' }, 503, 0);
    let body;
    try { body = await req.json(); } catch (e) { return json({ error: 'bad json' }, 400, 0); }
    const raw = String(body.tickers || '').slice(0, 300);
    if (!raw.trim() || /[^A-Za-z0-9.\-:,\s]/.test(raw)) {
      return json({ error: 'tickers must be symbols like AAPL or HSBA.L:HSBC, comma-separated' }, 400, 0);
    }
    const r = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/add-tickers.yml/dispatches`, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + env.GH_TOKEN, 'Accept': 'application/vnd.github+json',
        'User-Agent': 'valuetally-admin', 'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main', inputs: { tickers: raw } }),
    });
    if (r.status !== 204) return json({ error: 'dispatch failed (' + r.status + ')' }, 502, 0);
    return json({ ok: true, note: 'validating and adding — accepted tickers are live in ~4 min; you get an ntfy with the outcome' }, 200, 0);
  }

  if (route === 'admin/run' && req.method === 'POST') {
    if (!env.GH_TOKEN) return json({ error: 'not configured' }, 503, 0);
    let body;
    try { body = await req.json(); } catch (e) { return json({ error: 'bad json' }, 400, 0); }
    // whitelist — the PAT can dispatch anything in the repo, the console may not
    const WFS = {
      'hourly-refresh': 'hourly-refresh.yml',
      'daily-analyst': 'daily-analyst.yml',
      'weekly-universe': 'weekly-universe.yml',
      'annual-benchmarks': 'annual-benchmarks.yml',
      'backtest': 'backtest.yml',
    };
    const wf = WFS[body.workflow];
    if (!wf) return json({ error: 'unknown workflow' }, 400, 0);
    const payload = { ref: 'main' };
    if (body.workflow === 'daily-analyst' && body.full) payload.inputs = { full: true };
    const r = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/${wf}/dispatches`, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + env.GH_TOKEN, 'Accept': 'application/vnd.github+json',
        'User-Agent': 'valuetally-admin', 'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    if (r.status !== 204) return json({ error: 'dispatch failed (' + r.status + ')' }, 502, 0);
    return json({ ok: true, workflow: body.workflow }, 200, 0);
  }

  if (route === 'admin/adr-lookup' && req.method === 'GET') {
    // Suggest US-listed ADR candidates for a native (non-US) symbol: resolve
    // the symbol to a company name via Yahoo search, then search the name and
    // keep US-exchange equities. Suggestions only — the owner confirms, and
    // add-tickers.yml still validates the pair against Finnhub before adding.
    const sym = (new URL(req.url).searchParams.get('symbol') || '').toUpperCase().slice(0, 15);
    if (!/^[A-Z0-9.\-]{1,15}$/.test(sym)) return json({ error: 'bad symbol' }, 400, 0);
    const ysearch = async q => {
      const r = await fetch('https://query2.finance.yahoo.com/v1/finance/search?q=' + encodeURIComponent(q) + '&quotesCount=10&newsCount=0', { headers: YA_UA });
      return r.ok ? (await r.json()).quotes || [] : [];
    };
    const own = (await ysearch(sym)).find(x => (x.symbol || '').toUpperCase() === sym);
    const name = own && (own.longname || own.shortname);
    if (!name) return json({ symbol: sym, name: null, candidates: [] }, 200, 0);
    const US_EXCH = { NYQ: 'NYSE', NMS: 'NASDAQ', NGM: 'NASDAQ', NCM: 'NASDAQ', ASE: 'AMEX', PNK: 'OTC' };
    const cleanName = name.replace(/\s+(ORD|ADR|DRN|FPO|R|S|V).*$/i, '').trim();
    const shortName = cleanName.split(/\s+/).slice(0, 2).join(' ');
    const [r1, r2] = await Promise.all([ysearch(cleanName), shortName !== cleanName ? ysearch(shortName) : []]);
    const seen = new Set();
    // rank: listed exchanges before OTC; among OTC, Y-suffix sponsored ADRs
    // before F-suffix ordinary lines (often dead — RHHBY-not-RHHVF trap)
    const score = x => (x.exchange !== 'PNK' ? 0 : /Y$/.test(x.symbol) ? 1 : /F$/.test(x.symbol) ? 3 : 2);
    const candidates = [...r1, ...r2]
      .filter(x => x.quoteType === 'EQUITY' && US_EXCH[x.exchange] && !/[.]/.test(x.symbol || '') && (x.symbol || '').toUpperCase() !== sym)
      .filter(x => { const s = x.symbol.toUpperCase(); if (seen.has(s)) return false; seen.add(s); return true; })
      .sort((a, b) => score(a) - score(b))
      .slice(0, 3)
      .map(x => ({ symbol: x.symbol.toUpperCase(), exchange: US_EXCH[x.exchange], name: x.longname || x.shortname || '' }));
    return json({ symbol: sym, name: cleanName, candidates }, 200, 0);
  }

  if (route === 'admin/send-digests' && req.method === 'POST') {
    // manual trigger for the daily watchlist digest (same code the cron runs)
    await sendDigests(env, ctx);
    return json({ ok: true, note: 'digest pass completed — emails sent only to opted-in users with events today' }, 200, 0);
  }

  if (route === 'admin/stats') {
    const days = Math.min(30, Math.max(1, parseInt(new URL(req.url).searchParams.get('days') || '7', 10) || 7));
    // Preferred: Analytics Engine SQL (per-route breakdown) — needs a scoped
    // API token (Account Analytics:Read); the Global API Key can't call it.
    if (env.CF_ANALYTICS_TOKEN) {
      const sql = `SELECT toStartOfInterval(timestamp, INTERVAL '1' DAY) AS day, blob1 AS route, ` +
        `sum(_sample_interval) AS requests FROM ${AE_DATASET} ` +
        `WHERE timestamp > NOW() - INTERVAL '${days}' DAY GROUP BY day, route ORDER BY day ASC`;
      const r = await fetch(`https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/analytics_engine/sql`, {
        method: 'POST', headers: { 'Authorization': 'Bearer ' + env.CF_ANALYTICS_TOKEN }, body: sql,
      });
      if (r.ok) {
        const data = await r.json();
        return json({ days, rows: data.data || [] }, 200, 0);
      }
    }
    // Fallback: Workers invocation totals via GraphQL (Global API Key auth) —
    // daily request counts only, no per-route split.
    if (env.CF_API_EMAIL && env.CF_API_KEY) {
      const since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
      const q = `{viewer{accounts(filter:{accountTag:"${ACCOUNT_ID}"}){` +
        `workersInvocationsAdaptive(limit:1000,filter:{scriptName:"stockdash-proxy",date_geq:"${since}"})` +
        `{sum{requests} dimensions{date}}}}}`;
      const r = await fetch('https://api.cloudflare.com/client/v4/graphql', {
        method: 'POST',
        headers: { 'X-Auth-Email': env.CF_API_EMAIL, 'X-Auth-Key': env.CF_API_KEY, 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q }),
      });
      if (!r.ok) return json({ error: 'analytics query failed (' + r.status + ')' }, 502, 0);
      const data = await r.json();
      const buckets = (((data.data || {}).viewer || {}).accounts || [{}])[0].workersInvocationsAdaptive || [];
      const rows = buckets.map(b => ({ day: b.dimensions.date, route: 'all routes', requests: b.sum.requests }));
      return json({ days, rows, note: 'Totals only — add a scoped analytics token for the per-route split.' }, 200, 0);
    }
    return json({ error: 'analytics not configured' }, 503, 0);
  }

  return json({ error: 'not found' }, 404, 0);
}

async function handleRefresh(env, ctx) {
  if (!env.GH_TOKEN) return json({ error: 'refresh trigger not configured' }, 503, 30);
  const gh = {
    'Authorization': 'Bearer ' + env.GH_TOKEN,
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'valuetally-refresh-trigger',
  };
  const r = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/${GH_WF}/dispatches`, {
    method: 'POST', headers: { ...gh, 'Content-Type': 'application/json' }, body: JSON.stringify({ ref: 'main' }),
  });
  if (r.status !== 204) return json({ error: 'trigger failed (' + r.status + ')' }, 502, 0);
  return json({ ok: true, note: 'refresh started — fresh data publishes in ~2 minutes' }, 200, 0);
}

// ---------- accounts (2026-07-24, owner-requested email+password) ----------
// D1 binding DB; Resend for verification/reset mail (secret RESEND_API_KEY,
// MAIL_FROM switches from Resend's test sender to account@valuetally.com once
// the domain is verified). Passwords stored as PBKDF2-SHA256 (210k iters,
// per-user salt) — never plaintext. Sessions are 32-byte bearer tokens, 90d.
// Rate limiting is D1-backed (strongly consistent, unlike KV).

const SESSION_DAYS = 90;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const TICK_RE = /^[A-Z0-9.\-]{1,12}$/;

const hex = b => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
const randHex = n => hex(crypto.getRandomValues(new Uint8Array(n)));

async function pwHash(password, saltHex) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']);
  const salt = new Uint8Array(saltHex.match(/../g).map(h => parseInt(h, 16)));
  const bits = await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: 100000 }, key, 256);
  return hex(bits);
}

// fixed-window rate limit in D1; true = allowed
async function rateOk(env, key, limit, windowSec) {
  const now = Math.floor(Date.now() / 1000);
  const row = await env.DB.prepare('SELECT n, reset_at FROM attempts WHERE key = ?').bind(key).first();
  if (!row || row.reset_at < now) {
    await env.DB.prepare('INSERT INTO attempts (key, n, reset_at) VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET n = 1, reset_at = ?')
      .bind(key, now + windowSec, now + windowSec).run();
    return true;
  }
  if (row.n >= limit) return false;
  await env.DB.prepare('UPDATE attempts SET n = n + 1 WHERE key = ?').bind(key).run();
  return true;
}

async function sendMail(env, to, subject, html) {
  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + env.RESEND_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from: env.MAIL_FROM || 'ValueTally <account@valuetally.com>', to: [to], subject, html }),
  });
  if (!r.ok) throw new Error('mail failed ' + r.status + ' ' + (await r.text()).slice(0, 200));
}

// Site-matched email shell (2026-07-24 owner request): light page tint, white
// card, brand wordmark in the accent→tip gradient colors, accent button. All
// styles inline — email clients strip <style> blocks.
const mailWrap = body => `<div style="background:#f4f6fa;padding:32px 16px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <div style="max-width:440px;margin:0 auto">
    <div style="font-size:21px;font-weight:800;margin:0 0 14px;letter-spacing:.2px"><span style="color:#1e4f91">Value</span><span style="color:#3fa3dd">Tally</span></div>
    <div style="background:#ffffff;border:1px solid #dbe2ee;border-radius:14px;padding:26px 24px;color:#16233a;font-size:15px;line-height:1.55">${body}</div>
    <p style="font-size:12px;color:#7d8595;margin:16px 4px 0;line-height:1.5">If you didn't request this, you can safely ignore this email — nothing changes unless the link is used.<br>
    <a href="https://valuetally.com" style="color:#1e4f91;text-decoration:none">valuetally.com</a> — US &amp; European stocks, scored.</p>
  </div></div>`;
const mailBtn = (href, label) => `<p style="margin:20px 0 6px"><a href="${href}" style="display:inline-block;background:#1e4f91;color:#ffffff;padding:12px 22px;border-radius:9px;text-decoration:none;font-weight:600">${label}</a></p>`;

async function sessionUser(req, env) {
  const tok = (req.headers.get('Authorization') || '').replace(/^Bearer\s+/i, '');
  if (!/^[a-f0-9]{64}$/.test(tok)) return null;
  const now = Math.floor(Date.now() / 1000);
  return env.DB.prepare(
    'SELECT u.id, u.email, u.verified, s.token FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ?'
  ).bind(tok, now).first();
}

async function issueSession(env, userId) {
  const token = randHex(32);
  const exp = Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400;
  await env.DB.prepare('INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)').bind(token, userId, exp).run();
  return token;
}

async function userPayload(env, u, token) {
  const w = await env.DB.prepare('SELECT tickers FROM watchlists WHERE user_id = ?').bind(u.id).first();
  return { token, email: u.email, verified: !!u.verified, alerts: !!u.alerts, watchlist: w ? JSON.parse(w.tickers) : [] };
}

async function sendVerifyMail(env, userId, email) {
  const t = randHex(24);
  await env.DB.prepare('INSERT INTO tokens (token, user_id, kind, expires_at) VALUES (?, ?, ?, ?)')
    .bind(t, userId, 'verify', Math.floor(Date.now() / 1000) + 172800).run();
  await sendMail(env, email, 'Confirm your ValueTally email',
    mailWrap(`<p style="margin:0 0 4px;font-size:17px;font-weight:700">Welcome to ValueTally 👋</p>
      <p style="margin:8px 0 0">Tap the button to confirm this email address and your account is all set — your watchlist will sync to every device you sign in on.</p>
      ${mailBtn('https://api.valuetally.com/auth/verify?t=' + t, 'Confirm my email')}`));
}

async function handleAuth(route, req, env, ctx) {
  if (!env.DB) return json({ error: 'accounts not provisioned yet' }, 503, 0);
  const ip = req.headers.get('CF-Connecting-IP') || 'unknown';
  const body = (req.method === 'POST' || req.method === 'PUT') ? await req.json().catch(() => null) : null;
  const email = body && String(body.email || '').trim().toLowerCase();

  if (route === 'auth/signup' && req.method === 'POST') {
    if (!email || !EMAIL_RE.test(email) || email.length > 254) return json({ error: 'enter a valid email address' }, 400, 0);
    const pw = String((body && body.password) || '');
    if (pw.length < 8) return json({ error: 'password must be at least 8 characters' }, 400, 0);
    if (!(await rateOk(env, 'su:' + ip, 10, 3600))) return json({ error: 'too many attempts — try again later' }, 429, 0);
    const exists = await env.DB.prepare('SELECT id FROM users WHERE email = ?').bind(email).first();
    if (exists) return json({ error: 'an account with this email already exists — sign in instead' }, 409, 0);
    const salt = randHex(16);
    const hash = await pwHash(pw, salt);
    const res = await env.DB.prepare('INSERT INTO users (email, pw_hash, salt) VALUES (?, ?, ?)').bind(email, hash, salt).run();
    const uid = res.meta.last_row_id;
    try { await sendVerifyMail(env, uid, email); } catch (e) { /* account still usable; resend available */ }
    const token = await issueSession(env, uid);
    return json(await userPayload(env, { id: uid, email, verified: 0 }, token), 200, 0);
  }

  if (route === 'auth/login' && req.method === 'POST') {
    if (!email) return json({ error: 'enter your email' }, 400, 0);
    if (!(await rateOk(env, 'li:' + ip + ':' + email, 10, 3600))) return json({ error: 'too many attempts — try again later' }, 429, 0);
    const u = await env.DB.prepare('SELECT * FROM users WHERE email = ?').bind(email).first();
    const ok = u && (await pwHash(String((body && body.password) || ''), u.salt)) === u.pw_hash;
    if (!ok) return json({ error: 'wrong email or password' }, 401, 0);
    const token = await issueSession(env, u.id);
    return json(await userPayload(env, u, token), 200, 0);
  }

  if (route === 'auth/logout' && req.method === 'POST') {
    const u = await sessionUser(req, env);
    if (u) await env.DB.prepare('DELETE FROM sessions WHERE token = ?').bind(u.token).run();
    return json({ ok: true }, 200, 0);
  }

  if (route === 'auth/verify' && req.method === 'GET') {
    const t = new URL(req.url).searchParams.get('t') || '';
    const now = Math.floor(Date.now() / 1000);
    const row = await env.DB.prepare("SELECT user_id FROM tokens WHERE token = ? AND kind = 'verify' AND expires_at > ?").bind(t, now).first();
    if (row) {
      await env.DB.prepare('UPDATE users SET verified = 1 WHERE id = ?').bind(row.user_id).run();
      await env.DB.prepare('DELETE FROM tokens WHERE token = ?').bind(t).run();
    }
    return new Response(null, { status: 302, headers: { 'Location': SITE + (row ? '#verified=1' : '#verified=0'), ...CORS } });
  }

  if (route === 'auth/resend-verify' && req.method === 'POST') {
    const u = await sessionUser(req, env);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    if (u.verified) return json({ ok: true, note: 'already verified' }, 200, 0);
    if (!(await rateOk(env, 'rv:' + u.id, 3, 3600))) return json({ error: 'too many attempts — try again later' }, 429, 0);
    await sendVerifyMail(env, u.id, u.email);
    return json({ ok: true }, 200, 0);
  }

  if (route === 'auth/forgot' && req.method === 'POST') {
    if (!email) return json({ error: 'enter your email' }, 400, 0);
    if (!(await rateOk(env, 'fp:' + ip, 5, 3600))) return json({ error: 'too many attempts — try again later' }, 429, 0);
    const u = await env.DB.prepare('SELECT id FROM users WHERE email = ?').bind(email).first();
    if (u) {
      const t = randHex(24);
      await env.DB.prepare('INSERT INTO tokens (token, user_id, kind, expires_at) VALUES (?, ?, ?, ?)')
        .bind(t, u.id, 'reset', Math.floor(Date.now() / 1000) + 3600).run();
      try {
        await sendMail(env, email, 'Reset your ValueTally password',
          mailWrap(`<p style="margin:0 0 4px;font-size:17px;font-weight:700">Password reset</p>
            <p style="margin:8px 0 0">Tap the button to choose a new password. The link works once and expires in an hour; using it signs you out everywhere else.</p>
            ${mailBtn(SITE + '#reset=' + t, 'Choose a new password')}`));
      } catch (e) { return json({ error: 'could not send the email — try again shortly' }, 502, 0); }
    }
    // same response whether or not the account exists (no address probing)
    return json({ ok: true, note: 'if that address has an account, a reset link is on its way' }, 200, 0);
  }

  if (route === 'auth/reset' && req.method === 'POST') {
    const t = String((body && body.token) || '');
    const pw = String((body && body.password) || '');
    if (pw.length < 8) return json({ error: 'password must be at least 8 characters' }, 400, 0);
    const now = Math.floor(Date.now() / 1000);
    const row = await env.DB.prepare("SELECT user_id FROM tokens WHERE token = ? AND kind = 'reset' AND expires_at > ?").bind(t, now).first();
    if (!row) return json({ error: 'this reset link has expired — request a new one' }, 400, 0);
    const salt = randHex(16);
    const hash = await pwHash(pw, salt);
    await env.DB.prepare('UPDATE users SET pw_hash = ?, salt = ?, verified = 1 WHERE id = ?').bind(hash, salt, row.user_id).run();
    await env.DB.prepare('DELETE FROM tokens WHERE token = ?').bind(t).run();
    await env.DB.prepare('DELETE FROM sessions WHERE user_id = ?').bind(row.user_id).run();   // sign out everywhere
    const u = await env.DB.prepare('SELECT * FROM users WHERE id = ?').bind(row.user_id).first();
    const token = await issueSession(env, u.id);
    return json(await userPayload(env, u, token), 200, 0);
  }

  if (route === 'auth/delete-account' && req.method === 'POST') {
    const u = await sessionUser(req, env);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    await env.DB.batch([
      env.DB.prepare('DELETE FROM sessions WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM tokens WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM watchlists WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM users WHERE id = ?').bind(u.id),
    ]);
    return json({ ok: true, note: 'account and data deleted' }, 200, 0);
  }

  if (route === 'me' && req.method === 'GET') {
    const u = await sessionUser(req, env);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const full = await env.DB.prepare('SELECT * FROM users WHERE id = ?').bind(u.id).first();
    return json(await userPayload(env, full, undefined), 200, 0);
  }

  if (route === 'me/alerts' && req.method === 'POST') {
    const u = await sessionUser(req, env);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const on = !!(body && body.on);
    if (on) {
      // ensure an unsubscribe token exists before the first digest can go out
      const row = await env.DB.prepare('SELECT unsub_token FROM users WHERE id = ?').bind(u.id).first();
      const tok = (row && row.unsub_token) || randHex(16);
      await env.DB.prepare('UPDATE users SET alerts = 1, unsub_token = ? WHERE id = ?').bind(tok, u.id).run();
    } else {
      await env.DB.prepare('UPDATE users SET alerts = 0 WHERE id = ?').bind(u.id).run();
    }
    return json({ ok: true, alerts: on }, 200, 0);
  }

  if (route === 'alerts/unsubscribe' && req.method === 'GET') {
    const t = new URL(req.url).searchParams.get('u') || '';
    if (/^[a-f0-9]{32}$/.test(t)) {
      await env.DB.prepare('UPDATE users SET alerts = 0 WHERE unsub_token = ?').bind(t).run();
    }
    return new Response(null, { status: 302, headers: { 'Location': SITE + '#alerts=off', ...CORS } });
  }

  if (route === 'me/watchlist' && req.method === 'PUT') {
    const u = await sessionUser(req, env);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const list = body && body.tickers;
    if (!Array.isArray(list) || list.length > 100 || !list.every(t => typeof t === 'string' && TICK_RE.test(t))) {
      return json({ error: 'bad watchlist' }, 400, 0);
    }
    const uniq = [...new Set(list)];
    await env.DB.prepare('INSERT INTO watchlists (user_id, tickers, updated_at) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET tickers = ?, updated_at = ?')
      .bind(u.id, JSON.stringify(uniq), Math.floor(Date.now() / 1000), JSON.stringify(uniq), Math.floor(Date.now() / 1000)).run();
    return json({ ok: true, watchlist: uniq }, 200, 0);
  }

  return json({ error: 'not found' }, 404, 0);
}

// ---------- daily watchlist digest (Worker cron, weekdays 20:30 UTC) ----------
// One email per opted-in verified user, only when a starred stock did something
// noteworthy today: |price move| >= 5% or |combined-score change vs yesterday|
// >= 0.05. Data comes from the pipeline's last-data.json (public, no vendor
// re-serving concern: the digest goes to the user, not an API).
async function sendDigests(env, ctx) {
  if (!env.DB || !env.RESEND_API_KEY) return;
  const r = await fetch(STATE_RAW + 'last-data.json');
  if (!r.ok) return;
  const by = {};
  for (const d of await r.json()) by[d.ticker] = d;
  const users = (await env.DB.prepare(
    'SELECT u.id, u.email, u.unsub_token, w.tickers FROM users u JOIN watchlists w ON w.user_id = u.id WHERE u.alerts = 1 AND u.verified = 1'
  ).all()).results || [];
  for (const u of users) {
    let list;
    try { list = JSON.parse(u.tickers); } catch (e) { continue; }
    const events = [];
    for (const t of list) {
      const d = by[t];
      if (!d) continue;
      const move = typeof d.dayChange === 'number' ? d.dayChange : null;
      const sd = typeof d.scoreDelta === 'number' ? d.scoreDelta : null;
      const notes = [];
      if (move !== null && Math.abs(move) >= 5) notes.push((move > 0 ? '▲ up ' : '▼ down ') + Math.abs(move).toFixed(1) + '% today');
      if (sd !== null && Math.abs(sd) >= 0.05) notes.push('score ' + (sd > 0 ? 'up' : 'down') + ' ' + Math.abs(sd).toFixed(2) + ' vs yesterday');
      if (notes.length) events.push({ d, notes });
    }
    if (!events.length) continue;
    const rows = events.map(({ d, notes }) => `
      <tr><td style="padding:9px 0;border-bottom:1px solid #eef1f6"><b>${d.ticker}</b> <span style="color:#7d8595">${(d.name || '').slice(0, 28)}</span><br>
      <span style="font-size:13px;color:#4c5566">${notes.join(' · ')}</span></td>
      <td style="padding:9px 0;border-bottom:1px solid #eef1f6;text-align:right;white-space:nowrap">${d.price != null ? d.price : ''} <span style="color:${(d.dayChange || 0) >= 0 ? '#0ca30c' : '#d03b3b'}">${d.dayChange != null ? (d.dayChange >= 0 ? '+' : '') + d.dayChange.toFixed(1) + '%' : ''}</span></td></tr>`).join('');
    const html = mailWrap(`<p style="margin:0 0 4px;font-size:17px;font-weight:700">Your watchlist today</p>
      <p style="margin:6px 0 10px;color:#4c5566;font-size:13.5px">Stocks you follow that moved meaningfully — full picture on <a href="${SITE}#watchlist" style="color:#1e4f91">your watchlist</a>.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14.5px">${rows}</table>
      <p style="font-size:12px;color:#7d8595;margin:14px 0 0">Mechanical screen, not investment advice. <a href="https://api.valuetally.com/alerts/unsubscribe?u=${u.unsub_token}" style="color:#7d8595">Unsubscribe from these alerts</a>.</p>`);
    try { await sendMail(env, u.email, 'ValueTally watchlist: ' + events.map(e => e.d.ticker).slice(0, 4).join(', ') + (events.length > 4 ? '…' : ''), html); } catch (e) { /* next user */ }
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(sendDigests(env, ctx));
  },
  async fetch(req, env, ctx) {
    if (req.method === 'OPTIONS') return new Response(null, { headers: CORS });
    const url = new URL(req.url);
    const route = url.pathname.replace(/^\/+|\/+$/g, '');
    // anonymous traffic point: route group + country (no IPs, no UAs)
    try {
      const group = route.split('/')[0] || 'root';
      env.TRAFFIC?.writeDataPoint({ blobs: [group, (req.cf && req.cf.country) || ''], indexes: [group] });
    } catch (e) { /* analytics must never break serving */ }
    if (route.startsWith('admin')) {
      try { return await handleAdmin(route, req, env, ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (route.startsWith('auth/') || route === 'me' || route.startsWith('me/') || route.startsWith('alerts/')) {
      try { return await handleAuth(route, req, env, ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (route === 'refresh' && req.method === 'POST') {
      const flags = await getFlags(env);
      if (!flags.fullRefresh) return json({ error: 'full refresh is currently disabled by the owner', disabled: true }, 403, 0);
      try { return await handleRefresh(env, ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (route === 'prices' && req.method === 'GET') {
      const flags = await getFlags(env);
      if (!flags.livePrices) return json({ disabled: true }, 200, 60);
      try { return await handlePrices(ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 10); }
    }
    if (req.method === 'GET' && (route === 'api' || route.startsWith('api/'))) {
      try { return await handleApi(route, ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 30); }
    }
    if (req.method !== 'GET' || !TTL[route]) {
      return new Response(JSON.stringify({ error: 'not found' }), { status: 404, headers: { 'Content-Type': 'application/json', ...CORS } });
    }
    if (route === 'quote' || route === 'metric') {
      const flags = await getFlags(env);
      if (!flags.stockRefresh) return json({ disabled: true }, 200, 60);
    }
    const params = [];
    for (const p of ALLOWED[route]) {
      const v = (url.searchParams.get(p) || '').slice(0, 60);
      if (v) params.push([p, v]);
    }
    if (!params.length) {
      return new Response(JSON.stringify({ error: 'missing parameter' }), { status: 400, headers: { 'Content-Type': 'application/json', ...CORS } });
    }

    const cacheKey = new Request('https://cache.internal/' + route + '?' +
      params.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).sort().join('&'));
    const cache = caches.default;
    const hit = await cache.match(cacheKey);
    if (hit) return hit;

    const up = new URL('https://finnhub.io/api/v1/' + (route === 'metric' ? 'stock/metric' : route));
    for (const [k, v] of params) up.searchParams.set(k, v);
    if (route === 'metric') up.searchParams.set('metric', 'all');
    up.searchParams.set('token', env.FINNHUB_API_KEY);

    let upstream;
    try {
      upstream = await fetch(up.toString());
    } catch (e) {
      return new Response(JSON.stringify({ error: 'upstream unreachable' }), { status: 502, headers: { 'Content-Type': 'application/json', ...CORS } });
    }
    const body = await upstream.text();
    // Long-lived stale copy per key: served when Finnhub rate-limits (429),
    // so interactive refreshes degrade to slightly-old data instead of failing
    // while the hourly pipeline is eating the shared per-minute call budget.
    const staleKey = new Request(cacheKey.url.replace('cache.internal/', 'cache.internal/stale/'));
    if (!upstream.ok) {
      const stale = await cache.match(staleKey);
      if (stale) {
        const r = new Response(stale.body, stale);
        r.headers.set('X-Stale', '1');
        return r;
      }
      // Finnhub sends HTML error pages — never pass those through as JSON.
      const msg = upstream.status === 429
        ? 'rate limited by data provider — try again in a minute'
        : 'upstream error ' + upstream.status;
      return new Response(JSON.stringify({ error: msg }), { status: upstream.status, headers: { 'Content-Type': 'application/json', ...CORS } });
    }
    const res = new Response(body, {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'public, max-age=' + TTL[route],
        ...CORS,
      },
    });
    ctx.waitUntil(cache.put(cacheKey, res.clone()));
    ctx.waitUntil(cache.put(staleKey, new Response(body, {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=86400', ...CORS },
    })));
    return res;
  },
};
