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
  301-redirects to the domain. **Enforce HTTPS enabled 2026-07-24** (owner
  ticked it in repo Settings → Pages; http:// now 301s — the "Not secure"
  badge era is over). Favicon: the PWA icons are linked as `rel="icon"` in
  refresh.py's wrapper AND admin.html (`/icon-192.png`).
  Owner still to do: register valuetally.co.uk/.uk defensively.
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
| `hourly-refresh.yml` | `scripts/refresh.py` | GitHub cron thinned to backup-sentinel `45 9,15 * * 1-5` (2026-07-22) — cron-job.org is primary at the full `45 7-19 * * 1-5` cadence; the workflow's dedup step skips duplicate slots | `claude/pages` (index.html + detail-data.json + `pwa/` + CNAME), `watchlist-state.json`, `last-data.json`, `price-history.json`, `history/` shards, `requests-log.json`, `track-record.json`, ntfy push. **Yahoo-only since 2026-07-22**: prices/charts/FX from Yahoo; fundamentals read from `fundamentals-state.json` (daily prefetch), marketCap scaled by price drift vs `refPrice`; Finnhub hit per ticker only as fallback (bootstrap/new entrant/failed Yahoo) — keeps the shared 60/min budget for the page's refresh buttons. Charts are incremental: range=5d stitched onto stored `price-history-long.json`; full 2y refetch Mondays or when stored <260 days; if the 5d overlap disagrees >3% on 2+ days (Yahoo split/dividend rewrite) the ticker resyncs from 5y and REPLACES its stored price series (score series kept). Long history is SHARDED one file per ticker in `history/{T}.json` (2026-07-22; slashes→underscores), each shard written only when a durable change lands (new daily close/score point) — readers: refresh.py `lh_read`, template `fetchLongHistory(t)`, Worker score-history, backfill_history.py (MIGRATE=1 splits a legacy single file). **Page payload split (2026-07-22)**: index.html embeds slim records; breakdowns/technicals/earnings-detail ship in `detail-data.json` beside it (lazy-fetched on first card open; contract = refresh.py DETAIL_FIELDS ↔ template fetchDetail). Per-run live-Finnhub fallback capped at 25 tickers (FALLBACK_CAP). Watchlist requires ≥2 scored valuation metrics AND ≥2 indicator components (thin-data guard — excludes brand-new listings until they have trend data). **Data-quality guards (2026-07-24)**: trailing P/E is nulled (with PEG) when pe>400 or eps≤0 (not-meaningful — e.g. Bloom Energy); `last-data.json` is pruned to the current universe (no ghost tickers); earnings-calendar reporting-currency guard per Finnhub traps below |

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
  Worker secrets survive script updates (SEVEN Worker secrets as of 2026-07-26:
  `FINNHUB_API_KEY`, `GH_TOKEN`, `ADMIN_KEY`, `CF_ANALYTICS_TOKEN`,
  `RESEND_API_KEY`, `MAIL_FROM`, `VAULT_KEY` — re-set all after a full
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
- **Layout (2026-07-24 evening; 4th tab 07-25)**: FOUR tabs — **Overview**
  (Status; then a two-column desktop grid ≥1000px in a 1240px container:
  Traffic | Run-pipelines + Kill-switches), **Requests** (visitor cards left,
  add form right), **Backtest** (per-horizon quintile charts + Run button),
  **Track record** (live pick performance — see below). NOTE the tab-switcher
  hard-codes the pane list `['overview','requests','backtest','track']` —
  a new tab must be added THERE too, not just as a button (the Track tab
  shipped invisible once). Run-pipelines card =
  five dispatch buttons (hourly / analyst / analyst FULL seed / universe
  rescreen / benchmarks behind a confirm since a re-anchor re-grades all
  absolute scores). Kill-switches: livePrices / fullRefresh / stockRefresh →
  Worker KV `flags`, namespace `stockdash-config` id
  f7e94fe4cd224ded94bc270d659a238d, ~60s propagation, fail-open.
- **Requests tab**: grid merges the durable log with ntfy's live ~12h cache
  (same `TICK (#N)` title contract; NEW markers; 60s auto-refresh; TEST
  filtered). Log timestamps are epoch SECONDS — normalize to ms before Date()
  (shipped a 21-Jan-1970 bug). **Add** on a dotted native symbol calls
  `/admin/adr-lookup` (Yahoo-search two-step: symbol→name→US-exchange
  candidates; ranked listed > Y-ADR > F-line per the RHHBY trap) and stages a
  suggested `NATIVE:ADR` pair with alternatives shown; genuinely ADR-less
  names (MML.AX McLaren) say so and are currently NOT addable (native-only
  escape hatch not built). Add-stocks form: `SYMBOL` or `NATIVE:ADR`,
  ≤20/submission → `/admin/add-tickers` → `add-tickers.yml` on main (validate,
  append to pool, DEEPEN 5y backfill, push state, chain hourly refresh, ntfy;
  BARC/BCS + TEST hard-blocked in add_tickers.py).
- **Extra admin routes** (beyond config/add-tickers/stats): `/admin/run`
  (whitelisted workflow dispatch incl. `backtest`), `/admin/adr-lookup`,
  `/admin/send-digests` (manual alerts-digest pass), `/admin/send-weekly`
  (manual weekly-wrap pass — SENDS REAL EMAIL to opted-in users),
  `/admin/deadman-check` (GET, ALWAYS a dry run — reports staleMinutes/
  wouldAlert without touching the owner ntfy topic).
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

## Accounts (2026-07-24, owner-requested email+password)

- **Stack**: Worker routes `/auth/*`, `/me`, `/me/watchlist` + **D1** database
  `valuetally` (uuid f6e7639b-133d-4ecf-88d6-d7e006756833, binding `DB`; schema
  in `worker/schema.sql`: users/sessions/tokens/watchlists/attempts). Passwords
  = PBKDF2-SHA256 100k iters + per-user salt (never plaintext); sessions = 64-hex
  bearer tokens, 90d; rate limiting is D1-backed (attempts table). Flows:
  signup (verify email), login, logout, verify (302 → site `#verified=1`),
  resend-verify, forgot → one-time 1h reset link (`#reset=TOKEN`; reset signs
  out all sessions), delete-account (full wipe — GDPR). Forgot always answers
  the same whether the account exists (no address probing).
- **Email**: Resend; valuetally.com DOMAIN VERIFIED 2026-07-24, sender is
  `ValueTally <account@valuetally.com>` (`MAIL_FROM` secret). The original
  send-only key was rotated same-day; the replacement (full-access, owner
  choice) is Worker secret `RESEND_API_KEY`. NO tracking domain (owner-agreed:
  auth links must not be rewritten). Templates share the site-styled shell
  (`mailWrap`/`mailBtn` in worker.js: light card, two-blue wordmark, accent
  button — all styles inline). Replies to account@ currently BOUNCE — Cloudflare
  Email Routing forward suggested, not yet set up.
- **UI (template.html)**: header account button + modal (sign in/up/forgot/
  reset/account states) — the modal MUST be injected inside `.viz-root`
  (theme vars + font live there, not on body; a body-level modal shipped as
  unstyled serif soup once). Watchlist tab (`#watchlist` view): quick-add
  search box (client-side name/ticker match, star from results), starred table
  (live-price aware) + system top/bottom-3 chips; star buttons in detail card
  and watchlist rows. **Watchlists are SIGNED-IN ONLY (owner decision
  2026-07-25)**: the D1 server copy is the only store — no device-local
  stars, no union-merge (both existed 07-24→07-25 and were removed after the
  merge surprised the owner). A signed-out star tap opens the sign-in modal;
  the watchlist tab shows a sign-in prompt (`#wlSignedOut`, add-box hidden);
  sign-in REPLACES STARS with the server list; sign-out/delete clears STARS
  and repaints every star button (`repaintStars()`). Legacy `vt-stars`
  localStorage is deleted on load. Write-through PUT on every toggle.
  Session in localStorage `vt-session`; 401 anywhere clears it. Gate carries an "Accounts & privacy" paragraph — keep it honest
  with what's actually stored.
- **Worker deploys now carry THREE bindings** (kv_namespace CONFIG,
  analytics_engine TRAFFIC, d1 DB) + keep_bindings secret_text; SEVEN secrets
  since 2026-07-26: FINNHUB_API_KEY, GH_TOKEN, ADMIN_KEY, CF_ANALYTICS_TOKEN,
  RESEND_API_KEY, MAIL_FROM, VAULT_KEY (broker-key encryption). Body parsing
  covers POST **and PUT**; CORS allows GET/POST/PUT.
- Owner signed up in-session with a TEMP password that appeared in chat — they
  were told to reset it via the emailed link immediately.

## Alerts / score history / sectors / backtest (2026-07-24 evening batch)

- **Watchlist email digest**: Worker cron `30 20 * * 1-5` (schedules API) →
  `sendDigests`: opted-in verified users (users.alerts=1, unsub_token) get ONE
  email listing starred stocks with |dayChange|≥5% or |scoreDelta|≥15 (was
  0.05 — recalibrated 07-25 for the ~0-100 score scale); no
  events = no email. Toggle in the account modal (`POST /me/alerts`);
  one-click `GET /alerts/unsubscribe?u=TOKEN`. Manual trigger:
  `POST /admin/send-digests`. scoreDelta is computed BEFORE the last-data.json
  dump in refresh.py specifically so the digest can read it there.
- **Score history UI**: score overlay now on 30d/1Y/5Y detail charts; Compare's
  "Combined Score" metric supports the 1Y range = FULL daily score history from
  shards (short lines = tracking began July 2026).
- **Sectors view**: `#sectors` tab (8 tabs now — icon-only labels <480px):
  per-sector cards (median P/E, median combined, avg day move, top-3 chips),
  click to expand full member list. Pure client-side from DATA.
- **Backtest**: `scripts/backtest.py` (STATE_DIR) → `backtest.json` on
  claude/state; `backtest.yml` on main (dispatch-only; PR #16); admin console
  Backtest tab renders per-horizon quintile bars + benchmark line + caveats,
  Run button via /admin/run whitelist. **5-year window since 2026-07-24
  (owner request)**: SAMPLE_YEARS=5; the script fetches its OWN ~10y closes
  from Yahoo at run time (range=10y, ~4 min for the pool; stored-shard
  fallback per ticker) because stored shards cap at 5y and 5y of samples
  needs a 6th forward year — shards deliberately unchanged. Method: monthly
  samples, TODAY's ratios price-scaled (reconstruction bias — flatters mean
  reversion), technicals from closes, market-pool percentiles,
  survivorship-biased universe. 5y dividend-adjusted run (60 samples, 331
  tickers, 2026-07-25): 12m Q5 +41.8% / Q1 +12.1% / bench +24.6%, monotonic
  at every horizon — present ONLY with caveats. NOTE: backtest.yml also commits backtest.json, so local
  runs can conflict on rebase — regenerate or take the newer side.
- **Hourly ntfy = status check (2026-07-24, owner request)**: body built in
  refresh.py — ▲/▼ counts + avg move + biggest mover / score ups↑ downs↓ +
  buy & sell lists / live-count + Finnhub-fallback health, plus a loud
  "WATCHLIST CHANGED" line and the earnings-tomorrow previews. Title
  "ValueTally hourly status" + valuetally.com click-through live in
  hourly-refresh.yml (PR #17). Owner-approved replacement for the plain
  "Refreshed" pulse.

## Track record / screener / dead-man / weekly wrap (2026-07-25 batch)

- **Live track record**: refresh.py appends one entry per trading day to
  `track-record.json` on claude/state (`{startedAt, updatedAt, days: [{d,
  buy, sell, buyRet, sellRet, benchRet, buyIdx, sellIdx, benchIdx}]}`):
  end-of-day top-3/bottom-3 baskets; each day's return = mean dayChange of
  the PREVIOUS entry's baskets (equal weight) vs a whole-universe benchmark;
  cumulative indices start at 100 (tracking began 2026-07-25 — history before
  that is unrecoverable by design). Hourly runs overwrite today's provisional
  entry (last run of the day stands); hourly-refresh.yml `git add`s the file
  (PR #19). Admin **Track record** tab renders the three cumulative lines +
  KPIs; unlike the Backtest tab nothing is reconstructed — this is the real
  out-of-sample record. Currently ADMIN-ONLY (owner choice); could go public
  later.
- **Table screener**: "Filters" chip on #table opens `#screenPanel` — sector
  (true sectors w/ counts), min combined score, max P/E, dividend/yield floor,
  region (US vs UK/EU; proxy `isEuRow` = dotted ticker OR non-USD currency —
  ARM/SPOT deliberately count as US). Named saved screens in localStorage
  `vt-screens` (device-local; stores the raw select VALUES so restore is
  verbatim — don't store parsed floats, "1.0" ≠ String(1.0)). Filter stacks
  with the position chips + sector legend + search.
- **"What changed" line**: refresh.py `change_note()` builds a plain-English
  day-over-day note per ticker (score delta ≥10 — combinedScore is ~0-100
  with ~5 pts of ROUTINE daily churn since v5 percentiles reshuffle with
  prices, so small-scale thresholds like 0.005 fire on 90% of the pool
  (shipped that way once); daily-digest score events were re-thresholded to
  ≥15 at the same time. SMA50/200 crossings —
  yesterday's state recomputed from the shard closes, RSI zone entry/exit,
  |weekChange|≥5%) → `changeNote`, shipped via detail-data.json (in
  DETAIL_FIELDS; template mergeDetail must copy AND clear it — absent means
  quiet day). Renders as the accent-bordered "Since yesterday:" callout under
  Main drivers. `weekChange` (close vs 5 trading days back, from the shard)
  is now a slim/last-data field feeding the weekly wrap + note.
- **Dead-man alarm**: Worker cron `15 10-19 * * 1-5` → `deadmanCheck`: if
  watchlist-state.json updatedAt is >2h old (or unreadable) during market
  hours → ntfy to the OWNER topic ("pipeline stalled", click → Actions),
  KV-deduped to one alert per 4h (`deadmanLastAlert`). Test ONLY via
  `GET /admin/deadman-check` (always dry — never send tests to the owner
  topic). Silence from the hourly pulse now genuinely means "all fine".
- **Weekly wrap email**: Worker cron `0 9 * * 6` → `sendWeekly`: every
  opted-in verified user with a watchlist gets a Saturday wrap — their starred
  stocks' week, universe top/bottom-5 by weekChange, system watchlist +
  week's in/out (from track-record.json). Always sends (wrap, not alert);
  same alerts flag + unsubscribe token as the daily digest. Manual:
  `POST /admin/send-weekly`. The Worker now registers THREE cron schedules
  via the schedules API — a redeploy does not touch them, but re-registering
  must PUT all three (`30 20 * * 1-5`, `15 10-19 * * 1-5`, `0 9 * * 6`);
  `scheduled()` dispatches on `event.cron`.
- **Backtest returns are dividend-adjusted** since 2026-07-25 (Yahoo
  `adjclose`, fallback raw close; stored-shard fallback stays raw) — the
  "no dividends" caveat is retired, "no costs" remains. Chart benchmark
  lines carry a "universe avg +X%" label.

## Insiders / column picker / peers / data-quality / user-stats (2026-07-26 batch)

- **Insider activity**: daily_analyst.py's rotation bundle is now SEVEN
  Finnhub calls (+insider-transactions, 90d window, ADR symbol for EU rows);
  build() counts ONLY open-market codes P (buys) and S (sales) — option
  exercises/awards/withholdings (M/A/F/G) are noise by design. Ships as
  `insiders: {b, s}` in analyst-state → refresh.py `d["insiders"]` →
  DETAIL_FIELDS → "Insiders (90d)" tile in the detail card's tech grid
  ("—" until a ticker's first post-change rotation fetch; FULL seed run
  2026-07-26 populated the whole universe).
- **Column picker**: "Columns" chip on #table opens a checkbox panel;
  custom set in localStorage `vt-cols` (ticker column forced on). Custom
  OVERRIDES full/compact; the Compact chip clears it. Reset button returns
  to full.
- **Sector peers table**: detail card shows the 4 largest same-sector peers
  + self (highlighted) with mktcap/P/E/div/combined; rows navigate via a
  delegated `[data-peer]` click handler; hidden when <2 peers or no sector.
- **Data-quality monitor**: refresh.py writes `data-quality.json` on
  claude/state each run (advisory, nothing auto-corrected): P/E ratio jumps
  >5x, currency flips, price jumps >±67% outside split-guard resyncs,
  mktcap swings >3x, missing prices; capped 40 issues. hourly-refresh.yml
  `git add`s it (PR #21). Admin Status card renders the "Data health" line
  from it (✓ clean / ⚠ list).
- **Admin user-stats**: `GET /admin/user-stats` (D1 aggregate counts only —
  no emails leave the DB): users/verified/alerts/non-empty watchlists;
  four KPI tiles on the admin Status card.

## Security monitoring (2026-07-26)

- **security_log** (D1 table, appended to `worker/schema.sql`): `{id, at
  (epoch s), kind, detail, country}`. Kinds: `admin_auth_fail` (wrong admin
  key, detail=route), `login_fail`, `signup`, `password_reset`,
  `account_delete`, `canary_login`. Written best-effort by `secLog()` in
  worker.js (try/catch + ctx.waitUntil — a broken security path must NEVER
  break serving). **Privacy**: detail is route names/generic text only —
  never emails, passwords or tokens.
- **Canary tripwire**: one decoy user row (`canary+<hex>@valuetally.com`,
  random pw_hash/salt, verified=0 — credentials exist NOWHERE else), id in
  KV `canaryUserId` (Worker self-bootstraps the key from D1 by the
  `canary+%@valuetally.com` LIKE if missing — the deploy token can't write
  KV). Any login attempt resolving to it, session token resolving to it, or
  password reset on it → `canary_login` log + ntfy to the OWNER topic
  ("ValueTally SECURITY ALERT", rotating_light, click → /admin) — any touch
  means someone is reading/using DB contents. No dedupe by design, only a
  30-min flood cap (KV `canaryLastAlert`). Detection only — the request is
  NOT blocked. Don't "clean up" the canary user row.
- **Hourly anomaly check**: `securityCheck(env, dry)` piggybacks the
  dead-man cron branch (`15 10-19 * * 1-5`; both waitUntil'd). Alerts the
  owner topic ("ValueTally security warning", deduped one per 4h via KV
  `secLastAlert`) when: users count dropped > max(2, 20%) vs KV baseline
  `secBaseline` (mass deletion), admin_auth_fail ≥10/h (key brute-force),
  or login_fail ≥50/h (credential stuffing). Baseline always rewritten
  after a real (non-dry) check.
- **Admin**: `GET /admin/security` → `{recent (last 30 rows),
  failedAdmin24h, failedLogin24h, users, sessions, canaryOk}` (canaryOk =
  canary row present, zero canary_login events, zero canary sessions);
  `GET /admin/security-check` = ALWAYS-dry run of the anomaly check (never
  alerts, never moves the baseline — same standing rule as deadman-check).
  admin.html renders a Security card on Overview (right column, below
  Kill-switches): 3 KPIs + last-12 events table, loaded in loadAll().
- **Never send test messages to the owner ntfy topic** — alert paths are
  verified by code inspection + the dry routes only. Note wrong-key admin
  pings and bad logins DO create real log rows (that's fine, it's the
  point); the e2e check used exactly one of each.

## Trading 212 portfolio import (2026-07-26)

- **Feature (owner-requested)**: a signed-in user connects a READ-ONLY
  Trading 212 API key on the new site **Portfolio tab** (`#portfolio`, 9 tabs
  now) and sees their live holdings — qty / avg price / price / value / P/L +
  totals row — with covered stocks linked to their detail cards (delegated
  `[data-peer]`). Signed-out = sign-in prompt (watchlist pattern); connected UI
  has Refresh (client-disabled 6s after click) and Disconnect (confirm).
- **Key storage, encrypted at rest**: D1 table `broker_keys(user_id PK,
  provider 't212', enc, env 'live'|'demo', created_at)` — `enc` =
  base64(iv || AES-256-GCM ciphertext) under Worker secret **`VAULT_KEY`**
  (32-byte hex, generated 2026-07-26, exists ONLY as a Worker secret — never
  in git, never printed; rotating it makes stored keys undecryptable →
  users just see the reconnect prompt, no data loss beyond that). NEVER
  store broker keys plaintext. Keys are never echoed in any response/log.
- **Worker routes** (sessionUser-gated, no-store): `POST /me/t212 {key}` —
  format check (15–300 non-space chars), validate via T212
  `/equity/account/cash` against **live first, then demo** on 401/403
  (remembers which env worked), fetch `/equity/account/info` for
  currencyCode, encrypt+upsert, rate-limited 6/5min per user;
  `POST /me/t212/delete` — row + KV caches wiped; `GET /me/portfolio` —
  404 `{connected:false}` when no key, else decrypt → T212
  `/equity/portfolio` from the stored env, positions mapped to
  `{t212, qty, avgPrice, price, ppl, fxPpl}`, response
  `{connected, env, currency, positions, fetchedAt}`. T212 429 → friendly
  429; T212 401/403 (revoked key) → `{connected:true, keyInvalid:true}` so
  the UI prompts reconnect. Account deletion also wipes broker_keys + caches.
  secLog kinds `t212_connect` / `t212_delete` (no key material in detail).
- **Rate-limit/cache design**: T212 allows ~1 req/5s per endpoint per key, so
  portfolio responses are KV-cached 60s per user (`t212:{userId}`,
  expirationTtl 60, CONFIG binding), account currency cached 30d
  (`t212cur:{userId}`); the page's Refresh button self-disables for 6s.
  T212 auth header is the RAW key, NOT `Bearer`.
- **Ticker mapping (best-effort, client-side in template.html `pfMap`)**:
  `XXX_US_EQ` → `XXX`; one trailing lowercase exchange letter before `_EQ`
  maps l→`.L`, d→`.DE`, p→`.PA`, a→`.AS`; matched against DATA tickers AND
  `adr` fields; unmatched holdings render fine from T212's own numbers
  (they include currentPrice), just unlinked. Per-instrument prices are in
  the instrument's own currency (plain numbers in the UI); `ppl` and its
  total are in the ACCOUNT currency (symbol + header label from the
  response's `currencyCode`).
- **Template state**: portfolio code lives inside the accounts IIFE (needs
  SESSION/api); `PF` state is reset on sign-in/sign-out/account-delete.
  Gate "Accounts & privacy" paragraph now covers the broker key — keep it
  honest. The demo env is labeled "practice account" in the UI.

## Admin accounts list / score quintiles (2026-07-26 late)

- **Admin Accounts tab** (5th admin tab, owner-requested): `GET /admin/users`
  (admin-key gated, no-store) → email, verified, alerts, has_watchlist,
  has_broker, created_at for every account (LIMIT 500, newest first); the
  security canary is EXCLUDED by email pattern. Emails appear ONLY here —
  never in any public/cached response. admin.html `atab-users` pane +
  `loadUsers()`; the tab-switcher pane list now includes 'users'.
  `users.created_at` is a TEXT datetime in production (not epoch) — the
  formatter handles both.
- **Combined-score quintiles** (owner-requested): `computeQuintiles()` in
  template.html buckets all covered stocks by combinedScore into fifths
  (Q5 = top), client-side, at load and at the end of `recomputeDerived`.
  Shown as a "Quintile" table column (in COLS/column picker) and a Q-tag +
  sentence in the detail card's Combined Score box. Wording is deliberately
  factual (which bucket) — backtest return numbers stay admin-side with
  their caveats. These are the same buckets backtest.py ranks by.

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
  not-meaningful guard, scatter overhaul, history-depth audit (all complete);
  admin console (traffic analytics, kill-switches, ticker adds w/ ADR
  auto-lookup, requests grid, pipeline buttons, three-tab layout); HTTPS
  enforced + favicon; accounts (email+password, D1) + synced watchlists +
  daily alert digests; Resend domain verified + site-styled emails; score-
  history UI; Sectors tab; 5y quintile backtest; hourly ntfy → status check;
  KPI additions (Advancing "by share price, not score" + Scores Improving).
- 2026-07-25 (late 07-24 UTC): live track record (track-record.json + admin
  4th tab; PR #19); table screener with saved screens; "Since yesterday"
  change note (changeNote in the detail contract) + weekChange field;
  dead-man alarm cron (owner ntfy on stalled publishes, KV-deduped);
  Saturday weekly-wrap email cron; backtest switched to dividend-adjusted
  returns; Worker now runs three cron schedules.
- 2026-07-26: watchlists made signed-in-only (union-merge removed); insider
  activity (7-call analyst bundle); column picker; sector-peers table;
  data-quality monitor (PR #21); admin user-stats; score-delta threshold
  recalibration for the 0-100 scale; security monitoring (security_log +
  canary tripwire + anomaly cron, via agent); Trading 212 portfolio import
  (encrypted broker keys + #portfolio 9th tab, via agent); admin Accounts
  tab (5th admin tab); combined-score quintile display.

## Open items (owner-side)

- Rotate the OLD Finnhub key (it sat in template.html in public git history
  pre-2026-07-18) — needs updating in the GitHub secret + Worker secret.
  STILL the oldest open item.
- Register valuetally.co.uk / .uk defensively.
- Finnhub commercial-licensing email still unanswered; monetization paused
  (also pending Barclays consent).
- Cloudflare Email Routing forward for account@/support@ (replies to the
  account sender currently bounce).
- RESOLVED 2026-07-24: the Global API Key was rolled by the owner; deploys now
  use a scoped Workers-edit token and analytics a scoped Analytics:Read token
  (both owner-held, pasted in-session when needed — never committed).
