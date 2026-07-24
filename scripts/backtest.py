"""Quintile backtest of the combined score against forward returns.

Owner-facing sanity check (admin console → Backtest tab), NOT research-grade:
  - Fundamentals are reconstructed by scaling TODAY's ratios with the price
    path (pe_m = pe_now * p_m / p_now etc.) — earnings changes over the window
    are invisible, so old scores are approximations.
  - Universe = today's members (survivorship bias: today's winners were
    always "in").
  - Equal weight, monthly rebalance, no costs. Returns ARE dividend-adjusted
    (Yahoo adjclose) since 2026-07-25; the stored-shard fallback series is raw
    close, so a handful of tickers may miss dividends on a Yahoo hiccup.

Method: at each month-end over the last ~4 years, compute an approximate
combined score per stock (value percentile x technical component, mirroring
refresh.py's shape but market-pool only), bucket into quintiles (Q5 =
highest score), and record forward equal-weight returns at 21/63/126/252
trading days. Output: {STATE}/backtest.json.

Env: STATE_DIR (claude/state checkout with history/ shards + last-data.json).
"""

import datetime
import json
import os
import statistics
import time
import urllib.request

STATE = os.environ.get("STATE_DIR", "state")
HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
METRICS = ("pe", "pb", "ps", "evEbitda")  # lower = cheaper; price-scaled
SAMPLE_YEARS = 5           # how far back the month-samples go
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

records = json.load(open(f"{STATE}/last-data.json"))


def fetch_deep(ticker):
    """~10y of daily closes straight from Yahoo. The stored shards cap at 5y
    (right for the site), but 5 years of SAMPLES need a 6th year of forward
    returns — so this dispatch-only job fetches its own deeper series.
    Uses ADJUSTED closes (dividends + splits) when Yahoo provides them, so
    forward returns are total returns; raw close is the fallback."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.request.quote(ticker)}?range=10y&interval=1d")
    try:
        req = urllib.request.Request(url, headers=UA)
        res = json.load(urllib.request.urlopen(req, timeout=25))["chart"]["result"][0]
        ts = res.get("timestamp") or []
        ind = res["indicators"]
        adj = ((ind.get("adjclose") or [{}])[0]).get("adjclose")
        cl = ind["quote"][0]["close"]
        if adj and len(adj) == len(cl):
            cl = adj
        t, p = [], []
        for tt, cc in zip(ts, cl):
            if cc is not None:
                t.append(tt // 86400)
                p.append(cc)
        return {"t": t, "p": p} if len(t) > 300 else None
    except Exception:
        return None


shards = {}
for d in records:
    t = d["ticker"]
    h = fetch_deep(t)
    time.sleep(0.25)
    if h is None:
        # Yahoo hiccup — fall back to the stored 5y shard so the ticker still
        # contributes to the recent samples rather than vanishing entirely
        try:
            h = json.load(open(f"{STATE}/history/{t.replace('/', '_')}.json"))
            if not (h.get("t") and len(h["t"]) > 300):
                h = None
        except Exception:
            h = None
    if h:
        shards[t] = h

# month-end sample points on the calendar of the longest series, skipping the
# final year so every sample has a full 12m forward window
all_dn = sorted({dn for h in shards.values() for dn in h["t"]})
month_ends, seen = [], set()
for dn in all_dn:
    ym = datetime.date.fromtimestamp(dn * 86400).strftime("%Y-%m")
    if ym not in seen:
        seen.add(ym)
        month_ends.append(dn)  # first trading day of each month ~ month boundary
month_ends = month_ends[1:]
last_dn = all_dn[-1]
# window: SAMPLE_YEARS of monthly samples, each with a full 12m forward window
samples = [dn for dn in month_ends
           if last_dn - (SAMPLE_YEARS * 365 + 370) <= dn <= last_dn - 370]

def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None

def rsi14(closes):
    if len(closes) < 15:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-15:-1], closes[-14:]):
        ch = b - a
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)

results = {h: {q: [] for q in range(1, 6)} for h in HORIZONS}
bench = {h: [] for h in HORIZONS}
n_samples = 0

for dn in samples:
    rows = []
    for d in records:
        t = d["ticker"]
        h = shards.get(t)
        if not h:
            continue
        # position of the sample date in this ticker's series
        idx = None
        for i in range(len(h["t"]) - 1, -1, -1):
            if h["t"][i] <= dn:
                idx = i
                break
        if idx is None or idx < 60:
            continue
        closes = h["p"][: idx + 1]
        p_m, p_now = closes[-1], h["p"][-1]
        if not p_m or not p_now:
            continue
        # forward returns
        fwd = {}
        for name, td in HORIZONS.items():
            j = idx + td
            if j < len(h["p"]) and h["p"][j]:
                fwd[name] = h["p"][j] / p_m - 1
        if "12m" not in fwd:
            continue
        # reconstructed value multiples (today's ratio scaled by price path)
        vals = {}
        for m in METRICS:
            v = d.get(m)
            if isinstance(v, (int, float)) and v > 0:
                vals[m] = v * p_m / p_now
        if len(vals) < 2:
            continue
        # technicals from closes only
        s50, s200 = sma(closes, 50), sma(closes, 200)
        r = rsi14(closes)
        tech = []
        if s50:
            tech.append(1.06 if p_m > s50 else 0.94)
        if s200:
            tech.append(1.08 if p_m > s200 else 0.92)
        if s50 and s200:
            tech.append(1.05 if s50 > s200 else 0.95)
        if r is not None:
            tech.append(1.05 if r < 30 else 0.95 if r > 70 else 1.0)
        indicator = sum(tech) / len(tech) if tech else 1.0
        rows.append({"t": t, "vals": vals, "ind": indicator, "fwd": fwd})
    if len(rows) < 60:
        continue
    n_samples += 1
    # market-pool percentile per metric (higher = cheaper), mean -> valueScore
    for m in METRICS:
        pool = sorted(r["vals"][m] for r in rows if m in r["vals"])
        if len(pool) < 20:
            continue
        for r in rows:
            if m in r["vals"]:
                rank = sum(1 for x in pool if x < r["vals"][m]) / len(pool)
                r.setdefault("scores", []).append(1 - rank)
    scored = [r for r in rows if r.get("scores")]
    for r in scored:
        r["combined"] = (sum(r["scores"]) / len(r["scores"])) * r["ind"]
    scored.sort(key=lambda r: r["combined"])
    n = len(scored)
    for i, r in enumerate(scored):
        q = min(5, 1 + i * 5 // n)  # 1 = lowest scores, 5 = highest
        for hz, ret in r["fwd"].items():
            results[hz][q].append(ret)
            bench[hz].append(ret)

out = {
    "runAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "samples": n_samples,
    "tickersUsed": len(shards),
    "horizons": {
        hz: {
            "quintiles": {str(q): round(100 * statistics.mean(v), 2) if v else None
                           for q, v in results[hz].items()},
            "benchmark": round(100 * statistics.mean(bench[hz]), 2) if bench[hz] else None,
            "n": len(bench[hz]),
        }
        for hz in HORIZONS
    },
    "caveats": "5-year sample window (10y dividend-adjusted prices fetched at run time — returns are total returns). Reconstructed fundamentals (today's ratios price-scaled), survivorship bias (today's universe), equal weight, no costs. Rough sanity check only.",
}
json.dump(out, open(f"{STATE}/backtest.json", "w"), indent=1)
print(f"backtest: {n_samples} month-samples, {len(shards)} tickers; 12m Q5={out['horizons']['12m']['quintiles']['5']}% Q1={out['horizons']['12m']['quintiles']['1']}% bench={out['horizons']['12m']['benchmark']}%")
