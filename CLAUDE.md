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
- **Overview de-clutter (2026-07-28, owner-chosen)**: the "Average Value
  Score by Sector" bars card was RETIRED (Sectors tab covers it; a link line
  `.ov-secs-link` replaced it — don't re-add the bars), and both earnings
  cards are collapsed-by-default: headline carries the count ("N stocks
  report in the next week"), body expands on tap (`wireEarnToggle`,
  `#earnHead`/`#earnBody` + recent equivalents).
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
- **Overview layout (2026-07-28)**: the two hand-balanced column divs were
  replaced by ONE `.cols-flow` container (CSS multicol: `columns:2` ≥1000px,
  cards `break-inside:avoid` + inline-block) — columns self-balance as card
  contents grow; don't reintroduce fixed column divs. `.atabs` wraps on
  narrow screens (the 5-tab strip overflowed phones); `#uTbl` sits in an
  overflow-x wrapper (same mobile-table trap as the site's peer table).
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
  filtered). **Fulfilled requests auto-hide (2026-07-27)**: tickers present
  in `/api/scores` (i.e. actually LIVE, not merely submitted) are filtered
  out of the grid, with a "N fulfilled requests hidden" note; `REQ_ALIAS`
  (GOOGL→GOOG, BRK.A/B→BRK-B, FB→META) counts fulfilled-under-another-symbol
  requests as live. The durable log itself is untouched. Log timestamps are epoch SECONDS — normalize to ms before Date()
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
  **Since 2026-07-27** the same cron pass also evaluates custom alert rules
  and snapshots connected T212 portfolios (see the 07-27 batch section); the
  digest user query is a LEFT JOIN so rule-only users (no watchlist) still
  get their alert emails.
- **Score history UI**: score overlay now on 30d/1Y/5Y detail charts; Compare's
  "Combined Score" metric supports the 1Y range = FULL daily score history from
  shards (short lines = tracking began July 2026).
- **Sectors view**: `#sectors` tab (8 tabs now — icon-only labels <480px;
  since 2026-07-28 shows the 12 largest sectors by cap with a "Show all N
  sectors" expander — ~38 cards made the page too long):
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
  **Mobile-CSS scoping trap (fixed 2026-07-27)**: the mobile rules that hide
  the main table's Rank column and pin its Ticker used unscoped
  `tbody td:first-child`/`nth-child(2)` selectors — on phones they hid the
  FIRST COLUMN of every other table (peer tickers, watchlist stars,
  portfolio instruments). They're now scoped to `.table-scroll` — any new
  main-table mobile rule must be too.
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
- **T212 auth is HTTP BASIC with a key+secret PAIR (2026-07-26 evening)**:
  the app's key-creation screen shows an API key AND a one-time secret;
  `Authorization: Basic base64(key:secret)`. The older single-token header
  just 401s (burned an hour on the owner's valid keys). Stored credential =
  `key:secret` in one encrypted blob; t212Fetch falls back to the raw-token
  header for any legacy colon-less value. Connect form has TWO fields.
- **Worker routes** (sessionUser-gated, no-store): `POST /me/t212
  {key, secret}` — format check (10–300 non-space chars each), validate via T212
  **`/equity/portfolio`** against **live first, then demo** (remembers which
  env worked). NOT /equity/account/cash: T212 keys have GRANULAR permission
  checkboxes and a Portfolio-only key 403s on account endpoints — that
  mis-rejected the owner's valid key on day one (fixed 2026-07-26; error
  now surfaces upstream statuses + a scope hint, and the validation
  response primes the 60s portfolio cache so the immediate page load
  doesn't hit T212's ~1 req/5s limit). `/equity/account/info` (currencyCode)
  needs the Account-data permission and is skipped silently without it.
  Then encrypt+upsert, rate-limited 6/5min per user;
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
- **Currency handling (2026-07-26 late)**: T212 prices are in each
  INSTRUMENT's currency (US in USD, LSE in PENCE) while ppl is in the
  ACCOUNT currency — raw qty×price sums were meaningless across a mixed
  portfolio (shipped that way briefly). Worker infers instrument ccy from
  the ticker suffix (`t212Ccy`: _US_EQ→USD, l→GBX, d/p/a/e→EUR, s→CHF),
  converts each value to the account currency via Yahoo FX (`fxRate`,
  1h KV cache `fx:{PAIR}`, GBX = GBP/100) → per-position `ccy` +
  `valueAcct`. UI: prices shown in instrument ccy (pence as `1,553p`),
  Value column + total in account ccy; unconvertible rows fall back to
  instrument-ccy value and are excluded from the total (asterisk note).
  If connect can't resolve the account currency (T212 rate limit on
  account/info) the primed cache lives only 10s so the next fetch fixes it.
- **Ticker mapping (best-effort, client-side in template.html `pfMap`)**:
  `XXX_US_EQ` → `XXX`; one trailing lowercase exchange letter before `_EQ`
  maps l→`.L`, d→`.DE`, p→`.PA`, a→`.AS`; matched against DATA tickers AND
  `adr` fields; `PF_ALIAS` (2026-07-27) folds share classes into the tracked
  line (GOOGL→GOOG, BRK.A/BRK.B→BRK-B) so e.g. a GOOGL holding links to the
  GOOG card — do NOT add the other class to the universe instead (one line
  per company by design); unmatched holdings render fine from T212's own numbers
  (they include currentPrice), just unlinked. Per-instrument prices are in
  the instrument's own currency (plain numbers in the UI); `ppl` and its
  total are in the ACCOUNT currency (symbol + header label from the
  response's `currencyCode`).
- **Template state**: portfolio code lives inside the accounts IIFE (needs
  SESSION/api); `PF` state is reset on sign-in/sign-out/account-delete.
  Gate "Accounts & privacy" paragraph now covers the broker key — keep it
  honest. The demo env is labeled "practice account" in the UI.

## Options-implied metrics (2026-07-28, owner-approved after discussion)

- **Decision frame**: options as a PRODUCT (calculators, chains, strategy
  suggestions) was evaluated and REJECTED — advice-adjacent and off-brand.
  What shipped is options as a DATA SOURCE: per-stock factual numbers,
  deliberately NOT inputs to any score (score credibility rests on the
  accruing track record — don't change scoring inputs casually).
- **daily_analyst.py**: `fetch_opt(sym)` (Yahoo v7 options chain, same
  cookie+crumb opener as targets; US-listed symbol — the ADR for EU rows;
  RHHBY-style no-options names stay None). Nearest expiry <7 days out
  (megacap weeklies — an expiring AAPL weekly showed 52% "IV" vs ~24% real)
  triggers ONE refetch of the first expiry ≥7d via `?date=`. Stores
  `opt: {iv (ATM call/put mean IV %), em (± straddle-mid/spot % by expiry),
  exp}` in analyst-state; carried forward on failure; guards iv∈(1,500)%,
  em∈(0,50)%.
- **refresh.py**: merges `opt` → `iv`/`expMove`/`optExp` (DETAIL fields, NOT
  slim; entries with a passed expiry are dropped — blank beats stale); daily
  `viv` series in the shards (same padded-v* pattern) so IV RANK becomes
  possible once history accrues (not built yet).
- **template**: "Implied vol" tile in the tech grid + an IV-vs-realized
  sentence under it ("options price the next few weeks at X% vs Y%
  realized — expecting choppier/calmer/in-line"); earnings section gains
  "options price a move of about ±X% by <expiry>" when the report date is
  on or before the straddle expiry. All wording = factual range, never
  direction. mergeDetail copies iv/expMove/optExp with null-clearing.
- Derived options fields stay OFF the public /api (same rule as other
  vendor-derived data).

## 2026-07-28 batch (quintile history / digest notes / alias search / pipeline health)

- **Alias-aware search**: `TICKER_ALIAS` (GOOGL→GOOG, FB→META, BRK.A/B→
  BRK-B) + `ALT_SYMS` (per-ticker list incl. each EU row's ADR) at template
  top level — the top search AND the watchlist quick-add match them
  (alias/ADR exact ranks between ticker-exact and ticker-prefix). THREE
  alias maps now exist by design (search TICKER_ALIAS, portfolio PF_ALIAS,
  admin REQ_ALIAS) — add new share-class/rename cases to all three.
- **Admin Pipeline health**: `GET /admin/pipeline` (Worker, via the same
  Actions PAT) returns the last run per workflow (status/conclusion/
  started/duration); admin card under Traffic renders ✓/✗/⏳ + age + took.
  Hourly scheduled runs that dedup-skip still conclude success — the card
  says so.

## Owner Q5 pie builder (2026-07-27, owner-requested)

- **POST /admin/t212-pie** {key, secret, count≤50} + "Q5 pie" card on the
  admin Overview: builds an equal-weight UNFUNDED pie ("ValueTally Q5
  <date>") of the top-N stocks by combined score in the OWNER'S OWN T212
  account. The pies-write credential is **per-request only — never stored,
  never logged** (standing rule: write-capable broker keys must not sit at
  rest anywhere; the stored /me/t212 keys stay read-only). Mapping goes
  through T212's own metadata instrument list (key needs Pies + Metadata
  permissions; live tried first, then demo); unmappable tickers are skipped
  and reported; weights are 4dp summing to exactly 1. secLog kind
  `admin_pie`. Funding/auto-invest/rebalance stay in the T212 app.
  **User-facing execution was evaluated and REJECTED (2026-07-27)**: FCA
  advising/arranging exposure + custody risk of write-capable user keys
  (pie-weight edits redirect auto-invest money) + contradicts the site's
  not-advice stance. Don't resurrect it as a user feature; the discussed
  safe alternatives are a manual order-plan/pie-recipe page, practice-account
  pies, and virtual portfolios (none built yet). Also do NOT publish a T212
  pie share-link of the picks — same advice+execution shape via T212's
  social feature.

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

## Risk metrics / metric history / alerts / portfolio analytics / broadcast (2026-07-27 batch)

- **Risk metrics (refresh.py)**: `vol1y` (annualized stdev of daily returns,
  %) and `mdd1y` (worst peak-to-trough, %, negative) computed from the last
  ~252 shard closes + today's price in the same loop as scoreDelta/weekChange;
  null until ~60 daily returns exist (young listings). SLIM fields (also in
  last-data.json) → "Volatility (1y)" / "Max drawdown (1y)" tiles in the
  detail card's tech grid. `rsi` and `t200` (1 above / 0 below 200-day, null
  unknown) are top-level slim copies of the technicals — the table screener
  and the Worker's rule evaluation read slim/last-data, not detail-data.
- **Daily valuation history (2026-07-27)**: shards gain `vt/vpe/vev/vdy`
  (daynum + P/E + EV/EBITDA + divYield, 2dp, one point per UTC day, never
  pruned) **+ `vq` (daily quintile, 2026-07-28)** appended in refresh.py's
  shard loop — refresh.py now computes `d["quintile"]` server-side
  (rank-based fifths over the scored pool, Q5 = top, same buckets as the
  template/backtest; in slim + last-data too). Series added later than vt
  are None-padded to stay aligned with the vt daynums — keep that pattern
  for any future v* series — same cadence as the score
  series so NO extra git churn. Purpose: accumulate data so future "P/E over
  time" charts are possible (none drawn yet — series only starts 2026-07-27).
  backfill_history.py deepen/main now write `{**old, "t": t, "p": p}` so the
  extra series (and any future shard keys) survive a price-series rebuild —
  don't regress that to an explicit key list.
- **Custom alert rules**: D1 `alert_rules(id, user_id, ticker, kind,
  threshold, created_at, triggered_at)`; kinds price_above/price_below/
  score_above/score_below/rsi_above/rsi_below (_above fires at ≥, _below at
  ≤). Routes GET/POST `/me/rules`, POST `/me/rules/delete`, `/me/rules/rearm`
  (all sessionUser-gated; 20 rules/user cap). Evaluated once per weekday by
  sendDigests against last-data.json; **one-shot**: triggered_at is stamped
  ONLY after the email actually sends (mail failure = re-fires next day) and
  fired rules sit paused until re-armed. Price thresholds are in the LISTING
  unit (pence for London) — same numbers the site shows. Emails ride the
  existing digest ("Your alerts" section, same alerts opt-in + unsub token).
  UI: "My alerts" card on the watchlist tab (add form + armed/fired table,
  re-arm/remove; RULES state reset inside resetPF so user switches can't
  leak another user's rules). Since 2026-07-28 digest event rows also carry
  the stock's `changeNote` ("since yesterday" line) read from last-data.
- **Portfolio value history**: D1 `portfolio_history(user_id, d, total,
  invested, ppl)` PK(user_id,d); `snapshotPortfolios` piggybacks the 20:30
  digest cron — ONE `/equity/account/cash` call per connected broker key,
  same-day re-runs overwrite (last run stands). `GET /me/portfolio/history`
  → the user's series; rows wiped on T212 disconnect AND account delete
  (privacy: no key, no broker-derived data). Template draws an SVG value
  line + "£X now · +Y% since date" under the holdings table (needs ≥2 points
  — appears from the second trading day after connecting).
- **ETF look-through (2026-07-27, owner request)**: common index ETFs held in
  a connected T212 portfolio get a score after all — as the cap-weighted mean
  of the covered constituents we ALREADY score. `ETF_BASKETS` (sp500/ndx/
  allworld/ftse100/europe/hidiv basket definitions over DATA) + `ETF_MAP`
  (~30 T212 base symbols: VUSA/CSPX/SPY→sp500, EQQQ→ndx top-100-US-non-fin,
  VWRP/IWDA→allworld, VUKG/ISF→ftse100, VEUR/VEUA→europe, VHYL→hidiv…) +
  `etfLT()` (cached per page load) in the accounts IIFE. Holdings-table cell
  shows `BASE + ETF tag` with the look-through score in the tooltip;
  analytics decompose each ETF's value into basket sector/region weights (the
  allocation bars show what's owned THROUGH funds), include it in the
  weighted portfolio score, list per-ETF look-through lines under the
  profile, and EXCLUDE ETF positions from the single-stock >15% flag
  (diversified by nature). Deliberately approximate (largest names, honest
  `cov` wording) and NEVER in stock quintiles/watchlists — broad baskets are
  mid by construction. Adding an ETF = one ETF_MAP entry (+ a basket def if
  it's a new index).
- **Portfolio hero (2026-07-27)**: `#pfHero` atop the Portfolio tab — big
  account total + all-time P/L (+%) + free cash, ALL Trading 212's own
  figures (owner explicitly rejected a look-through day-change estimate:
  T212 truth only, no derived numbers in the hero). The old cash sentence
  in the footnote was folded into it.
- **Portfolio analytics (template, client-only)**: `renderPfAnalytics()` in
  the accounts IIFE — allocation bars by sector/region/listing currency
  (converted `valueAcct` shares only), value-weighted combined score + its
  percentile among covered stocks, concentration flags (single position >15%,
  single sector >40%). Shown when ≥2 positions have converted values.
- **Sortable headers everywhere (2026-07-27)**: the watchlist and portfolio
  tables gained main-table-style header-click sorting (`WL_SORT`/`PF_SORT`
  in the accounts IIFE; null values always last; portfolio Total row stays
  pinned; global `thead th` CSS already provided the pointer/arrow styling).
- **Table extras**: **CSV export** chip downloads the CURRENT view (filters +
  screen + sort + visible columns; raw field values, BOM'd UTF-8; Ticker
  gains a Name column, Price a Currency column) — the row pipeline was
  extracted to `tableRows()`, shared by renderBody and the export so they
  can't diverge. **Screener depth**: RSI zone (`d.rsi`), trend vs 200-day
  (`d.t200`), quintile, market-cap bucket (mega ≥200 / 50–200 / 10–50 /
  <10 $B) — new `els` keys ride the existing saved-screens mechanism.
  FIXED while there: the min-combined-score options still carried the
  pre-v5 scale (≥1.2/1.1/1.0/0.9 — matched everything); now ≥70/60/50/40.
  Old saved screens referencing the stale values silently lose that one
  filter on restore (select falls back to "any").
- **Worker error log**: D1 `error_log(at, route, detail)`; `errLog()` (same
  best-effort pattern as secLog) called from every catch in the fetch
  dispatcher (admin/auth/refresh/prices/api). detail = OUR exception text
  only, never request bodies/tokens. `GET /admin/errors` → 24h count + last
  30 rows; admin Status card renders the "Worker errors" line (`#errLine`,
  `loadErrors()` in loadAll).
- **Admin broadcast**: `POST /admin/broadcast {subject, body[, confirm]}` —
  without `confirm:true` returns the recipient count ONLY (no send); with it,
  emails every verified alerts=1 user (canary excluded) via the site mail
  shell + standard unsubscribe link, then secLogs kind `broadcast`. Admin UI:
  "Email broadcast" card on Overview (right column) — recipient-count
  preview → browser confirm() → send. Body is plain text (escaped,
  line-breaks kept).
- D1 migrations for the three new tables ran 2026-07-27 (REST /query with the
  deploy token); `worker/schema.sql` documents them. Gate's "Accounts &
  privacy" paragraph updated to cover alert rules + the daily account-total
  snapshot — keep it honest with what's stored.

## Stake building & deals (2026-08-02, owner-requested)

- **Feature**: site tab `#stakes` (10 tabs now) — regulatory ownership/deal
  disclosures for the whole universe, "most significant made obvious" per the
  owner's brief (takeovers/mergers/restructurings first-class).
- **Fetcher**: `scripts/daily_stakes.py` (stdlib, NO API key — both sources
  public) runs as a daily-analyst.yml step (PR #25) → `stakes-state.json` on
  claude/state (`{updatedAt, leis:{".L"→LEI}, byTicker:{row:{events[],
  offer, cik, lastFetch}}}`; events deduped by accession/disclosure id,
  550d window, 180d for passive 13G/A, cap 60/ticker). Failures non-fatal.
- **EDGAR side** (US rows + EU rows via ADR symbol): data.sec.gov submissions
  per CIK (map from sec.gov company_tickers.json, "." → "-"). Forms:
  13D/13G(+/A), SC TO-T/TO-I, 14D9, 13E3, DEFM14A/PREM14A, S-4, 425, and 8-K
  filtered BY ITEM CODE (1.03 bankruptcy → BANKRUPTCY, 2.01 → COMPLETED-ACQ;
  other 8-Ks ignored). Structured XML (all 13D/G since Dec 2024) parsed for
  filer/percent/rule — 13G schema uses coverPageHeaderReportingPersonDetails/
  classPercent/issuerCik, 13D uses reportingPersonInfo/percentOfClass/
  issuerCIK (both handled; classPercent content can carry trailing prose).
  **Direction matters**: a company's own feed contains filings it made ABOUT
  other issuers (UAL→AZUL, AZN→Monopar) — issuerCik≠own CIK ⇒ event gets
  `subject` (outward stake, the owner's original "interest in other firms"
  ask). Repeat deal-doc amendments (425, /A forms) squelched within 21d.
- **FCA NSM side** (.L rows only): POST api.data.fca.org.uk/search?index=
  nsm-search — criteria `company_lei` MUST be the positional 4-array
  ["", LEI, "disclose_org", "related_org"] (plain LEI errors; keyword search
  IGNORES criteria — mutually exclusive paths). type_codes: HOL (TR-1) +
  OFB/OFD/OFF/ORE/OUP/POT/CAS/TEN/RTE/CAR/ACQ/DIS as events; Form 8.x codes
  (RET/DCC/FEE/FEO/FER) are NOT events — they aggregate into an offer-period
  flag `{last, n45}`. LEIs self-resolve once via keyword search, cached in
  state. TR-1 docs (data.fca.org.uk/artefacts/ + download_link) parsed for
  holder + resulting/previous % — anchor on "Resulting situation on the
  date" NOT "threshold was crossed or reached" (that phrase appears 3×; the
  section-8A heading match scoops "5.1" out of "DTR5.1"). Two holder-name
  template wordings handled.
- **Significance** (`sig` 0-100, stored per event): deal events 95/85/80,
  8-K 2.01 70, CAR 65, ACQ/DIS 60, new 13D 80 (90 if the filer previously
  held 13G — passive-to-active switch; +5 at ≥10%), 13D/A 55(+10 big delta),
  13G 45, TR-1 35(+pct/delta bumps), 13G/A 25 — passive index churn ranks
  low BY DESIGN. Outward events: 13D 80 / 13D/A 60 / 13G 55 / 13G/A 35.
- **Publish**: refresh.py builds `stakes-data.json` beside detail-data.json
  (`{updatedAt, events[], offers{}}`, events pre-sorted by `rank` = sig ×
  exp(-age/60d); passive forms ship only ≤180d); hourly-refresh.yml copies
  it to pages (guarded `[ -f ]`). Template `stakesView` IIFE lazy-fetches on
  first tab open: filter chips (deals/activist/cross-stakes/holdings) +
  search, "Most significant — last 60 days" cards (border color by sig:
  ≥85 critical / ≥60 accent), offer-period banners, 400-row table, NEW badge
  ≤2d, company links via `#TICKER` deep-links. All external text escHtml'd.
- **Seed** run locally 2026-08-02 (DOC_CAP=6000). Steady-state daily run
  ~340 EDGAR submissions + ~15 NSM queries + only-new doc fetches (~2 min).
- Coverage honesty (stated in the tab sub): continental EU rows are covered
  only through US ADR filings; UK Takeover Code flow only for .L rows.
- **Event notes (2026-08-02, owner-requested)**: events carry `note` — a
  description extracted from the filing itself so nobody has to open it:
  13D/13D/A use the structured XML `transactionPurpose` (Item 4 prose); deal
  forms + 8-Ks fetch the primary doc (capped 200KB read) and `snippet()`
  picks the best deal-keyword sentence (scored: action phrases beat
  annex cross-references; EDGAR/SGML header lines with .htm/.txt excluded);
  NSM docs start after the repeated headline (kills the RNS-number header).
  S-4s whose note reads "Notes due/aggregate principal" are RECLASSIFIED
  DEBT-EXCHANGE (sig 35) — registered debt swaps are not M&A (AVGO/CMCSA
  hit this). note ships through stakes-data.json; card shows it as an
  italic quote, table as a muted second line. Passive 13G forms get no
  note by design (cost, no story). Backfilled 351 events 2026-08-02.
- **Deal numbers (2026-08-02, owner-requested)**: events also carry
  `shares` (13D/G XML aggregate holdings; TR-1 total voting rights —
  int regex needs the (?<![\d.]) lookbehind or it eats a float's decimals)
  and `terms` (`extract_terms`: per-share price / headline value / exchange
  ratio, context-filtered — par value, liquidation preference, redemption
  price, principal amount are boilerplate money that must never surface).
  S-4 debt-exchange reclass keys off the note. Rendered as accent chips on
  cards; appended to the table note line.
- **Click-in filing summary (2026-08-02, owner-requested)**: Worker route
  `GET /stakes/detail?u=<filing-url>&form=` — allowlist ONLY
  sec.gov/Archives/edgar/data/ + data.fca.org.uk/artefacts/ (NOT an open
  proxy), strips the EDGAR xsl viewer prefix to raw XML, extracts ≤7
  key-fact bullets per form type (13D/G XML: filer/subject/position/event
  date/source of funds/stated purpose/rule; deal HTML: parties,
  consideration, value, exchange ratio, termination fee, timing,
  conditions, board rec; TR-1: holder/reason/threshold date/resulting %
  + voting rights/previous %). Filings are immutable → edge-cached 7d
  (upstream fetch cf-cached too). Template: "Details ▾" button on cards
  AND table rows (delegated [data-skd] handler, client Map cache,
  .sk-bullets inset). Failure text points at the Filing link.
- **Financial-filer churn rule (2026-08-02, post-seed)**: outward 13G/13G/A
  from rows whose sector is Financial Services/Banking/Insurance/Asset
  Management are SKIPPED at ingest (BLK/GS/MS file 13Gs on their whole fund
  and dealer books — 621 seed events were that). Outward 13Ds kept for
  everyone. Squelch window measures against the CANDIDATE filing date (a
  today-relative compare shipped in the seed and duplicated same-day 425s);
  XML entity names unescaped in the parser.

## 2026-08-13 batch (resilience / dividends / stakes integration)

- **Yahoo circuit breaker (refresh.py `get()`)**: consecutive-failure counter
  on yahoo.com URLs — ≥3 → single-attempt mode, ≥12 → circuit OPEN for the
  rest of the run (every Yahoo call returns None; tickers carry stored data,
  run summary appends "YAHOO-CIRCUIT-OPEN"); Yahoo Retry-After sleeps capped
  15s (Finnhub untouched at 60). Built after the 2026-08-06 outage — which
  post-mortem showed was actually a GITHUB runner-capacity incident (jobs
  queued 15 min with runner_id=0, then GH-cancelled; self-healed) — kept
  because the retry ladder genuinely can't survive a real Yahoo brownout.
- **Dividend calendar**: daily_analyst's quoteSummary call adds the
  calendarEvents module (same request as targets) → `dcal:{ex,pay}` in
  analyst-state; refresh ships slim `exDiv`/`divPay` (dropped 5 days after
  the date passes). UI: collapsed "Upcoming ex-dividend dates" card on
  #overview (14-day window, earnings-card pattern) + `exDiv` table column
  marked `optOnly` (picker-only; visibleCols' full branch filters optOnly —
  new picker-only columns use that flag).
- **Stakes → emails (worker.js)**: `recentStakeEvents(ctx,days,minSig)`
  reads stakes-state.json via stateJson; sendDigests adds a "Stake & deal
  filings on your watchlist" section (sig≥60, 2-day window — may repeat a
  yesterday item once, accepted) and deal-only days still email (subject
  'ValueTally filings: …'); sendWeekly adds "Deals & stake building this
  week" (top-5 by sig, best event per company, all recipients).
- **Detail-card Ownership & deals strip**: `renderCardStakes(t)` +
  `window.__stakes = {load, line}` exposed from the stakesView IIFE (load()
  now returns a shared promise). Latest 3 events + offer-period banner +
  "All filings →" link that pre-fills the #stakes search via
  `window._skPending` (consumed in the view's viewchange handler).
- **Ownership timeline**: the Details expander appends an SVG line of the
  clicked FILER's percent history in that stock (client-side from
  stakes-data events; needs ≥2 points, same-filer case-insensitive,
  inward events only). moreBtn buttons carry data-skt/data-skfiler.

## Filings engine repair (2026-08-15, owner-flagged)

- **Churn loop (root cause of "engine isn't pulling the data")**: dedupe
  membership used to be only the STORED events, so anything pruned by the
  60-per-ticker cap was re-ingested as "new" next day (~731 events/run),
  exhausting DOC_CAP=60 before enrichment → a week of events with 5 filer
  names. Fix: `ent["seen"]` — durable per-ticker accession memory (bounded
  800), membership = seen ∪ stored; squelched + non-merger 1.01 filings are
  marked seen too. NEVER regress dedupe to stored-events-only.
- **Enrichment repair pass**: after the main loops, leftover doc budget is
  spent re-reading structured XML for stored 13D/G events missing `filer`
  (newest first; handles outward reclassification + fin-churn drops). Ran
  once locally with DOC_CAP=1500: 437 repaired, missing-filer 459→22,
  content-dupes 277→11. Daily DOC_CAP now 150 (workflow env, PR #29).
- **8-K item 1.01 → MERGER-AGREEMENT (sig 95)**: material-agreement 8-Ks are
  fetched and pattern-checked ("agreement and plan of merger" family); credit
  agreements are discarded and marked seen. Day-one deal announcements now
  land weeks before the S-4/proxy did (first catches: VRTX Jul 6, ICE Jul 29).
  Labels added in template DEAL_LABEL/DEAL_FORMS + worker SK_MAIL_LABEL.
- **Payload**: events now ship `id` (accession) + `ts` (EDGAR acceptance
  time, new events onward); refresh.py collapses exact content twins
  (t/form/d/filer/pct/subject) at publish — same-day per-class amendments
  stay stored but render once.
- **Worker deploy pending owner token**: the SK_MAIL_LABEL addition is in
  worker/worker.js source but NOT deployed (Cloudflare deploy token lives
  only in-session and was lost to a container restart — owner must re-paste;
  until then digest emails print the raw form name, harmless).

## Command v2 + insider trades live (2026-08-15)

- **Command (/command/) is a multi-panel workspace** (owner rejected the
  single-focus v1): L1/L2/L4/L6 grid layouts, each panel independently runs
  TICKER + FUNCTION; command grammar `NVDA VAL 3` / `3 NVDA VAL` targets a
  panel, bare ticker keeps function, bare function keeps ticker; Alt+1..6 /
  click / Tab switch active panel; Up/Down = history; HELP overlay; CLR.
  Functions: DES VAL TECH OWN INS EARN PEER GP + market-wide MOV WIRE SEC
  SCR. Workspace persists in localStorage `vt-cmd-ws`. Function registry in
  pro/command.html is where function #13 gets added.
- **trades-data.json** published at site root: Form 4 insider transactions
  per ticker (name/role/code/dir/sh/px/after, 120d window, cap 25 published
  / 40 stored). Seeded 2026-08-15: 3,962 trades across 298 tickers. INS
  function + future retail surfaces read it; page shows "awaiting first
  data publish" if absent. EU rows have none (PDMR notices are RNS-side —
  future work). Stakes events now carry `parties` (EFTS display_names,
  178 backfilled), `dir` (new/inc/dec/exit at publish), `id`, `ts`.

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
- 2026-07-27: T212 auth fixed to Basic key:secret + currency truth from
  instrument metadata + account-total line (late 07-26/early 07-27); then the
  9-feature batch: risk metrics (vol/drawdown) + slim rsi/t200; daily
  valuation-history accumulation in shards; custom per-stock alert rules
  (D1 + digest evaluation + watchlist-tab UI); portfolio value snapshots +
  value-over-time line; portfolio analytics (allocation/weighted score/
  concentration); CSV export + deeper screener (RSI/trend/quintile/mktcap,
  stale score options fixed); Worker error log + admin line; admin broadcast
  email.

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

## Terminal redesign (2026-08-14, owner-approved)

- **"Ice terminal" look shipped to template.html** (owner approved the
  Terminal concept from the design mockups, explicitly NOT the amber
  Bloomberg palette). Ported natively into the template's own CSS — there is
  no override-sandwich; the original rules were edited.
- **DARK IS NOW THE DEFAULT THEME; light is the alternate.** The four token
  blocks inverted roles: base `.viz-root` = dark (#0a0e14 page, #101620
  surfaces, cyan accent #3fb6dd), `@media (prefers-color-scheme: light)` =
  light "cold paper" (#eef1f5 page, white cards, accent #177ba3), plus both
  `data-theme` pins. Unstamped visitors follow the system preference with
  dark as the no-preference ground. Two NEW tokens exist in ALL FOUR blocks:
  `--chrome` (rail/status-strip surface) and `--accent-ink` (text painted ON
  an accent background — dark ink on the cyan accent in dark mode, white in
  light mode; every former hardcoded `color:#fff`-on-accent uses it). `body`
  gets an explicit per-theme background (tokens live on .viz-root, so
  overscroll would otherwise show UA color). The e-chip/news-tag/mkt-stale
  hardcodes were replaced with color-mix on --warning/--good/--critical — no
  color may exist in only one theme's block.
- **Typography**: `--mono` (ui-monospace stack) + tabular-nums applied to all
  DATA surfaces via one grouped selector list near the top of the style block
  (tables, KPI values, prices, tickers, badges, chips, axis labels, #asof…);
  sans stays for prose (section subs, gate, notes). Radii flattened to 2-3px
  everywhere (999px pills are gone); section titles/kpi labels/chips are
  uppercase + letter-spaced; table cells tightened to 6px 9px.
- **Layout, ≥1000px** (breakpoint moved from 900px): the SAME `#tabbar`
  becomes a fixed full-height 64px left icon rail (labels 8px uppercase —
  9px truncated PORTFOLIO/WATCHLIST at 64px width); the SAME
  `header.page-head` becomes a fixed 44px top status strip (tagline `p` and
  `#sourceNote` display:none there — the freshness line, NOT the attribution;
  the "Data: Finnhub & Yahoo Finance" credit stays in the footer). `.wrap`
  goes full-width with padding-top 60px; `.stale-banner` pins below the strip;
  `.detail-card` has scroll-margin-top for the fixed chrome. `#view-overview.
  active` becomes a 6-col grid: heat-strip/KPIs/grid-2 span full width,
  watchlist + movers sit side-by-side (span 3 each via `.ov-watch` class +
  `#moversCard`), the three calendar cards go three-up (`.ov-cal`, span 2) —
  those classes replaced the old inline `margin-bottom:16px` styles. BELOW
  1000px nothing structural changed: stock bottom tab bar + stacked cards,
  new palette only.
- **PWA/meta**: pwa/manifest.json background/theme colors → #0a0e14. The
  `theme-color` meta lives in refresh.py's wrapper (scripts lane) and still
  says #0a101d — pipeline lane should update it to #0a0e14 (one-line change).
- **Traps hit**: rail width forces 8px labels (9px ellipsizes); the overview
  grid must target `#view-overview.active` (plain `#view-overview` is
  display:none when inactive, and showView toggles .active only); compare-
  chart PALETTE in JS is a separate mid-saturation line palette that works on
  both grounds — deliberately untouched (JS lane).

## Terminal follow-ups (2026-08-14 evening, owner-approved batch)

- **PWA icons recolored** to the terminal palette: same brand-arrow mark (the
  header's brandGrad shape — NOTE: the mark is the arrow, not the "ascending
  bars" older notes mention), same filenames/sizes/safe-zone placement
  (icon-192/512 maskable at ~52% mark width, apple-touch at ~64%), bg #0a0e14,
  gradient #3fb6dd→#7fd4f2 in the logo's direction. Generated with cairosvg
  from the exact SVG path; manifest.json colors were already #0a0e14.
- **"Ownership & deals" promoted to a first-class panel** (`.dc-stakes`):
  `#dcStakes` moved up the detail card — directly after `scoreNarrative()`
  (Main drivers), before the breakdowns/technicals/peers. Accent left border,
  accent-tinted surface, uppercase title; up to 4 events (was 3), each with a
  form tag (`window.__stakes.tag`, new) + right-aligned mono date; offer-period
  banner gets a warning-tinted `.dcs-offer`. Still renders nothing when a
  stock has no events; `_skPending` deep-link wiring unchanged. On <640px the
  filing text takes the full line and tag+date wrap beneath.
- **Watchlist tab → TWS-style quote grid**: columns Star | Ticker (tiny name
  beneath) | Last | Chg% | Chg (abs, derived from price+dayChange) | Wk% |
  Score | Q(uintile); ~32px rows, hairline separators, styling scoped to
  `#wlTable` (the td:first-child trap). WL_SORT kept — `dayAbs` sorts via a
  derived getter. **Live tick flashes**: `WL_LAST` map remembers each ticker's
  last RENDERED price; the 45s poller's existing `renderWatchlistView()` call
  makes changed rows' Last+Chg% cells re-mount with `.wl-up`/`.wl-dn`
  (600ms background pulse via keyframes, disabled under
  prefers-reduced-motion). Renders >90s apart rebase silently so re-entering
  the tab doesn't flash everything. Chg%/Wk%/Chg are persistently pos/neg
  colored; Last only flashes. Signed-out prompt, add-box, star wiring, D1
  write-through, system chips, My-alerts card all untouched.
- **Divbar (Most Under/Overvalued) densified** — root cause found: density
  keyed on container width <480px, and the redesign's grid-2 column is ~456px
  on desktop, so desktops rendered the airy touch layout. Now keys on
  `(pointer: coarse)`: fine pointers get 21px rows / 8px bars / rx 1 /10.5px
  labels; touch keeps 28px/11px. List is ~340px tall on desktop (was ~640).
- **Sectors view filters** (`#secControls`, standard `.controls` chrome):
  sector-name search, region chips All/US/UK&EU (screener's `isEuRow` proxy —
  ARM/SPOT count US), sort select (total cap default / member count / median
  combined / median P/E asc / avg day move; nulls last). Region filtering
  recomputes every aggregate over filtered members only; empty sectors drop;
  `#secCount` shows sectors·stocks; the top-12 expander works against the
  filtered+sorted result.
- Test harness: `mini/e2e-followup.mjs` (serves mini root so /out-test and
  /out-before coexist) — injects a fake `vt-session` + route-fulfills
  `/me` & `/me/rules` to exercise the signed-in grid, and simulates a live
  tick by mutating DATA + calling renderWatchlistView in page context.

## Multi-product split (2026-08-14, owner-approved)

- **URL map**: valuetally.com/ = NEW landing page (`home.html` on claude/state,
  a COMPLETE standalone document — full doctype/head/body, NOT processed by
  refresh.py) · `/app/` = the retail dashboard (template.html, unchanged
  pipeline) · `/command/` `/wall/` `/ledger/` = the three pro layouts
  (`pro/command.html`, `pro/wall.html`, `pro/ledger.html` on claude/state —
  standalone documents like home, productionized from the owner-approved
  concept mockups; wall = the "macrowall" concept). Publish wiring (workflows
  lane) copies each to `<dir>/index.html` on claude/pages.
- **Absolute data-path rule**: every published data file lives at the SITE
  ROOT and is fetched with a leading slash — `/data.json` (slim array, written
  by refresh.py beside index.html), `/detail-data.json`, `/stakes-data.json`,
  `/price-history.json` (this one is pipeline STATE, not an OUT file — the
  publish step must copy it to the root). template.html's detail/stakes
  fetches were switched to absolute 2026-08-14 so the retail app works from
  `/app/`; PH_URL/PHL_BASE/NEWS_URL/REQLOG_URL stay raw.githubusercontent and
  are unaffected. Any NEW fetch of a published file must be root-absolute.
- **Shared gate key**: all four products enforce the first-visit disclaimer
  via the SAME localStorage key `sd-agreed-v1` — the pro pages show a compact
  per-concept styled blocking overlay (`#gate`, essential content only:
  mechanical screen / not advice / Finnhub & Yahoo may be delayed or wrong /
  information only); agreeing anywhere passes everywhere. Don't fork the key.
- **Pro-page furniture** (all three): product switcher in each page's chrome
  (wordmark → `/`, links Retail `/app/` · Command · Wall · Ledger, current
  marked `.cur`); `#dataErr` full-screen "data unavailable" state on any
  failed data fetch (never a blank page); `#mobNote` dismissible
  built-for-desktop bar under 700px (sessionStorage `vt-pro-mob`) — the pages
  are deliberately desktop-first, no responsive rebuild; favicon
  `/icon-192.png`, per-concept theme-color (#080806/#07090d/#121419). The
  Ledger top bar sheds #count/<1500px, shrinks search/<1340px, drops Q-chips
  /<1180px so the switcher never clips.
- **home.html hash-redirect contract**: the FIRST script in home's head is
  `if (location.hash) location.replace('/app/' + location.hash);` — every
  legacy deep-link (`#watchlist`, `#stakes`, `#TICKER`…) from emails/API
  docs/bookmarks lands in the retail app unchanged. Must stay first, before
  any paint. Landing is ice-terminal family (dark #0a0e14 ground, cyan
  #3fb6dd, brand arrow mark), dark-first with a prefers-color-scheme light
  palette, responsive to 360px, previews are its only external requests.
- **Previews path contract**: `pro/previews/{retail,command,wall,ledger}.jpg`
  on claude/state (800px wide JPEG q75, <120KB) are published to
  `/previews/…` — home.html references them absolutely. Retail preview is a
  dark-scheme 1280×900 shot of the live #overview; the other three are
  downscaled concept screenshots — re-shoot when a product's look changes
  materially.
- **Test harness**: `staging/e2e-ship.mjs` in the session scratchpad builds
  the production layout (home at root, app/command/wall/ledger subdirs, data
  at root) and asserts cards+previews, hash redirects (fresh document load —
  same-document hash changes don't re-run head scripts), per-page gate
  show/dismiss/persist, switcher link resolution, data render, 404 → dataErr,
  and the retail stock card merging /detail-data.json from under /app/.
