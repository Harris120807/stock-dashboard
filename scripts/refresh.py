#!/usr/bin/env python3
"""Hourly dashboard refresh for Harris120807/stock-dashboard.

Reads pipeline state (universe/analyst/watchlist/template) from a checkout of the
claude/state branch, fetches live data for all 80 tickers, recomputes scores,
builds index.html for the claude/pages branch, and updates watchlist state.

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
PAGES_URL = "https://harris120807.github.io/stock-dashboard/"
os.makedirs(f"{OUT}/ck", exist_ok=True)

def get(url, headers=None, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries: return None
            time.sleep(1 + i)

# ---------- state ----------
universe = json.load(open(f"{STATE}/universe.json"))
analyst = json.load(open(f"{STATE}/analyst-state.json"))["byTicker"]
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

def fetch(ticker, sym):
    ckf = f"{OUT}/ck/{ticker.replace('/','_')}.json"
    if os.path.exists(ckf): return json.load(open(ckf))
    is_eu = sym != ticker or "." in ticker
    b = {}
    b["profile"] = get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={KEY}") or {}; time.sleep(0.25)
    b["quote"] = None
    if not is_eu:
        b["quote"] = get(f"https://finnhub.io/api/v1/quote?symbol={sym}&token={KEY}"); time.sleep(0.25)
    b["metric"] = (get(f"https://finnhub.io/api/v1/stock/metric?symbol={sym}&metric=all&token={KEY}") or {}).get("metric", {}); time.sleep(0.25)
    chart = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(ticker)}?range=2y&interval=1d", UA); time.sleep(0.2)
    closes, meta = [], {}
    try:
        res = chart["chart"]["result"][0]
        closes = [x for x in res["indicators"]["quote"][0]["close"] if x is not None]
        meta = res.get("meta", {})
    except Exception:
        pass
    b["closes"], b["meta"] = closes, meta
    json.dump(b, open(ckf, "w"))
    return b

TARGETS = {"C":155.15,"BAC":65.45,"WFC":97.93,"MU":1486.0,"BRK-B":520.33,"JPM":352.76,"XOM":167.38,"META":827.91,"AMZN":312.91,"MSFT":559.93,"CVX":217.14,"MS":216.48,"GS":1012.2,"AXP":366.58,"ORCL":251.85,"PG":163.3,"GOOG":428.54,"UNH":420.46,"NFLX":113.15,"DELL":487.26,"JNJ":258.59,"GEV":1222.63,"NVDA":301.62,"INTC":100.88,"RTX":215.73,"V":401.16,"IBM":294.57,"KO":86.18,"PM":194.86,"MRK":132.07,"SNDK":2035.05,"HD":370.34,"WMT":138.59,"MA":643.84,"CSCO":127.18,"TXN":298.0,"LLY":1222.62,"AMAT":578.91,"COST":1080.33,"CAT":962.49,"AVGO":523.73,"AAPL":315.57,"GE":372.05,"AMD":516.12,"LRCX":357.77,"PANW":318.32,"PLTR":183.12,"KLAC":225.79,"ABBV":254.38,"TSLA":424.56}

def num(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None

records, live = [], 0
for ticker, sym in pairs:
    b = fetch(ticker, sym)
    p, q, m, closes, meta = b["profile"], b["quote"], b["metric"], b["closes"], b["meta"]
    is_eu = sym != ticker or "." in ticker
    price = None
    if is_eu:
        price = num(meta.get("regularMarketPrice")) or (closes[-1] if closes else None)
    else:
        price = num(q and q.get("c")) or None
        if price == 0: price = None
    rate = fx(p.get("currency"))
    mcap = num(p.get("marketCapitalization"))
    d = {
        "ticker": ticker, "name": p.get("name"), "sector": p.get("finnhubIndustry"),
        "price": price,
        "marketCap": round(mcap * rate / 1000, 1) if (mcap and rate) else None,
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
    d["target"] = TARGETS.get(ticker)
    d["upside"] = (d["target"] - price) / price * 100 if (d["target"] and price) else None
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
    d["earnings"] = a.get("earnings") or {"nextDate": None, "nextHour": None, "epsEstimate": None,
                                          "revenueEstimate": None, "beatCount": None, "beatTotal": None}
    if d["dataSource"] == "Finnhub (live)": live += 1
    records.append(d)

# ---------- color groups ----------
MERGE = {"Financial Services": "Financial Services & Banking", "Banking": "Financial Services & Banking"}
from collections import Counter
groups = Counter(MERGE.get(d["sector"], d["sector"]) for d in records if d["sector"])
top8 = {g for g, _ in groups.most_common(8)}
for d in records:
    g = MERGE.get(d["sector"], d["sector"])
    d["colorGroup"] = g if g in top8 else "Other"

# ---------- value scores ----------
for metric in ("pe", "peg", "pb", "evEbitda"):
    vals = sorted(d[metric] for d in records if d[metric] and d[metric] > 0)
    n = len(vals)
    for d in records:
        d.setdefault("scoreBreakdown", {})
        v = d[metric]
        if v and v > 0 and n:
            d["scoreBreakdown"][metric] = 50.0 if n == 1 else round(100 * (n - 1 - vals.index(v)) / (n - 1), 1)
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

records.sort(key=lambda d: (d["combinedScore"] is None, -(d["combinedScore"] or 0)))

# ---------- watchlist ----------
scored = [d["ticker"] for d in records if d["combinedScore"] is not None]
buy, sell = scored[:3], scored[-3:][::-1]
pb_, ps_ = set(prior_watch.get("buy") or []), set(prior_watch.get("sell") or [])
entered = [t for t in buy if t not in pb_] + [t for t in sell if t not in ps_]
exited = [t for t in pb_ if t not in buy] + [t for t in ps_ if t not in sell]
changed = bool(entered or exited)
now = datetime.datetime.now(datetime.timezone.utc)
json.dump({"buy": buy, "sell": sell, "updatedAt": now.isoformat()}, open(f"{STATE}/watchlist-state.json", "w"), indent=1)

# ---------- build html ----------
data_json = json.dumps(records, separators=(",", ":"), ensure_ascii=False)
final = template.replace("/*__DATA__*/", data_json)
open(f"{OUT}/dashboard_final.html", "w").write(final)
js = "\n".join(re.findall(r"<script>(.*?)</script>", final, re.S))
open(f"{OUT}/check.js", "w").write(js)
subprocess.run(["node", "--check", f"{OUT}/check.js"], check=True)
i = final.index("</style>") + len("</style>")
wrapped = ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
           + final[:i] + "\n</head>\n<body>\n" + final[i:] + "\n</body>\n</html>\n")
open(f"{OUT}/index.html", "w").write(wrapped)

print(f"OK live={live}/{len(records)} changed={changed} buy={buy} sell={sell}")
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
        "Title": "Stock Dashboard Refreshed", "Tags": "bar_chart", "Click": PAGES_URL})
    urllib.request.urlopen(req, timeout=15)
