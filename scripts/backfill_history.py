#!/usr/bin/env python3
"""5-year daily price backfill for the sharded long history (state/history/{T}.json).

Seeds/deepens the per-ticker files consumed/maintained by scripts/refresh.py:
  {"updatedAt": iso, "byTicker": {TICKER: {"t": [unix_ts//86400 daynums, ascending],
                                           "p": [daily closes, rounded 3dp],
                                           "st": [], "s": []}}}
"t"/"p" are the daily close series (native listing currency — London in pence),
capped to the most recent 1830 days. "st"/"s" are the daily combinedScore series;
they start empty and accumulate one point per UTC day via refresh.py (never pruned).

Fetches Yahoo chart range=5y interval=1d per ticker: US tickers as-is, European
tickers via the NATIVE listing symbol (same symbol the dashboard charts use).
Stdlib only. Env: STATE_DIR (checkout of claude/state; reads universe.json,
writes history/{T}.json shards). MIGRATE=1 splits a legacy
price-history-long.json into shards (one-time).
"""
import datetime, json, os, time, urllib.request

STATE = os.environ.get("STATE_DIR", "statebranch")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CAP_DAYS = 1830  # rolling window on "t"/"p" (calendar daynums), ~5 years

def get(url, headers=None, retries=1):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries:
                return None
            time.sleep(2)

def fetch_daily(ticker):
    """Return ({daynums}, {closes}) ascending with nulls dropped, or None on failure."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.request.quote(ticker)}?range=5y&interval=1d")
    chart = get(url, UA)
    try:
        res = chart["chart"]["result"][0]
        raw_t = res.get("timestamp") or []
        raw_c = res["indicators"]["quote"][0]["close"]
    except Exception:
        return None
    if len(raw_t) != len(raw_c):
        return None
    t, p = [], []
    for tt, cc in zip(raw_t, raw_c):
        if cc is None:
            continue
        dn = tt // 86400
        if t and dn <= t[-1]:  # collapse any duplicate/backward daynum, keep latest
            if dn == t[-1]:
                p[-1] = round(cc, 3)
            continue
        t.append(dn)
        p.append(round(cc, 3))
    if not t:
        return None
    cut = t[-1] - (CAP_DAYS - 1)
    while t and t[0] < cut:
        t.pop(0); p.pop(0)
    return t, p

def shard_path(t):
    return f"{STATE}/history/{t.replace('/', '_')}.json"

def shard_read(t):
    try:
        return json.load(open(shard_path(t)))
    except Exception:
        return {"t": [], "p": [], "st": [], "s": []}

def shard_write(t, e):
    os.makedirs(f"{STATE}/history", exist_ok=True)
    json.dump(e, open(shard_path(t), "w"), separators=(",", ":"))

def migrate():
    """One-time: split a legacy price-history-long.json into per-ticker shards."""
    data = json.load(open(f"{STATE}/price-history-long.json"))
    for t, e in data.get("byTicker", {}).items():
        shard_write(t, e)
    os.remove(f"{STATE}/price-history-long.json")
    print(f"migrated {len(data.get('byTicker', {}))} tickers to history/ shards")

def deepen():
    """Merge mode (DEEPEN=1): fetch 5y history ONLY for tickers whose stored
    series is missing or shallow (< SHALLOW_DAYS points — e.g. new universe
    entrants seeded from refresh.py's 2y window), replacing their "t"/"p"
    while PRESERVING "st"/"s" and any stored closes newer than the fetch.
    Safe to run repeatedly; no-op when nobody is shallow. Run by the weekly
    universe workflow so new entrants get full chart depth automatically."""
    SHALLOW_DAYS = 1000
    universe = json.load(open(f"{STATE}/universe.json"))
    tickers = list(universe["us"]) + [e["ticker"] for e in universe["europe"]]
    targets = [t for t in tickers if len(shard_read(t).get("t") or []) < SHALLOW_DAYS]
    wrote = 0
    for tkr in targets:
        tp = fetch_daily(tkr)
        time.sleep(0.5)
        if tp is None:
            print(f"WARNING: deepen failed for {tkr} — kept as-is")
            continue
        t, p = tp
        old = shard_read(tkr)
        for dn, cc in zip(old["t"], old["p"]):  # keep closes newer than the fetch
            if dn > t[-1]:
                t.append(dn); p.append(cc)
        if len(t) <= len(old["t"]):
            print(f"{tkr}: fetch no deeper than stored ({len(old['t'])} points) — kept")
            continue
        shard_write(tkr, {"t": t, "p": p, "st": old.get("st") or [], "s": old.get("s") or []})
        wrote += 1
        print(f"{tkr}: deepened {len(old['t'])} -> {len(t)} points")
    print(f"deepen: {len(targets)} shallow of {len(tickers)}; wrote={wrote}")

def main():
    universe = json.load(open(f"{STATE}/universe.json"))
    tickers = list(universe["us"]) + [e["ticker"] for e in universe["europe"]]
    wrote, failed = 0, []
    for tkr in tickers:
        tp = fetch_daily(tkr)
        time.sleep(0.5)
        if tp is None:  # retry once from scratch
            time.sleep(2)
            tp = fetch_daily(tkr)
            time.sleep(0.5)
        if tp is None:
            print(f"WARNING: backfill failed for {tkr} — skipped")
            failed.append(tkr)
            continue
        t, p = tp
        prior = shard_read(tkr)
        shard_write(tkr, {"t": t, "p": p, "st": prior.get("st") or [], "s": prior.get("s") or []})
        wrote += 1
        print(f"{tkr}: {len(t)} points, last close {p[-1]}")
    print(f"Wrote {wrote}/{len(tickers)} tickers"
          + (f"; failed: {', '.join(failed)}" if failed else ""))

if __name__ == "__main__":
    migrate() if os.environ.get("MIGRATE") == "1" else deepen() if os.environ.get("DEEPEN") == "1" else main()
