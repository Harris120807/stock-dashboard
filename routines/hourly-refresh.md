You are refreshing a live financial dashboard. This is a fully automated, non-interactive scheduled run — do not ask questions, do not wait for confirmation, complete every step and finish. You have zero prior context beyond this message, which is fully self-contained.

TIMING: this fires 15 minutes before its target notification time (e.g. 12:45 UTC for a 13:00 UTC notification). Work efficiently — finish well within 15 minutes.

GOAL: Fetch fresh fundamentals and price/technical data for the dashboard's 80-ticker universe (top 50 US stocks + top 30 UK/European stocks on their native home-exchange listings), combine with a daily analyst cache, recompute Value/Indicator/Combined scores, rebuild the HTML dashboard from a template, publish to GitHub Pages (and a fixed Artifact URL if the Artifact tool is available), and maintain a buy/sell watchlist that only notifies on change.

FINNHUB API KEY: <FINNHUB_API_KEY — real value lives in the Routine trigger config, not in git> (base https://finnhub.io/api/v1)
YAHOO USER-AGENT (required on every Yahoo call): "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
GITHUB REPO: Harris120807/stock-dashboard. No token — the environment's git proxy authenticates itself and only permits pushes to `claude/`-prefixed branches. Plain git over https only; do NOT call api.github.com. The public site is served from branch `claude/pages`: https://harris120807.github.io/stock-dashboard/
FIXED ARTIFACT URL (only if an Artifact tool is available in your session): https://claude.ai/code/artifact/d5987bbf-966d-431c-a4fd-d9a68c40059d

=== STEP 0 — Read pipeline state ===
git clone --depth 1 --branch claude/state https://github.com/Harris120807/stock-dashboard.git /tmp/state
This gives you: template.html, universe.json, analyst-state.json, watchlist-state.json.
- universe.json: "us" = ~50 US tickers (display ticker = fetch symbol); "europe" = ~30 pairs {"ticker": <native listing, e.g. "HSBA.L">, "adr": <US symbol for Finnhub, e.g. "HSBC">}; "tickers" = all 80 display tickers.
- Verify template.html is non-empty and contains the literal `/*__DATA__*/`. TEMPLATE SELF-HEAL (idempotent): if it does not contain `color-scheme: light dark`, insert `  :root { color-scheme: light dark; }` right after the first `<style>` tag and save (mobile forced-dark-mode contrast fix).
- If universe.json is missing/unparseable use this fallback ("FETCH>DISPLAY" where they differ):
US: NVDA, AAPL, GOOG, MSFT, AMZN, AVGO, SPCX, META, TSLA, SKHY, MU, BRK-B, LLY, JPM, WMT, AMD, V, JNJ, XOM, INTC, AMAT, MA, CSCO, BAC, ABBV, LRCX, CAT, COST, UNH, ORCL, GE, CVX, MS, KO, HD, PG, GS, PLTR, NFLX, MRK, KLAC, DELL, PANW, GEV, TXN, PM, WFC, RTX, SNDK, AXP
EUROPE: ASML>ASML.AS, RHHBY>RO.SW, ARM>ARM, LVMUY>MC.PA, NSRGY>NESN.SW, HSBC>HSBA.L, SIEGY>SIE.DE, LRLCY>OR.PA, NVS>NOVN.SW, NVO>NOVO-B.CO, SAN>SAN.MC, HESAY>RMS.PA, AZN>AZN.L, IDEXY>ITX.MC, SAP>SAP.DE, ALIZY>ALV.DE, SBGSY>SU.PA, EADSY>AIR.PA, SHEL>SHEL.L, TTE>TTE.PA, IBDRY>IBE.MC, RYCEY>RR.L, SAFRY>SAF.PA, ABBNY>ABBN.SW, SMERY>ENR.DE, DTEGY>DTE.DE, BBVA>BBVA.MC, UNCRY>UCG.MI, BUD>ABI.BR, UBS>UBSG.SW

=== STEP 1 — Fetch market data (pace ~0.3s between calls; ~300 calls; checkpoint per ticker so a timeout loses nothing; rerun skips fetched tickers; after 2 retries record null and move on) ===
For each US ticker (fetch symbol = display ticker):
1. Finnhub /stock/profile2?symbol={T} → name, finnhubIndustry, marketCapitalization (millions, local currency), shareOutstanding (millions), currency
2. Finnhub /quote?symbol={T} → c (current price)
3. Finnhub /stock/metric?symbol={T}&metric=all → metric object (fields listed in Step 2)
4. Yahoo https://query1.finance.yahoo.com/v8/finance/chart/{T}?range=2y&interval=1d → result[0].indicators.quote[0].close (filter nulls, keep order) AND result[0].meta (currency, regularMarketPrice)
For each European pair: calls 1 and 3 use the `adr` symbol; call 4 uses the native `ticker`; SKIP call 2 (the ADR quote is often dead) — the price comes from the native chart's meta.regularMarketPrice, falling back to the last non-null close.

=== STEP 2 — Build each record ===
{"ticker": <display ticker>, "name": profile.name, "sector": profile.finnhubIndustry (verbatim), "price": <see Step 1>, "marketCap": <see FX below>, "pe": metric.peTTM, "fpe": metric.forwardPE, "peg": metric.pegTTM, "ps": metric.psTTM, "pb": metric.pb else metric.pbQuarterly, "evRev": metric.evRevenueTTM, "evEbitda": metric.evEbitdaTTM, "eps": <see FX below>, "divYield": metric.currentDividendYieldTTM (already a percent — do NOT multiply by 100), "hi52"/"lo52": <see FX below>, "beta": metric.beta, "margin": metric.netProfitMarginTTM, "roe": metric.roeTTM, "roa": metric.roaTTM, "revenue": <see FX below>, "de": metric["totalDebt/totalEquityQuarterly"]*100 if present else null, "currency": <Yahoo chart meta currency, e.g. "USD","GBp","EUR","CHF","DKK">, "dataSource": "Finnhub (live)"}
Use JSON null for anything missing. For European records also store "adr": <the ADR symbol>.

CURRENCY NORMALIZATION — the dashboard mixes listings, so level quantities must be handled explicitly (ratios like pe/peg/pb/evEbitda/margin/roe/divYield are share-line and currency invariant — use them as-is):
- FX(CCY) = last close of Yahoo chart {CCY}USD=X?range=5d (cache per currency per run; FX("USD")=1). Finnhub profile2.currency tells you the fundamentals currency.
- marketCap (always USD billions): round(profile.marketCapitalization × FX(profile.currency) / 1000, 1)
- revenue (USD billions): round(metric.revenuePerShareTTM × profile.shareOutstanding × FX(profile.currency) / 1000, 2) if both present else null
- price / eps / hi52 / lo52 stay in the DISPLAY listing's local currency (the `currency` field): for US tickers, eps = metric.epsTTM and hi52/lo52 = metric["52WeekHigh"]/metric["52WeekLow"] as before; for European tickers compute eps = price/pe if pe is a positive number else null, and hi52/lo52 = max/min of the last 252 native closes (Finnhub's per-share numbers are in the home currency at ADR-ratio scale — do not use them for display).
SANITY (US tickers, dual-class protection): if hi52 > price*50 or hi52 < price/50 → null (same for lo52); if abs(eps) > price*20 → eps = price/pe if pe > 0 else null.

=== STEP 3 — Analyst target & upside (static baseline, US only; targets move slowly) ===
TARGET_PRICE_BASELINE = {"C":155.15,"BAC":65.45,"WFC":97.93,"MU":1486.0,"BRK-B":520.33,"JPM":352.76,"XOM":167.38,"META":827.91,"AMZN":312.91,"MSFT":559.93,"CVX":217.14,"MS":216.48,"GS":1012.2,"AXP":366.58,"ORCL":251.85,"PG":163.3,"GOOG":428.54,"UNH":420.46,"NFLX":113.15,"DELL":487.26,"JNJ":258.59,"GEV":1222.63,"NVDA":301.62,"INTC":100.88,"RTX":215.73,"V":401.16,"IBM":294.57,"KO":86.18,"PM":194.86,"MRK":132.07,"SNDK":2035.05,"HD":370.34,"WMT":138.59,"MA":643.84,"CSCO":127.18,"TXN":298.0,"LLY":1222.62,"AMAT":578.91,"COST":1080.33,"CAT":962.49,"AVGO":523.73,"AAPL":315.57,"GE":372.05,"AMD":516.12,"LRCX":357.77,"PANW":318.32,"PLTR":183.12,"KLAC":225.79,"ABBV":254.38,"TSLA":424.56}
"target" = baseline value if the ticker is listed, else null. "upside" = (target-price)/price*100 or null.

=== STEP 4 — Sector color groups ===
MERGE = {"Financial Services":"Financial Services & Banking","Banking":"Financial Services & Banking"}. candidate_group = MERGE.get(sector, sector). Count per group across all 80; the top 8 groups by count keep their name as "colorGroup", everything else gets "Other". Recompute fresh every run.

=== STEP 5 — Value score (percentile within the 80-stock set; higher = cheaper) ===
For each of pe, peg, pb, evEbitda independently over stocks with a positive value: sort ascending; score = 100*(n-1-rank)/(n-1) (rank 0-indexed ascending; n==1 → 50), round 1dp. Missing metric → null in breakdown.
"scoreBreakdown" = {"pe":...,"peg":...,"pb":...,"evEbitda":...}; "valueScore" = mean of non-null scores (1dp, null if none); "position": rank valueScores, thirds → "Overvalued"/"Fair Value"/"Undervalued" (null → "Unclassified").

=== STEP 6 — Analyst cache ===
From /tmp/state/analyst-state.json (byTicker keyed by DISPLAY ticker — native symbols for Europe): pull analystScore, analystRec, earnings onto each record. Missing ticker or file → analystScore/analystRec null and earnings all-null object. Do NOT call Finnhub's recommendation/earnings endpoints in this routine.

=== STEP 7 — Indicator Score (from the Step 1 closes — native closes for Europe) ===
sma50 = mean(last 50 closes) [≥50 needed]; sma200 = mean(last 200) [≥200 needed]; RSI14 from the last 14 day-over-day changes: gains/losses means → rs = avg_gain/avg_loss, rsi = 100-100/(1+rs) (avg_loss 0 → 100), 1dp, null if <15 closes.
sma50Score = 1 + clamp((price-sma50)/sma50, ±0.25); sma200Score = 1 + clamp((price-sma200)/sma200, ±0.30); crossState = "golden" if sma50>sma200 else "death" (null if either missing); crossScore = 1.15/0.85/null; rsiScore = 1+(50-rsi14)/100; analystScore from Step 6.
"indicatorScore" = mean of the non-null five (3dp, null if all null). "combinedScore" = round(valueScore*indicatorScore, 2), falling back to valueScore alone.
Store "technicals": {"sma50","sma200","rsi14","crossState","analystRec",...,"scoreBreakdown":{"sma50","sma200","cross","rsi","analyst"}} plus top-level "indicatorScore", "combinedScore", "earnings".
(Note: price and closes are both in the native listing currency, so these ratios are consistent by construction.)

=== STEP 8 — Watchlist + change detection ===
Sort by combinedScore desc (nulls last). buy_watch = top 3 tickers; sell_watch = bottom 3 (lowest non-null).
Prior state = /tmp/state/watchlist-state.json ({"buy":[...],"sell":[...]}; missing → empty lists).
entered_buy/exited_buy/entered_sell/exited_sell = set diffs; changed = any non-empty (or no prior state).
Write /tmp/state/watchlist-state.json = {"buy":buy_watch,"sell":sell_watch,"updatedAt":"<ISO now>"}; then in /tmp/state: git config user.email "pipeline@claude.local" && git config user.name "Stock Dashboard Pipeline" && git add watchlist-state.json && git commit -m "watchlist: <ISO now>" && git push origin claude/state (on rejection: git pull --rebase origin claude/state, push again). Do this every run.

=== STEP 9 — Build the HTML ===
JSON array of all 80 records (compact), sorted by combinedScore desc (nulls last). Replace the literal `/*__DATA__*/` in template.html → dashboard_final.html. Sanity check: extract the <script>…</script> contents to a .js file and `node --check` it; fix any encoding issue (unescaped characters in company names) and retry.

=== STEP 10 — Publish ===
10a (only if an Artifact tool is available in your session — otherwise skip this sub-step entirely, it is optional): call it with file_path=dashboard_final.html, url="https://claude.ai/code/artifact/d5987bbf-966d-431c-a4fd-d9a68c40059d", favicon="📊", description="Top 50 US + top 30 UK/European stocks with live fundamentals, technicals, analyst sentiment, and earnings calendar.", label="auto refresh <date + target time ET>".
10b GitHub Pages: dashboard_final.html is a fragment (no doctype/head/body — correct for Artifacts, invalid standalone). Split at the first `</style>`: head_part = through `</style>`, body_part = rest. Wrap:
<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n{head_part}\n</head>\n<body>\n{body_part}\n</body>\n</html>
Save as dashboard_github.html, then:
git clone --depth 1 https://github.com/Harris120807/stock-dashboard.git /tmp/ghpages && cp dashboard_github.html /tmp/ghpages/index.html && cd /tmp/ghpages && git config user.email "pipeline@claude.local" && git config user.name "Stock Dashboard Pipeline" && git checkout -B claude/pages && git add index.html && git commit -m "auto refresh <ISO timestamp>" && git push -f origin claude/pages
(The force push is intentional: each run recreates the branch with one fresh commit; nothing else writes to it.) Log full stdout/stderr of clone+push. If a git step fails, do NOT fail the run — continue to Step 11 and append " (page may show stale data — GitHub push failed: <one-line reason>)" to the notification.

=== STEP 11 — Notify (always, via curl) ===
curl -H "Title: Stock Dashboard Refreshed" -H "Tags: bar_chart" -H "Click: https://harris120807.github.io/stock-dashboard/" -d "MESSAGE_BODY" https://ntfy.sh/harris-stockdash-3cb22f88
MESSAGE_BODY: base = "Refreshed for {target time = fire time + 15 min, in ET} — {N}/80 tickers live{, X stale if any}."
If changed (Step 8): append " Buy watch: {buy_watch}. Sell watch: {sell_watch}. New this run: {entered_buy+entered_sell, omit if empty}. Ranked by value × technical/sentiment score — not investment advice."
If not changed: base sentence only.
EARNINGS HEADS-UP: for any ticker whose earnings.nextDate == tomorrow, append " {TICKER} reports earnings tomorrow ({before open/after close}): consensus EPS est ${epsEstimate}, beat estimates in {beatCount}/{beatTotal} of last quarters." (Factual only — no predictions, no buy/sell phrasing. The EPS estimate for European tickers is per ADR share in USD.)
Send the notification even on partial failure — describe the issue in the body instead of skipping.

If something fails partway (API outage), publish with whatever data is available rather than aborting, and note it in the notification.
