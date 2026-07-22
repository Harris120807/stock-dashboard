You are running a silent, unattended weekly data-prep job. This is fully automated — do not ask any questions, do not wait for confirmation. You have zero prior context beyond this message, which is fully self-contained.

GOAL: Recompute the dashboard universe — the top 50 US stocks by market cap PLUS the top 30 UK/European stocks by market cap — from a live market-wide screen, and write it to `universe.json` on the `claude/state` branch of the GitHub repo Harris120807/stock-dashboard, for two separate routines (a daily analyst prefetch and an hourly refresh) to read. This routine does NOT publish a dashboard.

GIT ACCESS: no token needed — this environment's proxy authenticates git itself and only permits pushing to branches prefixed `claude/`. Use plain git over https (clone/commit/push); do NOT call api.github.com.
Read/write state like this:
```
git clone --depth 1 --branch claude/state https://github.com/Harris120807/stock-dashboard.git /tmp/state
# ...edit /tmp/state/universe.json...
cd /tmp/state && git config user.email "pipeline@claude.local" && git config user.name "Stock Dashboard Pipeline"
git add universe.json && git commit -m "universe: weekly refresh <ISO timestamp>" && git push origin claude/state
```
If the push is rejected (concurrent writer), run `git pull --rebase origin claude/state` and push again.

FINNHUB API KEY: <FINNHUB_API_KEY — real value lives in the Routine trigger config, not in git> (base https://finnhub.io/api/v1)
YAHOO USER-AGENT (required on every Yahoo call): "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
NOTIFICATION (only if the universe changed — Step 7): curl -H "Title: Stock Dashboard Universe Updated" -H "Tags: bar_chart" -H "Click: https://harris120807.github.io/stock-dashboard/" -d "MESSAGE_BODY" https://ntfy.sh/harris-stockdash-3cb22f88

=== STEP 1 — Yahoo crumb + cookie ===
Using a session that persists cookies (e.g. Python requests.Session): GET https://fc.yahoo.com (404 is normal; the cookie matters), then GET https://query1.finance.yahoo.com/v1/test/getcrumb — the body is the crumb. Retry the two-step flow once if empty.

=== STEP 2 — Screener, top 500 ===
POST twice (offset 0 and offset 250) to https://query1.finance.yahoo.com/v1/finance/screener?formatted=true&lang=en-US&region=US&crumb=<URL-encoded crumb>, headers Content-Type/Accept application/json + the User-Agent, body {"size":250,"offset":<0|250>,"sortField":"intradaymarketcap","sortType":"DESC","quoteType":"EQUITY","query":{"operator":"AND","operands":[{"operator":"EQ","operands":["region","us"]}]}}.
Parse response.finance.result[0].quotes; per quote keep: symbol, longName/shortName, exchange, fullExchangeName, marketCap.raw. Merge pages, dedupe by symbol, sort by marketCap desc. If the screener fails after one full retry, re-write the prior universe.json unchanged (so downstream still has a file) and stop.

=== STEP 3 — Hygiene filters (both lists start here) ===
1. Drop symbols containing "-" except BRK-A/BRK-B.
2. Dedupe rows sharing the same longName: prefer GOOG and BRK-B explicitly; otherwise prefer a non-OTC row (exchange not in {PNK,OQX,OQB,OTC,OID} and fullExchangeName not containing "OTC"); otherwise keep the first (higher mcap). Then drop GOOGL and BRK-A if their preferred twin exists.

=== STEP 4 — Classify US vs foreign ===
A row is FOREIGN if any of: (a) it is OTC per the definition above; (b) its name matches this case-insensitive end-anchored regex (do NOT pre-strip trailing periods): \b(plc|p\.l\.c\.|n\.v\.|nv|s\.a\.|sa|ag|se|asa|oyj|a/s|ltd|limited|spa|s\.p\.a\.|ab)\.?$  — e.g. "ASML Holding N.V." and "HSBC Holdings plc" match, but "NVIDIA" must NOT (word boundary, end anchor); (c) its symbol is in KNOWN_FOREIGN = {TSM,BABA,RY,TD,BMO,BNS,CM,SU,ENB,CNQ,TRP,PDD,JD,BIDU,NTES,SONY,TCOM,NIO,LI,XPEV,MUFG,SMFG,MFG,TM,IBN,HDB,RIO,SNY}.
US_50 = the first 50 non-foreign rows (market-cap descending). Note this deliberately keeps US-listed companies like SKHY and SPCX that the name/OTC rules don't catch — do not add extra exclusions.

=== STEP 5 — Europe top 30 ===
From the FOREIGN rows, in market-cap-descending order:
1. Skip US-operational tax-inversion domiciles: {LIN,ETN,MDT,ACN,TT,JCI,AON,WTW,CB,APTV,PNR,ALLE,STE,IR,GRMN,TEL,ICLR,AMCR,SW,VRT}.
2. Stop scanning below $60B screen market cap.
3. Swap thin F-suffix OTC ordinary lines for their liquid Y-ADR before any Finnhub call: {DOGEF:DNNGY, RHHVF:RHHBY, NSRGF:NSRGY, ALIZF:ALIZY, SBGSF:SBGSY, EADSF:EADSY, IBDSF:IBDRY, DTEGF:DTEGY, UNCFF:UNCRY, GLCNF:GLNCY, MURGF:MURGY, ENGQF:ENGIY, BAESF:BAESY, LVMHF:LVMUY, SIEGF:SIEGY, SMEGF:SMERY}.
4. GET Finnhub /stock/profile2?symbol={SYM} (pace 0.3s). Keep only rows whose profile2 `country` is in {GB,IE,FR,DE,NL,CH,SE,DK,NO,FI,ES,IT,BE,AT,PT,LU,PL,JE,GG,IM}.
5. Trusted USD mcap = profile2.marketCapitalization × FX(profile2.currency), where FX(CCY) = last close of Yahoo chart {CCY}USD=X (range=5d, cache per currency; FX(USD)=1). SANITY: if the Yahoo screen mcap for the row exists and trusted/screen ratio is >3 or <1/3, use the screen value instead (catches Finnhub currency mislabels, e.g. Equinor reporting NOK values labeled USD).
6. Re-rank the European rows by trusted USD mcap descending. For each candidate, resolve its NATIVE home listing: first try NATIVE_MAP = {ASML:ASML.AS, RHHBY:RO.SW, ARM:ARM, LVMUY:MC.PA, NSRGY:NESN.SW, HSBC:HSBA.L, SIEGY:SIE.DE, LRLCY:OR.PA, NVS:NOVN.SW, NVO:NOVO-B.CO, SAN:SAN.MC, HESAY:RMS.PA, AZN:AZN.L, IDEXY:ITX.MC, SAP:SAP.DE, ALIZY:ALV.DE, SBGSY:SU.PA, EADSY:AIR.PA, SHEL:SHEL.L, TTE:TTE.PA, IBDRY:IBE.MC, RYCEY:RR.L, SAFRY:SAF.PA, ABBNY:ABBN.SW, SMERY:ENR.DE, DTEGY:DTE.DE, BBVA:BBVA.MC, UNCRY:UCG.MI, BUD:ABI.BR, UBS:UBSG.SW, BNPQY:BNP.PA, ISNPY:ISP.MI, BTI:BATS.L, CFRUY:CFR.SW, RIO:RIO.L, UL:ULVR.L, GSK:GSK.L, BP:BP.L, SPOT:SPOT, EQNR:EQNR.OL, ING:INGA.AS, BCS:BARC.L, LYG:LLOY.L, NGG:NG.L, SNY:SAN.PA, ZURVY:ZURN.SW, AXAHY:CS.PA, PROSY:PRX.AS, ADYEY:ADYEN.AS, DB:DBK.DE, ESLOY:EL.PA, RACE:RACE.MI, CRH:CRH, NWG:NWG.L, MURGY:MUV2.DE, BAESY:BA.L, GLNCY:GLEN.L, E:ENI.MI, DNNGY:ORSTED.CO}; if not in the map, check Finnhub profile2's `ticker` field — if it contains a "." exchange suffix (e.g. "SAN.PA") use that; otherwise keep the US symbol itself (some genuinely have only a US listing, like ARM and SPOT).
7. VALIDATE each candidate before accepting: Yahoo chart for the native symbol (range=2y, interval=1d) must return ≥250 non-null closes and a meta.regularMarketPrice; Finnhub /stock/metric?symbol={ADR}&metric=all must return a non-null metric.peTTM or metric.pb. Accept validated candidates in rank order until you have 30 (skip failures and continue down the list).
EUROPE_30 = the resulting list of {ticker: <native>, adr: <US symbol>} pairs.

=== STEP 6 — Write state ===
Read the prior /tmp/state/universe.json (its `tickers` array). Build the new file:
{"updatedAt":"<ISO now>", "tickers":[US_50 tickers + EUROPE_30 native tickers, all sorted descending by USD market cap], "us":[US_50], "europe":[EUROPE_30 pairs], "added":[tickers in new not in prior], "dropped":[tickers in prior not in new]}
Commit and push to claude/state as shown above. Do this every run even if unchanged.

=== STEP 7 — Notify only on change ===
If added or dropped is non-empty (and the prior list was non-empty): send the ntfy notification with MESSAGE_BODY = "Universe updated — added: {added or 'none'}. dropped: {dropped or 'none'}. Takes effect on the next daily/hourly refresh." Otherwise stay silent.

Do not call the Artifact tool. Never fail the run without writing SOME universe.json (fall back to re-writing the prior one).
