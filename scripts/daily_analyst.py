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

# ---------- weekday rotation (2026-07-22, for the 300-US universe) ----------
# The 6-call Finnhub bundle per ticker doesn't fit in one run at ~380 tickers
# (~42 min at rate-limit pacing), and fundamentals/recommendations move slowly.
# Each weekday covers a stable fifth of the universe (md5-hash bucket, so
# membership survives pool reordering); tickers NEW to analyst-state are always
# fetched same-day; news additionally refreshes daily for the 50 largest names.
# FULL=1 fetches everything (one-time seeding / manual runs). Yahoo targets and
# fundamentals carry-forward remain daily for the whole universe.
import hashlib
FULL = os.environ.get("FULL", "0") == "1"
try:
    prior_by = json.load(open(f"{STATE}/analyst-state.json")).get("byTicker", {})
except Exception:
    prior_by = {}
wd = datetime.date.today().weekday() % 5
bucket = lambda t: int(hashlib.md5(t.encode()).hexdigest(), 16) % 5
rotation = {t for t, _ in pairs if FULL or bucket(t) == wd or t not in prior_by}
news_daily = {t for t, _ in list(pairs)[:50]}  # pairs are mcap-ordered; big names get fresh news daily

today = datetime.date.today()
# Calendar window: ~1y back (report-date markers on the price chart) through next
# week (upcoming section). The recent-results section filters to the past week client-side.
frm, to = (today - datetime.timedelta(days=370)).isoformat(), (today + datetime.timedelta(days=7)).isoformat()
news_frm = (today - datetime.timedelta(days=7)).isoformat()
ins_frm = (today - datetime.timedelta(days=90)).isoformat()  # insider-transactions window

def fetch_news_only(sym):
    ck = f"{OUT}/ck/news-{sym.replace('/', '_')}.json"
    if os.path.exists(ck): return json.load(open(ck))
    news = get(f"https://finnhub.io/api/v1/company-news?symbol={sym}&from={news_frm}&to={today.isoformat()}&token={KEY}"); time.sleep(FH_PACE)
    b = {"news": news}
    json.dump(b, open(ck, "w"))
    return b

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
    ins = get(f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={sym}&from={ins_frm}&to={today.isoformat()}&token={KEY}"); time.sleep(FH_PACE)
    b = {"rec": rec, "cal": cal, "hist": hist, "news": news, "profile": prof, "metric": met, "ins": ins}
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
    # Insider activity, last 90d: only open-market purchases (code P) and sales
    # (code S) count — option exercises/awards/tax withholdings (M/A/F/G…) are
    # routine plumbing, not conviction signals.
    ins_b = ins_s = 0
    try:
        for x in ((b.get("ins") or {}).get("data") or []):
            c = x.get("transactionCode")
            if c == "P": ins_b += 1
            elif c == "S": ins_s += 1
    except Exception: pass
    insiders = {"b": ins_b, "s": ins_s} if (ins_b or ins_s or (b.get("ins") or {}).get("data") is not None) else None
    return {"analystScore": a_score, "analystRec": a_rec, "insiders": insiders,
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
    if ticker in rotation:
        by[ticker] = build(ticker, sym)
    else:
        by[ticker] = prior_by[ticker]  # off-rotation: carry yesterday's data forward

# One retry pass for transient Finnhub blips: refetch tickers that came back scoreless.
for ticker, sym in pairs:
    if ticker in rotation and by[ticker]["analystScore"] is None:
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
tgt_ok = dcal_ok = 0
ref_prices = {}  # live native-listing price per ticker — refresh.py's marketCap drift anchor
try:
    prior_dcal = {t: v.get("dcal") for t, v in by.items() if v.get("dcal")}
except Exception:
    prior_dcal = {}
for ticker, _sym in pairs:
    tgt = prior_targets.get(ticker)
    dcal = prior_dcal.get(ticker)
    if op:
        try:
            # calendarEvents rides the same request as the price targets
            # (dividend calendar, 2026-08-13): ex-dividend + payment dates
            u = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                 f"{urllib.parse.quote(ticker)}?modules=financialData%2CcalendarEvents&crumb={urllib.parse.quote(crumb)}")
            r0 = json.loads(op.open(u, timeout=15).read().decode())["quoteSummary"]["result"][0]
            fd = r0.get("financialData") or {}
            mean = (fd.get("targetMeanPrice") or {}).get("raw")
            n = (fd.get("numberOfAnalystOpinions") or {}).get("raw")
            cur = (fd.get("currentPrice") or {}).get("raw")
            if cur: ref_prices[ticker] = round(cur, 3)
            if mean and n and n >= 3:
                tgt = {"mean": round(mean, 2), "analysts": n, "yPrice": round(cur, 3) if cur else None}
                tgt_ok += 1
            cal = r0.get("calendarEvents") or {}
            _ex = (cal.get("exDividendDate") or {}).get("raw")
            _pay = (cal.get("dividendDate") or {}).get("raw")
            if _ex or _pay:
                _iso = lambda ts: datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d") if ts else None
                dcal = {"ex": _iso(_ex), "pay": _iso(_pay)}
                dcal_ok += 1
        except Exception:
            pass
        time.sleep(0.4)
    if ticker in by and tgt:
        by[ticker]["target"] = tgt
    if ticker in by and dcal:
        by[ticker]["dcal"] = dcal
print(f"targets: {tgt_ok}/{len(pairs)} fetched live; dividend dates: {dcal_ok}")

# ---------- options-implied metrics (Yahoo options chain, 2026-07-28) ----------
# Uses the US-LISTED symbol (native for US rows, the ADR for EU rows — many
# ADRs carry listed options; the rest simply stay None). One default-chain
# request per ticker; if the nearest expiry is <7 days out (megacap weeklies —
# too spiky: an expiring AAPL weekly showed 52%+ "IV" vs ~24% real) the first
# expiry >=7 days out is refetched via ?date=. Per ticker: opt = {iv (ATM
# call/put mean, annualized %), em (± expected move % by expiry = straddle
# mid / spot), exp "YYYY-MM-DD"}. Prior values carry forward on failure;
# refresh.py drops entries whose expiry has passed.
try:
    prior_opt = {t: (v.get("opt") or None) for t, v in
                 json.load(open(f"{STATE}/analyst-state.json")).get("byTicker", {}).items()}
except Exception:
    prior_opt = {}

def _opt_mid(c):
    b, a = c.get("bid"), c.get("ask")
    if isinstance(b, (int, float)) and isinstance(a, (int, float)) and a >= b > 0:
        return (a + b) / 2
    lp = c.get("lastPrice")
    return lp if isinstance(lp, (int, float)) and lp > 0 else None

def fetch_opt(sym):
    base = (f"https://query2.finance.yahoo.com/v7/finance/options/"
            f"{urllib.parse.quote(sym)}?crumb={urllib.parse.quote(crumb)}")
    j = json.loads(op.open(base, timeout=15).read().decode())["optionChain"]["result"][0]
    spot = (j.get("quote") or {}).get("regularMarketPrice")
    exps = j.get("expirationDates") or []
    chain = (j.get("options") or [None])[0]
    now_ts = time.time()
    if chain and exps and chain["expirationDate"] - now_ts < 7 * 86400:
        pick = next((e for e in exps if e - now_ts >= 7 * 86400), None)
        if pick:
            j = json.loads(op.open(base + f"&date={pick}", timeout=15).read().decode())["optionChain"]["result"][0]
            chain = (j.get("options") or [None])[0]
    if not (chain and isinstance(spot, (int, float)) and spot > 0):
        return None
    call_by = {c.get("strike"): c for c in chain.get("calls") or []}
    put_by = {p.get("strike"): p for p in chain.get("puts") or []}
    for k in sorted(set(call_by) & set(put_by), key=lambda s: abs(s - spot))[:3]:
        c, p = call_by[k], put_by[k]
        ivs = [x["impliedVolatility"] for x in (c, p)
               if isinstance(x.get("impliedVolatility"), (int, float)) and 0.01 < x["impliedVolatility"] < 5]
        if not ivs:
            continue
        cm, pm = _opt_mid(c), _opt_mid(p)
        em = round((cm + pm) / spot * 100, 1) if (cm and pm) else None
        if em is not None and not (0 < em < 50):
            em = None
        return {"iv": round(sum(ivs) / len(ivs) * 100, 1), "em": em,
                "exp": datetime.datetime.fromtimestamp(chain["expirationDate"], datetime.timezone.utc).strftime("%Y-%m-%d")}
    return None

opt_ok = 0
for ticker, sym in pairs:
    o = prior_opt.get(ticker)
    if op:
        try:
            got = fetch_opt(sym)
            if got:
                o = got
                opt_ok += 1
        except Exception:
            pass
        time.sleep(0.35)
    if ticker in by and o:
        by[ticker]["opt"] = o
print(f"options: {opt_ok}/{len(pairs)} fetched live")

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

try:
    prior_news = json.load(open(f"{STATE}/news-state.json")).get("byTicker", {})
except Exception:
    prior_news = {}
news_by = {}
for ticker, sym in pairs:
    if ticker not in rotation and ticker in news_daily:
        items = fetch_news_only(sym).get("news") or []       # big name off-rotation: fresh news anyway
    elif ticker not in rotation:
        news_by[ticker] = prior_news.get(ticker) or []       # off-rotation: carry last known news
        continue
    else:
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
