#!/usr/bin/env python3
"""Stake-building / deal-event fetcher.

Pulls ownership and takeover disclosures for the whole universe into
stakes-state.json on claude/state:

  US + ADR rows (EDGAR, data.sec.gov):
    - Schedule 13D / 13G + amendments (structured XML since Dec 2024:
      filer name, percent of class, rule cited)
    - Tender offers (SC TO-T/TO-I, SC 14D9), going-private (SC 13E3),
      merger proxies (DEFM14A/PREM14A, S-4, 425)
    - 8-K item 1.03 (bankruptcy/receivership) and 2.01 (completed
      acquisition/disposition)
  London rows (FCA National Storage Mechanism search API):
    - TR-1 Holding(s) in Company (HOL) with holder + percent parsed from
      the document (best effort)
    - Takeover Code lifecycle: OFB/OFD/OFF/ORE/OUP/POT/CAS/TEN/RTE,
      capital reorganisation (CAR), acquisitions/disposals (ACQ/DIS)
    - Form 8.x dealing disclosures are NOT stored as events; their
      presence sets an "offer period" flag per company.

Each event carries a significance score 0-100 (deal events > activist
13Ds > passive index-fund churn). No API key needed; both sources are
public. Pacing respects SEC fair use (<10 req/s).

Env: STATE_DIR (claude/state checkout), DOC_CAP (TR-1/XML doc fetch cap
per run, default 60). First run (empty state) seeds ~550 days back
(180 days for passive 13G/A noise); later runs are incremental by
accession/disclosure id.
"""
import datetime
import html as htmllib
import json
import os
import re
import time
import urllib.request

STATE_DIR = os.environ.get("STATE_DIR", "state")
DOC_CAP = int(os.environ.get("DOC_CAP", "60"))
UA = "ValueTally stake-monitor (contact@valuetally.com)"

TRADE_CAP = int(os.environ.get("TRADE_CAP", "300"))  # Form 4 XML reads per run
EFTS_CAP = int(os.environ.get("EFTS_CAP", "80"))      # counterparty lookups per run

FULL_WINDOW_D = 550     # deal forms + 13D
PASSIVE_WINDOW_D = 180  # 13G/A institutional churn
EVENT_CAP = 60          # per ticker
TRADE_WINDOW_D = 120    # insider (Form 4) transactions
TRADE_STORE_CAP = 40    # per ticker
SQUELCH_D = 21          # collapse repeat deal-doc amendments (425, /A) within this window

STAKE_FORMS = {
    "SCHEDULE 13D": "13D", "SC 13D": "13D",
    "SCHEDULE 13D/A": "13D/A", "SC 13D/A": "13D/A",
    "SCHEDULE 13G": "13G", "SC 13G": "13G",
    "SCHEDULE 13G/A": "13G/A", "SC 13G/A": "13G/A",
}
DEAL_FORMS = {
    "SC TO-T": "TENDER", "SC TO-T/A": "TENDER/A",
    "SC TO-I": "TENDER-SELF", "SC TO-I/A": "TENDER-SELF/A",
    "SC 14D9": "14D9", "SC 14D9/A": "14D9/A",
    "SC 13E3": "GOING-PRIVATE", "SC 13E3/A": "GOING-PRIVATE/A",
    "DEFM14A": "MERGER-PROXY", "PREM14A": "MERGER-PROXY",
    "S-4": "MERGER-REG", "425": "MERGER-COMM",
}
SQUELCH_FORMS = {"MERGER-COMM", "TENDER/A", "TENDER-SELF/A", "14D9/A", "GOING-PRIVATE/A", "MERGER-REG"}

# FCA NSM headline type codes
NSM_EVENT_CODES = {
    "HOL": "TR-1", "OFB": "OFFER-BY", "OFD": "POSSIBLE-OFFER", "OFF": "OFFER-FOR",
    "ORE": "OFFER-REJECTED", "OUP": "OFFER-UPDATE", "POT": "PANEL-STATEMENT",
    "CAS": "COMPULSORY-ACQ", "TEN": "TENDER", "RTE": "TENDER-RESULT",
    "CAR": "CAPITAL-REORG", "ACQ": "ACQUISITION", "DIS": "DISPOSAL",
}
NSM_OFFER_CODES = {"RET", "DCC", "FEE", "FEO", "FER"}  # Form 8.x dealing disclosures → offer-period flag


def http(url, data=None, hdrs=None, timeout=25, retries=1, cap=None):
    for att in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(hdrs or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(cap) if cap else r.read()
        except Exception:
            if att >= retries:
                return None
            time.sleep(1.5)


def jget(url, **kw):
    b = http(url, **kw)
    if b is None:
        return None
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return None


def nsm_query(body):
    b = http("https://api.data.fca.org.uk/search?index=nsm-search",
             data=json.dumps(body).encode(), hdrs={"Content-Type": "application/json"})
    if b is None:
        return None
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return None


def flatten_html(raw):
    raw = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", raw)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t)


DEAL_WORDS = re.compile(
    r"\b(merger|acquisition|acquire[ds]?|business combination|tender offer|"
    r"purchase agreement|definitive agreement|chapter 11|bankruptcy|"
    r"reorganization|scheme of arrangement|take-?over|going.private|divest|"
    r"combine[ds]? with|all.(?:cash|stock) (?:deal|transaction)|offer to exchange)", re.I)
BOILER = re.compile(
    r"(incorporated (?:herein )?by reference|as defined (?:below|herein)|"
    r"securities and exchange commission|washington,? d\.?c|check the appropriate|"
    r"pursuant to (?:section|rule) \d|commission file|cusip|table of contents|"
    r"forward-looking statements)", re.I)


def snippet(text, prefer_deal_words=True, limit=250):
    """Best-effort one/two informative sentences out of a filing's text."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", text)
    # abbreviation splits ("Inc. (“Amazon”), Grapefruit…") leave fragments that
    # start with a parenthetical — require a proper sentence opening
    cands = [x.strip() for x in sents
             if 40 <= len(x.strip()) <= 500 and not BOILER.search(x)
             and re.match(r"^[A-Z“\"'0-9]", x.strip())
             and ".htm" not in x and ".txt" not in x]  # SGML/EDGAR header lines
    pick = None
    if prefer_deal_words:
        # prefer sentences that SAY what happened over cross-references to annexes
        act = re.compile(r"(entered into|announce[ds]?|agreed to|approved|will acquire|"
                         r"to acquire|acquisition of|merger of|combination of|offer (?:for|to)|"
                         r"completed|has acquired|filed for)", re.I)
        deref = re.compile(r"(copy of|attached as|annex|accompanying|more fully described|"
                           r"you may|form of merger consideration|proration)", re.I)
        best_s = -9
        for x in cands:
            if not DEAL_WORDS.search(x):
                continue
            sc = (2 if act.search(x) else 0) - (3 if deref.search(x) else 0)
            if sc > best_s:
                best_s, pick = sc, x
    if pick is None:
        pick = cands[0] if cands else None
    if not pick:
        return None
    return (pick[:limit - 1] + "…") if len(pick) > limit else pick


MONEY = r"(?:€|£|\$|US\$) ?\d[\d.,]*(?: ?(?:billion|million))?"


def extract_terms(text):
    """Deal economics out of filing prose: per-share price, headline value,
    exchange ratio. Context-filtered — 'par value $0.0001 per share' is
    boilerplate on every US filing, never a deal term."""
    out = []

    def add(x):
        x = re.sub(r"\s+", " ", x).strip(" ,.;:")
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x)

    # explicit per-share consideration
    for m in re.finditer(rf"({MONEY})(?: in cash)?(?:,? without interest,?)? (?:per|for each) (?:share|ADS|ordinary share)", text):
        ctx = text[max(0, m.start() - 45):m.start()].lower()
        try:
            v = float(re.sub(r"[^\d.]", "", m.group(1).replace(",", "")))
        except ValueError:
            v = 0
        if re.search(r"par value|nominal value|liquidation preference|redemption price|preferred stock|principal amount", ctx) or v < 0.05:
            continue
        add(m.group(0))
        if len(out) >= 2:
            break
    for m in re.finditer(r"\d[\d.]* pence (?:in cash )?per share", text):
        add(m.group(0))
        break
    # "consideration: $90.00 in cash" style (merger consideration clauses)
    if not out:
        for m in re.finditer(rf"({MONEY}) in cash", text):
            ctx = text[max(0, m.start() - 90):m.end() + 60].lower()
            if re.search(r"consideration|each share|per share|holders? of", ctx) and "par value" not in ctx:
                add(m.group(0))
                break
    # headline transaction value
    for m in re.finditer(rf"(?:consideration of (?:up to )?|purchase price of |offer price of |transaction valued? (?:at|of) |valued at |enterprise value of |equity value of |aggregate value of )(?:approximately |about )?({MONEY})", text, re.I):
        add(m.group(0))
        break
    else:
        for m in re.finditer(rf"(?:approximately|about) ({MONEY})", text):
            if not re.search(r"billion|million", m.group(1)):
                continue
            ctx = text[max(0, m.start() - 90):m.end() + 90].lower()
            if re.search(r"transaction|acquisition|merger|deal|consideration|offer", ctx) and "revenue" not in ctx:
                add(m.group(0))
                break
    # exchange ratio
    m = re.search(rf"exchange ratio represented (?:approximately )?({MONEY})", text)
    if m:
        add(m.group(0))
    else:
        m = re.search(r"exchange ratio of (?:approximately )?\d?\.\d{2,4}\b(?: shares?[a-zA-Z .]{0,40})?", text)
        if m:
            add(m.group(0))
    return (" · ".join(out)[:170]) or None


def sig_for(form, pct, delta, was_13g):
    """Significance 0-100. Deal events top, activist stakes next, passive churn bottom."""
    if form in ("TENDER", "TENDER-SELF", "14D9", "GOING-PRIVATE", "OFFER-BY", "OFFER-FOR",
                "COMPULSORY-ACQ", "BANKRUPTCY", "TENDER-RESULT", "MERGER-AGREEMENT"):
        return 95
    if form in ("POSSIBLE-OFFER", "OFFER-REJECTED", "OFFER-UPDATE", "PANEL-STATEMENT"):
        return 85
    if form in ("MERGER-PROXY", "MERGER-REG", "MERGER-COMM",
                "TENDER/A", "TENDER-SELF/A", "14D9/A", "GOING-PRIVATE/A"):
        return 80
    if form == "COMPLETED-ACQ":
        return 70
    if form == "DEBT-EXCHANGE":
        return 35
    if form == "CAPITAL-REORG":
        return 65
    if form in ("ACQUISITION", "DISPOSAL"):
        return 60
    if form == "13D":
        s = 90 if was_13g else 80  # passive holder turning active is the classic signal
        return min(95, s + (5 if (pct or 0) >= 10 else 0))
    if form == "13D/A":
        return 55 + (10 if abs(delta or 0) >= 2 else 0)
    if form == "13G":
        return 45 + (10 if (pct or 0) >= 10 else 0)
    if form == "13G/A":
        s = 25 + (15 if abs(delta or 0) >= 2 else 0)
        return s + (10 if (pct or 0) >= 10 and (delta or 0) > 0 else 0)
    if form == "TR-1":
        s = 35 + (10 if (pct or 0) >= 5 else 0) + (10 if (pct or 0) >= 10 else 0)
        return s + (5 if abs(delta or 0) >= 1 else 0)
    return 30


def parse_13dg_xml(raw):
    """Structured Schedule 13D/G primary_doc.xml → (filer, pct, rule, issuerCik,
    issuerName). Takes the reporting person with the largest percent of class.
    The issuer fields matter: a company's EDGAR feed also contains filings the
    company itself made ABOUT other issuers (e.g. United's 13D on AZUL) — the
    caller compares issuerCik with the company's own CIK to set direction."""
    txt = raw.decode("utf-8", "replace")
    best = (None, None, None)
    # 13G schema wraps persons in coverPageHeaderReportingPersonDetails/classPercent;
    # 13D uses reportingPersonInfo/percentOfClass — handle both
    for m in re.finditer(r"<(?:coverPageHeaderReportingPersonDetails|reportingPersonInfo)>(.*?)</(?:coverPageHeaderReportingPersonDetails|reportingPersonInfo)>", txt, re.S):
        blk = m.group(1)
        nm = re.search(r"<reportingPersonName>(.*?)</reportingPersonName>", blk, re.S)
        pc = re.search(r"<(?:classPercent|percentOfClass)>\s*([\d]+(?:\.\d+)?)", blk)  # content may carry trailing prose
        pct = float(pc.group(1)) if pc else None
        sh = re.search(r"<(?:reportingPersonBeneficiallyOwnedAggregateNumberOfShares|aggregateAmountOwned)>\s*(\d+)", blk)
        if best[1] is None or (pct is not None and pct > (best[1] or -1)):
            best = (nm.group(1).strip() if nm else None, pct, int(sh.group(1)) if sh else None)
    rule = re.search(r"<designateRulePursuantThisScheduleFiled>(.*?)</designateRulePursuantThisScheduleFiled>", txt)
    icik = re.search(r"<issuerCik>0*(\d+)</issuerCik>", txt, re.I)
    inm = re.search(r"<issuerName>(.*?)</issuerName>", txt, re.S)
    # 13D structured XML carries the Item 4 "purpose of transaction" prose —
    # the single most descriptive field a stake filing has
    purp = re.search(r"<transactionPurpose>(.*?)</transactionPurpose>", txt, re.S)
    note = snippet(htmllib.unescape(purp.group(1))) if purp else None
    unesc = lambda v: htmllib.unescape(v.strip()) if v else None  # XML carries &amp; etc.
    return (unesc(best[0]), best[1], (rule.group(1).strip() if rule else None),
            (int(icik.group(1)) if icik else None), unesc(inm.group(1) if inm else None), note, best[2])


def parse_form4(raw):
    """Structured Form 4 (ownershipDocument XML) -> one summarized trade:
    who, role, dominant transaction code, direction, total shares, weighted
    average price, shares owned after. Non-derivative table only (the
    derivative table is options plumbing; its presence is flagged)."""
    txt = raw.decode("utf-8", "replace")
    name = re.search(r"<rptOwnerName>(.*?)</rptOwnerName>", txt)
    owners = re.findall(r"<rptOwnerName>(.*?)</rptOwnerName>", txt)
    title = re.search(r"<officerTitle>(.*?)</officerTitle>", txt)
    role = []
    if re.search(r"<isDirector>(1|true)</isDirector>", txt): role.append("Director")
    if re.search(r"<isOfficer>(1|true)</isOfficer>", txt):
        role.append(htmllib.unescape(title.group(1).strip()) if title else "Officer")
    if re.search(r"<isTenPercentOwner>(1|true)</isTenPercentOwner>", txt): role.append("10% owner")
    by_code = {}
    after = None
    for m in re.finditer(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", txt, re.S):
        b = m.group(1)
        g = lambda t: (re.search(rf"<{t}>\s*<value>\s*([^<]*?)\s*</value>", b, re.S)
                       or re.search(rf"<{t}>\s*([^<]*?)\s*</{t}>", b, re.S))
        code = g("transactionCode"); ad = g("transactionAcquiredDisposedCode")
        sh = g("transactionShares"); px = g("transactionPricePerShare")
        aft = g("sharesOwnedFollowingTransaction")
        if not code: continue
        try: shv = float(sh.group(1)) if sh else 0.0
        except ValueError: shv = 0.0
        try: pxv = float(px.group(1)) if px and px.group(1) else None
        except ValueError: pxv = None
        k = (code.group(1).strip(), (ad.group(1).strip() if ad else ""))
        e = by_code.setdefault(k, {"sh": 0.0, "val": 0.0, "pxsh": 0.0})
        e["sh"] += shv
        if pxv is not None:
            e["val"] += shv * pxv; e["pxsh"] += shv
        if aft:
            try: after = int(float(aft.group(1)))
            except ValueError: pass
    if not by_code:
        return None
    (code, ad), agg = max(by_code.items(), key=lambda kv: kv[1]["val"] or kv[1]["sh"])
    px = round(agg["val"] / agg["pxsh"], 2) if agg["pxsh"] else None
    dirn = "buy" if (code == "P" or (ad == "A" and code not in ("M", "A", "G"))) else \
           "sell" if (code == "S" or (ad == "D" and code == "F")) else "other"
    who = htmllib.unescape(owners[0].strip()) if owners else None
    if who and len(owners) > 1:
        who += f" +{len(owners)-1}"
    return {"name": who, "role": ", ".join(role) or None, "code": code, "dir": dirn,
            "sh": int(agg["sh"]), "px": px, "after": after,
            "deriv": "<derivativeTransaction>" in txt}


def efts_parties(acc):
    """All filing parties for an accession via EDGAR full-text search —
    display_names carries filer AND subject entities."""
    j = jget('https://efts.sec.gov/LATEST/search-index?q=%22' + acc + '%22')
    names = []
    for h in (j or {}).get("hits", {}).get("hits", []):
        for dn in h.get("_source", {}).get("display_names", []) or []:
            n = re.sub(r"\s*\(CIK[^)]*\)", "", re.sub(r"\s*\(([A-Z0-9.,\- ]{1,25})\)\s*(?=\()", " ", dn)).strip()
            if n and n not in names:
                names.append(n)
    return names[:4] or None


def parse_tr1(raw):


    """TR-1 Holding(s) in Company HTML → (holder, resulting_pct, prev_pct). Best effort."""
    t = flatten_html(raw.decode("utf-8", "replace"))
    holder = None
    # two RNS template wordings: "Full name of person(s) subject to the
    # notification obligation iii : FMR LLC 3. ..." and "Details of person
    # subject to the notification obligation Name The Capital Group Companies,
    # Inc. City of registered office ..."
    m = re.search(r"subject to the notification obligation\s*(?:i+[iv]*\b)?\s*:?\s*(?:Name\b\s*)?(.{3,90}?)\s+(?:City of|Country of|[34]\.\s|Full name of shareholder)", t, re.I)
    if m:
        holder = m.group(1).strip(" :;.,")
        if re.match(r"^(if |the person|name$|above)", holder, re.I) or len(holder) < 3:
            holder = None

    def floats_after(anchor, window):
        # anchors chosen so the DTR5.1/DTR5.2.1 rule references further down
        # the form can't be mistaken for percentages
        i = t.lower().find(anchor.lower())
        if i < 0:
            return []
        return [float(x) for x in re.findall(r"(\d{1,3}\.\d{1,6})\b", t[i:i + window])[:3]]

    def total_from(vals):
        # columns: shares %, instruments %, total of both % — take the 3rd, else 1st
        for v in (vals[2:3] or vals[:1]):
            if 0 < v <= 100:
                return v
        return None

    res = total_from(floats_after("resulting situation on the date", 300))
    prev = total_from(floats_after("position of previous notification", 160))
    shares = None
    i = t.lower().find("resulting situation on the date")
    if i >= 0:
        m = re.search(r"(?<![\d.])(\d{5,13})(?![\d.])", t[i:i + 320])  # total voting rights held (not a float's decimals)
        if m:
            shares = int(m.group(1))
    return holder, res, prev, shares


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"byTicker": {}, "leis": {}}


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    state_path = os.path.join(STATE_DIR, "stakes-state.json")
    state = load_state(state_path)
    by = state.setdefault("byTicker", {})
    leis = state.setdefault("leis", {})

    with open(os.path.join(STATE_DIR, "universe.json")) as f:
        uni = json.load(f)
    names, fin_rows = {}, set()
    try:
        with open(os.path.join(STATE_DIR, "last-data.json")) as f:
            for r in json.load(f):
                names[r.get("ticker")] = r.get("name")
                # asset managers / banks / insurers file outward 13Gs on their whole
                # portfolio and dealer books — that churn is business-as-usual, not
                # strategic interest, so their outward PASSIVE events are dropped
                # (outward 13Ds stay: activist intent is notable from anyone)
                if r.get("sector") in ("Financial Services", "Banking", "Insurance", "Asset Management"):
                    fin_rows.add(r.get("ticker"))
    except Exception:
        pass

    # rows: (dashboard row key, EDGAR symbol, NSM company name or None)
    rows = [(t, t, None) for t in uni.get("us", [])]
    for p in uni.get("europe", []):
        nat, adr = p["ticker"], p.get("adr") or p["ticker"]
        rows.append((nat, adr, names.get(nat) if nat.endswith(".L") else None))

    docs_left = DOC_CAP
    trades_left = TRADE_CAP
    efts_left = EFTS_CAP
    ev_new = xml_ok = tr_new = 0
    PARTY_FORMS = {"MERGER-AGREEMENT", "TENDER", "TENDER-SELF", "14D9", "MERGER-REG",
                   "MERGER-COMM", "MERGER-PROXY", "GOING-PRIVATE", "COMPLETED-ACQ"}

    # ---- EDGAR ----
    cikmap = {}
    ct = jget("https://www.sec.gov/files/company_tickers.json")
    if ct:
        for v in ct.values():
            cikmap.setdefault(v["ticker"].upper(), v["cik_str"])
    else:
        print("WARN: EDGAR ticker map unavailable; skipping EDGAR side")

    for rowkey, esym, _ in rows:
        cik = cikmap.get(esym.upper().replace(".", "-"))
        ent = by.setdefault(rowkey, {"events": []})
        ent["cik"] = cik
        if not cik:
            continue
        # "seen" is the durable dedupe set (2026-08-15): membership used to be
        # only the STORED events, so anything pruned by the per-ticker cap was
        # re-ingested as "new" the next day — a churn loop that re-added ~700
        # events/run and burned the whole doc budget before enrichment could
        # touch anything. seen remembers every accession ever processed.
        seen = set(ent.get("seen") or []) | {e["id"] for e in ent["events"]}
        known = seen
        seeded = bool(ent.get("lastFetch"))
        time.sleep(0.13)
        sub = jget(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if not sub:
            continue
        r = sub.get("filings", {}).get("recent", {})
        forms, dates, accs, docs = r.get("form", []), r.get("filingDate", []), r.get("accessionNumber", []), r.get("primaryDocument", [])
        items = r.get("items", [""] * len(forms))
        accepts = r.get("acceptanceDateTime", [""] * len(forms))
        seenT = set(ent.get("seenT") or [])
        trades = ent.setdefault("trades", [])
        for i in range(len(forms)):
            f, d = forms[i], dates[i]
            if f == "4":
                # insider transactions (2026-08-15): structured ownershipDocument
                # XML — who, role, code, direction, shares, price, held-after
                if (today - datetime.date.fromisoformat(d)).days > TRADE_WINDOW_D:
                    continue
                acc4 = accs[i]
                if acc4 in seenT or trades_left <= 0 or not docs[i]:
                    continue
                trades_left -= 1
                time.sleep(0.11)
                nod4 = acc4.replace("-", "")
                raw4 = http(f"https://www.sec.gov/Archives/edgar/data/{cik}/{nod4}/{docs[i].split('/')[-1]}")
                seenT.add(acc4)
                if raw4:
                    t4 = parse_form4(raw4)
                    if t4 and t4["sh"] > 0:
                        t4.update({"id": acc4, "d": d, "ts": (accepts[i] or "")[:16] or None,
                                   "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{nod4}/{docs[i]}"})
                        trades.append(t4)
                        tr_new += 1
                continue
            form = STAKE_FORMS.get(f) or DEAL_FORMS.get(f)
            if f == "8-K":
                it = items[i] or ""
                if "1.03" in it:
                    form = "BANKRUPTCY"
                elif "2.01" in it:
                    form = "COMPLETED-ACQ"
                elif "1.01" in it:
                    # material agreements: mostly credit facilities, but this is
                    # where MERGER AGREEMENTS are first announced — weeks before
                    # the S-4/proxy. Candidate until the document confirms it.
                    form = "AGREEMENT-CANDIDATE"
            if not form:
                continue
            window = PASSIVE_WINDOW_D if form == "13G/A" else FULL_WINDOW_D
            if not seeded and (today - datetime.date.fromisoformat(d)).days > window:
                continue
            if seeded and (today - datetime.date.fromisoformat(d)).days > 45:
                break  # feed is date-sorted; nothing older is new
            acc = accs[i]
            if acc in known:
                continue
            if form in SQUELCH_FORMS and any(
                    e["form"] == form and abs((datetime.date.fromisoformat(e["d"]) - datetime.date.fromisoformat(d)).days) <= SQUELCH_D
                    for e in ent["events"]):
                seen.add(acc)
                continue
            nodash = acc.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{docs[i]}" if docs[i] else \
                  f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/"
            filer = pct = rule = subject = note = shares = terms = None
            outward = False
            if form in ("13D", "13D/A", "13G", "13G/A") and f.startswith("SCHEDULE") and docs_left > 0:
                docs_left -= 1
                time.sleep(0.13)
                raw = http(f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/primary_doc.xml")
                if raw:
                    filer, pct, rule, icik, inm, note, shares = parse_13dg_xml(raw)
                    xml_ok += 1
                    if icik and icik != cik:  # company is the FILER: stake in another firm
                        outward, subject = True, inm
                        if form in ("13G", "13G/A") and rowkey in fin_rows:
                            continue  # financial-sector portfolio churn
            elif form not in ("13G", "13G/A") and docs[i] and docs_left > 0:
                # deal forms + 8-K events: pull a descriptive sentence + deal
                # economics from the document itself (capped read — S-4s run to
                # megabytes, the deal is described up front)
                docs_left -= 1
                time.sleep(0.13)
                raw = http(url, cap=200_000)
                if raw:
                    _txt = flatten_html(raw.decode("utf-8", "replace"))
                    note = snippet(_txt)
                    terms = extract_terms(_txt)
                    if form == "AGREEMENT-CANDIDATE":
                        if re.search(r"agreement and plan of merger|definitive (?:merger|acquisition) agreement|agreement to acquire|to be acquired by|business combination agreement", _txt, re.I):
                            form = "MERGER-AGREEMENT"
                        else:
                            seen.add(acc)  # ordinary credit/commercial agreement — never an event
                            continue
                # S-4s also register routine debt exchange offers — don't let
                # refinancing masquerade as M&A in the feed
                if form == "MERGER-REG" and note and re.search(r"notes due|aggregate principal amount|exchange.{0,40}notes", note, re.I):
                    form = "DEBT-EXCHANGE"
            if form == "AGREEMENT-CANDIDATE":
                continue  # doc budget exhausted or fetch failed — retry while in scan window
            prev = next((e.get("pct") for e in ent["events"]
                         if e.get("filer") and filer and e["filer"].lower() == filer.lower()
                         and e.get("pct") is not None
                         and bool(e.get("subject")) == outward
                         and (not outward or e.get("subject") == subject)), None)
            delta = (pct - prev) if (pct is not None and prev is not None) else None
            was_13g = form == "13D" and not outward and filer and any(
                e["form"].startswith("13G") and not e.get("subject")
                and (e.get("filer") or "").lower() == filer.lower()
                for e in ent["events"])
            ev = {
                "id": acc, "d": d, "src": "edgar", "form": form, "filer": filer,
                "ts": (accepts[i] or "")[:16] or None,
                "pct": pct, "prevPct": prev, "rule": rule, "note": note,
                "shares": shares, "terms": terms,
                "sig": sig_for(form, pct, delta, was_13g), "url": url,
            }
            if outward:
                ev["subject"] = subject
                # corporate stake in another firm — strategic, often an M&A precursor
                ev["sig"] = {"13D": 80, "13D/A": 60, "13G": 55, "13G/A": 35}.get(form, 50)
            if form in PARTY_FORMS and efts_left > 0:
                efts_left -= 1
                time.sleep(0.25)
                ev["parties"] = efts_parties(acc)
            ent["events"].append(ev)
            seen.add(acc)
            ev_new += 1
        ent["seen"] = sorted(seen)[-800:]  # durable dedupe memory, bounded
        ent["seenT"] = sorted(seenT)[-1500:]
        ent["lastFetch"] = now.isoformat()

    # ---- FCA NSM (London rows) ----
    all_codes = list(NSM_EVENT_CODES) + sorted(NSM_OFFER_CODES)
    for rowkey, _, nm in rows:
        if not nm:
            continue
        ent = by.setdefault(rowkey, {"events": []})
        lei = leis.get(rowkey)
        if not lei:
            time.sleep(0.25)
            res = nsm_query({"from": 0, "size": 25, "sort": "submitted_date", "sortorder": "desc",
                             "keyword": re.sub(r"\b(plc|p\.l\.c\.|group|holdings|the)\b", "", nm, flags=re.I).strip(),
                             "criteriaObj": {"criteria": [], "dateCriteria": []}})
            counts = {}
            tok = nm.split()[0].lower()
            for h in (res or {}).get("hits", {}).get("hits", []):
                s = h["_source"]
                if tok in (s.get("company") or "").lower() and s.get("lei"):
                    counts[s["lei"]] = counts.get(s["lei"], 0) + 1
            if counts:
                lei = max(counts, key=counts.get)
                leis[rowkey] = lei
        if not lei:
            continue
        known = {e["id"] for e in ent["events"]}
        time.sleep(0.25)
        res = nsm_query({"from": 0, "size": 80, "sort": "submitted_date", "sortorder": "desc",
                         "criteriaObj": {"criteria": [
                             {"name": "company_lei", "value": ["", lei, "disclose_org", "related_org"]},
                             {"name": "type_code", "value": all_codes}], "dateCriteria": []}})
        if not res:
            continue
        offer_last, offer_n = None, 0
        for h in res.get("hits", {}).get("hits", []):
            s = h["_source"]
            code = s.get("type_code")
            d = (s.get("publication_date") or s.get("submitted_date") or "")[:10]
            if not d:
                continue
            age = (today - datetime.date.fromisoformat(d)).days
            if code in NSM_OFFER_CODES:
                if age <= 45:
                    offer_n += 1
                    offer_last = max(offer_last or d, d)
                continue
            form = NSM_EVENT_CODES.get(code)
            if not form or age > FULL_WINDOW_D:
                continue
            eid = s.get("disclosure_id") or s.get("seq_id") or h.get("_id")
            if eid in known:
                continue
            url = "https://data.fca.org.uk/artefacts/" + s.get("download_link", "")
            filer = pct = prev = note = shares = terms = None
            if docs_left > 0 and s.get("download_link"):
                docs_left -= 1
                time.sleep(0.2)
                raw = http(url, cap=200_000)
                if raw and form == "TR-1":
                    filer, pct, prev, shares = parse_tr1(raw)
                elif raw:
                    _t = flatten_html(raw.decode("utf-8", "replace"))
                    # RNS docs repeat the headline right before the body — cut
                    # the NSM/RNS-number header boilerplate by starting there
                    _hl = (s.get("headline") or "").strip()
                    _i = _t.find(_hl) if _hl else -1
                    _t = _t[_i + len(_hl):] if _i >= 0 else _t
                    note = snippet(_t)
                    terms = extract_terms(_t)
            delta = (pct - prev) if (pct is not None and prev is not None) else None
            ent["events"].append({
                "id": eid, "d": d, "src": "nsm", "form": form, "filer": filer,
                "pct": pct, "prevPct": prev, "hl": (s.get("headline") or "")[:120], "note": note,
                "shares": shares, "terms": terms,
                "sig": sig_for(form, pct, delta, False), "url": url,
            })
            ev_new += 1
        ent["offer"] = {"last": offer_last, "n45": offer_n} if offer_n else None

    # ---- enrichment repair (2026-08-15) ----
    # Spend any leftover doc budget on stored stake events that never got
    # their structured XML read (doc-cap exhaustion during the churn-loop era,
    # or transient SEC throttling). Newest first — those are what users see.
    _repair = [(t, e) for t, ent2 in by.items() for e in ent2["events"]
               if e.get("src") == "edgar" and e["form"] in ("13D", "13D/A", "13G", "13G/A")
               and e.get("filer") is None and "primary_doc.xml" in (e.get("url") or "")]
    _repair.sort(key=lambda x: x[1]["d"], reverse=True)
    rep_ok = 0
    for t, e in _repair:
        if docs_left <= 0:
            break
        docs_left -= 1
        time.sleep(0.13)
        raw = http(re.sub(r"xsl[^/]+/", "", e["url"]))
        if not raw:
            continue
        filer, pct, rule, icik, inm, note, shares = parse_13dg_xml(raw)
        if filer is None and pct is None:
            continue
        e["filer"], e["pct"], e["rule"], e["shares"] = filer, pct, rule, shares
        if note and not e.get("note"):
            e["note"] = note
        cik2 = by[t].get("cik")
        if icik and cik2 and icik != cik2:  # outward: company's own stake elsewhere
            e["subject"] = inm
            if e["form"] in ("13G", "13G/A") and t in fin_rows:
                e["_drop"] = True  # financial-sector portfolio churn
            else:
                e["sig"] = {"13D": 80, "13D/A": 60, "13G": 55, "13G/A": 35}.get(e["form"], 50)
        else:
            e["sig"] = sig_for(e["form"], pct, None, False)
        rep_ok += 1
    for ent2 in by.values():
        ent2["events"] = [e for e in ent2["events"] if not e.get("_drop")]
    # counterparty repair: EFTS indexing lags fresh filings a little, and
    # events created before 2026-08-15 never had a lookup — newest first
    _prep = [e for ent2 in by.values() for e in ent2["events"]
             if e.get("src") == "edgar" and e["form"] in PARTY_FORMS and "parties" not in e]
    _prep.sort(key=lambda e: e["d"], reverse=True)
    for e in _prep:
        if efts_left <= 0:
            break
        efts_left -= 1
        time.sleep(0.25)
        p = efts_parties(e["id"])
        if p:
            e["parties"] = p

    # prune + cap
    for t, ent in by.items():
        evs = [e for e in ent["events"]
               if (today - datetime.date.fromisoformat(e["d"])).days <=
               (PASSIVE_WINDOW_D if e["form"] == "13G/A" else FULL_WINDOW_D)]
        evs.sort(key=lambda e: (e["d"], e.get("sig", 0)), reverse=True)
        ent["events"] = evs[:EVENT_CAP]
    for t, ent in by.items():
        trs = [x for x in ent.get("trades") or []
               if (today - datetime.date.fromisoformat(x["d"])).days <= TRADE_WINDOW_D]
        trs.sort(key=lambda x: x["d"], reverse=True)
        ent["trades"] = trs[:TRADE_STORE_CAP]
    live = {r[0] for r in rows}
    for t in list(by):
        if t not in live:
            del by[t]

    state["updatedAt"] = now.isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    total = sum(len(e["events"]) for e in by.values())
    total_tr = sum(len(e.get("trades") or []) for e in by.values())
    print(f"stakes: {ev_new} new events this run ({rep_ok} repaired), {tr_new} new insider trades ({total_tr} stored), {total} stored across "
          f"{sum(1 for e in by.values() if e['events'])} tickers; "
          f"{xml_ok} structured 13D/G parsed, {DOC_CAP - docs_left} docs fetched, "
          f"{len(leis)} LEIs cached")


if __name__ == "__main__":
    main()
