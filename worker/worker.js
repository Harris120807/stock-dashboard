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
//   /api/scores          -> all 80 stocks: scores, position, watchlist flags
//   /api/scores/:ticker  -> one stock incl. score breakdown + daily score history
//   /api/watchlist       -> current buy/sell watchlist
// Backed by the claude/state JSON files, edge-cached ~5 min.

const STATE_RAW = 'https://raw.githubusercontent.com/Harris120807/stock-dashboard/claude/state/';
const API_TTL = 300;
const SITE = 'https://harris120807.github.io/stock-dashboard/';

const TTL = { quote: 60, metric: 600, search: 86400 };
const ALLOWED = { quote: ['symbol'], metric: ['symbol'], search: ['q'] };
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
};

function json(obj, status, ttl) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=' + (ttl || API_TTL), ...CORS },
  });
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
  name: 'StockDash scores API',
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
      const long = await stateJson('price-history-long.json', ctx);
      const h = (long.byTicker || {})[d.ticker];
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

async function handleRefresh(env, ctx) {
  if (!env.GH_TOKEN) return json({ error: 'refresh trigger not configured' }, 503, 30);
  const gh = {
    'Authorization': 'Bearer ' + env.GH_TOKEN,
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'stockdash-refresh-trigger',
  };
  const r = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/${GH_WF}/dispatches`, {
    method: 'POST', headers: { ...gh, 'Content-Type': 'application/json' }, body: JSON.stringify({ ref: 'main' }),
  });
  if (r.status !== 204) return json({ error: 'trigger failed (' + r.status + ')' }, 502, 0);
  return json({ ok: true, note: 'refresh started — fresh data publishes in ~2 minutes' }, 200, 0);
}

export default {
  async fetch(req, env, ctx) {
    if (req.method === 'OPTIONS') return new Response(null, { headers: CORS });
    const url = new URL(req.url);
    const route = url.pathname.replace(/^\/+|\/+$/g, '');
    if (route === 'refresh' && req.method === 'POST') {
      try { return await handleRefresh(env, ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 0); }
    }
    if (req.method === 'GET' && (route === 'api' || route.startsWith('api/'))) {
      try { return await handleApi(route, ctx); }
      catch (e) { return json({ error: 'temporarily unavailable' }, 503, 30); }
    }
    if (req.method !== 'GET' || !TTL[route]) {
      return new Response(JSON.stringify({ error: 'not found' }), { status: 404, headers: { 'Content-Type': 'application/json', ...CORS } });
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
