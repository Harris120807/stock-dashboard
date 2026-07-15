#!/usr/bin/env python3
"""Weekly universe refresh for the stock dashboard.

Screens Yahoo Finance for the top 500 US-tradeable stocks by market cap, derives:
  - US 50:    top 50 US-domiciled companies
  - Europe 30: top 30 UK/European-domiciled companies, displayed on their native
               home-exchange listing with their US ADR symbol kept for Finnhub
and writes universe.json into STATE_DIR. Prints CHANGED/UNCHANGED and writes a
notification body to OUT_DIR/notify.txt only when membership changed.

Env: FINNHUB_API_KEY (required), STATE_DIR (default "state"), OUT_DIR (default "out").
"""
import datetime, http.cookiejar, json, os, re, time, urllib.parse, urllib.request

KEY = os.environ["FINNHUB_API_KEY"]
STATE = os.environ.get("STATE_DIR", "state")
OUT = os.environ.get("OUT_DIR", "out")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
os.makedirs(OUT, exist_ok=True)

SUFFIX_RE = re.compile(r"\b(plc|p\.l\.c\.|n\.v\.|nv|s\.a\.|sa|ag|se|asa|oyj|a/s|ltd|limited|spa|s\.p\.a\.|ab)\.?$", re.I)
KNOWN_FOREIGN = {"TSM","BABA","RY","TD","BMO","BNS","CM","SU","ENB","CNQ","TRP","PDD","JD","BIDU","NTES","SONY","TCOM","NIO","LI","XPEV","MUFG","SMFG","MFG","TM","IBN","HDB","RIO","SNY"}
EUROPE = {"GB","IE","FR","DE","NL","CH","SE","DK","NO","FI","ES","IT","BE","AT","PT","LU","PL","JE","GG","IM"}
INVERSIONS = {"LIN","ETN","MDT","ACN","TT","JCI","AON","WTW","CB","APTV","PNR","ALLE","STE","IR","GRMN","TEL","ICLR","AMCR","SW","VRT"}
ADR_FIX = {"DOGEF":"DNNGY","RHHVF":"RHHBY","NSRGF":"NSRGY","ALIZF":"ALIZY","SBGSF":"SBGSY","EADSF":"EADSY","IBDSF":"IBDRY","DTEGF":"DTEGY","UNCFF":"UNCRY","GLCNF":"GLNCY","MURGF":"MURGY","ENGQF":"ENGIY","BAESF":"BAESY","LVMHF":"LVMUY","SIEGF":"SIEGY","SMEGF":"SMERY"}
NATIVE_MAP = {"ASML":"ASML.AS","RHHBY":"RO.SW","ARM":"ARM","LVMUY":"MC.PA","NSRGY":"NESN.SW","HSBC":"HSBA.L","SIEGY":"SIE.DE","LRLCY":"OR.PA","NVS":"NOVN.SW","NVO":"NOVO-B.CO","SAN":"SAN.MC","HESAY":"RMS.PA","AZN":"AZN.L","IDEXY":"ITX.MC","SAP":"SAP.DE","ALIZY":"ALV.DE","SBGSY":"SU.PA","EADSY":"AIR.PA","SHEL":"SHEL.L","TTE":"TTE.PA","IBDRY":"IBE.MC","RYCEY":"RR.L","SAFRY":"SAF.PA","ABBNY":"ABBN.SW","SMERY":"ENR.DE","DTEGY":"DTE.DE","BBVA":"BBVA.MC","UNCRY":"UCG.MI","BUD":"ABI.BR","UBS":"UBSG.SW","BNPQY":"BNP.PA","ISNPY":"ISP.MI","BTI":"BATS.L","CFRUY":"CFR.SW","RIO":"RIO.L","UL":"ULVR.L","GSK":"GSK.L","BP":"BP.L","SPOT":"SPOT","EQNR":"EQNR.OL","ING":"INGA.AS","BCS":"BARC.L","LYG":"LLOY.L","NGG":"NG.L","SNY":"SAN.PA","ZURVY":"ZURN.SW","AXAHY":"CS.PA","PROSY":"PRX.AS","ADYEY":"ADYEN.AS","DB":"DBK.DE","ESLOY":"EL.PA","RACE":"RACE.MI","CRH":"CRH","NWG":"NWG.L","MURGY":"MUV2.DE","BAESY":"BA.L","GLNCY":"GLEN.L","E":"ENI.MI","DNNGY":"ORSTED.CO"}

def get_json(url, headers=None, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries: return None
            time.sleep(1 + i)

# ---------- Yahoo screener (cookie + crumb, 2 pages) ----------
def screen500():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", UA)]
    try: opener.open("https://fc.yahoo.com", timeout=15)
    except Exception: pass  # 404 expected; cookie is what matters
    crumb = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
    rows = []
    for offset in (0, 250):
        body = json.dumps({"size": 250, "offset": offset, "sortField": "intradaymarketcap", "sortType": "DESC",
                           "quoteType": "EQUITY", "query": {"operator": "AND", "operands": [{"operator": "EQ", "operands": ["region", "us"]}]}}).encode()
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v1/finance/screener?formatted=true&lang=en-US&region=US&crumb=" + urllib.parse.quote(crumb),
            data=body, headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
        resp = json.loads(opener.open(req, timeout=30).read().decode())
        for q in resp["finance"]["result"][0]["quotes"]:
            mc = q.get("marketCap"); mc = mc.get("raw") if isinstance(mc, dict) else mc
            rows.append({"symbol": q.get("symbol"), "name": q.get("longName") or q.get("shortName"),
                         "exchange": q.get("exchange"), "fx": q.get("fullExchangeName") or "", "mcap": mc})
        time.sleep(1)
    seen, out = set(), []
    for q in rows:
        if q["symbol"] and q["symbol"] not in seen:
            seen.add(q["symbol"]); out.append(q)
    out.sort(key=lambda q: -(q["mcap"] or 0))
    return out

def is_otc(q): return q["exchange"] in {"PNK","OQX","OQB","OTC","OID"} or "otc" in q["fx"].lower()
def name_foreign(q): return bool(SUFFIX_RE.search((q.get("name") or "").strip()))

fxc = {}
def fx(ccy):
    if ccy in (None, "USD"): return 1.0
    if ccy not in fxc:
        c = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{ccy}USD=X?range=5d&interval=1d")
        try: fxc[ccy] = [x for x in c["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x][-1]
        except Exception: fxc[ccy] = None
    return fxc[ccy]

def main():
    prior = json.load(open(f"{STATE}/universe.json"))
    try:
        quotes = screen500()
    except Exception as e:
        # Screener down even after retries: rewrite prior unchanged so downstream keeps working.
        prior["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        prior["added"], prior["dropped"] = [], []
        json.dump(prior, open(f"{STATE}/universe.json", "w"), indent=1)
        print(f"SCREENER FAILED ({e}) — prior universe rewritten unchanged")
        return

    # hygiene + dual-class dedupe
    seen, clean = {}, []
    for q in quotes:
        s = q["symbol"]
        if "-" in s and s not in ("BRK-A", "BRK-B"): continue
        key = (q.get("name") or s).lower()
        if key in seen:
            prev = seen[key]
            if s in ("GOOG", "BRK-B") or (is_otc(prev) and not is_otc(q)):
                clean[clean.index(prev)] = q; seen[key] = q
            continue
        seen[key] = q; clean.append(q)
    clean = [q for q in clean if q["symbol"] not in ("GOOGL", "BRK-A")]

    us = [q for q in clean if not (is_otc(q) or name_foreign(q) or q["symbol"] in KNOWN_FOREIGN)]
    foreign = [q for q in clean if is_otc(q) or name_foreign(q) or q["symbol"] in KNOWN_FOREIGN]
    us50 = us[:50]

    # Europe: country lookup, trusted mcap, rank, resolve native, validate
    cands = []
    for q in foreign:
        if (q["mcap"] or 0) < 60e9: break
        sym = ADR_FIX.get(q["symbol"], q["symbol"])
        if sym in INVERSIONS or q["symbol"] in INVERSIONS: continue
        p = get_json(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={KEY}") or {}
        time.sleep(0.3)
        if p.get("country") not in EUROPE: continue
        rate = fx(p.get("currency"))
        trusted = p.get("marketCapitalization") and rate and p["marketCapitalization"] * rate * 1e6
        # When the two sources disagree wildly, the inflated one is the broken one:
        # Finnhub inflates via currency mislabels (Equinor NOK-as-USD), the Yahoo screen
        # inflates on thin F-lines via bogus share counts (Orsted DOGEF). Take the smaller.
        if trusted and q["mcap"] and (trusted / q["mcap"] > 3 or trusted / q["mcap"] < 1/3):
            trusted = min(trusted, q["mcap"])
        cands.append({"adr": sym, "mcap": trusted or q["mcap"] or 0, "profile_ticker": p.get("ticker") or ""})
    cands.sort(key=lambda c: -c["mcap"])

    eu30 = []
    for c in cands:
        if len(eu30) >= 30: break
        native = NATIVE_MAP.get(c["adr"]) or (c["profile_ticker"] if "." in c["profile_ticker"] else c["adr"])
        chart = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(native)}?range=2y&interval=1d")
        time.sleep(0.25)
        try:
            res = chart["chart"]["result"][0]
            closes = [x for x in res["indicators"]["quote"][0]["close"] if x is not None]
            ok_chart = len(closes) >= 250 and res["meta"].get("regularMarketPrice")
        except Exception:
            ok_chart = False
        m = (get_json(f"https://finnhub.io/api/v1/stock/metric?symbol={c['adr']}&metric=all&token={KEY}") or {}).get("metric", {})
        time.sleep(0.3)
        if ok_chart and (m.get("peTTM") is not None or m.get("pb") is not None):
            eu30.append({"ticker": native, "adr": c["adr"], "mcap": c["mcap"]})

    us_mcap = {q["symbol"]: q["mcap"] or 0 for q in us50}
    eu_mcap = {e["ticker"]: e["mcap"] for e in eu30}
    tickers = sorted([q["symbol"] for q in us50] + [e["ticker"] for e in eu30],
                     key=lambda t: -(us_mcap.get(t) or eu_mcap.get(t) or 0))
    prior_set = set(prior.get("tickers") or [])
    added = sorted(set(tickers) - prior_set)
    dropped = sorted(prior_set - set(tickers))
    out = {"updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "tickers": tickers, "us": [q["symbol"] for q in us50],
           "europe": [{"ticker": e["ticker"], "adr": e["adr"]} for e in eu30],
           "added": added, "dropped": dropped}
    json.dump(out, open(f"{STATE}/universe.json", "w"), indent=1)

    changed = bool((added or dropped) and prior_set)
    if changed:
        open(f"{OUT}/notify.txt", "w").write(
            f"Universe updated — added: {', '.join(added) or 'none'}. dropped: {', '.join(dropped) or 'none'}. "
            f"Takes effect on the next daily/hourly refresh.")
    print(f"{'CHANGED' if changed else 'UNCHANGED'} us={len(us50)} eu={len(eu30)} added={added} dropped={dropped}")

main()
