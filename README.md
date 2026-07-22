# Stock Dashboard — pipeline state branch

This branch is the state store and template source for the automated stock-value
dashboard published to GitHub Pages (branch `claude/pages`) at
https://harris120807.github.io/stock-dashboard/

It replaced the previous Google Drive state files on 2026-07-15.

## Files

| File | Written by | Read by | Contents |
|---|---|---|---|
| `template.html` | manual edits | hourly refresh | Dashboard HTML fragment with a `/*__DATA__*/` placeholder that the hourly routine replaces with the live 80-record JSON array |
| `universe.json` | weekly universe refresh (Mon 11:00 UTC) | daily + hourly | `tickers` (80 = top 50 US + top 30 UK/EU by market cap), `us`, `europe` (native ticker + ADR pairs), `added`, `dropped`, `updatedAt` |
| `analyst-state.json` | daily pre-market prefetch (weekdays 12:00 UTC) | hourly | Per-ticker analyst recommendation counts, analyst sentiment score, next-earnings consensus + beat/miss history |
| `watchlist-state.json` | hourly refresh (weekdays 12:45–19:45 UTC) | hourly (next run) | Current top-3 buy / bottom-3 sell watchlist by Combined Score, for change detection |

## Universe definition

- **US 50**: Yahoo screener, US region, top 50 by market cap after filtering OTC
  listings, preferred share classes, duplicate share classes (GOOG over GOOGL,
  BRK-B over BRK-A), and non-US-domiciled companies.
- **Europe 30**: from the same screen, companies domiciled in the UK/Europe
  (classified via Finnhub country of HQ), excluding US-operational tax-inversion
  domiciles (Linde, Eaton, Medtronic, Accenture, …), ranked by Finnhub market cap
  converted to USD at spot FX (cross-checked against the Yahoo screen value; if the
  two differ by more than 3x the Yahoo USD value wins — catches Finnhub currency
  mislabels like Equinor's NOK-as-USD). Each entry is displayed on its native home-exchange
  listing (`ticker`, e.g. HSBA.L, MC.PA, NESN.SW — priced in local currency via
  Yahoo), while fundamentals and analyst data are fetched from its US ADR line
  (`adr`, e.g. HSBC, LVMUY — Finnhub's free tier only serves US symbols). ARM has
  no home listing and stays on Nasdaq. Valuation ratios are share-line invariant,
  so Value Scores are unaffected by which line supplies them.

Non-USD level quantities (market cap, revenue) are normalized to USD by the
hourly routine using Yahoo `{CCY}USD=X` spot rates; per-share quantities (price,
EPS, 52-week range) stay in the listing's local currency and each record carries
a `currency` field that the template's `fmtMoney` formatter renders (p, €, CHF,
kr, $).
