# Stock Dashboard — Project Memory

Auto-refreshing stock value dashboard: **top 50 US + top 30 UK/European stocks** by
market cap, scored on valuation multiples, technicals, and analyst sentiment.

- **Live site**: https://harris120807.github.io/stock-dashboard/ (GitHub Pages, served from `claude/pages`)
- **Artifact mirror** (may lag; optional publish target): https://claude.ai/code/artifact/d5987bbf-966d-431c-a4fd-d9a68c40059d
- **Owner notifications**: ntfy.sh topic `harris-stockdash-3cb22f88` → owner's phone. **Never send test messages to it.**

## Branch map (read this before touching anything)

| Branch | Contents | Write rules |
|---|---|---|
| `main` | Workflows + legacy snapshot | Changes go via PR. **Standing owner authorization (2026-07-16): Claude may open AND self-merge PRs for small pipeline/workflow changes.** Anything that changes what the system *does* — new data sources, notification behavior, spending money, big architecture — still needs the owner's explicit OK first. |
| `claude/pages` | `index.html` only — the published site | Force-pushed as ONE fresh commit per refresh, only by a pipeline run. Never hand-edit; it's overwritten on every refresh. |
| `claude/state` | Pipeline state: `template.html`, `universe.json`, `analyst-state.json`, `watchlist-state.json`, `routines/*.md` (docs), `README.md` | Normal commits; on push rejection `git pull --rebase origin claude/state` and retry. |
| `claude/stock-dashboard-updates-*` | Dev/session branches | Per-session work. |

## Architecture

The pipeline runs on **GitHub Actions** (scheduled workflows on `main`, secret
`FINNHUB_API_KEY` in repo settings). Pure-Python stdlib scripts in `scripts/` —
no Claude sessions in the loop:

| Workflow | Script | Cron (UTC) | Writes |
|---|---|---|---|
| `weekly-universe.yml` | `scripts/weekly_universe.py` | `0 11 * * 1` | `universe.json`; ntfy only on membership change |
| `daily-analyst.yml` | `scripts/daily_analyst.py` | `0 12 * * 1-5` | `analyst-state.json` |
| `hourly-refresh.yml` | `scripts/refresh.py` | `45 12-19 * * 1-5` (backup — cron-job.org is primary, see workflow comment) | `claude/pages` (page + `pwa/` copy), `watchlist-state.json`, `last-data.json`, `price-history.json`, `price-history-long.json`, `requests-log.json`, ntfy push |

All three support `workflow_dispatch` for manual runs. Scripts read `FINNHUB_API_KEY`,
`STATE_DIR` (checkout of `claude/state`), `OUT_DIR`; they only write files — the
workflow steps do the git pushes and the ntfy notification (from `OUT_DIR/notify.txt`).

**History note**: three Claude Routine triggers (`trig_01EZNpuGei4t6XJryyAXtEKG` weekly,
`trig_01Bn3hEqV1UWQn5r8eUwmTsf` daily, `trig_01C1qZnkgCmAG9Y8HffytCYN` hourly) predate
the Actions port. Their spawned sessions could fetch and notify but never push, so they
were superseded; they should be disabled/deleted once Actions is confirmed running — if
they're still enabled and Actions is live, the owner gets DOUBLE notifications. Their
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
  `divYield` is already a percent.
- **Classification traps**: the foreign-name regex must match end-anchored, unstripped
  names (`p.l.c.`, `N.V.`); Rio Tinto/Sanofi carry no suffix → in KNOWN_FOREIGN; tax
  inversions (Linde, Eaton, Medtronic, Accenture…) are excluded from "European";
  GOOG is kept over GOOGL, BRK-B over BRK-A; SKHY/SPCX are treated as US by design.
- **Scoring**: valueScore = mean of percentile ranks of pe/peg/pb/evEbitda within the
  80-stock set (higher = cheaper); indicatorScore = mean of sma50/sma200/cross/RSI/analyst
  component scores centered at 1.0; combinedScore = valueScore × indicatorScore;
  watchlist = top 3 / bottom 3 by combinedScore, notify only on membership change
  (prior state in `watchlist-state.json`).

## Template / UI

`template.html` on `claude/state` is the single UI source — an HTML **fragment**
(no doctype/head/body) with a `/*__DATA__*/` placeholder that gets replaced by the
compact 80-record JSON array. The GitHub Pages copy is wrapped (split at first
`</style>`). After any template edit: extract `<script>` contents, `node --check`
them, then republish via `refresh.py`.

- `fmtMoney(d, v)` renders per-record currency (1,479p / €495.80 / CHF 334.00 / kr / $).
- **Per-stock refresh button** in the detail card calls Finnhub client-side (CORS `*`);
  US rows get live price + fundamentals, European rows fundamentals only (Yahoo has no
  CORS, so native prices can't refresh in-browser). Updates are view-local.
- The Finnhub API key **is embedded in the public page** — the owner explicitly
  authorized this (2026-07-15), and also authorized it inside trigger prompts. Don't
  re-litigate; also don't copy the key into new public files without asking. Key value:
  see `template.html` on `claude/state` (deliberately not duplicated here).
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
  `data-theme` dark/light) must define it. Chart *data* colors (`--series-*`,
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
- **Stock requests page** POSTs to public ntfy topic `harris-stockdash-req-a2962152`
  (deliberately separate from the private pipeline topic — it's spam-exposed by
  design; owner subscribes to it read-only). Client-side throttle: 1/min, 10/day (rolling 24h)
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
- **Deeper history**: `price-history-long.json` on `claude/state` =
  `{updatedAt, byTicker: {T: {t:[daynums], p:[daily closes, native ccy], st:[daynums],
  s:[combinedScore]}}}` — 5y daily closes (capped 1830d, seeded by
  `scripts/backfill_history.py`) + never-pruned daily score series, maintained
  incrementally by `refresh.py`. The 5Y chart range and its score overlay lazy-fetch
  this file (`PHL_URL`); 30D/1Y still come from `price-history.json`.

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
- Universe content questions (why isn't X listed?): check `universe.json` first — top 50
  US + top 30 EU by market cap, recomputed Mondays. Barclays/Corning/SK-Hynix-ADR were
  evaluated and fall outside the cutoffs; Rolls-Royce (RR.L) is in (#~22).

## History

- Pre-2026-07-15: pipeline state lived in Google Drive files, driven by http_api-created
  routines (deleted by owner). Drive files (`stock-dashboard-*.json`, template v5) are
  orphaned — ignore them.
- 2026-07-15: migrated to git state (`claude/state`), universe extended 50→80 with native
  European listings, currency normalization added, per-stock refresh button shipped,
  pipeline ported to GitHub Actions after Routine-spawned sessions proved unable to push.
