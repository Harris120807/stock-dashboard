# Stock Dashboard — Project Memory

Auto-refreshing stock value dashboard: **top 50 US + top 30 UK/European stocks** by
market cap, scored on valuation multiples, technicals, and analyst sentiment.

- **Live site**: https://harris120807.github.io/stock-dashboard/ (GitHub Pages, served from `claude/pages`)
- **Artifact mirror** (may lag; optional publish target): https://claude.ai/code/artifact/d5987bbf-966d-431c-a4fd-d9a68c40059d
- **Owner notifications**: ntfy.sh topic `harris-stockdash-3cb22f88` → owner's phone. **Never send test messages to it.**

## Branch map (read this before touching anything)

| Branch | Contents | Write rules |
|---|---|---|
| `main` | Legacy snapshot | Git proxy denies pushes — only `claude/*` branches are pushable. Changes to main need a PR the owner merges. |
| `claude/pages` | `index.html` only — the published site | Force-pushed as ONE fresh commit per refresh, only by a pipeline run. Never hand-edit; it's overwritten on every refresh. |
| `claude/state` | Pipeline home: `template.html`, `universe.json`, `analyst-state.json`, `watchlist-state.json`, `scripts/refresh.py`, `routines/*.md`, `README.md` | Normal commits; on push rejection `git pull --rebase origin claude/state` and retry. |
| `claude/stock-dashboard-updates-*` | Dev/session branches | Per-session work. |

## Architecture

Three scheduled Routines (Claude Code triggers, all `create_new_session_on_fire`):

| Routine | Trigger ID | Cron (UTC) | Writes |
|---|---|---|---|
| Weekly universe refresh | `trig_01EZNpuGei4t6XJryyAXtEKG` | `0 11 * * 1` | `universe.json` |
| Daily analyst prefetch | `trig_01Bn3hEqV1UWQn5r8eUwmTsf` | `0 12 * * 1-5` | `analyst-state.json` |
| Hourly market refresh | `trig_01C1qZnkgCmAG9Y8HffytCYN` | `45 12-19 * * 1-5` | `claude/pages`, `watchlist-state.json`, ntfy push |

Full prompt sources: `routines/*.md` on `claude/state` (API key redacted there; the live
key is in the trigger configs and in `template.html`).

**KNOWN LIMITATION (open as of 2026-07-15)**: sessions spawned by these triggers fetch
data and send notifications fine, but their **git pushes never land** (no pre-authorized
permission config — that capability was lost when the original http_api-created routines
were replaced). So the hourly routine notifies the owner but cannot publish. Publishing
currently happens from an interactive session running `scripts/refresh.py`. The agreed
fix candidate is a **GitHub Actions port** (PR to main + `FINNHUB_API_KEY` repo secret) —
awaiting owner go-ahead. If you solve this, update this section.

## Running the pipeline manually

```
git clone --depth 1 --branch claude/state https://github.com/Harris120807/stock-dashboard.git state
cd state && FINNHUB_API_KEY=<key> STATE_DIR=. OUT_DIR=/tmp/run NOTIFY=0 python3 scripts/refresh.py
# then push /tmp/run/index.html to claude/pages (force, one commit) and the updated
# watchlist-state.json to claude/state — see routines/hourly-refresh.md Step 10.
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

## Multi-agent coordination

- **Lanes**: (1) UI/template → `template.html`; (2) scoring/pipeline → `scripts/refresh.py`
  (+ mirror any behavior change into `routines/hourly-refresh.md`); (3) universe rules →
  `routines/weekly-universe-refresh.md` + trigger prompt; (4) automation/infra → the
  Actions port. Stay in your lane; state file *schemas* are shared contracts — changing
  one requires updating every reader (refresh.py, routines, this file).
- **Concurrency**: `claude/state` uses pull-rebase-retry; never force-push it.
  `claude/pages` is force-pushed single commits — never run two publishes at once.
- **Trigger edits**: `update_trigger` on these (meta_mcp-created) triggers can change
  name/cron/enabled only. Prompt changes = delete + recreate (keep `routines/*.md` in
  sync, key redacted — the permission classifier blocks committing the key to git except
  in `template.html` where the owner authorized it).
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
  European listings, currency normalization added, per-stock refresh button shipped.
