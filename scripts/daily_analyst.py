#!/usr/bin/env python3
"""Daily analyst/earnings prefetch for the stock dashboard.

Reads universe.json from STATE_DIR (a checkout of claude/state), fetches analyst
recommendations + earnings calendar + beat/miss history from Finnhub for all 80
tickers (European rows fetched via their ADR symbol, keyed by native ticker),
and writes analyst-state.json back into STATE_DIR. The caller commits/pushes.

Env: FINNHUB_API_KEY (required), STATE_DIR (default "state"), OUT_DIR (default "out").
"""
import datetime, json, os, time, urllib.request

KEY = os.environ["FINNHUB_API_KEY"]
STATE = os.environ.get("STATE_DIR", "state")
OUT = os.environ.get("OUT_DIR", "out")
os.makedirs(f"{OUT}/ck", exist_ok=True)

def get(url, retries=2):
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries: return None
            time.sleep(1 + i)

universe = json.load(open(f"{STATE}/universe.json"))
pairs = [(t, t) for t in universe["us"]] + [(e["ticker"], e["adr"]) for e in universe["europe"]]

today = datetime.date.today()
frm, to = today.isoformat(), (today + datetime.timedelta(days=30)).isoformat()

def fetch(sym):
    ck = f"{OUT}/ck/{sym.replace('/', '_')}.json"
    if os.path.exists(ck): return json.load(open(ck))
    rec = get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={sym}&token={KEY}"); time.sleep(0.3)
    cal = get(f"https://finnhub.io/api/v1/calendar/earnings?from={frm}&to={to}&symbol={sym}&token={KEY}"); time.sleep(0.3)
    hist = get(f"https://finnhub.io/api/v1/stock/earnings?symbol={sym}&token={KEY}"); time.sleep(0.3)
    b = {"rec": rec, "cal": cal, "hist": hist}
    json.dump(b, open(ck, "w"))
    return b

by = {}
def build(ticker, sym):
    b = fetch(sym)
    rec0 = (b["rec"] or [None])[0] if isinstance(b["rec"], list) and b["rec"] else None
    a_score = a_rec = None
    if rec0:
        tot = sum(rec0.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        if tot > 0:
            nb = (rec0.get("strongBuy", 0)*2 + rec0.get("buy", 0) - rec0.get("sell", 0) - rec0.get("strongSell", 0)*2) / (tot*2)
            a_score = round(1 + nb*0.3, 3)
        a_rec = {k: rec0.get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell", "period")}
    e0 = None
    try: e0 = (b["cal"] or {}).get("earningsCalendar", [None])[0]
    except Exception: pass
    q4 = [x for x in (b["hist"] or [])[:4] if x and x.get("surprisePercent") is not None]
    return {"analystScore": a_score, "analystRec": a_rec,
                  "earnings": {"nextDate": e0.get("date") if e0 else None,
                               "nextHour": e0.get("hour") if e0 else None,
                               "epsEstimate": e0.get("epsEstimate") if e0 else None,
                               "revenueEstimate": e0.get("revenueEstimate") if e0 else None,
                               "beatCount": sum(1 for x in q4 if x["surprisePercent"] > 0) if q4 else None,
                               "beatTotal": len(q4) if q4 else None}}

for ticker, sym in pairs:
    by[ticker] = build(ticker, sym)

# One retry pass for transient Finnhub blips: refetch tickers that came back scoreless.
for ticker, sym in pairs:
    if by[ticker]["analystScore"] is None:
        ck = f"{OUT}/ck/{sym.replace('/', '_')}.json"
        if os.path.exists(ck): os.remove(ck)
        by[ticker] = build(ticker, sym)

state = {"updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "byTicker": by}
json.dump(state, open(f"{STATE}/analyst-state.json", "w"))
missing = [t for t, v in by.items() if v["analystScore"] is None]
print(f"OK {len(by)} tickers; missing analyst scores: {missing or 'none'}")
