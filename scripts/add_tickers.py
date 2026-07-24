"""Owner-initiated universe additions (admin dashboard "add tickers" form).

Reads TICKERS (env) — a comma/whitespace-separated list where each entry is
either a US-listed symbol ("TSM") or a European native:ADR pair ("HSBA.L:HSBC"
— native listing for Yahoo prices, US ADR for Finnhub fundamentals, matching
the universe's hybrid listing design). Validates each entry, appends accepted
ones to the append-only pool in {STATE}/universe.json (`us` / `europe` — NOT
the core lists, which belong to the weekly screen), and writes:

  {OUT}/notify.txt   — human summary for the ntfy step ("" when nothing to say)
  {OUT}/result.json  — {added: [...], rejected: [{entry, reason}]}

History backfill is NOT done here — the workflow runs backfill_history.py
DEEPEN=1 right after, which picks up any pool ticker with a shallow/missing
shard (i.e. exactly the ones just added).

Validation per entry:
  - symbol charset [A-Z0-9.\-]{1,12}; pairs split on ":"
  - hard blocks: BARC/BCS (standing owner rule — never add Barclays), TEST
    (reserved for e2e checks)
  - duplicates: already in the us pool, a europe native, or a europe ADR
  - a dotted native symbol without an ADR is rejected (needs the pair form —
    Finnhub free tier serves nothing for non-US listings)
  - native/US leg must return a Yahoo chart with a price
  - ADR leg must return a non-empty Finnhub profile2 (otherwise fundamentals
    would be permanently blank — blank-by-design beats silently-broken)
"""

import json, os, re, time, urllib.request

STATE = os.environ.get("STATE_DIR", "state")
OUT = os.environ.get("OUT_DIR", "out")
KEY = os.environ.get("FINNHUB_API_KEY", "")
RAW = os.environ.get("TICKERS", "")
os.makedirs(OUT, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SYM_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")
BLOCKED = {"BARC": "not permitted (standing rule)", "BCS": "not permitted (standing rule)",
           "TEST": "reserved for e2e checks"}


def get(url, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        return json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return None


def yahoo_ok(sym):
    """Native/US leg check: Yahoo must serve a chart with a live price."""
    d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}?range=5d&interval=1d", UA)
    try:
        res = d["chart"]["result"][0]
        meta = res.get("meta", {})
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        return bool(meta.get("regularMarketPrice") or closes)
    except Exception:
        return False


def finnhub_ok(sym):
    """ADR leg check: profile2 must be non-empty on the free tier."""
    d = get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={KEY}")
    return bool(d) and bool(d.get("name") or d.get("ticker"))


universe = json.load(open(f"{STATE}/universe.json"))
us_pool = set(universe["us"])
eu_native = {e["ticker"] for e in universe["europe"]}
eu_adr = {e["adr"] for e in universe["europe"]}

added, rejected = [], []
seen = set()
entries = [e for e in re.split(r"[,\s]+", RAW.strip()) if e]

for entry in entries[:20]:  # sanity cap per submission
    entry = entry.upper()
    if entry in seen:
        continue
    seen.add(entry)
    parts = entry.split(":")
    if len(parts) > 2 or not all(SYM_RE.match(p) for p in parts if p) or "" in parts:
        rejected.append({"entry": entry, "reason": "bad format — use SYMBOL or NATIVE:ADR"})
        continue
    native, adr = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], None)
    blocked = BLOCKED.get(native) or (BLOCKED.get(adr) if adr else None)
    if blocked:
        rejected.append({"entry": entry, "reason": blocked})
        continue
    if native in us_pool or native in eu_native or native in eu_adr or (adr and (adr in eu_adr or adr in us_pool)):
        rejected.append({"entry": entry, "reason": "already tracked"})
        continue
    if adr is None and "." in native:
        rejected.append({"entry": entry, "reason": "looks like a non-US listing — resubmit as NATIVE:ADR (e.g. HSBA.L:HSBC)"})
        continue
    if not yahoo_ok(native):
        rejected.append({"entry": entry, "reason": f"no Yahoo price data for {native}"})
        continue
    time.sleep(0.3)
    if adr:
        if not finnhub_ok(adr):
            rejected.append({"entry": entry, "reason": f"Finnhub serves no fundamentals for ADR {adr} — check the symbol"})
            continue
        time.sleep(1.1)
        universe["europe"].append({"ticker": native, "adr": adr})
        eu_native.add(native); eu_adr.add(adr)
    else:
        if not finnhub_ok(native):
            # US-listed but unknown to Finnhub: still add (prices/charts work via
            # Yahoo; fundamentals stay blank like other thin-data rows) but say so.
            added.append(f"{native} (no Finnhub fundamentals — will show prices/technicals only)")
            universe["us"].append(native)
            us_pool.add(native)
            continue
        time.sleep(1.1)
        universe["us"].append(native)
        us_pool.add(native)
    added.append(entry)

if added:
    universe["tickers"] = list(universe["us"]) + [e["ticker"] for e in universe["europe"]]
    universe["added"] = sorted(set(universe.get("added") or []) | {a.split(" ")[0] for a in added})
    json.dump(universe, open(f"{STATE}/universe.json", "w"), indent=1)

lines = []
if added:
    lines.append("Added to universe: " + ", ".join(added))
if rejected:
    lines.append("Rejected: " + "; ".join(f"{r['entry']} ({r['reason']})" for r in rejected))
open(f"{OUT}/notify.txt", "w").write("\n".join(lines))
json.dump({"added": added, "rejected": rejected}, open(f"{OUT}/result.json", "w"), indent=1)
print(f"add_tickers: {len(added)} added, {len(rejected)} rejected of {len(entries)} submitted")
