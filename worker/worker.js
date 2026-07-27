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
//   GET  /admin/security    -> intrusion-monitor status (security_log, canary)
//   GET  /admin/security-check -> DRY run of the hourly anomaly check
//   GET  /admin/errors      -> Worker route-error log (24h count + recent rows)
//   POST /admin/broadcast   -> announcement email to opted-in users
//                              (two-step: confirm:false = recipient count only)
// Trading 212 portfolio import (2026-07-26, signed-in only, no-store):
//   POST /me/t212        -> validate + encrypt-store a READ-ONLY T212 key
//   POST /me/t212/delete -> disconnect (row + caches + value history wiped)
//   GET  /me/portfolio   -> positions from T212 (KV-cached 60s per user)
//   GET  /me/portfolio/history -> daily account-value snapshots (20:30 cron)
// Custom alert rules (2026-07-27, signed-in only, one-shot, digest-evaluated):
//   GET/POST /me/rules, POST /me/rules/delete, POST /me/rules/rearm
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
  if (!(await adminAuthed(req, env))) {
    secLog(env, ctx, 'admin_auth_fail', route, req);
    return json({ error: 'unauthorized' }, 401, 0);
  }

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

  if (route === 'admin/send-weekly' && req.method === 'POST') {
    // manual trigger for the Saturday weekly wrap (same code the cron runs)
    await sendWeekly(env, ctx);
    return json({ ok: true, note: 'weekly wrap pass completed — sent to opted-in verified users' }, 200, 0);
  }

  if (route === 'admin/user-stats' && req.method === 'GET') {
    // aggregate account counts only — no emails or per-user data leave D1
    if (!env.DB) return json({ error: 'accounts not provisioned' }, 503, 0);
    const u = (await env.DB.prepare(
      'SELECT COUNT(*) AS total, COALESCE(SUM(verified),0) AS verified, COALESCE(SUM(alerts),0) AS alerts FROM users'
    ).first()) || {};
    const w = (await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM watchlists WHERE tickers IS NOT NULL AND tickers != '[]'"
    ).first()) || {};
    return json({ users: u.total || 0, verified: u.verified || 0, alerts: u.alerts || 0, watchlists: w.n || 0 }, 200, 0);
  }

  if (route === 'admin/users' && req.method === 'GET') {
    // account list for the owner: emails + status flags. Admin-key gated,
    // never cached. The security canary account is hidden (it's plumbing,
    // not a user — and listing it would just cause confusion).
    if (!env.DB) return json({ error: 'accounts not provisioned' }, 503, 0);
    const rows = (await env.DB.prepare(
      `SELECT u.email, u.verified, u.alerts, u.created_at,
              CASE WHEN w.tickers IS NOT NULL AND w.tickers != '[]' THEN 1 ELSE 0 END AS has_watchlist,
              CASE WHEN b.user_id IS NOT NULL THEN 1 ELSE 0 END AS has_broker
       FROM users u
       LEFT JOIN watchlists w ON w.user_id = u.id
       LEFT JOIN broker_keys b ON b.user_id = u.id
       WHERE u.email NOT LIKE 'canary+%@valuetally.com'
       ORDER BY u.created_at DESC LIMIT 500`
    ).all()).results || [];
    return json({ users: rows }, 200, 0);
  }

  if (route === 'admin/errors' && req.method === 'GET') {
    // Worker route-error log: 24h count + the most recent rows. Rows are our
    // own exception text only — nothing user-supplied beyond the route name.
    if (!env.DB) return json({ error: 'accounts not provisioned' }, 503, 0);
    const now = Math.floor(Date.now() / 1000);
    let count24h = 0, recent = [];
    try {
      count24h = ((await env.DB.prepare('SELECT COUNT(*) AS n FROM error_log WHERE at > ?').bind(now - 86400).first()) || {}).n || 0;
      recent = (await env.DB.prepare('SELECT at, route, detail FROM error_log ORDER BY id DESC LIMIT 30').all()).results || [];
    } catch (e) { /* table not provisioned yet */ }
    return json({ count24h, recent }, 200, 0);
  }

  if (route === 'admin/broadcast' && req.method === 'POST') {
    // One-off announcement to opted-in verified users (same audience + same
    // unsubscribe token as the digests). Two-step: without confirm:true it
    // only returns the recipient count; with it, it actually sends. Body is
    // plain text — escaped and line-preserved into the site-styled shell.
    if (!env.DB || !env.RESEND_API_KEY) return json({ error: 'not configured' }, 503, 0);
    let body;
    try { body = await req.json(); } catch (e) { return json({ error: 'bad json' }, 400, 0); }
    const subject = String(body.subject || '').trim().slice(0, 120);
    const text = String(body.body || '').trim().slice(0, 5000);
    const users = (await env.DB.prepare(
      "SELECT email, unsub_token FROM users WHERE alerts = 1 AND verified = 1 AND email NOT LIKE 'canary+%@valuetally.com'"
    ).all()).results || [];
    if (!body.confirm) return json({ recipients: users.length, note: 'preview only — send again with confirm:true to actually email' }, 200, 0);
    if (!subject || !text) return json({ error: 'subject and body are both required' }, 400, 0);
    const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    let sent = 0;
    for (const u of users) {
      const html = mailWrap(`<p style="margin:0 0 10px;font-size:17px;font-weight:700">${esc(subject)}</p>
        <p style="margin:0;white-space:pre-wrap">${esc(text)}</p>
        <p style="font-size:12px;color:#7d8595;margin:16px 0 0"><a href="https://api.valuetally.com/alerts/unsubscribe?u=${u.unsub_token}" style="color:#7d8595">Unsubscribe from these emails</a>.</p>`);
      try { await sendMail(env, u.email, subject, html); sent++; } catch (e) { /* next recipient */ }
    }
    secLog(env, ctx, 'broadcast', 'sent ' + sent + '/' + users.length, req);
    return json({ ok: true, sent, recipients: users.length }, 200, 0);
  }

  if (route === 'admin/deadman-check' && req.method === 'GET') {
    // ALWAYS a dry run — reports what the dead-man cron would do without ever
    // pinging the owner's ntfy topic (no test messages there, standing rule)
    return json(await deadmanCheck(env, true), 200, 0);
  }

  if (route === 'admin/security' && req.method === 'GET') {
    // intrusion-monitoring status for the admin Security panel: recent events,
    // 24h failure counts, account totals, and the canary account's health
    if (!env.DB) return json({ error: 'accounts not provisioned' }, 503, 0);
    const now = Math.floor(Date.now() / 1000);
    const recent = ((await env.DB.prepare(
      'SELECT kind, at, detail, country FROM security_log ORDER BY id DESC LIMIT 30'
    ).all()).results) || [];
    const c24 = k => env.DB.prepare('SELECT COUNT(*) AS n FROM security_log WHERE kind = ? AND at > ?').bind(k, now - 86400).first();
    const [fa, fl, uc, sc] = await Promise.all([
      c24('admin_auth_fail'), c24('login_fail'),
      env.DB.prepare('SELECT COUNT(*) AS n FROM users').first(),
      env.DB.prepare('SELECT COUNT(*) AS n FROM sessions').first(),
    ]);
    let canaryOk = false;
    try {
      const cid = await canaryId(env);
      if (cid) {
        const [row, trips, sess] = await Promise.all([
          env.DB.prepare('SELECT id FROM users WHERE id = ?').bind(cid).first(),
          env.DB.prepare("SELECT COUNT(*) AS n FROM security_log WHERE kind = 'canary_login'").first(),
          env.DB.prepare('SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?').bind(cid).first(),
        ]);
        canaryOk = !!row && !((trips || {}).n) && !((sess || {}).n);
      }
    } catch (e) { /* canaryOk stays false — absence of proof is itself a flag */ }
    return json({
      recent,
      failedAdmin24h: (fa || {}).n || 0, failedLogin24h: (fl || {}).n || 0,
      users: (uc || {}).n || 0, sessions: (sc || {}).n || 0,
      canaryOk,
    }, 200, 0);
  }

  if (route === 'admin/security-check' && req.method === 'GET') {
    // ALWAYS a dry run (same standing rule as deadman-check): reports what the
    // hourly anomaly check would do — never alerts, never moves the baseline
    return json(await securityCheck(env, true), 200, 0);
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
// custom alert-rule kinds (2026-07-27): _above fires at value >= threshold,
// _below at value <= threshold — evaluated once daily by the digest cron
const RULE_KINDS = new Set(['price_above', 'price_below', 'score_above', 'score_below', 'rsi_above', 'rsi_below']);

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

// ---------- broker-key vault (2026-07-26, Trading 212 portfolio import) ----------
// Broker API keys are AES-256-GCM encrypted at rest with Worker secret
// VAULT_KEY (32-byte hex, exists ONLY as a Worker secret): random 12-byte IV
// per encryption, stored as base64(iv || ciphertext) in D1 broker_keys.enc.
// Keys never appear in logs, responses, or error messages.

function vaultKey(env) {
  const raw = new Uint8Array(env.VAULT_KEY.match(/../g).map(h => parseInt(h, 16)));
  return crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

async function vaultEncrypt(env, text) {
  const key = await vaultKey(env);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, new TextEncoder().encode(text)));
  const buf = new Uint8Array(iv.length + ct.length);
  buf.set(iv); buf.set(ct, iv.length);
  return btoa(String.fromCharCode(...buf));
}

async function vaultDecrypt(env, b64) {
  const buf = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const key = await vaultKey(env);
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: buf.slice(0, 12) }, key, buf.slice(12));
  return new TextDecoder().decode(pt);
}

// Trading 212 public API. Auth header is the RAW key (no "Bearer"). Rate
// limits are strict (~1 req / 5s per endpoint per key) — portfolio responses
// are KV-cached 60s per user and the page throttles its refresh button.
const T212_BASE = { live: 'https://live.trading212.com/api/v0', demo: 'https://demo.trading212.com/api/v0' };
// T212 auth is HTTP Basic with an API key + secret pair (verified 2026-07-26 —
// the older single-token header now just 401s). Stored credential = "key:secret";
// a legacy colon-less value falls back to the raw-token header.
const t212Fetch = (envName, path, cred) =>
  fetch(T212_BASE[envName] + path, { headers: {
    'Authorization': cred.includes(':') ? 'Basic ' + btoa(cred) : cred,
    'Accept': 'application/json',
  } });

// T212 prices come in each INSTRUMENT's currency (US in USD, LSE in PENCE)
// while ppl is in the ACCOUNT currency — summing raw qty×price across a mixed
// portfolio is meaningless. Infer the instrument currency from the T212 ticker
// suffix and convert values to the account currency via Yahoo FX (1h KV cache).
const t212Ccy = t => {
  if (!t) return null;
  if (/_US_EQ$/.test(t)) return 'USD';
  const m = String(t).match(/([a-z])_EQ$/);
  if (m) return { l: 'GBX', d: 'EUR', p: 'EUR', a: 'EUR', e: 'EUR', s: 'CHF' }[m[1]] || null;
  return null;
};

async function fxRate(env, from, to) {
  if (!from || !to) return null;
  if (from === to) return 1;
  if (from === 'GBX') { const r = await fxRate(env, 'GBP', to); return r === null ? null : r / 100; }
  const k = 'fx:' + from + to;
  try { const c = await env.CONFIG?.get(k); if (c) return parseFloat(c); } catch (e) { /* miss */ }
  try {
    const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${from}${to}=X?range=1d&interval=1d`, { headers: YA_UA });
    const p = (await r.json()).chart.result[0].meta.regularMarketPrice;
    if (p) { try { await env.CONFIG?.put(k, String(p), { expirationTtl: 3600 }); } catch (e) { /* cache only */ } return p; }
  } catch (e) { /* FX unavailable — values stay unconverted */ }
  return null;
}

// raw T212 /equity/portfolio array -> our position shape, with per-position
// instrument currency and account-currency value when FX is resolvable.
// Currency comes from T212's OWN instrument metadata (LSE mixes GBX equities
// with GBP-priced ETFs — the suffix alone misled by 100x); the metadata list
// (~4MB) is fetched only when unseen tickers appear and the needed entries are
// cached 30d in one KV map. Suffix heuristic is the fallback (keys without
// the metadata permission 403 there).
async function t212Positions(env, rawPf, acctCcy, envName, cred) {
  const positions = (Array.isArray(rawPf) ? rawPf : []).map(p => ({
    t212: p.ticker, qty: p.quantity, avgPrice: p.averagePrice, price: p.currentPrice,
    ppl: p.ppl ?? null, fxPpl: p.fxPpl ?? null, ccy: null, valueAcct: null,
  }));
  let map = {};
  try { map = JSON.parse((await env.CONFIG?.get('t212ccymap', { cacheTtl: 3600 })) || '{}'); } catch (e) { /* fresh map */ }
  if (positions.some(p => !map[p.t212]) && envName && cred) {
    try {
      const r = await t212Fetch(envName, '/equity/metadata/instruments', cred);
      if (r.ok) {
        const need = new Set(positions.map(p => p.t212));
        for (const x of await r.json()) if (need.has(x.ticker) && x.currencyCode) map[x.ticker] = x.currencyCode;
      }
    } catch (e) { /* metadata unavailable — heuristic below */ }
    for (const p of positions) if (!map[p.t212]) { const g = t212Ccy(p.t212); if (g) map[p.t212] = g; }
    try { await env.CONFIG?.put('t212ccymap', JSON.stringify(map), { expirationTtl: 30 * 86400 }); } catch (e) { /* cache only */ }
  }
  for (const p of positions) p.ccy = map[p.t212] || t212Ccy(p.t212);
  if (acctCcy) {
    const rates = {};
    for (const c of new Set(positions.map(p => p.ccy).filter(Boolean))) rates[c] = await fxRate(env, c, acctCcy);
    for (const p of positions) {
      const r = p.ccy && rates[p.ccy];
      if (r && p.qty != null && p.price != null) p.valueAcct = Math.round(p.qty * p.price * r * 100) / 100;
    }
  }
  return positions;
}

async function sessionUser(req, env, ctx) {
  const tok = (req.headers.get('Authorization') || '').replace(/^Bearer\s+/i, '');
  if (!/^[a-f0-9]{64}$/.test(tok)) return null;
  const now = Math.floor(Date.now() / 1000);
  const row = await env.DB.prepare(
    'SELECT u.id, u.email, u.verified, s.token FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ?'
  ).bind(tok, now).first();
  if (row) {
    // canary tripwire: a session can only resolve to the canary account if
    // someone forged one from DB contents — that's a compromise signal
    try {
      const cid = await canaryId(env);
      if (cid && row.id === cid) {
        const p = canaryTrip(env, req, 'session token resolved to the canary account');
        if (ctx) ctx.waitUntil(p);
      }
    } catch (e) { /* detection must never break auth */ }
  }
  return row;
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
    secLog(env, ctx, 'signup', 'auth/signup', req);
    try { await sendVerifyMail(env, uid, email); } catch (e) { /* account still usable; resend available */ }
    const token = await issueSession(env, uid);
    return json(await userPayload(env, { id: uid, email, verified: 0 }, token), 200, 0);
  }

  if (route === 'auth/login' && req.method === 'POST') {
    if (!email) return json({ error: 'enter your email' }, 400, 0);
    if (!(await rateOk(env, 'li:' + ip + ':' + email, 10, 3600))) return json({ error: 'too many attempts — try again later' }, 429, 0);
    const u = await env.DB.prepare('SELECT * FROM users WHERE email = ?').bind(email).first();
    try {
      // canary tripwire: nobody legitimate knows the canary email exists
      const cid = await canaryId(env);
      if (u && cid && u.id === cid) ctx.waitUntil(canaryTrip(env, req, 'login attempt on the canary account'));
    } catch (e) { /* detection must never break auth */ }
    const ok = u && (await pwHash(String((body && body.password) || ''), u.salt)) === u.pw_hash;
    if (!ok) {
      secLog(env, ctx, 'login_fail', 'auth/login', req);
      return json({ error: 'wrong email or password' }, 401, 0);
    }
    const token = await issueSession(env, u.id);
    return json(await userPayload(env, u, token), 200, 0);
  }

  if (route === 'auth/logout' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
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
    const u = await sessionUser(req, env, ctx);
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
    try {
      const cid = await canaryId(env);
      if (cid && row.user_id === cid) ctx.waitUntil(canaryTrip(env, req, 'password reset on the canary account'));
    } catch (e) { /* detection must never break auth */ }
    secLog(env, ctx, 'password_reset', 'auth/reset', req);
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
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    await env.DB.batch([
      env.DB.prepare('DELETE FROM sessions WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM tokens WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM watchlists WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM broker_keys WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM portfolio_history WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM alert_rules WHERE user_id = ?').bind(u.id),
      env.DB.prepare('DELETE FROM users WHERE id = ?').bind(u.id),
    ]);
    try {
      await env.CONFIG?.delete('t212:' + u.id);
      await env.CONFIG?.delete('t212cur:' + u.id);
    } catch (e) { /* cache only */ }
    secLog(env, ctx, 'account_delete', 'auth/delete-account', req);
    return json({ ok: true, note: 'account and data deleted' }, 200, 0);
  }

  if (route === 'me' && req.method === 'GET') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const full = await env.DB.prepare('SELECT * FROM users WHERE id = ?').bind(u.id).first();
    return json(await userPayload(env, full, undefined), 200, 0);
  }

  if (route === 'me/alerts' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
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

  // ----- Trading 212 portfolio import (2026-07-26) -----
  // Connect a READ-ONLY T212 key: validate against the cheap /account/cash
  // endpoint (live first, then demo/practice), fetch the account currency,
  // encrypt-at-rest and upsert. The key itself is never echoed back.
  if (route === 'me/t212' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    if (!env.VAULT_KEY) return json({ error: 'broker connections not provisioned' }, 503, 0);
    const apiKey = String((body && body.key) || '').trim();
    const secret = String((body && body.secret) || '').trim();
    if (!/^\S{10,300}$/.test(apiKey)) return json({ error: 'that does not look like a Trading 212 API key — copy it from the T212 app (Settings → API)' }, 400, 0);
    if (!/^\S{10,300}$/.test(secret)) return json({ error: 'the secret key is missing — Trading 212 shows it once when the key is created; paste both fields' }, 400, 0);
    const key = apiKey + ':' + secret;
    if (!(await rateOk(env, 'br:' + u.id, 6, 300))) return json({ error: 'too many attempts — try again in a few minutes' }, 429, 0);
    // Validate against /equity/portfolio — the ONLY endpoint this feature
    // needs. T212 keys have granular permission checkboxes; a key with just
    // the Portfolio permission 403s on /equity/account/cash, which used to
    // fail perfectly good keys here.
    let t212env = null, r = null, rLive = null;
    try {
      rLive = r = await t212Fetch('live', '/equity/portfolio', key);
      if (r.ok) t212env = 'live';
      else {
        // practice keys 401 on the live host — try demo before giving up
        r = await t212Fetch('demo', '/equity/portfolio', key);
        if (r.ok) t212env = 'demo';
      }
    } catch (e) { return json({ error: 'could not reach Trading 212 — try again shortly' }, 502, 0); }
    if (!t212env) {
      if (r && r.status === 429) return json({ error: 'Trading 212 is rate-limiting right now — wait a few seconds and try again' }, 429, 0);
      const st = `${rLive ? rLive.status : '?'} live / ${r ? r.status : '?'} demo`;
      const scopeHint = (rLive && rLive.status === 403) || (r && r.status === 403)
        ? ' — the key was recognised but lacks the Portfolio permission: regenerate it with Portfolio ticked'
        : ' — check it is copied in full, is the API key (not the account number), and still active';
      return json({ error: `Trading 212 did not accept this key (${st})${scopeHint}` }, 400, 0);
    }
    const rawPf = await r.json().catch(() => null);
    let currency = null;
    try {
      // optional — needs the Account data permission; skipped silently without it
      const ri = await t212Fetch(t212env, '/equity/account/info', key);
      if (ri.ok) currency = ((await ri.json()) || {}).currencyCode || null;
    } catch (e) { /* label only — connection still succeeds */ }
    const enc = await vaultEncrypt(env, key);
    const now = Math.floor(Date.now() / 1000);
    await env.DB.prepare(
      "INSERT INTO broker_keys (user_id, provider, enc, env, created_at) VALUES (?, 't212', ?, ?, ?) " +
      'ON CONFLICT(user_id) DO UPDATE SET provider = ?, enc = ?, env = ?, created_at = ?'
    ).bind(u.id, enc, t212env, now, 't212', enc, t212env, now).run();
    try {
      if (currency) await env.CONFIG?.put('t212cur:' + u.id, currency, { expirationTtl: 30 * 86400 });
      // prime the portfolio cache with the validation response — the page loads
      // holdings right after connecting, and T212 only allows ~1 req/5s
      const positions = await t212Positions(env, rawPf, currency, t212env, key);
      // no currency (account/info rate-limited during connect) -> short-lived
      // prime only, so the next fetch resolves the currency and converts values
      await env.CONFIG?.put('t212:' + u.id, JSON.stringify(
        { connected: true, env: t212env, currency, positions, fetchedAt: new Date().toISOString() }
      ), { expirationTtl: currency ? 60 : 10 });
    } catch (e) { /* cache only */ }
    secLog(env, ctx, 't212_connect', 'env=' + t212env, req);
    return json({ ok: true, currency, env: t212env }, 200, 0);
  }

  if (route === 'me/t212/delete' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    await env.DB.prepare('DELETE FROM broker_keys WHERE user_id = ?').bind(u.id).run();
    // disconnect wipes the value history too — privacy-first: no broker key,
    // no stored broker-derived data
    try { await env.DB.prepare('DELETE FROM portfolio_history WHERE user_id = ?').bind(u.id).run(); } catch (e) { /* table optional */ }
    try {
      await env.CONFIG?.delete('t212:' + u.id);
      await env.CONFIG?.delete('t212cur:' + u.id);
    } catch (e) { /* cache only */ }
    secLog(env, ctx, 't212_delete', 'me/t212/delete', req);
    return json({ ok: true }, 200, 0);
  }

  if (route === 'me/portfolio/history' && req.method === 'GET') {
    // daily account-value snapshots (written by the 20:30 cron) — the user's
    // own performance line on the Portfolio tab. d = UTC daynum.
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    let days = [];
    try {
      days = (await env.DB.prepare('SELECT d, total, invested, ppl FROM portfolio_history WHERE user_id = ? ORDER BY d').bind(u.id).all()).results || [];
    } catch (e) { /* table not provisioned yet */ }
    return json({ days }, 200, 0);
  }

  // ---- custom per-stock alert rules (2026-07-27) ----
  // One-shot rules evaluated by the daily digest cron against last-data.json:
  // when a rule fires it's stamped triggered_at and won't fire again until the
  // user re-arms it. Price thresholds are in the stock's LISTING unit (pence
  // for London) — the same numbers the site displays. Emails still require
  // the account-level alerts opt-in (same flag as the watchlist digest).
  if (route === 'me/rules' && req.method === 'GET') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    let rules = [];
    try {
      rules = (await env.DB.prepare('SELECT id, ticker, kind, threshold, triggered_at FROM alert_rules WHERE user_id = ? ORDER BY id DESC').bind(u.id).all()).results || [];
    } catch (e) { /* table not provisioned yet */ }
    return json({ rules }, 200, 0);
  }

  if (route === 'me/rules' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const t = String((body && body.ticker) || '').toUpperCase();
    const kind = String((body && body.kind) || '');
    const thr = Number(body && body.threshold);
    if (!TICK_RE.test(t) || !RULE_KINDS.has(kind) || !isFinite(thr)) return json({ error: 'bad rule' }, 400, 0);
    const n = ((await env.DB.prepare('SELECT COUNT(*) AS n FROM alert_rules WHERE user_id = ?').bind(u.id).first()) || {}).n || 0;
    if (n >= 20) return json({ error: 'rule limit reached (20) — delete one first' }, 400, 0);
    await env.DB.prepare('INSERT INTO alert_rules (user_id, ticker, kind, threshold, created_at) VALUES (?, ?, ?, ?, ?)')
      .bind(u.id, t, kind, thr, Math.floor(Date.now() / 1000)).run();
    const rules = (await env.DB.prepare('SELECT id, ticker, kind, threshold, triggered_at FROM alert_rules WHERE user_id = ? ORDER BY id DESC').bind(u.id).all()).results || [];
    return json({ ok: true, rules }, 200, 0);
  }

  if (route === 'me/rules/delete' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const id = Number(body && body.id);
    if (!Number.isInteger(id)) return json({ error: 'bad id' }, 400, 0);
    await env.DB.prepare('DELETE FROM alert_rules WHERE id = ? AND user_id = ?').bind(id, u.id).run();
    return json({ ok: true }, 200, 0);
  }

  if (route === 'me/rules/rearm' && req.method === 'POST') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const id = Number(body && body.id);
    if (!Number.isInteger(id)) return json({ error: 'bad id' }, 400, 0);
    await env.DB.prepare('UPDATE alert_rules SET triggered_at = NULL WHERE id = ? AND user_id = ?').bind(id, u.id).run();
    return json({ ok: true }, 200, 0);
  }

  if (route === 'me/portfolio' && req.method === 'GET') {
    const u = await sessionUser(req, env, ctx);
    if (!u) return json({ error: 'not signed in' }, 401, 0);
    const row = await env.DB.prepare("SELECT enc, env FROM broker_keys WHERE user_id = ? AND provider = 't212'").bind(u.id).first();
    if (!row) return json({ connected: false }, 404, 0);
    if (!env.VAULT_KEY) return json({ error: 'broker connections not provisioned' }, 503, 0);
    try {
      const hit = await env.CONFIG?.get('t212:' + u.id);
      if (hit) return json(JSON.parse(hit), 200, 0);
    } catch (e) { /* cache miss */ }
    let key;
    try { key = await vaultDecrypt(env, row.enc); }
    catch (e) { return json({ connected: true, keyInvalid: true }, 200, 0); }   // undecryptable (e.g. rotated VAULT_KEY) -> reconnect
    let r;
    try { r = await t212Fetch(row.env, '/equity/portfolio', key); }
    catch (e) { return json({ error: 'could not reach Trading 212 — try again shortly' }, 502, 0); }
    if (r.status === 401 || r.status === 403) return json({ connected: true, keyInvalid: true }, 200, 0);
    if (r.status === 429) return json({ error: 'Trading 212 is rate-limiting — try again in a few seconds', rateLimited: true }, 429, 0);
    if (!r.ok) return json({ error: 'Trading 212 error ' + r.status }, 502, 0);
    const raw = await r.json().catch(() => null);
    let currency = null;
    try { currency = await env.CONFIG?.get('t212cur:' + u.id); } catch (e) { /* optional */ }
    if (!currency) {
      try {
        const ri = await t212Fetch(row.env, '/equity/account/info', key);
        if (ri.ok) {
          currency = ((await ri.json()) || {}).currencyCode || null;
          if (currency) await env.CONFIG?.put('t212cur:' + u.id, currency, { expirationTtl: 30 * 86400 });
        }
      } catch (e) { /* label only */ }
    }
    const positions = await t212Positions(env, raw, currency, row.env, key);
    // account cash/total (needs the Account-data permission — skipped silently
    // without it) lets the page reconcile with the T212 app's headline number
    let cash = null;
    try {
      const rc = await t212Fetch(row.env, '/equity/account/cash', key);
      if (rc.ok) { const c = await rc.json(); cash = { free: c.free ?? null, total: c.total ?? null, invested: c.invested ?? null, ppl: c.ppl ?? null }; }
    } catch (e) { /* optional */ }
    const out = { connected: true, env: row.env, currency, positions, cash, fetchedAt: new Date().toISOString() };
    try { await env.CONFIG?.put('t212:' + u.id, JSON.stringify(out), { expirationTtl: 60 }); } catch (e) { /* cache only */ }
    return json(out, 200, 0);
  }

  if (route === 'me/watchlist' && req.method === 'PUT') {
    const u = await sessionUser(req, env, ctx);
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
// One email per opted-in verified user when a starred stock did something
// noteworthy today (|price move| >= 5% or |score change| >= 15) OR one of
// their custom alert rules fired. Data comes from the pipeline's
// last-data.json (public, no vendor re-serving concern: the digest goes to
// the user, not an API). Fired rules are stamped triggered_at only after the
// email actually sends, so a mail failure doesn't eat the alert.
async function sendDigests(env, ctx) {
  if (!env.DB || !env.RESEND_API_KEY) return;
  const r = await fetch(STATE_RAW + 'last-data.json');
  if (!r.ok) return;
  const by = {};
  for (const d of await r.json()) by[d.ticker] = d;
  // LEFT JOIN: a user with custom rules but no watchlist still gets alerts
  const users = (await env.DB.prepare(
    'SELECT u.id, u.email, u.unsub_token, w.tickers FROM users u LEFT JOIN watchlists w ON w.user_id = u.id WHERE u.alerts = 1 AND u.verified = 1'
  ).all()).results || [];
  let rulesBy = {};
  try {
    for (const rl of (await env.DB.prepare('SELECT id, user_id, ticker, kind, threshold FROM alert_rules WHERE triggered_at IS NULL').all()).results || []) {
      (rulesBy[rl.user_id] = rulesBy[rl.user_id] || []).push(rl);
    }
  } catch (e) { /* rules table not provisioned yet */ }
  const ruleVal = (d, kind) => kind.startsWith('price') ? d.price
    : kind.startsWith('score') ? d.combinedScore
    : (d.rsi != null ? d.rsi : (d.technicals && d.technicals.rsi14));
  const RULE_TEXT = { price_above: 'price rose to', price_below: 'price fell to',
    score_above: 'combined score rose above', score_below: 'combined score fell below',
    rsi_above: 'RSI rose above', rsi_below: 'RSI fell below' };
  for (const u of users) {
    let list = [];
    try { list = JSON.parse(u.tickers || '[]'); } catch (e) { /* no watchlist */ }
    const events = [];
    for (const t of list) {
      const d = by[t];
      if (!d) continue;
      const move = typeof d.dayChange === 'number' ? d.dayChange : null;
      const sd = typeof d.scoreDelta === 'number' ? d.scoreDelta : null;
      const notes = [];
      if (move !== null && Math.abs(move) >= 5) notes.push((move > 0 ? '▲ up ' : '▼ down ') + Math.abs(move).toFixed(1) + '% today');
      // combinedScore is ~0-100 with ~5 pts of routine daily churn — only a
      // top-decile move (>=15) counts as an alert-worthy event
      if (sd !== null && Math.abs(sd) >= 15) notes.push('score ' + (sd > 0 ? 'up' : 'down') + ' ' + Math.abs(sd).toFixed(1) + ' vs yesterday');
      if (notes.length) events.push({ d, notes });
    }
    // custom rules: one-shot threshold crossings on today's snapshot
    const fired = [];
    for (const rl of rulesBy[u.id] || []) {
      const d = by[rl.ticker];
      if (!d) continue;
      const v = ruleVal(d, rl.kind);
      if (typeof v !== 'number') continue;
      if (rl.kind.endsWith('_above') ? v >= rl.threshold : v <= rl.threshold) fired.push({ rl, d, v });
    }
    if (!events.length && !fired.length) continue;
    const rows = events.map(({ d, notes }) => `
      <tr><td style="padding:9px 0;border-bottom:1px solid #eef1f6"><b>${d.ticker}</b> <span style="color:#7d8595">${(d.name || '').slice(0, 28)}</span><br>
      <span style="font-size:13px;color:#4c5566">${notes.join(' · ')}</span></td>
      <td style="padding:9px 0;border-bottom:1px solid #eef1f6;text-align:right;white-space:nowrap">${d.price != null ? d.price : ''} <span style="color:${(d.dayChange || 0) >= 0 ? '#0ca30c' : '#d03b3b'}">${d.dayChange != null ? (d.dayChange >= 0 ? '+' : '') + d.dayChange.toFixed(1) + '%' : ''}</span></td></tr>`).join('');
    const fRows = fired.map(({ rl, d, v }) => `
      <tr><td style="padding:9px 0;border-bottom:1px solid #eef1f6"><b>${d.ticker}</b> <span style="color:#7d8595">${(d.name || '').slice(0, 28)}</span><br>
      <span style="font-size:13px;color:#4c5566">your alert: ${RULE_TEXT[rl.kind]} ${rl.threshold} (now ${typeof v === 'number' ? Math.round(v * 100) / 100 : v})</span></td>
      <td style="padding:9px 0;border-bottom:1px solid #eef1f6;text-align:right;white-space:nowrap">${d.price != null ? d.price : ''}</td></tr>`).join('');
    const html = mailWrap(`<p style="margin:0 0 4px;font-size:17px;font-weight:700">${events.length ? 'Your watchlist today' : 'Your alerts fired'}</p>
      <p style="margin:6px 0 10px;color:#4c5566;font-size:13.5px">${events.length ? 'Stocks you follow that moved meaningfully' : 'Alert rules you set were hit today'} — full picture on <a href="${SITE}#watchlist" style="color:#1e4f91">your watchlist</a>.</p>
      ${rows ? `<table style="width:100%;border-collapse:collapse;font-size:14.5px">${rows}</table>` : ''}
      ${fRows ? `<p style="margin:${rows ? '14px' : '0'} 0 4px;font-size:13px;font-weight:700;color:#1c2534">Your alerts</p>
      <table style="width:100%;border-collapse:collapse;font-size:14.5px">${fRows}</table>
      <p style="font-size:12px;color:#7d8595;margin:8px 0 0">Fired alerts pause until you re-arm them on the site.</p>` : ''}
      <p style="font-size:12px;color:#7d8595;margin:14px 0 0">Mechanical screen, not investment advice. <a href="https://api.valuetally.com/alerts/unsubscribe?u=${u.unsub_token}" style="color:#7d8595">Unsubscribe from these alerts</a>.</p>`);
    const subjTicks = [...new Set([...events.map(e => e.d.ticker), ...fired.map(f => f.d.ticker)])];
    try {
      await sendMail(env, u.email, 'ValueTally ' + (events.length ? 'watchlist' : 'alert') + ': ' + subjTicks.slice(0, 4).join(', ') + (subjTicks.length > 4 ? '…' : ''), html);
      const now = Math.floor(Date.now() / 1000);
      for (const f of fired) {
        try { await env.DB.prepare('UPDATE alert_rules SET triggered_at = ? WHERE id = ?').bind(now, f.rl.id).run(); } catch (e) { /* re-fires tomorrow */ }
      }
    } catch (e) { /* next user */ }
  }
}

// ---------- daily portfolio value snapshot (2026-07-27) ----------
// One row per connected T212 user per day: account total/invested/P&L from
// the cheap /equity/account/cash call (positions aren't needed for a value
// history). Runs on the 20:30 digest cron (after US close); re-runs the same
// day overwrite the row, so the last run stands. Feeds /me/portfolio/history.
async function snapshotPortfolios(env, ctx) {
  if (!env.DB || !env.VAULT_KEY) return;
  const dn = Math.floor(Date.now() / 86400000);
  let rows = [];
  try { rows = (await env.DB.prepare('SELECT user_id, enc, env FROM broker_keys').all()).results || []; } catch (e) { return; }
  for (const row of rows) {
    try {
      const cred = await vaultDecrypt(env, row.enc);
      const r = await t212Fetch(row.env, '/equity/account/cash', cred);
      if (!r.ok) continue; // revoked key etc. — the portfolio route surfaces that
      const c = await r.json();
      if (typeof c.total !== 'number') continue;
      await env.DB.prepare(
        'INSERT INTO portfolio_history (user_id, d, total, invested, ppl) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, d) DO UPDATE SET total = ?, invested = ?, ppl = ?'
      ).bind(row.user_id, dn, c.total, c.invested ?? null, c.ppl ?? null, c.total, c.invested ?? null, c.ppl ?? null).run();
    } catch (e) { /* next user */ }
  }
}

// ---------- dead-man alarm (2026-07-25): is the hourly publish still landing? ----------
// Cron "15 10-19 * * 1-5" — publish hours are 7:45–19:45 UTC weekdays, so by
// 10:15 the newest publish should never be older than ~90 min. Alerts the
// owner's ntfy topic only when watchlist-state.json's updatedAt is >2h old
// (two consecutive publishes missed) or state can't be read at all, deduped
// via KV to one alert per 4h so a dead pipeline doesn't spam the phone.
// dry=true (the /admin/deadman-check route) NEVER sends — the owner topic
// must not receive test messages.
const OWNER_NTFY = 'https://ntfy.sh/harris-stockdash-3cb22f88';
async function deadmanCheck(env, dry) {
  const out = { staleMinutes: null, wouldAlert: false, alerted: false, dry: !!dry };
  try {
    const r = await fetch(STATE_RAW + 'watchlist-state.json');
    if (!r.ok) throw new Error('state fetch ' + r.status);
    const upd = Date.parse((await r.json()).updatedAt);
    out.staleMinutes = Math.round((Date.now() - upd) / 60000);
    out.wouldAlert = !(out.staleMinutes >= 0) || out.staleMinutes > 120;
  } catch (e) {
    out.error = String((e && e.message) || e);
    out.wouldAlert = true; // unreadable state is itself an alarm condition
  }
  if (!out.wouldAlert || dry) return out;
  const last = Number(await env.CONFIG?.get('deadmanLastAlert')) || 0;
  if (Date.now() - last < 4 * 3600 * 1000) { out.deduped = true; return out; }
  await env.CONFIG?.put('deadmanLastAlert', String(Date.now()));
  await fetch(OWNER_NTFY, {
    method: 'POST',
    headers: { 'Title': 'ValueTally pipeline stalled', 'Tags': 'rotating_light',
               'Click': 'https://github.com/Harris120807/stock-dashboard/actions' },
    body: out.staleMinutes != null
      ? `No hourly publish for ${Math.floor(out.staleMinutes / 60)}h${out.staleMinutes % 60}m during market hours. Check GitHub Actions and cron-job.org.`
      : `Pipeline state is unreadable (${out.error || 'unknown error'}). Check GitHub Actions and the claude/state branch.`,
  });
  out.alerted = true;
  return out;
}

// ---------- security monitoring (2026-07-26): D1 tamper/intrusion detection ----------
// Best-effort event log (D1 table security_log) + a canary decoy account + an
// hourly anomaly check piggybacked on the dead-man cron. All of it is wrapped
// so a broken security path can NEVER break serving. detail holds route names
// or generic text only — never emails, passwords or tokens.

// Fire-and-forget event log row. Returns the (already .catch'd) promise so
// callers inside waitUntil'd work can await it; request handlers pass ctx.
function secLog(env, ctx, kind, detail, req) {
  try {
    if (!env.DB) return;
    const p = env.DB.prepare('INSERT INTO security_log (at, kind, detail, country) VALUES (?, ?, ?, ?)')
      .bind(Math.floor(Date.now() / 1000), kind, String(detail || '').slice(0, 80), (req && req.cf && req.cf.country) || null)
      .run().catch(() => {});
    if (ctx) ctx.waitUntil(p);
    return p;
  } catch (e) { /* security logging must never break serving */ }
}

// Best-effort route-error log (D1 error_log, 2026-07-27): our own exceptions,
// so a silently broken route shows up as an "Errors (24h)" count on the admin
// Status card instead of waiting for user complaints. detail = our error text
// only — never request bodies, tokens, or user data.
function errLog(env, ctx, route, e) {
  try {
    if (!env.DB) return;
    const p = env.DB.prepare('INSERT INTO error_log (at, route, detail) VALUES (?, ?, ?)')
      .bind(Math.floor(Date.now() / 1000), String(route || '').slice(0, 60), String((e && e.message) || e || '').slice(0, 140))
      .run().catch(() => {});
    if (ctx) ctx.waitUntil(p);
  } catch (e2) { /* error logging must never break serving */ }
}

// The canary user's id lives in KV ('canaryUserId'). The deploy token can't
// write KV, so if the key is missing the Worker resolves it from D1 once
// (the canary is the only 'canary+…@valuetally.com' row) and persists it.
async function canaryId(env) {
  try {
    const v = await env.CONFIG?.get('canaryUserId', { cacheTtl: 300 });
    if (v) return Number(v) || null;
    const row = await env.DB?.prepare("SELECT id FROM users WHERE email LIKE 'canary+%@valuetally.com' ORDER BY id LIMIT 1").first();
    if (row && row.id) { await env.CONFIG?.put('canaryUserId', String(row.id)); return row.id; }
  } catch (e) { /* no canary known — checks degrade gracefully */ }
  return null;
}

// The canary account's credentials exist NOWHERE outside its DB row — no
// mailbox, no password anyone was ever told. Any login attempt or session that
// resolves to it therefore means someone is reading/using database contents.
// Always logs; ntfy is capped to one per 30 min purely to avoid a flood loop.
async function canaryTrip(env, req, via) {
  try {
    await secLog(env, null, 'canary_login', via, req);
    const last = Number(await env.CONFIG?.get('canaryLastAlert')) || 0;
    if (Date.now() - last < 30 * 60 * 1000) return;
    await env.CONFIG?.put('canaryLastAlert', String(Date.now()));
    await fetch(OWNER_NTFY, {
      method: 'POST',
      headers: { 'Title': 'ValueTally SECURITY ALERT', 'Tags': 'rotating_light',
                 'Click': 'https://valuetally.com/admin' },
      body: `Canary account touched (${via}). Its credentials exist nowhere legitimate, so this very likely means the accounts database has been read or copied. Check the admin Security panel; consider rotating the Worker secrets and forcing password resets.`,
    });
  } catch (e) { /* never break the request that tripped it */ }
}

// Hourly anomaly check (piggybacks the '15 10-19 * * 1-5' dead-man cron).
// Compares user/session counts against the KV baseline ('secBaseline') and
// scans the last hour of security_log. dry=true (the /admin/security-check
// route) NEVER alerts and NEVER moves the baseline.
async function securityCheck(env, dry) {
  const out = { dry: !!dry, wouldAlert: false, reasons: [], alerted: false };
  try {
    if (!env.DB) { out.error = 'accounts not provisioned'; return out; }
    const now = Math.floor(Date.now() / 1000);
    const n1 = async (sql, ...binds) => ((await env.DB.prepare(sql).bind(...binds).first()) || {}).n || 0;
    out.users = await n1('SELECT COUNT(*) AS n FROM users');
    out.sessions = await n1('SELECT COUNT(*) AS n FROM sessions');
    out.adminFails1h = await n1("SELECT COUNT(*) AS n FROM security_log WHERE kind = 'admin_auth_fail' AND at > ?", now - 3600);
    out.loginFails1h = await n1("SELECT COUNT(*) AS n FROM security_log WHERE kind = 'login_fail' AND at > ?", now - 3600);
    let base = null;
    try { base = JSON.parse((await env.CONFIG?.get('secBaseline')) || 'null'); } catch (e) { /* first run */ }
    out.baseline = base;
    if (base && typeof base.users === 'number') {
      const drop = base.users - out.users;
      if (drop > Math.max(2, base.users * 0.2)) out.reasons.push(`user count fell ${base.users} → ${out.users} since the last check (possible mass deletion)`);
    }
    if (out.adminFails1h >= 10) out.reasons.push(out.adminFails1h + ' failed admin-key attempts in the last hour (possible key brute-force)');
    if (out.loginFails1h >= 50) out.reasons.push(out.loginFails1h + ' failed logins in the last hour (possible credential stuffing)');
    out.wouldAlert = out.reasons.length > 0;
    if (dry) return out;
    // baseline always moves after a real check, alert or not
    await env.CONFIG?.put('secBaseline', JSON.stringify({ users: out.users, sessions: out.sessions, at: now }));
    if (!out.wouldAlert) return out;
    const last = Number(await env.CONFIG?.get('secLastAlert')) || 0;
    if (Date.now() - last < 4 * 3600 * 1000) { out.deduped = true; return out; }
    await env.CONFIG?.put('secLastAlert', String(Date.now()));
    await fetch(OWNER_NTFY, {
      method: 'POST',
      headers: { 'Title': 'ValueTally security warning', 'Tags': 'warning',
                 'Click': 'https://valuetally.com/admin' },
      body: 'Accounts-database anomaly: ' + out.reasons.join('; ') + '. Details on the admin Security panel.',
    });
    out.alerted = true;
  } catch (e) { out.error = String((e && e.message) || e); }
  return out;
}

// ---------- Saturday weekly wrap (2026-07-25) ----------
// One email per opted-in verified user with a watchlist: the universe's week
// (top/bottom movers by weekChange, computed by refresh.py from the daily
// shards), the system watchlist's week (from track-record.json), and the
// user's own starred stocks' week. Unlike the daily digest this always sends
// — it's a wrap, not an event alert. Same opt-in flag + unsubscribe link.
async function sendWeekly(env, ctx) {
  if (!env.DB || !env.RESEND_API_KEY) return;
  const r = await fetch(STATE_RAW + 'last-data.json');
  if (!r.ok) return;
  const all = await r.json();
  const by = {};
  for (const d of all) by[d.ticker] = d;
  const wk = all.filter(d => typeof d.weekChange === 'number').sort((a, b) => b.weekChange - a.weekChange);
  if (!wk.length) return;
  const pct = v => `<span style="color:${v >= 0 ? '#0ca30c' : '#d03b3b'}">${(v >= 0 ? '+' : '') + v.toFixed(1)}%</span>`;
  const li = d => `<tr><td style="padding:6px 0;border-bottom:1px solid #eef1f6"><b>${d.ticker}</b> <span style="color:#7d8595">${(d.name || '').slice(0, 26)}</span></td><td style="padding:6px 0;border-bottom:1px solid #eef1f6;text-align:right">${pct(d.weekChange)}</td></tr>`;
  let wlLine = '';
  try {
    const tr = await (await fetch(STATE_RAW + 'track-record.json')).json();
    const days = (tr.days || []).slice(-6);
    if (days.length) {
      const f = days[0], l = days[days.length - 1];
      const inn = [...l.buy.filter(t => !f.buy.includes(t)), ...l.sell.filter(t => !f.sell.includes(t))];
      const outt = [...f.buy.filter(t => !l.buy.includes(t)), ...f.sell.filter(t => !l.sell.includes(t))];
      wlLine = `<p style="margin:12px 0 0;font-size:13.5px;color:#4c5566"><b style="color:#1c2534">System watchlist:</b> buy ${l.buy.join(', ')} · sell ${l.sell.join(', ')}` +
        (inn.length || outt.length ? ` — this week in: ${inn.join(', ') || 'none'}; out: ${outt.join(', ') || 'none'}.` : ' — unchanged this week.') + '</p>';
    }
  } catch (e) { /* optional section */ }
  const users = (await env.DB.prepare(
    'SELECT u.id, u.email, u.unsub_token, w.tickers FROM users u JOIN watchlists w ON w.user_id = u.id WHERE u.alerts = 1 AND u.verified = 1'
  ).all()).results || [];
  for (const u of users) {
    let list;
    try { list = JSON.parse(u.tickers); } catch (e) { continue; }
    const mine = list.map(t => by[t]).filter(d => d && typeof d.weekChange === 'number')
      .sort((a, b) => b.weekChange - a.weekChange).slice(0, 15);
    const html = mailWrap(`<p style="margin:0 0 4px;font-size:17px;font-weight:700">Your week on ValueTally</p>
      <p style="margin:6px 0 12px;color:#4c5566;font-size:13.5px">How the market — and your list — moved this week.</p>
      ${mine.length ? `<p style="margin:0 0 4px;font-size:13px;font-weight:700;color:#1c2534">Your starred stocks</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px">${mine.map(li).join('')}</table>` : ''}
      <p style="margin:14px 0 4px;font-size:13px;font-weight:700;color:#1c2534">Best of the universe</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px">${wk.slice(0, 5).map(li).join('')}</table>
      <p style="margin:14px 0 4px;font-size:13px;font-weight:700;color:#1c2534">Worst of the universe</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px">${wk.slice(-5).reverse().map(li).join('')}</table>
      ${wlLine}
      <p style="font-size:12px;color:#7d8595;margin:16px 0 0">Mechanical screen, not investment advice. <a href="https://api.valuetally.com/alerts/unsubscribe?u=${u.unsub_token}" style="color:#7d8595">Unsubscribe from these emails</a>.</p>`);
    try { await sendMail(env, u.email, 'Your ValueTally weekly wrap', html); } catch (e) { /* next user */ }
  }
}

export default {
  async scheduled(event, env, ctx) {
    // three crons share this handler — dispatch on the expression
    if (event.cron === '0 9 * * 6') ctx.waitUntil(sendWeekly(env, ctx));
    else if (event.cron === '15 10-19 * * 1-5') {
      ctx.waitUntil(deadmanCheck(env, false));
      ctx.waitUntil(securityCheck(env, false));   // hourly accounts-DB anomaly check
    }
    else { // 30 20 * * 1-5: portfolio snapshots first (cheap), then the digest
      ctx.waitUntil(snapshotPortfolios(env, ctx));
      ctx.waitUntil(sendDigests(env, ctx));
    }
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
      catch (e) { errLog(env, ctx, route, e); return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (route.startsWith('auth/') || route === 'me' || route.startsWith('me/') || route.startsWith('alerts/')) {
      try { return await handleAuth(route, req, env, ctx); }
      catch (e) { errLog(env, ctx, route, e); return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (route === 'refresh' && req.method === 'POST') {
      const flags = await getFlags(env);
      if (!flags.fullRefresh) return json({ error: 'full refresh is currently disabled by the owner', disabled: true }, 403, 0);
      try { return await handleRefresh(env, ctx); }
      catch (e) { errLog(env, ctx, route, e); return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (route === 'prices' && req.method === 'GET') {
      const flags = await getFlags(env);
      if (!flags.livePrices) return json({ disabled: true }, 200, 60);
      try { return await handlePrices(ctx); }
      catch (e) { errLog(env, ctx, route, e); return json({ error: 'temporarily unavailable' }, 503, 10); }
    }
    if (req.method === 'GET' && (route === 'api' || route.startsWith('api/'))) {
      try { return await handleApi(route, ctx); }
      catch (e) { errLog(env, ctx, route, e); return json({ error: 'temporarily unavailable' }, 503, 30); }
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
