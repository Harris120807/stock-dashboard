#!/usr/bin/env python3
"""Daily analyst/earnings prefetch for the stock dashboard.

Reads universe.json from STATE_DIR (a checkout of claude/state), fetches analyst
recommendations + earnings calendar + beat/miss history + fundamentals (profile2
and metric, prefetched here daily so the hourly refresh can run Yahoo-only) from
Finnhub for all tickers (European rows fetched via their ADR symbol, keyed by
native ticker), and writes analyst-state.json, news-state.json and
fundamentals-state.json back into STATE_DIR. The caller commits/pushes.

Env: FINNHUB_API_KEY (required), STATE_DIR (default "state"), OUT_DIR (default "out").
"""
import datetime, http.cookiejar, json, os, time, urllib.parse, urllib.request

KEY = os.environ["FINNHUB_API_KEY"]
STATE = os.environ.get("STATE_DIR", "state")
OUT = os.environ.get("OUT_DIR", "out")
os.makedirs(f"{OUT}/ck", exist_ok=True)

def get(url, retries=4):
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if i == retries: return None
            ra = e.headers.get("Retry-After") if e.headers else None
            time.sleep(min(int(ra) if (ra and str(ra).isdigit()) else 2 ** (i + 1), 60))
        except Exception:
            if i == retries: return None
            time.sleep(2 ** i)

FH_PACE = float(os.environ.get("FINNHUB_PACE", "1.1"))  # stay under Finnhub free-tier 60 calls/min

universe = json.load(open(f"{STATE}/universe.json"))
pairs = [(t, t) for t in universe["us"]] + [(e["ticker"], e["adr"]) for e in universe["europe"]]

today = datetime.date.today()
# Calendar window: ~1y back (report-date markers on the price chart) through next
# week (upcoming section). The recent-results section filters to the past week client-side.
frm, to = (today - datetime.timedelta(days=370)).isoformat(), (today + datetime.timedelta(days=7)).isoformat()
news_frm = (today - datetime.timedelta(days=7)).isoformat()

def fetch(sym):
    ck = f"{OUT}/ck/{sym.replace('/', '_')}.json"
    if os.path.exists(ck): return json.load(open(ck))
    rec = get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={sym}&token={KEY}"); time.sleep(FH_PACE)
    cal = get(f"https://finnhub.io/api/v1/calendar/earnings?from={frm}&to={to}&symbol={sym}&token={KEY}"); time.sleep(FH_PACE)
    hist = get(f"https://finnhub.io/api/v1/stock/earnings?symbol={sym}&token={KEY}"); time.sleep(FH_PACE)
    news = get(f"https://finnhub.io/api/v1/company-news?symbol={sym}&from={news_frm}&to={today.isoformat()}&token={KEY}"); time.sleep(FH_PACE)
    # Fundamentals prefetch for the hourly refresh: profile2 + metric change on a
    # daily timescale, so they're fetched here once a day and served to refresh.py
    # via fundamentals-state.json instead of being refetched every hour.
    prof = get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={KEY}") or {}; time.sleep(FH_PACE)
    met = (get(f"https://finnhub.io/api/v1/stock/metric?symbol={sym}&metric=all&token={KEY}") or {}).get("metric", {}); time.sleep(FH_PACE)
    b = {"rec": rec, "cal": cal, "hist": hist, "news": news, "profile": prof, "metric": met}
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
    # The calendar window is [today-7d, today+7d]: entries with a reported
    # epsActual feed the "recent earnings" section, the earliest not-yet-reported
    # entry is the upcoming one. A today-dated entry counts as upcoming until
    # its actual lands.
    cal_entries = []
    try:
        cal_entries = [x for x in ((b["cal"] or {}).get("earningsCalendar") or []) if x and x.get("date")]
    except Exception: pass
    cal_entries.sort(key=lambda x: x["date"])
    t_iso = today.isoformat()
    upcoming = [x for x in cal_entries if x["date"] > t_iso or (x["date"] == t_iso and x.get("epsActual") is None)]
    reported = [x for x in cal_entries if x["date"] <= t_iso and x.get("epsActual") is not None]
    e0 = upcoming[0] if upcoming else None
    r0 = reported[-1] if reported else None
    # past report dates + EPS surprise, for beat/miss markers on the price chart
    def _sp(x):
        a, est = x.get("epsActual"), x.get("epsEstimate")
        return round((a - est) / abs(est) * 100, 1) if (a is not None and est) else None
    reports = [{"d": x["date"], "sp": _sp(x)} for x in reported[-5:]]
    q4 = [x for x in (b["hist"] or [])[:4] if x and x.get("surprisePercent") is not None]
    return {"analystScore": a_score, "analystRec": a_rec,
                  "earnings": {"nextDate": e0.get("date") if e0 else None,
                               "nextHour": e0.get("hour") if e0 else None,
                               "epsEstimate": e0.get("epsEstimate") if e0 else None,
                               "revenueEstimate": e0.get("revenueEstimate") if e0 else None,
                               "beatCount": sum(1 for x in q4 if x["surprisePercent"] > 0) if q4 else None,
                               "beatTotal": len(q4) if q4 else None,
                               "recent": ({"date": r0.get("date"), "hour": r0.get("hour"),
                                           "epsActual": r0.get("epsActual"), "epsEstimate": r0.get("epsEstimate"),
                                           "revenueActual": r0.get("revenueActual"), "revenueEstimate": r0.get("revenueEstimate")}
                                          if r0 else None),
                               "reports": reports}}

for ticker, sym in pairs:
    by[ticker] = build(ticker, sym)

# One retry pass for transient Finnhub blips: refetch tickers that came back scoreless.
for ticker, sym in pairs:
    if by[ticker]["analystScore"] is None:
        ck = f"{OUT}/ck/{sym.replace('/', '_')}.json"
        if os.path.exists(ck): os.remove(ck)
        by[ticker] = build(ticker, sym)

# ---------- analyst price targets (Yahoo quoteSummary, native symbols) ----------
# Works for both US and European listings; targets come back in the listing's
# trading unit (London in pence, matching our stored prices). Prior targets are
# carried forward for tickers whose fetch fails, so a bad Yahoo day degrades
# gracefully instead of nulling upside across the page.
try:
    prior_targets = {t: (v.get("target") or None) for t, v in
                     json.load(open(f"{STATE}/analyst-state.json")).get("byTicker", {}).items()}
except Exception:
    prior_targets = {}

def yahoo_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")]
    try: op.open("https://fc.yahoo.com", timeout=15)
    except Exception: pass  # 404 expected; the cookie is what matters
    crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
    return op, crumb

try:
    op, crumb = yahoo_opener()
except Exception as e:
    op = crumb = None
    print("targets: crumb failed:", e)
tgt_ok = 0
ref_prices = {}  # live native-listing price per ticker — refresh.py's marketCap drift anchor
for ticker, _sym in pairs:
    tgt = prior_targets.get(ticker)
    if op:
        try:
            u = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                 f"{urllib.parse.quote(ticker)}?modules=financialData&crumb={urllib.parse.quote(crumb)}")
            fd = json.loads(op.open(u, timeout=15).read().decode())["quoteSummary"]["result"][0]["financialData"]
            mean = (fd.get("targetMeanPrice") or {}).get("raw")
            n = (fd.get("numberOfAnalystOpinions") or {}).get("raw")
            cur = (fd.get("currentPrice") or {}).get("raw")
            if cur: ref_prices[ticker] = round(cur, 3)
            if mean and n and n >= 3:
                tgt = {"mean": round(mean, 2), "analysts": n, "yPrice": round(cur, 3) if cur else None}
                tgt_ok += 1
        except Exception:
            pass
        time.sleep(0.4)
    if ticker in by and tgt:
        by[ticker]["target"] = tgt
print(f"targets: {tgt_ok}/{len(pairs)} fetched live")

state = {"updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "byTicker": by}
json.dump(state, open(f"{STATE}/analyst-state.json", "w"))

# ---------- fundamentals prefetch for the hourly refresh ----------
# Schema contract with refresh.py (FUND): {updatedAt, byTicker: {T: {profile,
# metric, refPrice}}}. A ticker whose fetch failed today keeps yesterday's
# entry so refresh.py doesn't fall back to per-ticker live Finnhub calls.
try:
    prior_fund = json.load(open(f"{STATE}/fundamentals-state.json")).get("byTicker", {})
except Exception:
    prior_fund = {}
fund = {}
for ticker, sym in pairs:
    try:
        cb = json.load(open(f"{OUT}/ck/{sym.replace('/', '_')}.json"))
    except Exception:
        cb = {}
    if cb.get("profile") and cb.get("metric"):
        fund[ticker] = {"profile": cb["profile"], "metric": cb["metric"],
                        "refPrice": ref_prices.get(ticker)}
    elif ticker in prior_fund:
        fund[ticker] = prior_fund[ticker]
json.dump({"updatedAt": state["updatedAt"], "byTicker": fund},
          open(f"{STATE}/fundamentals-state.json", "w"), separators=(",", ":"))
print(f"fundamentals: {sum(1 for t, _ in pairs if t in fund)}/{len(pairs)} stored")

# ---------- news with lexicon tone tags ----------
# Financial-sentiment word lexicon (Loughran-McDonald style, curated subset).
# Deterministic tone read of headline+summary text — NOT a price prediction.
POS = {"beat","beats","exceed","exceeds","exceeded","record","surge","surges","surged","soar","soars","soared",
"upgrade","upgraded","outperform","strong","stronger","strongest","growth","grow","grows","profit","profitable",
"gain","gains","rally","rallies","bullish","raise","raises","raised","hike","hikes","boost","boosts","boosted",
"buyback","dividend","approval","approved","win","wins","won","award","awarded","partnership","breakthrough",
"jump","jumps","jumped","top","tops","topped","robust","accelerate","accelerates","upbeat","optimistic",
"momentum","expansion","expand","expands","milestone","success","successful","innovative","launch","launches",
"upside","overweight","recovery","rebound","rebounds","surpass","surpasses","surpassed","best"}
NEG = {"loss","losses","lawsuit","sue","sues","sued","probe","probes","investigation","investigate","recall",
"recalls","downgrade","downgraded","miss","misses","missed","weak","weaker","weakest","decline","declines",
"declined","fall","falls","fell","drop","drops","dropped","plunge","plunges","plunged","layoff","layoffs","cut",
"cuts","warning","warns","warned","fraud","fine","fined","penalty","bankruptcy","default","slump","slumps",
"tumble","tumbles","tumbled","bearish","underperform","disappointing","disappoint","disappoints","delay","delays",
"delayed","halt","halts","halted","scandal","breach","hack","hacked","resign","resigns","resigned","selloff",
"crash","crashes","headwind","headwinds","slowdown","litigation","injunction","strike","strikes","risk","risks",
"concern","concerns","worst","downside","underweight","cautious","volatile","losses"}
import re as _re
def tone_tag(text):
    words = _re.findall(r"[a-z']+", (text or "").lower())
    p = sum(1 for w in words if w in POS)
    n = sum(1 for w in words if w in NEG)
    hits = p + n
    if hits == 0: return 0
    net = (p - n) / hits
    if net >= 0.6 and hits >= 3: return 2
    if net >= 0.25: return 1
    if net <= -0.6 and hits >= 3: return -2
    if net <= -0.25: return -1
    return 0

news_by = {}
for ticker, sym in pairs:
    ck = f"{OUT}/ck/{sym.replace('/', '_')}.json"
    try:
        items = json.load(open(ck)).get("news") or []
    except Exception:
        items = []
    seen, out = set(), []
    for it in sorted((x for x in items if x and x.get("headline")), key=lambda x: -(x.get("datetime") or 0)):
        h = it["headline"].strip()
        if h.lower() in seen: continue
        seen.add(h.lower())
        summary = (it.get("summary") or "").strip()
        if len(summary) > 240: summary = summary[:237].rsplit(" ", 1)[0] + "…"
        out.append({"h": h, "u": it.get("url"), "s": summary, "src": it.get("source"),
                    "d": datetime.datetime.fromtimestamp(it.get("datetime") or 0, datetime.timezone.utc).date().isoformat(),
                    "tag": tone_tag(h + " " + summary)})
        if len(out) == 4: break
    news_by[ticker] = out
json.dump({"updatedAt": state["updatedAt"], "byTicker": news_by}, open(f"{STATE}/news-state.json", "w"), separators=(",", ":"))

missing = [t for t, v in by.items() if v["analystScore"] is None]
n_news = sum(1 for v in news_by.values() if v)
print(f"OK {len(by)} tickers; missing analyst scores: {missing or 'none'}; news for {n_news} tickers")
