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
import json
import os
import re
import time
import urllib.request

STATE_DIR = os.environ.get("STATE_DIR", "state")
DOC_CAP = int(os.environ.get("DOC_CAP", "60"))
UA = "ValueTally stake-monitor (contact@valuetally.com)"

FULL_WINDOW_D = 550     # deal forms + 13D
PASSIVE_WINDOW_D = 180  # 13G/A institutional churn
EVENT_CAP = 60          # per ticker
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


def http(url, data=None, hdrs=None, timeout=25, retries=1):
    for att in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(hdrs or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
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
    t = re.sub(r"<[^>]+>", " ", raw)
    t = t.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#160;", " ")
    return re.sub(r"\s+", " ", t)


def sig_for(form, pct, delta, was_13g):
    """Significance 0-100. Deal events top, activist stakes next, passive churn bottom."""
    if form in ("TENDER", "TENDER-SELF", "14D9", "GOING-PRIVATE", "OFFER-BY", "OFFER-FOR",
                "COMPULSORY-ACQ", "BANKRUPTCY", "TENDER-RESULT"):
        return 95
    if form in ("POSSIBLE-OFFER", "OFFER-REJECTED", "OFFER-UPDATE", "PANEL-STATEMENT"):
        return 85
    if form in ("MERGER-PROXY", "MERGER-REG", "MERGER-COMM",
                "TENDER/A", "TENDER-SELF/A", "14D9/A", "GOING-PRIVATE/A"):
        return 80
    if form == "COMPLETED-ACQ":
        return 70
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
    best = (None, None)
    # 13G schema wraps persons in coverPageHeaderReportingPersonDetails/classPercent;
    # 13D uses reportingPersonInfo/percentOfClass — handle both
    for m in re.finditer(r"<(?:coverPageHeaderReportingPersonDetails|reportingPersonInfo)>(.*?)</(?:coverPageHeaderReportingPersonDetails|reportingPersonInfo)>", txt, re.S):
        blk = m.group(1)
        nm = re.search(r"<reportingPersonName>(.*?)</reportingPersonName>", blk, re.S)
        pc = re.search(r"<(?:classPercent|percentOfClass)>\s*([\d]+(?:\.\d+)?)", blk)  # content may carry trailing prose
        pct = float(pc.group(1)) if pc else None
        if best[1] is None or (pct is not None and pct > (best[1] or -1)):
            best = (nm.group(1).strip() if nm else None, pct)
    rule = re.search(r"<designateRulePursuantThisScheduleFiled>(.*?)</designateRulePursuantThisScheduleFiled>", txt)
    icik = re.search(r"<issuerCik>0*(\d+)</issuerCik>", txt, re.I)
    inm = re.search(r"<issuerName>(.*?)</issuerName>", txt, re.S)
    return (best[0], best[1], (rule.group(1).strip() if rule else None),
            (int(icik.group(1)) if icik else None), (inm.group(1).strip() if inm else None))


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
    return holder, res, prev


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
    names = {}
    try:
        with open(os.path.join(STATE_DIR, "last-data.json")) as f:
            for r in json.load(f):
                names[r.get("ticker")] = r.get("name")
    except Exception:
        pass

    # rows: (dashboard row key, EDGAR symbol, NSM company name or None)
    rows = [(t, t, None) for t in uni.get("us", [])]
    for p in uni.get("europe", []):
        nat, adr = p["ticker"], p.get("adr") or p["ticker"]
        rows.append((nat, adr, names.get(nat) if nat.endswith(".L") else None))

    docs_left = DOC_CAP
    ev_new = xml_ok = 0

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
        known = {e["id"] for e in ent["events"]}
        seeded = bool(ent.get("lastFetch"))
        time.sleep(0.13)
        sub = jget(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if not sub:
            continue
        r = sub.get("filings", {}).get("recent", {})
        forms, dates, accs, docs = r.get("form", []), r.get("filingDate", []), r.get("accessionNumber", []), r.get("primaryDocument", [])
        items = r.get("items", [""] * len(forms))
        for i in range(len(forms)):
            f, d = forms[i], dates[i]
            form = STAKE_FORMS.get(f) or DEAL_FORMS.get(f)
            if f == "8-K":
                it = items[i] or ""
                if "1.03" in it:
                    form = "BANKRUPTCY"
                elif "2.01" in it:
                    form = "COMPLETED-ACQ"
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
                    e["form"] == form and (today - datetime.date.fromisoformat(e["d"])).days <= SQUELCH_D
                    for e in ent["events"]):
                continue
            nodash = acc.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{docs[i]}" if docs[i] else \
                  f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/"
            filer = pct = rule = subject = None
            outward = False
            if form in ("13D", "13D/A", "13G", "13G/A") and f.startswith("SCHEDULE") and docs_left > 0:
                docs_left -= 1
                time.sleep(0.13)
                raw = http(f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/primary_doc.xml")
                if raw:
                    filer, pct, rule, icik, inm = parse_13dg_xml(raw)
                    xml_ok += 1
                    if icik and icik != cik:  # company is the FILER: stake in another firm
                        outward, subject = True, inm
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
                "pct": pct, "prevPct": prev, "rule": rule,
                "sig": sig_for(form, pct, delta, was_13g), "url": url,
            }
            if outward:
                ev["subject"] = subject
                # corporate stake in another firm — strategic, often an M&A precursor
                ev["sig"] = {"13D": 80, "13D/A": 60, "13G": 55, "13G/A": 35}.get(form, 50)
            ent["events"].append(ev)
            ev_new += 1
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
            filer = pct = prev = None
            if form == "TR-1" and docs_left > 0 and s.get("download_link"):
                docs_left -= 1
                time.sleep(0.2)
                raw = http(url)
                if raw:
                    filer, pct, prev = parse_tr1(raw)
            delta = (pct - prev) if (pct is not None and prev is not None) else None
            ent["events"].append({
                "id": eid, "d": d, "src": "nsm", "form": form, "filer": filer,
                "pct": pct, "prevPct": prev, "hl": (s.get("headline") or "")[:120],
                "sig": sig_for(form, pct, delta, False), "url": url,
            })
            ev_new += 1
        ent["offer"] = {"last": offer_last, "n45": offer_n} if offer_n else None

    # prune + cap
    for t, ent in by.items():
        evs = [e for e in ent["events"]
               if (today - datetime.date.fromisoformat(e["d"])).days <=
               (PASSIVE_WINDOW_D if e["form"] == "13G/A" else FULL_WINDOW_D)]
        evs.sort(key=lambda e: (e["d"], e.get("sig", 0)), reverse=True)
        ent["events"] = evs[:EVENT_CAP]
    live = {r[0] for r in rows}
    for t in list(by):
        if t not in live:
            del by[t]

    state["updatedAt"] = now.isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    total = sum(len(e["events"]) for e in by.values())
    print(f"stakes: {ev_new} new events this run, {total} stored across "
          f"{sum(1 for e in by.values() if e['events'])} tickers; "
          f"{xml_ok} structured 13D/G parsed, {DOC_CAP - docs_left} docs fetched, "
          f"{len(leis)} LEIs cached")


if __name__ == "__main__":
    main()
