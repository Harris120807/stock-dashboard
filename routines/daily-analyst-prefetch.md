You are running a silent, unattended daily data-prep job. This is fully automated — do not ask any questions, do not wait for confirmation. You have zero prior context beyond this message, which is fully self-contained.

GOAL: Fetch analyst-consensus data (recommendation sentiment + earnings estimates/history) for the dashboard's 80-ticker universe (top 50 US + top 30 UK/European stocks) and write it to `analyst-state.json` on the `claude/state` branch of the GitHub repo Harris120807/stock-dashboard, for a separate hourly routine to read. This routine does NOT publish anything and does NOT send any notification.

GIT ACCESS: no token needed — this environment's proxy authenticates git itself and only permits pushing to branches prefixed `claude/`. Use plain git over https; do NOT call api.github.com.
```
git clone --depth 1 --branch claude/state https://github.com/Harris120807/stock-dashboard.git /tmp/state
# ...write /tmp/state/analyst-state.json...
cd /tmp/state && git config user.email "pipeline@claude.local" && git config user.name "Stock Dashboard Pipeline"
git add analyst-state.json && git commit -m "analyst-state: daily prefetch <ISO timestamp>" && git push origin claude/state
```
If the push is rejected (concurrent writer), `git pull --rebase origin claude/state` and push again.

FINNHUB API KEY: <FINNHUB_API_KEY — real value lives in the Routine trigger config, not in git> (base https://finnhub.io/api/v1)

=== STEP 0 — Get the universe ===
Parse /tmp/state/universe.json from the clone above. It contains:
- "us": ~50 US tickers — for these, the DISPLAY ticker and the FETCH symbol are the same.
- "europe": ~30 objects {"ticker": <native listing, e.g. "HSBA.L">, "adr": <US symbol, e.g. "HSBC">} — for these, fetch from Finnhub using the `adr` symbol but key all output by the native `ticker` (Finnhub's free tier only serves US symbols; the ADR and native listing are the same company so analyst data is identical).
If the file is missing or unparseable, use this fallback (formatted "FETCH>DISPLAY" where they differ):
US: NVDA, AAPL, GOOG, MSFT, AMZN, AVGO, SPCX, META, TSLA, SKHY, MU, BRK-B, LLY, JPM, WMT, AMD, V, JNJ, XOM, INTC, AMAT, MA, CSCO, BAC, ABBV, LRCX, CAT, COST, UNH, ORCL, GE, CVX, MS, KO, HD, PG, GS, PLTR, NFLX, MRK, KLAC, DELL, PANW, GEV, TXN, PM, WFC, RTX, SNDK, AXP
EUROPE: ASML>ASML.AS, RHHBY>RO.SW, ARM>ARM, LVMUY>MC.PA, NSRGY>NESN.SW, HSBC>HSBA.L, SIEGY>SIE.DE, LRLCY>OR.PA, NVS>NOVN.SW, NVO>NOVO-B.CO, SAN>SAN.MC, HESAY>RMS.PA, AZN>AZN.L, IDEXY>ITX.MC, SAP>SAP.DE, ALIZY>ALV.DE, SBGSY>SU.PA, EADSY>AIR.PA, SHEL>SHEL.L, TTE>TTE.PA, IBDRY>IBE.MC, RYCEY>RR.L, SAFRY>SAF.PA, ABBNY>ABBN.SW, SMERY>ENR.DE, DTEGY>DTE.DE, BBVA>BBVA.MC, UNCRY>UCG.MI, BUD>ABI.BR, UBS>UBSG.SW

=== STEP 1 — Fetch (per ticker, using the FETCH symbol; pace ~0.3s between calls; ~240 total, budget 4 minutes) ===
1. GET /stock/recommendation?symbol={SYM}&token={KEY} → take element [0] for strongBuy, buy, hold, sell, strongSell, period
2. GET /calendar/earnings?from={today}&to={today+30d}&symbol={SYM}&token={KEY} → response.earningsCalendar[0] if present: date, hour, epsEstimate, revenueEstimate
3. GET /stock/earnings?symbol={SYM}&token={KEY} → first 4 entries for beat/miss history
Write a script that checkpoints per-ticker (save each ticker's raw bundle to a file as soon as its 3 calls succeed) so a timeout loses nothing; rerunning skips already-fetched tickers. After 2 failed retries record null for that piece and move on.

=== STEP 2 — Analyst sentiment score ===
total = strongBuy+buy+hold+sell+strongSell. If total > 0: netBullish = (strongBuy*2 + buy - sell - strongSell*2)/(total*2); analystScore = round(1 + netBullish*0.3, 3). Else null.

=== STEP 3 — Earnings object ===
{"nextDate": <YYYY-MM-DD|null>, "nextHour": <"bmo"/"amc"/null>, "epsEstimate": ..., "revenueEstimate": ..., "beatCount": <of up-to-4 recent quarters with surprisePercent > 0, or null>, "beatTotal": <quarters with data, ≤4, or null>}

=== STEP 4 — Write state ===
/tmp/state/analyst-state.json = {"updatedAt":"<ISO now>", "byTicker": {<DISPLAY ticker>: {"analystScore":..., "analystRec": {strongBuy,buy,hold,sell,strongSell,period}, "earnings": {...}}, ... one entry for every ticker in the universe ...}}
Commit and push as shown above. If Finnhub has a partial outage, still write the file with nulls for missing tickers rather than aborting — a partial cache beats none.

Do not call the Artifact tool. Do not send any notification.
