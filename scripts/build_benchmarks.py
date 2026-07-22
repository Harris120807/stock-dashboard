#!/usr/bin/env python3
"""One-off/annual benchmark builder for the absolute score.

Screens the top 500 US stocks (Yahoo), fetches Finnhub metrics + sector for
each, and writes benchmarks.json to STATE_DIR: market-wide and per-sector
percentile anchors for the absolute score's ladders. The file is a FROZEN
reference — refresh deliberately (roughly annually), never automatically,
so the score stays stable.

Env: FINNHUB_API_KEY (required), STATE_DIR (default "state").
"""
import http.cookiejar, json, os, time, urllib.parse, urllib.request, datetime

KEY = os.environ["FINNHUB_API_KEY"]
STATE = os.environ.get("STATE_DIR", "state")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
PACE = float(os.environ.get("FINNHUB_PACE", "1.1"))

def screen500():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    try: op.open("https://fc.yahoo.com", timeout=15)
    except Exception: pass
    crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
    syms = []
    for offset in (0, 250):
        body = json.dumps({"size": 250, "offset": offset, "sortField": "intradaymarketcap", "sortType": "DESC",
                           "quoteType": "EQUITY", "query": {"operator": "AND", "operands": [{"operator": "EQ", "operands": ["region", "us"]}]}}).encode()
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v1/finance/screener?formatted=true&lang=en-US&region=US&crumb=" + urllib.parse.quote(crumb),
            data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
        resp = json.loads(op.open(req, timeout=30).read().decode())
        syms += [q.get("symbol") for q in resp["finance"]["result"][0]["quotes"] if q.get("symbol")]
        time.sleep(1)
    return list(dict.fromkeys(syms))

def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2 ** (i + 1))
    return None

def num(v):
    return v if isinstance(v, (int, float)) else None

rows = []
syms = screen500()
print(f"screen: {len(syms)} symbols")
for i, s in enumerate(syms):
    p = get(f"https://finnhub.io/api/v1/stock/profile2?symbol={s}&token={KEY}") or {}
    time.sleep(PACE)
    m = (get(f"https://finnhub.io/api/v1/stock/metric?symbol={s}&metric=all&token={KEY}") or {}).get("metric", {})
    time.sleep(PACE)
    rows.append({"sector": p.get("finnhubIndustry"),
                 "pe": num(m.get("peTTM")), "peg": num(m.get("pegTTM")), "ev": num(m.get("evEbitdaTTM")),
                 "margin": num(m.get("netProfitMarginTTM")), "roe": num(m.get("roeTTM")),
                 "de": num(m.get("totalDebt/totalEquityQuarterly"))})
    if (i + 1) % 100 == 0: print(f"  {i+1}/{len(syms)}")

def pct(vals, ps):
    vals = sorted(v for v in vals if v is not None)
    if len(vals) < 8: return None
    out = []
    for q in ps:
        k = (len(vals) - 1) * q
        f, c = int(k), min(int(k) + 1, len(vals) - 1)
        out.append(round(vals[f] + (vals[c] - vals[f]) * (k - f), 3))
    return out

PS = [0.1, 0.25, 0.5, 0.75, 0.9]
def dist(rws):
    return {"pe": pct([r["pe"] for r in rws if r["pe"] and r["pe"] > 0], PS),
            "peg": pct([r["peg"] for r in rws if r["peg"] and r["peg"] > 0], PS),
            "ev": pct([r["ev"] for r in rws if r["ev"] and r["ev"] > 0], PS),
            "margin": pct([r["margin"] for r in rws], PS),
            "roe": pct([r["roe"] for r in rws], PS),
            "de": pct([r["de"] * 100 for r in rws if r["de"] is not None], PS)}

sectors = {}
for r in rows:
    if r["sector"]: sectors.setdefault(r["sector"], []).append(r)
out = {"builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "source": "S&P-scale top-500 US cross-section via Finnhub metrics (frozen reference; rebuild ~annually with scripts/build_benchmarks.py)",
       "percentiles": PS,
       "market": dist(rows),
       "bySector": {s: dist(rs) for s, rs in sectors.items() if len(rs) >= 8}}
json.dump(out, open(f"{STATE}/benchmarks.json", "w"), indent=1)
print(f"benchmarks.json written: market + {len(out['bySector'])} sectors from {len(rows)} stocks")
