#!/usr/bin/env python3
"""Hourly dashboard refresh for Harris120807/stock-dashboard.

Reads pipeline state (universe/analyst/watchlist/template) from a checkout of the
claude/state branch, fetches live data for all tickers, recomputes scores,
builds index.html for the claude/pages branch, and updates watchlist state.

Data sources per hourly run: prices/charts/FX from Yahoo only; fundamentals come
from fundamentals-state.json (prefetched daily by daily_analyst.py). Finnhub is
called per ticker only as a fallback (bootstrap, new entrants, failed Yahoo
fetch), keeping the shared 60/min budget free for the page's refresh buttons.

Env: FINNHUB_API_KEY (required), STATE_DIR (checkout of claude/state),
     OUT_DIR (workdir), NOTIFY=1 to send the ntfy push (default 0).
"""
import json, math, os, re, subprocess, sys, time, datetime, urllib.request

KEY = os.environ["FINNHUB_API_KEY"]
STATE = os.environ.get("STATE_DIR", "statebranch")
OUT = os.environ.get("OUT_DIR", "runout")
NOTIFY = os.environ.get("NOTIFY", "0") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
NTFY = "https://ntfy.sh/harris-stockdash-3cb22f88"
PAGES_URL = "https://valuetally.com/"
os.makedirs(f"{OUT}/ck", exist_ok=True)

def get(url, headers=None, retries=4):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if i == retries: return None
            ra = e.headers.get("Retry-After") if e.headers else None
            time.sleep(min(int(ra) if (ra and str(ra).isdigit()) else 2 ** (i + 1), 60))
        except Exception:
            if i == retries: return None
            time.sleep(2 ** i)

FH_PACE = float(os.environ.get("FINNHUB_PACE", "1.1"))  # ~55 Finnhub calls/min, under the free-tier 60/min limit

# ---------- state ----------
universe = json.load(open(f"{STATE}/universe.json"))
analyst = json.load(open(f"{STATE}/analyst-state.json"))["byTicker"]
# Fundamentals (profile2 + metric) are prefetched once a day by daily_analyst.py.
# The hourly path is Yahoo-only; Finnhub is hit per ticker only as a fallback
# (bootstrap, new universe entrants, or a failed Yahoo fetch).
try:
    FUND = json.load(open(f"{STATE}/fundamentals-state.json"))["byTicker"]
except Exception:
    FUND = {}
# Stored daily closes (5y, maintained by this script) — the hourly chart fetch is
# incremental (range=5d) stitched onto this, instead of refetching 2y per ticker
# per run. Yahoo retroactively rewrites history on splits/dividends, so Mondays
# do a full 2y refetch, and any run where the 5d overlap disagrees >3% with the
# stored closes on 2+ days triggers a per-ticker full resync.
# Long history is sharded one file per ticker (state/history/{T}.json,
# 2026-07-22 — a single 383-ticker file would be ~6MB per chart fetch).
def lh_read(t):
    try:
        return json.load(open(f"{STATE}/history/{t.replace('/', '_')}.json"))
    except Exception:
        return {"t": [], "p": [], "st": [], "s": []}
FULL_CHART = datetime.datetime.now(datetime.timezone.utc).weekday() == 0
try:
    prior_watch = json.load(open(f"{STATE}/watchlist-state.json"))
except Exception:
    prior_watch = {"buy": [], "sell": []}
template = open(f"{STATE}/template.html").read()
assert "/*__DATA__*/" in template
pairs = [(t, t) for t in universe["us"]] + [(e["ticker"], e["adr"]) for e in universe["europe"]]

# ---------- fetch ----------
fxc = {}
def fx(ccy):
    if ccy in (None, "USD"): return 1.0
    if ccy not in fxc:
        c = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ccy}USD=X?range=5d&interval=1d", UA)
        try: fxc[ccy] = [x for x in c["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x][-1]
        except Exception: fxc[ccy] = None
    return fxc[ccy]

FB_CAP = int(os.environ.get("FALLBACK_CAP", "25"))
FB_STATE = {"n": 0}

def fetch(ticker, sym):
    ckf = f"{OUT}/ck/{ticker.replace('/','_')}.json"
    if os.path.exists(ckf): return json.load(open(ckf))
    is_eu = sym != ticker or "." in ticker
    b = {}
    f = FUND.get(ticker) or {}
    b["profile"] = f.get("profile") or {}
    b["metric"] = f.get("metric") or {}
    b["refPrice"] = f.get("refPrice")
    b["quote"] = None
    if (not b["profile"] or not b["metric"]) and FB_STATE["n"] < FB_CAP:
        # live-Finnhub fallback, capped per run: a mass universe expansion would
        # otherwise burn the whole shared rate budget in one refresh — beyond the
        # cap, records render partial until the daily prefetch covers them
        FB_STATE["n"] += 1
        b["profile"] = get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={KEY}") or {}; time.sleep(FH_PACE)
        b["metric"] = (get(f"https://finnhub.io/api/v1/stock/metric?symbol={sym}&metric=all&token={KEY}") or {}).get("metric", {}); time.sleep(FH_PACE)
        b["refPrice"] = None
    def chart_get(rng):
        chart = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(ticker)}?range={rng}&interval=1d", UA); time.sleep(0.2)
        closes, close_ts, meta = [], [], {}
        try:
            res = chart["chart"]["result"][0]
            raw_c = res["indicators"]["quote"][0]["close"]
            raw_t = res.get("timestamp") or []
            if len(raw_t) == len(raw_c):
                for tt, cc in zip(raw_t, raw_c):
                    if cc is not None:
                        closes.append(cc); close_ts.append(tt)
            else:
                closes = [x for x in raw_c if x is not None]
            meta = res.get("meta", {})
        except Exception:
            pass
        return closes, close_ts, meta
    stored = lh_read(ticker)
    st_dn, st_p = list(stored.get("t") or []), list(stored.get("p") or [])
    if FULL_CHART or len(st_dn) < 260:
        closes, close_ts, meta = chart_get("2y")
    else:
        c5, t5, meta = chart_get("5d")
        m5 = {tt // 86400: cc for tt, cc in zip(t5, c5)}
        smap = dict(zip(st_dn, st_p))
        overlap = [dn for dn in m5 if dn in smap and dn < st_dn[-1]]
        bad = sum(1 for dn in overlap if smap[dn] and abs(m5[dn] - smap[dn]) / smap[dn] > 0.03)
        if bad >= 2 or not m5 or min(m5) > st_dn[-1] + 3:
            # Split/dividend rewrite, empty response, or a gap the 5d window
            # can't bridge — resync from the full 5y history and REPLACE the
            # stored price series (merging would leave pre-rewrite values in
            # place and re-trigger this detector every run).
            closes, close_ts, meta2 = chart_get("5y")
            meta = meta2 or meta
            b["resyncFull"] = bool(closes)
        else:
            for dn in sorted(m5):
                if dn == st_dn[-1]: st_p[-1] = m5[dn]
                elif dn > st_dn[-1]: st_dn.append(dn); st_p.append(m5[dn])
            closes, close_ts = st_p, [dn * 86400 for dn in st_dn]
    if not b.get("resyncFull"):
        closes, close_ts = closes[-520:], close_ts[-520:]
    b["closes"], b["closeTs"], b["meta"] = closes, close_ts, meta
    # Yahoo failed to give any price for a US row — fall back to a Finnhub quote
    # so one bad Yahoo response degrades to the old source, not to stale data.
    # (No EU fallback: the ADR quote is in the wrong listing/currency.)
    if not is_eu and meta.get("regularMarketPrice") is None and not closes:
        b["quote"] = get(f"https://finnhub.io/api/v1/quote?symbol={sym}&token={KEY}"); time.sleep(FH_PACE)
    json.dump(b, open(ckf, "w"))
    return b

TARGETS = {"C":155.15,"BAC":65.45,"WFC":97.93,"MU":1486.0,"BRK-B":520.33,"JPM":352.76,"XOM":167.38,"META":827.91,"AMZN":312.91,"MSFT":559.93,"CVX":217.14,"MS":216.48,"GS":1012.2,"AXP":366.58,"ORCL":251.85,"PG":163.3,"GOOG":428.54,"UNH":420.46,"NFLX":113.15,"DELL":487.26,"JNJ":258.59,"GEV":1222.63,"NVDA":301.62,"INTC":100.88,"RTX":215.73,"V":401.16,"IBM":294.57,"KO":86.18,"PM":194.86,"MRK":132.07,"SNDK":2035.05,"HD":370.34,"WMT":138.59,"MA":643.84,"CSCO":127.18,"TXN":298.0,"LLY":1222.62,"AMAT":578.91,"COST":1080.33,"CAT":962.49,"AVGO":523.73,"AAPL":315.57,"GE":372.05,"AMD":516.12,"LRCX":357.77,"PANW":318.32,"PLTR":183.12,"KLAC":225.79,"ABBV":254.38,"TSLA":424.56}

def num(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None

try:
    prior_data = {r["ticker"]: r for r in json.load(open(f"{STATE}/last-data.json"))}
except Exception:
    prior_data = {}

records, live = [], 0
CLOSES = {}  # ticker -> (daily closes, matching unix timestamps); feeds the 1Y chart series
RESYNC = {}  # ticker -> full 5y (closes, ts) when the split guard replaced stored history
def build_record(ticker, sym):
    b = fetch(ticker, sym)
    CLOSES[ticker] = (b["closes"][-260:], (b.get("closeTs") or [])[-260:])
    if b.get("resyncFull"):
        RESYNC[ticker] = (b["closes"], b.get("closeTs") or [])
    p, q, m, closes, meta = b["profile"], b["quote"], b["metric"], b["closes"], b["meta"]
    is_eu = sym != ticker or "." in ticker
    # Price comes from the Yahoo chart for every row (US and EU alike); the
    # Finnhub quote is only present when fetch() fell back for a US ticker.
    price = num(meta.get("regularMarketPrice")) or (closes[-1] if closes else None)
    if price is None and q:
        price = num(q.get("c")) or None
    if price == 0: price = None
    rate = fx(p.get("currency"))
    mcap = num(p.get("marketCapitalization"))
    mcap_usd = round(mcap * rate / 1000, 1) if (mcap and rate) else None
    # Fundamentals are fetched daily, but price moves hourly: market cap is
    # price × shares, so scale the daily figure by the price drift since the
    # daily fetch (refPrice, same listing units). Ratio clamp guards against
    # unit flips (e.g. pence/pounds) — outside it, keep the unscaled figure.
    ref = num(b.get("refPrice"))
    if mcap_usd and ref and price and 0.5 < price / ref < 2:
        mcap_usd = round(mcap_usd * price / ref, 1)
    d = {
        "ticker": ticker, "name": p.get("name"), "sector": p.get("finnhubIndustry"),
        "price": price,
        "marketCap": mcap_usd,
        "pe": num(m.get("peTTM")), "fpe": num(m.get("forwardPE")), "peg": num(m.get("pegTTM")),
        "ps": num(m.get("psTTM")), "pb": num(m.get("pb")) or num(m.get("pbQuarterly")),
        "evRev": num(m.get("evRevenueTTM")), "evEbitda": num(m.get("evEbitdaTTM")),
        "divYield": num(m.get("currentDividendYieldTTM")),
        "beta": num(m.get("beta")), "margin": num(m.get("netProfitMarginTTM")),
        "roe": num(m.get("roeTTM")), "roa": num(m.get("roaTTM")),
        "de": num(m.get("totalDebt/totalEquityQuarterly")) * 100 if num(m.get("totalDebt/totalEquityQuarterly")) is not None else None,
        "currency": meta.get("currency") or "USD",
        "dataSource": "Finnhub (live)" if (price and m) else "prior snapshot",
    }
    rps, so = num(m.get("revenuePerShareTTM")), num(p.get("shareOutstanding"))
    d["revenue"] = round(rps * so * rate / 1000, 2) if (rps and so and rate) else None
    if is_eu:
        d["adr"] = sym
        d["eps"] = round(price / d["pe"], 4) if (price and d["pe"] and d["pe"] > 0) else None
        d["hi52"] = round(max(closes[-252:]), 2) if closes else None
        d["lo52"] = round(min(closes[-252:]), 2) if closes else None
    else:
        d["eps"] = num(m.get("epsTTM"))
        d["hi52"], d["lo52"] = num(m.get("52WeekHigh")), num(m.get("52WeekLow"))
        if price:
            if d["hi52"] and (d["hi52"] > price * 50 or d["hi52"] < price / 50): d["hi52"] = None
            if d["lo52"] and (d["lo52"] > price * 50 or d["lo52"] < price / 50): d["lo52"] = None
            if d["eps"] and abs(d["eps"]) > price * 20:
                d["eps"] = round(price / d["pe"], 4) if (d["pe"] and d["pe"] > 0) else None
    # analyst consensus target: daily Yahoo fetch stored in analyst-state (native
    # trading units), falling back to the static US baseline dict. Ratio guards
    # catch Yahoo's occasional pounds-vs-pence flips on London names.
    tgt = None
    _t = (analyst.get(ticker) or {}).get("target")
    if isinstance(_t, dict) and _t.get("mean") and price:
        tgt = _t["mean"]
        ratio = tgt / price
        if ratio < 0.05: tgt *= 100
        elif ratio > 20: tgt /= 100
        if not (0.3 <= tgt / price <= 3): tgt = None
    d["target"] = round(tgt, 2) if tgt else TARGETS.get(ticker)
    d["upside"] = (d["target"] - price) / price * 100 if (d["target"] and price) else None
    # daily % change: latest-session move from the daily closes (Yahoo's 2y-range
    # meta carries no usable previousClose); Finnhub dp only on quote fallback
    d["dayChange"] = None
    dp = num(q and q.get("dp"))
    if dp is not None:
        d["dayChange"] = round(dp, 2)
    elif price and len(closes) >= 2:
        prev = closes[-2] if abs(price - closes[-1]) / price < 0.002 else closes[-1]
        if prev: d["dayChange"] = round((price - prev) / prev * 100, 2)
    # technicals
    sma50 = round(sum(closes[-50:]) / 50, 2) if len(closes) >= 50 else None
    sma200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else None
    rsi = None
    if len(closes) >= 15:
        diffs = [closes[i] - closes[i-1] for i in range(len(closes)-14, len(closes))]
        ag = sum(max(x, 0) for x in diffs) / 14
        al = sum(max(-x, 0) for x in diffs) / 14
        rsi = 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)
    a = analyst.get(ticker) or {}
    tb = {
        "sma50": round(1 + max(-0.25, min(0.25, (price - sma50) / sma50)), 3) if (price and sma50) else None,
        "sma200": round(1 + max(-0.30, min(0.30, (price - sma200) / sma200)), 3) if (price and sma200) else None,
        "cross": None, "rsi": round(1 + (50 - rsi) / 100, 3) if rsi is not None else None,
        "analyst": a.get("analystScore"),
    }
    cross = None
    if sma50 and sma200:
        cross = "golden" if sma50 > sma200 else "death"
        tb["cross"] = 1.15 if cross == "golden" else 0.85
    d["technicals"] = {"sma50": sma50, "sma200": sma200, "rsi14": rsi, "crossState": cross,
                       "analystRec": a.get("analystRec"), "scoreBreakdown": tb}
    e = a.get("earnings") or {"nextDate": None, "nextHour": None, "epsEstimate": None,
                              "revenueEstimate": None, "beatCount": None, "beatTotal": None}
    # Finnhub's earnings calendar reports foreign issuers in their *reporting*
    # currency (SK Hynix in KRW), while the page renders these fields as dollars.
    # When magnitudes are implausible against the listing price/market cap,
    # convert via the profile-currency FX rate already used for marketCap; values
    # still implausible after that (e.g. per-local-share EPS on a different US
    # share line) are dropped — a blank beats ₩71,109 rendered as $71,109.
    def _eps_bad(v):
        lim = abs(d["eps"]) * 2 + 1 if d["eps"] else (price or 0)
        return v is not None and lim and abs(v) > lim
    def _rev_bad(v):
        return v is not None and d["marketCap"] and v > d["marketCap"] * 1e9
    rc = e.get("recent") or {}
    if any(_eps_bad(x) for x in (e.get("epsEstimate"), rc.get("epsActual"), rc.get("epsEstimate"))) or \
       any(_rev_bad(x) for x in (e.get("revenueEstimate"), rc.get("revenueActual"), rc.get("revenueEstimate"))):
        e = json.loads(json.dumps(e))
        f = rate if (rate and rate != 1) else None
        for obj, keys in ((e, ("epsEstimate", "revenueEstimate")),
                          (e.get("recent") or {}, ("epsActual", "epsEstimate", "revenueActual", "revenueEstimate"))):
            for k in keys:
                v = obj.get(k)
                if v is None: continue
                if f: v *= f
                obj[k] = None if (_rev_bad(v) if k.startswith("revenue") else _eps_bad(v)) else round(v, 4)
    d["earnings"] = e
    return d

for ticker, sym in pairs:
    d = build_record(ticker, sym)
    if d["name"] is None or (d["pe"] is None and d["marketCap"] is None):
        # Finnhub failed for this ticker: retry once from scratch, then fall back to last good data.
        ckf = f"{OUT}/ck/{ticker.replace('/', '_')}.json"
        if os.path.exists(ckf): os.remove(ckf)
        time.sleep(5)
        d2 = build_record(ticker, sym)
        if d2["name"] is not None:
            d = d2
        elif ticker in prior_data:
            fresh_price, fresh_tech, fresh_hi, fresh_lo = d["price"], d["technicals"], d["hi52"], d["lo52"]
            d = dict(prior_data[ticker])
            d["dataSource"] = "prior snapshot"
            if fresh_price is not None: d["price"] = fresh_price
            if fresh_tech and fresh_tech.get("sma50") is not None:
                d["technicals"], d["hi52"], d["lo52"] = fresh_tech, fresh_hi or d.get("hi52"), fresh_lo or d.get("lo52")
    if d["dataSource"] == "Finnhub (live)": live += 1
    records.append(d)

# ---------- sector overrides ----------
# Payment networks are not banks: pull them out of Finnhub's "Financial Services"
# bucket. Applied after assembly so prior-snapshot fallback rows get it too.
SECTOR_OVERRIDE = {"V": "Payment Processors", "MA": "Payment Processors"}
for d in records:
    if d["ticker"] in SECTOR_OVERRIDE:
        d["sector"] = SECTOR_OVERRIDE[d["ticker"]]

# ---------- color groups ----------
MERGE = {"Financial Services": "Financial Services & Banking", "Banking": "Financial Services & Banking"}
# Groups kept even when too small for the top-8 cut (template must define their color)
FORCE_GROUPS = {"Payment Processors"}
from collections import Counter
groups = Counter(MERGE.get(d["sector"], d["sector"]) for d in records if d["sector"])
top8 = {g for g, _ in groups.most_common(8)}
for d in records:
    g = MERGE.get(d["sector"], d["sector"])
    d["colorGroup"] = g if (g in top8 or g in FORCE_GROUPS) else "Other"

# ---------- frozen benchmark anchors (shared by value + absolute scores) ----------
try:
    BENCH = json.load(open(f"{STATE}/benchmarks.json"))
except Exception:
    BENCH = None
    print("WARNING: benchmarks.json missing — anchor-based scoring degraded")

_QS = [0.1, 0.25, 0.5, 0.75, 0.9]
def _pos(v, anc):
    if v <= anc[0]: return 0.05
    if v >= anc[4]: return 0.95
    for i in range(4):
        if v <= anc[i + 1]:
            a, b = anc[i], anc[i + 1]
            return _QS[i] + (_QS[i + 1] - _QS[i]) * ((v - a) / (b - a) if b > a else 0)
    return 0.95

def _low(v, anc):  return round((1 - _pos(v, anc)) * 100, 1) if anc else None   # cheaper/lower = better
def _high(v, anc): return round(_pos(v, anc) * 100, 1) if anc else None        # higher = better

FIN_SECTORS = {"Banking", "Financial Services", "Insurance"}

# ---------- value scores ----------
# Sector-aware relative value (owner-chosen 2026-07-21): each metric's pool
# percentile is blended 50/50 with the stock's position in its SECTOR's frozen
# anchors (benchmarks.json), so structurally-cheap sectors (banks) must be
# cheap FOR THEIR SECTOR to rank. Financials (FIN_SECTORS) use the classic
# bank lens: P/B ranked against OTHER FINANCIALS only (owner: keep pb + roe),
# ROE joins their score (higher = better), and EV/EBITDA is unscored (it is
# undefined for banks — debt is their raw material). Non-financials' pb/ev
# pools exclude financials so the structural tilt doesn't distort anyone.
# Mirror in template.html recomputeDerived — change both together.
BENCH_KEY = {"pe": "pe", "peg": "peg", "evEbitda": "ev", "roe": "roe"}
for metric in ("pe", "peg", "pb", "evEbitda", "roe"):
    for d in records:
        d.setdefault("scoreBreakdown", {})
        is_fin = d.get("sector") in FIN_SECTORS
        if (metric == "evEbitda" and is_fin) or (metric == "roe" and not is_fin):
            d["scoreBreakdown"][metric] = None
            continue
        if metric in ("pb", "evEbitda", "roe"):
            pool = [x for x in records if (x.get("sector") in FIN_SECTORS) == is_fin]
        else:
            pool = records
        vals = sorted(x[metric] for x in pool if x[metric] and x[metric] > 0)
        n = len(vals)
        v = d[metric]
        if v and v > 0 and n:
            idx = vals.index(v)
            score = 50.0 if n == 1 else (100 * idx / (n - 1) if metric == "roe" else 100 * (n - 1 - idx) / (n - 1))
            anc = ((BENCH.get("bySector") or {}).get(d.get("sector")) or {}).get(BENCH_KEY[metric]) if (BENCH and metric in BENCH_KEY) else None
            if anc:
                anc_score = (_pos(v, anc) if metric == "roe" else 1 - _pos(v, anc)) * 100
                score = (score + anc_score) / 2
            d["scoreBreakdown"][metric] = round(score, 1)
        else:
            d["scoreBreakdown"][metric] = None
for d in records:
    avail = [v for v in d["scoreBreakdown"].values() if v is not None]
    d["valueScore"] = round(sum(avail) / len(avail), 1) if avail else None
ranked = sorted(d["valueScore"] for d in records if d["valueScore"] is not None)
third = len(ranked) / 3
for d in records:
    if d["valueScore"] is None: d["position"] = "Unclassified"; continue
    idx = ranked.index(d["valueScore"])
    d["position"] = "Overvalued" if idx < third else ("Fair Value" if idx < 2 * third else "Undervalued")

# ---------- indicator + combined ----------
for d in records:
    tb = d["technicals"]["scoreBreakdown"]
    comps = [v for v in tb.values() if v is not None]
    d["indicatorScore"] = round(sum(comps) / len(comps), 3) if comps else None
    d["combinedScore"] = round(d["valueScore"] * d["indicatorScore"], 2) if (d["valueScore"] is not None and d["indicatorScore"] is not None) else d["valueScore"]

# ---------- absolute score: empirical anchors, peer-independent ----------
# Graded against FROZEN top-500 US market distributions (benchmarks.json,
# rebuilt ~annually via scripts/build_benchmarks.py): valuation vs the whole
# market, quality vs the stock's own sector (market fallback), analyst view
# from targets + recommendation mix. FOUR synced pieces: this function, the
# JS mirror computeAbsolute (template.html), the /*__BENCH__*/ placeholder
# injection below, and the gate's explainer copy.
def compute_absolute(d):
    if not BENCH:
        d["absoluteScore"] = d["absLabel"] = d["absBreakdown"] = None
        return
    mkt = BENCH["market"]
    sec = (BENCH.get("bySector") or {}).get(d.get("sector")) or {}
    val, qual, an = [], [], []
    pe = d.get("pe")
    if pe is not None:
        val.append(5 if pe <= 0 else _low(pe, mkt.get("pe")))
    peg = d.get("peg")
    if peg is not None and peg > 0:
        val.append(_low(peg, mkt.get("peg")))
    ev = d.get("evEbitda")
    if ev is not None and ev > 0:
        val.append(_low(ev, mkt.get("ev")))
    margin = d.get("margin")
    if margin is not None:
        qual.append(5 if margin < 0 else _high(margin, sec.get("margin") or mkt.get("margin")))
    roe = d.get("roe")
    if roe is not None:
        qual.append(5 if roe < 0 else _high(roe, sec.get("roe") or mkt.get("roe")))
    de = d.get("de")
    if de is not None:
        anc = sec.get("de")
        if anc: qual.append(_low(de, anc))
        elif d.get("sector") not in FIN_SECTORS: qual.append(_low(de, mkt.get("de")))
        # financial sector without sector anchors: leverage unscored (market norms unfair)
    if d.get("upside") is not None:
        u = max(-30, min(30, d["upside"]))
        an.append(round((u + 30) / 60 * 100, 1))
    rec = (d.get("technicals") or {}).get("analystRec") or {}
    tot = sum(rec.get(k) or 0 for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    if tot >= 3:
        an.append(round(100 * ((rec.get("strongBuy") or 0) + (rec.get("buy") or 0)) / tot, 1))
    val = [v for v in val if v is not None]
    qual = [v for v in qual if v is not None]
    pillars = [(val, 0.4), (qual, 0.3), (an, 0.3)]
    parts = [(sum(p) / len(p), w) for p, w in pillars if p]
    if not parts:
        d["absoluteScore"] = d["absLabel"] = d["absBreakdown"] = None
        return
    score = sum(s * w for s, w in parts) / sum(w for _, w in parts)
    d["absoluteScore"] = round(score, 1)
    d["absLabel"] = ("Strong" if score >= 70 else "Solid" if score >= 55 else
                     "Mixed" if score >= 40 else "Stretched" if score >= 25 else "Weak")
    d["absBreakdown"] = {
        "valuation": round(sum(val) / len(val), 1) if val else None,
        "quality": round(sum(qual) / len(qual), 1) if qual else None,
        "analyst": round(sum(an) / len(an), 1) if an else None,
    }

for d in records:
    compute_absolute(d)

records.sort(key=lambda d: (d["combinedScore"] is None, -(d["combinedScore"] or 0)))

# ---------- movement vs previous refresh (top-movers section + table arrows) ----------
prior_scores = {t: num(r.get("combinedScore")) for t, r in prior_data.items()}
prior_ranked = sorted((t for t, s in prior_scores.items() if s is not None), key=lambda t: -prior_scores[t])
prior_rank = {t: i + 1 for i, t in enumerate(prior_ranked)}
for i, d in enumerate(records):
    pcs = prior_scores.get(d["ticker"])
    d["scoreDelta"] = round(d["combinedScore"] - pcs, 2) if (d["combinedScore"] is not None and pcs is not None) else None
    d["rankDelta"] = (prior_rank[d["ticker"]] - (i + 1)) if d["ticker"] in prior_rank else None

# ---------- watchlist ----------
# Watchlist eligibility: at ~380 names the top/bottom extremes are where data
# glitches live — require at least 2 scored valuation metrics AND 2 scored
# indicator components before a stock can make the buy/sell list.
def watch_eligible(d):
    b = d.get("scoreBreakdown") or {}
    tb = (d.get("technicals") or {}).get("scoreBreakdown") or {}
    return (sum(1 for v in b.values() if v is not None) >= 2
            and sum(1 for v in tb.values() if v is not None) >= 2)
scored = [d["ticker"] for d in records if d["combinedScore"] is not None and watch_eligible(d)]
buy, sell = scored[:3], scored[-3:][::-1]
pb_, ps_ = set(prior_watch.get("buy") or []), set(prior_watch.get("sell") or [])
entered = [t for t in buy if t not in pb_] + [t for t in sell if t not in ps_]
exited = [t for t in pb_ if t not in buy] + [t for t in ps_ if t not in sell]
changed = bool(entered or exited)
now = datetime.datetime.now(datetime.timezone.utc)
json.dump({"buy": buy, "sell": sell, "updatedAt": now.isoformat()}, open(f"{STATE}/watchlist-state.json", "w"), indent=1)

# ---------- persist last good data for next run's fallback ----------
merged_prior = dict(prior_data)
for d in records:
    if d["dataSource"] == "Finnhub (live)":
        merged_prior[d["ticker"]] = d
json.dump(list(merged_prior.values()), open(f"{STATE}/last-data.json", "w"), separators=(",", ":"))

# ---------- 30-day hourly price history (chart in the stock detail card) ----------
# Appends this run's live price per ticker and prunes to a rolling 30 days.
# Only live prices are appended — carried-forward prices would draw artificial
# flat lines. The page fetches this file lazily from the claude/state raw URL.
try:
    ph = json.load(open(f"{STATE}/price-history.json")).get("byTicker", {})
except Exception:
    ph = {}
now_s = int(now.timestamp())
cutoff = now_s - 30 * 24 * 3600
current = {d["ticker"] for d in records}
ph = {t: v for t, v in ph.items() if t in current}
for d in records:
    tkr = d["ticker"]
    h = ph.setdefault(tkr, {"t": [], "p": [], "s": []})
    if len(h.get("s") or []) != len(h["t"]):  # legacy entries predate the score series
        h["s"] = (h.get("s") or []) + [None] * (len(h["t"]) - len(h.get("s") or []))
    if d["price"] is not None and d["dataSource"] == "Finnhub (live)":
        if not h["t"] or now_s - h["t"][-1] > 900:  # ignore duplicate points from quick re-runs
            h["t"].append(now_s)
            h["p"].append(round(d["price"], 3))
            h["s"].append(d["combinedScore"])
    while h["t"] and h["t"][0] < cutoff:
        h["t"].pop(0); h["p"].pop(0); h["s"].pop(0)
    # (the 1Y daily series used to be duplicated here as h["d"] — the page now
    # reads the 1Y range from price-history-long.json, so it's gone)
    h.pop("d", None)
json.dump({"updatedAt": now.isoformat(), "byTicker": ph}, open(f"{STATE}/price-history.json", "w"), separators=(",", ":"))

# ---------- long-run daily history (5y prices + all-time daily score) ----------
# price-history-long.json (seeded by scripts/backfill_history.py):
#   {updatedAt, byTicker: {T: {t: [daynums asc], p: [closes 3dp, native ccy],
#                              st: [daynums], s: [combinedScore]}}}
# "t"/"p": union-merge this run's daily closes — append only daynums newer than the
# last stored one (today's provisional close may be refreshed in place), rolling
# 1830-day cap. "st"/"s": one combinedScore point per UTC day, NEVER pruned.
def update_long_history(e, ts, cl, score, today_dn, cap=1830):
    # Returns True only on durable changes (new daynum or new daily score point).
    # In-place refreshes of today's provisional close are NOT durable — skipping
    # the file write for those keeps the hourly git churn on this 1.2MB file
    # down to ~1-2 commits/day instead of one per run.
    changed = False
    if cl and ts and len(cl) == len(ts):
        last_dn = e["t"][-1] if e["t"] else -1
        for tt, cc in zip(ts, cl):
            dn = tt // 86400
            if dn > last_dn:
                e["t"].append(dn); e["p"].append(round(cc, 3)); last_dn = dn; changed = True
            elif e["t"] and dn == e["t"][-1]:
                e["p"][-1] = round(cc, 3)  # refresh today's in-progress close
        cut = e["t"][-1] - (cap - 1) if e["t"] else 0
        while e["t"] and e["t"][0] < cut:
            e["t"].pop(0); e["p"].pop(0)
    if score is not None and (not e["st"] or e["st"][-1] != today_dn):
        e["st"].append(today_dn); e["s"].append(score); changed = True
    return changed

os.makedirs(f"{STATE}/history", exist_ok=True)
today_dn = now_s // 86400
lh_written = 0
for d in records:
    e = lh_read(d["ticker"])
    cl, ts = CLOSES.get(d["ticker"], ([], []))
    dirty = False
    if d["ticker"] in RESYNC:
        # split guard refetched the full adjusted history — replace the stored
        # price series (the score series st/s is untouched)
        e["t"], e["p"] = [], []
        cl, ts = RESYNC[d["ticker"]]
        dirty = True
    dirty |= update_long_history(e, ts, cl, d["combinedScore"], today_dn)
    if dirty:
        json.dump(e, open(f"{STATE}/history/{d['ticker'].replace('/', '_')}.json", "w"), separators=(",", ":"))
        lh_written += 1

# ---------- build html ----------
# Page payload split (2026-07-22): the page embeds slim records (table/overview
# fields); detail-only structures ship in detail-data.json next to index.html,
# lazily fetched on first stock-card open. Contract with template.html
# fetchDetail()/mergeDetail — the field list must match what the card renders.
DETAIL_FIELDS = ("scoreBreakdown", "absBreakdown", "technicals", "dataSource", "adr")
detail_by = {}
slim_records = []
for d in records:
    det = {k: d[k] for k in DETAIL_FIELDS if k in d}
    e_full = d.get("earnings") or {}
    det["earnings"] = {k: e_full.get(k) for k in ("recent", "reports")}
    sd = {k: v for k, v in d.items() if k not in DETAIL_FIELDS}
    sd["earnings"] = {k: v for k, v in e_full.items() if k not in ("recent", "reports")}
    detail_by[d["ticker"]] = det
    slim_records.append(sd)
json.dump({"updatedAt": now.isoformat(), "byTicker": detail_by},
          open(f"{OUT}/detail-data.json", "w"), separators=(",", ":"), ensure_ascii=False)
data_json = json.dumps(slim_records, separators=(",", ":"), ensure_ascii=False)
pool_sectors = {d.get("sector") for d in records}
bench_slim = json.dumps({"market": BENCH["market"],
                         "bySector": {s: v for s, v in (BENCH.get("bySector") or {}).items() if s in pool_sectors}},
                        separators=(",", ":")) if BENCH else "null"
final = (template.replace("/*__DATA__*/", data_json)
         .replace("/*__BENCH__*/null", bench_slim)
         .replace("/*__BUILT__*/null", str(int(now.timestamp()))))
open(f"{OUT}/dashboard_final.html", "w").write(final)
js = "\n".join(re.findall(r"<script>(.*?)</script>", final, re.S))
open(f"{OUT}/check.js", "w").write(js)
subprocess.run(["node", "--check", f"{OUT}/check.js"], check=True)
i = final.index("</style>") + len("</style>")
wrapped = ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
           '<title>ValueTally</title>\n'
           '<link rel="manifest" href="manifest.json">\n'
           '<meta name="theme-color" content="#0a101d">\n'
           '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
           '<meta name="apple-mobile-web-app-capable" content="yes">\n'
           '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
           + final[:i] + "\n</head>\n<body>\n" + final[i:] + "\n</body>\n</html>\n")
open(f"{OUT}/index.html", "w").write(wrapped)

print(f"OK live={live}/{len(records)} changed={changed} buy={buy} sell={sell} historyShardsWritten={lh_written} finnhubFallbacks={FB_STATE['n']}")
body = f"Refreshed — {live}/{len(records)} tickers live."
if changed:
    body += f" Buy watch: {', '.join(buy)}. Sell watch: {', '.join(sell)}."
    if entered: body += f" New this run: {', '.join(entered)}."
    body += " Ranked by value × technical/sentiment score — not investment advice."
tomorrow = (now + datetime.timedelta(days=1)).date().isoformat()
for d in records:
    e = d.get("earnings") or {}
    if e.get("nextDate") == tomorrow:
        when = "before open" if e.get("nextHour") == "bmo" else ("after close" if e.get("nextHour") == "amc" else "time TBD")
        line = f" {d['ticker']} reports earnings tomorrow ({when})"
        if e.get("epsEstimate") is not None: line += f": consensus EPS est ${round(e['epsEstimate'], 2)}"
        if e.get("beatCount") is not None and e.get("beatTotal"): line += f", beat estimates in {e['beatCount']}/{e['beatTotal']} of last quarters"
        body += line + "."
open(f"{OUT}/notify.txt", "w").write(body)
if NOTIFY:
    req = urllib.request.Request(NTFY, data=body.encode(), headers={
        "Title": "ValueTally refreshed", "Tags": "bar_chart", "Click": PAGES_URL})
    urllib.request.urlopen(req, timeout=15)

# ---------- stock-request inbox: poll the public ntfy topic into a durable log ----------
# ntfy only caches messages ~12h; this hourly poll makes requests permanent and
# counts duplicates. Contract with the page's request form (template.html):
# titles are "Stock request: <TICKER> (#N)"; ticker "TEST" is reserved for
# end-to-end checks and never logged.
REQ_TOPIC = "harris-stockdash-req-a2962152"
try:
    try:
        reqlog = json.load(open(f"{STATE}/requests-log.json"))
    except Exception:
        reqlog = {"lastPollAt": 0, "byTicker": {}}
    since = int(reqlog.get("lastPollAt") or 0)
    with urllib.request.urlopen(
            f"https://ntfy.sh/{REQ_TOPIC}/json?poll=1&since={since if since else 'all'}", timeout=20) as r:
        lines = r.read().decode().splitlines()
    newest = since
    for ln in lines:
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if ev.get("event") != "message":
            continue
        t = ev.get("time") or 0
        if t <= since:
            continue
        if t > newest:
            newest = t
        title = ev.get("title") or ""
        if not title.startswith("Stock request"):
            continue
        # single: "Stock request: TICK (#N)"; batch: "Stock requests (k): T1 (#n1), T2 (#n2)"
        for tk in re.findall(r"([A-Z0-9.\-]{1,12}) \(#\d+\)", title):
            if tk == "TEST":
                continue
            e = reqlog["byTicker"].setdefault(tk, {"count": 0, "firstAt": t})
            e["count"] += 1
            e["lastAt"] = t
    reqlog["lastPollAt"] = newest
    json.dump(reqlog, open(f"{STATE}/requests-log.json", "w"), separators=(",", ":"))
except Exception as e:
    print("request poll skipped:", e)
