# Stock Dashboard — Project Memory

**ValueTally** (product name owner-chosen 2026-07-22; previously StockDash 07-21→07-22, renamed after discovering stockdash.co.uk — an unrelated, three-months-older UK portfolio app; valuetally.com/.co.uk/.uk were all free at decision time, owner registers them). Repo/infra IDs (stock-dashboard, stockdash-proxy, harris-stockdash ntfy topics) deliberately KEEP their old names — they are addresses, not brand — auto-refreshing stock value dashboard: **top 300 US + top 30 UK/European stocks** by
market cap (US raised 50→300 on 2026-07-22, owner request; pool ~380 with retained names), scored on valuation multiples, technicals, and analyst sentiment.

- **Live site**: https://valuetally.com/ (GitHub Pages, served from `claude/pages`).
  **Hosting/DNS (2026-07-22)**: domain bought at GoDaddy (no GoDaddy hosting — declined,
  Pages hosts for free), nameservers moved to the owner's Cloudflare free zone
  (id `c0e0bf4b6284c2f2f072b792da1a898a`): four **grey-cloud** A records
  185.199.108-111.153 (GitHub Pages) + `www` CNAME → harris120807.github.io.
  Orange-cloud proxying was deliberately NOT enabled (it breaks GitHub's cert
  issuance/renewal for the custom domain) — don't "fix" that. The workflow writes
  `pub/CNAME` = valuetally.com each publish (Pages reads it from the branch root;
  without it the custom domain detaches). harris120807.github.io/stock-dashboard
  301-redirects to the domain. Owner still to do: register valuetally.co.uk/.uk defensively.
- **Artifact mirror** (may lag; optional publish target): https://claude.ai/code/artifact/d5987bbf-966d-431c-a4fd-d9a68c40059d
- **Owner notifications**: ntfy.sh topic `harris-stockdash-3cb22f88` → owner's phone. **Never send test messages to it.**

## Branch map (read this before touching anything)

| Branch | Contents | Write rules |
|---|---|---|
| `main` | Workflows + legacy snapshot | Changes go via PR. **Standing owner authorization (2026-07-16): Claude may open AND self-merge PRs for small pipeline/workflow changes.** Anything that changes what the system *does* — new data sources, notification behavior, spending money, big architecture — still needs the owner's explicit OK first. |
| `claude/pages` | The published site: `index.html`, `detail-data.json`, `pwa/`, `CNAME` | Force-pushed as ONE fresh commit per refresh, only by a pipeline run. Never hand-edit; it's overwritten on every refresh. |
| `claude/state` | Pipeline state: `template.html`, `universe.json`, `analyst-state.json`, `fundamentals-state.json`, `watchlist-state.json`, `scripts/`, `routines/*.md` (docs), `README.md` | Normal commits; on push rejection `git pull --rebase origin claude/state` and retry. History squashed to a single snapshot 2026-07-22 (owner-approved one-time force-push — do NOT force-push otherwise). |
| `claude/stock-dashboard-updates-*` | Dev/session branches | Per-session work. |

## Architecture

The pipeline runs on **GitHub Actions** (scheduled workflows on `main`, secret
`FINNHUB_API_KEY` in repo settings). Pure-Python stdlib scripts in `scripts/` —
no Claude sessions in the loop:

| Workflow | Script | Cron (UTC) | Writes |
|---|---|---|---|
| `weekly-universe.yml` | `scripts/weekly_universe.py` | `0 11 * * 1` | `universe.json` (US core = top 300 since 2026-07-22; screens 1000 deep) + 5y `history/` deepen for new entrants (backfill_history.py DEEPEN=1: any shard <1000 pts gets a 5y refetch, score series preserved, no-op when the fetch is no deeper — so entrants get full lifetime history the day they join; audited 2026-07-24: all 334 shards complete, the only short series are genuine recent IPOs/spinoffs stored back to their first trading day); ntfy only on membership change |
| `daily-analyst.yml` | `scripts/daily_analyst.py` | `0 12 * * 1-5` | `analyst-state.json`, `news-state.json`, `fundamentals-state.json` (profile2+metric+refPrice prefetch — see hourly note). **Weekday rotation (2026-07-22)**: each run fetches the Finnhub bundle for a stable md5-bucket fifth of the universe (+ any ticker new to analyst-state, same-day) and carries the rest forward; news additionally refreshes daily for the 50 largest; Yahoo targets daily for ALL. `FULL=1` (workflow_dispatch input `full`) seeds everything (~45 min) |
| `hourly-refresh.yml` | `scripts/refresh.py` | GitHub cron thinned to backup-sentinel `45 9,15 * * 1-5` (2026-07-22) — cron-job.org is primary at the full `45 7-19 * * 1-5` cadence; the workflow's dedup step skips duplicate slots | `claude/pages` (index.html + detail-data.json + `pwa/` + CNAME), `watchlist-state.json`, `last-data.json`, `price-history.json`, `history/` shards, `requests-log.json`, ntfy push. **Yahoo-only since 2026-07-22**: prices/charts/FX from Yahoo; fundamentals read from `fundamentals-state.json` (daily prefetch), marketCap scaled by price drift vs `refPrice`; Finnhub hit per ticker only as fallback (bootstrap/new entrant/failed Yahoo) — keeps the shared 60/min budget for the page's refresh buttons. Charts are incremental: range=5d stitched onto stored `price-history-long.json`; full 2y refetch Mondays or when stored <260 days; if the 5d overlap disagrees >3% on 2+ days (Yahoo split/dividend rewrite) the ticker resyncs from 5y and REPLACES its stored price series (score series kept). Long history is SHARDED one file per ticker in `history/{T}.json` (2026-07-22; slashes→underscores), each shard written only when a durable change lands (new daily close/score point) — readers: refresh.py `lh_read`, template `fetchLongHistory(t)`, Worker score-history, backfill_history.py (MIGRATE=1 splits a legacy single file). **Page payload split (2026-07-22)**: index.html embeds slim records; breakdowns/technicals/earnings-detail ship in `detail-data.json` beside it (lazy-fetched on first card open; contract = refresh.py DETAIL_FIELDS ↔ template fetchDetail). Per-run live-Finnhub fallback capped at 25 tickers (FALLBACK_CAP). Watchlist requires ≥2 scored valuation metrics AND ≥2 indicator components (thin-data guard — excludes brand-new listings until they have trend data). **Data-quality guards (2026-07-24)**: trailing P/E is nulled (with PEG) when pe>400 or eps≤0 (not-meaningful — e.g. Bloom Energy); `last-data.json` is pruned to the current universe (no ghost tickers); earnings-calendar reporting-currency guard per Finnhub traps below |

**cron-job.org (primary scheduler, owner's account)**: job 8110348 "Dashboard
hourly refresh" (45 7-19 UTC Mon-Fri, extended to UK/EU hours 2026-07-21) and
job 8110352 "Dashboard daily analyst" (12:00 UTC Mon-Fri) POST to the GitHub
workflow-dispatch API. Managed via `api.cron-job.org` (Bearer auth; PATCH
`/jobs/{id}` with a full `schedule` object). The API key is NOT stored in this
public repo — the owner holds it and pastes it in-session when schedule changes
are needed (last provided 2026-07-21).

All three support `workflow_dispatch` for manual runs. Scripts read `FINNHUB_API_KEY`,
`STATE_DIR` (checkout of `claude/state`), `OUT_DIR`; they only write files — the
workflow steps do the git pushes and the ntfy notification (from `OUT_DIR/notify.txt`).

**History note**: three Claude Routine triggers (`trig_01EZNpuGei4t6XJryyAXtEKG` weekly,
`trig_01Bn3hEqV1UWQn5r8eUwmTsf` daily, `trig_01C1qZnkgCmAG9Y8HffytCYN` hourly) predate
the Actions port. Their spawned sessions could fetch and notify but never push, so they
were superseded; all three were DELETED 2026-07-22 with the owner's explicit authorization. Their
prompt sources remain in `routines/*.md` on `claude/state` as documentation of the logic.

## Running the pipeline manually

Either trigger the workflow (Actions tab → workflow → Run workflow), or locally:
```
git clone https://github.com/Harris120807/stock-dashboard.git repo && cd repo   # scripts on main
git clone --depth 1 --branch claude/state https://github.com/Harris120807/stock-dashboard.git state
FINNHUB_API_KEY=<key> STATE_DIR=state OUT_DIR=/tmp/run NOTIFY=0 python3 scripts/refresh.py
# then push /tmp/run/index.html to claude/pages (fresh single commit, force) and the
# updated state/watchlist-state.json to claude/state — see .github/workflows/hourly-refresh.yml.
```
`NOTIFY=1` sends the owner a phone push — only for real scheduled-equivalent runs.

## Data model & rules (violate these and the dashboard shows garbage)

- **Hybrid listing design**: US stocks fetch everything under their own symbol. European
  stocks are pairs `{ticker: native listing, adr: US symbol}` — fundamentals/analyst data
  from Finnhub via the **ADR** symbol (free tier only serves US symbols; returns nothing
  for `HSBA.L` etc.), prices/history/technicals from Yahoo via the **native** symbol.
  ARM and SPOT have no home listing — they stay US-listed rows in the `europe` set.
- **Currencies**: each record carries `currency` (from Yahoo chart meta: USD, GBp, EUR,
  CHF, DKK…). Prices/EPS/52wk stay in listing currency (London is **pence**); marketCap
  and revenue are normalized to **USD billions** via Yahoo `{CCY}USD=X`. Ratios (P/E,
  PEG, P/B, EV/EBITDA, margins, ROE, divYield-as-percent) are currency/share-line
  invariant — never convert them.
- **Finnhub traps** (all hit in practice): `profile2.marketCapitalization` comes in
  *local* currency (TSM in TWD, SK Hynix ADR in KRW); sometimes the currency label
  itself is wrong (Equinor: NOK values labeled USD — hence the 3x cross-check against
  Yahoo screen values); OTC "F-suffix" ordinary lines are often dead (prefer Y-ADRs:
  RHHBY not RHHVF); HXSCL (SK Hynix OTC ADR) has no quote and no chart — unusable;
  `divYield` is already a percent; `calendar/earnings` estimates/actuals come in the
  issuer's *reporting* currency (SK Hynix in KRW) — `refresh.py` plausibility-checks
  them against price/marketCap, FX-converts via the profile currency, and drops
  per-local-share EPS that can't map to the US share line (blank beats wrong).
- **Classification traps**: the foreign-name regex must match end-anchored, unstripped
  names (`p.l.c.`, `N.V.`); Rio Tinto/Sanofi carry no suffix → in KNOWN_FOREIGN; tax
  inversions (Linde, Eaton, Medtronic, Accenture…) are excluded from "European";
  GOOG is kept over GOOGL, BRK-B over BRK-A; SKHY/SPCX are treated as US by design;
  the suffix regex also catches `SAB de CV`/`Aktiengesellschaft`/`Société Anonyme`
  (2026-07-24 — FMX/PBR/AMX/DB slipped the top-300 screen into the US pool and were
  hand-removed; LatAm ADRs now in KNOWN_FOREIGN; non-US non-EU names are excluded by design).
- **Scoring (sector-RELATIVE v5 since 2026-07-24, owner request)**: valueScore = mean of
  per-metric scores for pe/peg/pb/evEbitda where each metric score = percentile within
  the stock's OWN SECTOR when ≥8 sector members have the metric (SECTOR_MIN), else the
  market pool (fins/non-fins split for pb/ev/roe as before); higher = cheaper
  blended 50/50 with the stock's position in its SECTOR's frozen anchors
  (benchmarks.json — pe/peg/ev/roe; no pb anchors exist). FIN_SECTORS
  (Banking/Financial Services/Insurance) use the bank lens (owner-chosen):
  pb ranked against OTHER FINANCIALS only, roe joins their score
  (higher = better, fin-pool + sector anchors), evEbitda unscored
  (undefined for banks); non-financials' pb/ev pools exclude financials.
  Fixes the structural bank tilt (old raw screen put 3 banks on the buy
  watchlist). scoreBreakdown gains a 'roe' key (null for non-fins; UI row
  hidden when null). Mirror: recomputeDerived + benchPos/VAL_BENCH_KEY in
  template.html — change both together.
  indicatorScore = mean of sma50/sma200/cross/RSI/analyst
  component scores centered at 1.0; combinedScore = valueScore × indicatorScore;
  watchlist = top 3 / bottom 3 by combinedScore, notify only on membership change
  (prior state in `watchlist-state.json`).
- **Price targets (2026-07-18)**: daily via Yahoo `quoteSummary/financialData`
  (cookie+crumb, NATIVE symbols — works for EU listings, unlike Finnhub free).
  Stored in `analyst-state.json` as `target: {mean, analysts, yPrice}` (min 3
  analysts; prior target carried on fetch failure). `refresh.py` reconciles
  units (pounds-vs-pence ratio guards, 0.3–3x sanity band) and falls back to
  the static `TARGETS` dict. Targets are in the LISTING's trading unit.
- **Absolute score (2026-07-18, empirical anchors)**: peer-INDEPENDENT 0–100
  graded against FROZEN market-history distributions in `benchmarks.json` on
  `claude/state` — quintile anchors ([p10,p25,p50,p75,p90]) for pe/peg/ev/
  margin/roe/de, market-wide + 21 sectors, harvested from a top-500 US
  cross-section by `scripts/build_benchmarks.py` (Yahoo screen + Finnhub
  metrics; rebuilt every July 15 by `annual-benchmarks.yml` on `main` —
  owner-approved cadence 2026-07-18, always ntfy-notifies since a re-anchor
  re-grades every absolute score; frozen yardstick between rebuilds).
  Hybrid anchoring (owner-chosen): valuation 40% (pe/peg/evEbitda) vs the
  MARKET distribution; quality 30% (margin/roe/de) vs the stock's SECTOR
  (market fallback; leverage unscored for Banking/Financial Services/Insurance
  when no sector anchor); analyst 30% (upside ladder ±30%, buy-share of rec
  counts, min 3 analysts). `_pos()` interpolates percentile position within
  the anchors, clamped to [0.05, 0.95]; missing pillars renormalize. Labels:
  Strong ≥70 / Solid ≥55 / Mixed ≥40 / Stretched ≥25 / Weak. Fields:
  `absoluteScore`, `absLabel`, `absBreakdown{valuation,quality,analyst}`.
  FOUR synced pieces: `compute_absolute` (refresh.py), `computeAbsolute`
  (template.html, feeds the refresh button — reads `/*__BENCH__*/` injected
  by refresh.py as a slimmed market+pool-sectors copy), the gate's explainer
  text, and `benchmarks.json` itself — change together. Served via /api
  (slim + breakdown). If benchmarks.json is missing the pipeline still runs
  with absolute scores nulled.

## Template / UI

`template.html` on `claude/state` is the single UI source — an HTML **fragment**
(no doctype/head/body) with a `/*__DATA__*/` placeholder that gets replaced by the
compact slim JSON array (~334 records; detail fields live in `detail-data.json`).
The GitHub Pages copy is wrapped (split at first `</style>`). After any template
edit: extract `<script>` contents, `node --check` them, then republish via
`refresh.py` — and ALWAYS exec-test refresh.py on a mini universe first
(py_compile misses NameErrors; one publish run died to that on 2026-07-24).

- `fmtMoney(d, v)` renders per-record currency (1,479p / €495.80 / CHF 334.00 / kr / $).
- **Per-stock refresh button** in the detail card calls Finnhub client-side (CORS `*`);
  US rows get live price + fundamentals, European rows fundamentals only (Yahoo has no
  CORS, so native prices can't refresh in-browser). Updates are view-local.
- **API proxy (2026-07-18)**: the page NO LONGER embeds the Finnhub key. All client
  market-data calls go through the owner's Cloudflare Worker
  (`API_PROXY = https://api.valuetally.com` since 2026-07-22) — routes
  `/quote?symbol=` (60s edge cache), `/metric?symbol=` (10 min), `/search?q=` (24h).
  Upstream usage is bounded by cache windows, not visitors, so there are no
  user-facing rate limits. Key lives ONLY in the GitHub Actions secret
  `FINNHUB_API_KEY` and as a Worker secret (owner authorized 2026-07-18) — never
  put it in any public file again (the old template.html exception is retired).
- **Pages/nav (2026-07-17 product split)**: the site is a hash-routed SPA — views
  `#overview` (visuals/watchlist/earnings), `#table`, `#stock` (search + detail card),
  `#compare`, `#requests`; `#TICKER` deep-links into `#stock`. Tab bar `#tabbar` is
  fixed-bottom on mobile, sticky-top ≥900px. New views must be added to `VIEWS`,
  given a `view-<name>` container, and a `.nav-tab`. Anything that draws from live
  layout size must listen for the `viewchange` CustomEvent (hidden views have no
  dimensions — see the compare page).
- **First-visit gate** `#gate` ("Before you use…": not-advice, score methodology,
  data caveats). Agreement stored as localStorage `sd-agreed-v1`; ℹ️ nav tab reopens
  it. Keep the score explanation in sync with scoring changes in `refresh.py`.
- **Table density**: `colMode` localStorage ('full' default / 'compact'), chip
  `#colModeChip`; compact column set in `COMPACT_COLS`.
- **Theme (2026-07-17, owner-chosen)**: dark blue. Single `--accent` CSS var drives
  every interactive element (active tab/chips, focus rings, buttons, range toggles,
  score overlay, `h1`) — light `#1e4f91`, dark `#3f7cc4`; surfaces are navy-tinted
  (`--page` dark `#0a101d`). All FOUR theme blocks (base, `@media` dark,
  `data-theme` dark/light) must define it — plus `--brand-tip` (2026-07-21),
  the bright end of the logo gradient (the brand mark strokes
  url(#brandGrad): accent → tip; PWA icons carry the same gradient). Chart *data* colors (`--series-*`,
  good/critical) are a separate palette — never collapse them into the accent.
  `theme-color` meta (in `refresh.py`'s wrapper) and `pwa/manifest.json` colors
  must stay in sync with the navy page color.
- **Tab icons** are inline single-colour stroke SVGs (`currentColor`, 1.8 width,
  round caps) — no emoji. PWA icons in `pwa/` match the theme (navy bg, ascending
  bars in accent blue) — regenerate together if the palette changes.
- **Source-attribution policy (owner decision 2026-07-17)**: footer/header carry
  only a one-line "Data: Finnhub & Yahoo Finance" credit; provider mechanics and
  freshness details live ONLY in the Info gate. Don't re-add verbose provenance
  to the visible chrome. Keep the Finnhub credit — likely a ToS requirement.
- **Full refresh button (2026-07-22, owner-requested)**: header button POSTs to the
  Worker's `/refresh`, which triggers `hourly-refresh.yml` via a fine-grained GitHub
  PAT (Actions-only on this repo) stored as Worker secret `GH_TOKEN` — the SAME PAT
  also lives in the cron-job.org job headers; **rotation must update both places**.
  No Worker-side cooldown (owner decision) — the workflow's 3-min dedup is the only
  rate control. Progress bar is time-calibrated, capped at 95% until the page's
  publish commit is detected. Owner chose to KEEP the hourly ntfy pulse (2026-07-22).
- **Live prices (2026-07-22)**: 45s poller (visible tab only) hits the Worker's
  `/prices` (batched Yahoo spark, native symbols so EU rows work, 30s edge cache)
  and updates DATA in place — KPIs, heat strip, table-if-visible, open detail card
  (with flash). View-local; the hourly publish remains the source of record.
- **Stale-page banner (2026-07-22)**: `#staleBanner` shows when `/api/watchlist`
  updatedAt is >90 min newer than the page's `BUILT_AT` — tells cached-PWA users
  to pull fresh.
- **"Data as of" is UK time** (Europe/London, BST/GMT abbrev — owner request 2026-07-22).
- **Main drivers (2026-07-22)**: detail card renders a plain-English drivers list
  (`scoreNarrative`) from scoreBreakdown — e.g. "Cheap vs sector peers". Keep its
  wording consistent with the scoring version.
- **Biggest Mover card** on #overview click-throughs to the stock's detail card.
- **Scatter (2026-07-24 overhaul)**: colorGroup = top-**12** sectors (refresh.py
  `most_common(12)`), colors assigned dynamically (`assignSectorColors()`:
  PREFERRED_COLOR pins the 9 legacy sector→series mappings, remaining series
  slots 1-12 fill by group size; `--series-10/11/12` exist in all four theme
  blocks). Legend is built from groups present in DATA (dead chips impossible);
  `#secHi` dropdown highlights ANY true sector (all ~38, with counts);
  `#scatterN` toggles Top 150 (default) / All — active filters union-in matching
  stocks below the size cut. Legend chips, heat-strip chips and the dropdown
  clear each other.
- **New-listing/spinoff notes**: detail card explains missing indicators for
  young listings (no 200d SMA yet) and missing valuation for fresh spinoffs
  (e.g. HONA — no standalone financials published yet). Don't "fix" those blanks.
- **Stock requests page** POSTs to public ntfy topic `harris-stockdash-req-a2962152`
  (deliberately separate from the private pipeline topic — it's spam-exposed by
  design; owner subscribes to it read-only). Client-side throttle: 15s double-submit cooldown only (daily caps removed 2026-07-18 once search went through the proxy)
  via localStorage. Never repoint it at the pipeline topic.
  **Requests v2 contract**: the form resolves any input (name/ticker) to a canonical
  ticker via Finnhub `/search`, then posts title `Stock request: <TICKER> (#N)` —
  or, for batches (up to 5 per submission, one throttle hit, one notification),
  `Stock requests (k): T1 (#n1), T2 (#n2)…`; the poller findall-parses `TICK (#N)`
  pairs from any title starting `Stock request`.
  `refresh.py` polls the topic each run into `requests-log.json` on `claude/state`
  (`{lastPollAt, byTicker: {T: {count, firstAt, lastAt}}}` — ntfy only caches ~12h,
  the log is the durable record; weekend requests can miss the log but still hit the
  owner's phone). The page reads the log + newer cache entries to compute N and to
  tell users about duplicates. Ticker `TEST` is reserved for e2e checks — never
  logged/counted. The title regex is a shared contract between template.html and
  refresh.py — change both together. Owner confirmed subscribed to the request
  topic and verified delivery end-to-end (2026-07-17).
- **Deeper history**: `history/{T}.json` shards on `claude/state` (per-ticker since
  2026-07-22) = `{t:[daynums], p:[daily closes, native ccy], st:[daynums],
  s:[combinedScore]}` — 5y daily closes (capped 1830d, seeded/deepened by
  `scripts/backfill_history.py`) + never-pruned daily score series, maintained
  incrementally by `refresh.py`. The 1Y and 5Y chart ranges (detail card + compare)
  lazy-fetch per-ticker shards (`fetchLongHistory(t)`, `PHL_BASE` raw URL,
  `_phlShards` cache); only 30D comes from `price-history.json`. Compare-page
  series shorter than the range = the stock IPO'd recently, not missing data.

## Cloudflare (client API proxy)

- **Custom domain (2026-07-22)**: the Worker is mounted at `api.valuetally.com` (Workers custom domain on the owner's valuetally.com zone, id c0e0bf4b6284c2f2f072b792da1a898a); the page's `API_PROXY` points there. The workers.dev URL keeps working as a fallback.
- Account ID `e3f3a97cb1349addb9ec089f9383d12d` (not secret). Worker `stockdash-proxy`,
  source versioned at `worker/` on `claude/state`; workers.dev subdomain
  `harris-stockdash`. Deploys via Cloudflare REST API or wrangler using the API token
  in the GitHub repo secret `CLOUDFLARE_API_TOKEN` — the token itself must NEVER be
  committed anywhere (it can deploy code and write storage on the owner's account).
  Worker secret `FINNHUB_API_KEY` is set; re-set it after any full re-provision.
  Cache TTLs and allowed params live in `worker/worker.js` — client contract:
  responses are Finnhub pass-through JSON, CORS `*`.
- **Public scores API (2026-07-18)** on the same Worker: `/api` (docs),
  `/api/scores`, `/api/scores/{ticker}` (breakdown + daily score history),
  `/api/watchlist` — backed by `last-data.json`/`watchlist-state.json`/
  `price-history-long.json` raw URLs, ~5 min edge cache. **Deliberately serves
  only the dashboard's own derived metrics (scores/positions/watchlist) — never
  re-serve raw vendor fields (prices, P/E, fundamentals) through it without a
  data license.** Deploy: REST upload with `keep_bindings: ["secret_text"]` so
  Worker secrets survive script updates (FOUR Worker secrets: `FINNHUB_API_KEY`,
  `GH_TOKEN`, `ADMIN_KEY`, `CF_ANALYTICS_TOKEN` — re-set all after a full
  re-provision).
- **`/prices` (2026-07-22)**: live-quote endpoint for the page's 45s poller — ONE
  batched Yahoo spark sweep of the whole universe (chunked 20 symbols/request:
  spark 400s above 20), 30s edge cache, so upstream cost is ~2 sweeps/min
  globally regardless of visitors. `/refresh` (POST) dispatches hourly-refresh.yml
  via the GH_TOKEN PAT.

## Admin console (2026-07-24, owner-requested)

- **valuetally.com/admin** — `admin.html` on `claude/state`, published to
  `/admin/index.html` each refresh. Static + key-gated: the page holds NO
  secrets; privileged calls send `Authorization: Bearer <ADMIN_KEY>`, which the
  Worker SHA-256-compares against Worker secret `ADMIN_KEY` (owner holds the
  key — handed over 2026-07-24; never commit it).
- **Sections**: Status; Traffic (from `/admin/stats`); Kill-switches
  (livePrices / fullRefresh / stockRefresh → Worker KV `flags`, namespace
  `stockdash-config` id f7e94fe4cd224ded94bc270d659a238d, ~60s propagation,
  fail-open); Visitor-requests grid (reads `requests-log.json` raw, tracked
  badges, Add stages into the form); Add-stocks form (`SYMBOL` or `NATIVE:ADR`,
  ≤20/submission → `/admin/add-tickers` → `add-tickers.yml` on main: validate,
  append to pool, DEEPEN 5y backfill, push state, chain hourly refresh, ntfy;
  BARC/BCS + TEST hard-blocked in add_tickers.py).
- **Traffic (live since 2026-07-24 ~20:10 UTC)**: every Worker request logs an
  anonymous data point (route group + country, no IPs/UAs) to Analytics Engine
  dataset `stockdash_traffic` (binding `TRAFFIC`; owner enabled AE on the
  account). `/admin/stats` queries it via the AE SQL API using Worker secret
  `CF_ANALYTICS_TOKEN` — a SCOPED token (Account Analytics:Read only) the owner
  created for this; the classifier had (correctly) refused storing the Global
  API Key. Counts exist only from the enable date forward. Deploy metadata must
  re-declare BOTH bindings (kv_namespace CONFIG + analytics_engine TRAFFIC)
  alongside `keep_bindings: ["secret_text"]`.
- **Template kill-switch handling**: `/prices` `{disabled:true}` stops the live
  poller for that page load; disabled `/quote`//`metric` → "live refresh is
  currently switched off" in the detail card; `/refresh` 403 shows the
  owner-disabled message.

## Multi-agent coordination

- **Lanes**: (1) UI/template → `template.html` on `claude/state`; (2) scoring/pipeline →
  `scripts/refresh.py` on `main`; (3) universe rules → `scripts/weekly_universe.py`;
  (4) analyst data → `scripts/daily_analyst.py`; (5) infra → `.github/workflows/*`.
  Scripts live on `claude/state`; workflows on `main` (PRs; Claude may self-merge small pipeline changes — see branch map); state + template live on `claude/state`
  (direct pushes OK). Stay in your lane; state file *schemas* are shared contracts —
  changing one requires updating every reader (all three scripts + this file).
- **Concurrency**: `claude/state` uses pull-rebase-retry; never force-push it.
  `claude/pages` is force-pushed single commits — never run two publishes at once.
- Schedule changes = edit the workflow cron on `main` via PR (self-merge OK per the standing authorization). The permission classifier
  blocks committing the API key to git except in `template.html` where the owner
  explicitly authorized it — scripts must read `FINNHUB_API_KEY` from the environment.
- The permission classifier requires **explicit owner authorization in-conversation**
  for: exposing credentials anywhere new, pushing new infrastructure, deleting triggers.
  Ask the owner plainly; vague approvals get blocked.
- Universe content questions (why isn't X listed?): check `universe.json` first.
  **Append-only pool (owner decision 2026-07-18)**: `us`/`europe` = FULL pool —
  current top-300/30 core PLUS every previously tracked name; stocks are never
  removed on falling below the cutoff. `coreUs`/`coreEurope` = who currently
  makes the cut; `fellOut` = this week's core exits (retained); `dropped` is
  always [] now. New entrants get full 5y history at join (weekly deepen) but
  lack analyst/target data until the next daily job. Runtime headroom is fine
  post-Yahoo-only (~2.5 min hourly at 334 tickers; the old ~150-ticker tiered-
  refresh trigger is obsolete). Corning/SK-Hynix-ADR were evaluated and fall
  outside the cutoffs; Rolls-Royce (RR.L) is in. **Never add Barclays (BARC)**
  — standing owner rule; monetization is also paused pending Barclays consent.

## History

- Pre-2026-07-15: pipeline state lived in Google Drive files, driven by http_api-created
  routines (deleted by owner). Drive files (`stock-dashboard-*.json`, template v5) are
  orphaned — ignore them.
- 2026-07-15: migrated to git state (`claude/state`), universe extended 50→80 with native
  European listings, currency normalization added, per-stock refresh button shipped,
  pipeline ported to GitHub Actions after Routine-spawned sessions proved unable to push.
- 2026-07-22: efficiency program (Yahoo-only hourly, incremental charts, sharded
  history, thinned backup cron, state history squash, legacy triggers deleted);
  ValueTally rebrand + valuetally.com + api.valuetally.com; live prices +
  stale banner + full-refresh button; US universe to 300.
- 2026-07-24: scoring v5 (sector-relative), foreign-filter hardening, P/E
  not-meaningful guard, scatter overhaul, history-depth audit (all complete).

## Open items (owner-side)

- Rotate the OLD Finnhub key (it sat in template.html in public git history
  pre-2026-07-18) — needs updating in the GitHub secret + Worker secret.
- Register valuetally.co.uk / .uk defensively.
- Finnhub commercial-licensing email still unanswered; monetization paused
  (also pending Barclays consent).
