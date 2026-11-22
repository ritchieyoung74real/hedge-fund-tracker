#!/usr/bin/env python3
"""
Hedge Fund Tracker - data updater.

Downloads public SEC EDGAR filings (13F, Form 4, Schedule 13D/G) for the funds in
config.json, works out quarter-over-quarter changes, consensus moves and returns
since each filing date (USD and GBP), writes docs/data.js for the dashboard, and
sends WhatsApp alerts for big new moves if you have set that up.

Uses only the Python standard library: no installs needed.

    python3 tracker.py               update data (and send alerts if configured)
    python3 tracker.py --no-alerts   update data only
    python3 tracker.py --test-alert  send a test WhatsApp message and stop

Research tool only. Not financial advice.
"""
import argparse
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
DOCS = os.path.join(ROOT, "docs")
STATE_FILE = os.path.join(ROOT, "state.json")
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

CFG = {}
_last_sec = [0.0]


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- HTTP helpers

def http_get(url, ua=None, sec=False, retries=3, data=None, headers=None, timeout=40):
    if sec:  # SEC allows max 10 requests/second
        wait = 0.12 - (time.time() - _last_sec[0])
        if wait > 0:
            time.sleep(wait)
        _last_sec[0] = time.time()
    h = {"User-Agent": ua or CFG.get("user_agent", "HedgeFundTracker/1.0"),
         "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 400, 401):
                raise
            time.sleep(2 * (attempt + 1) + (8 if e.code == 429 else 0))
        except Exception as e:  # network hiccup
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def cached(name, fetch, max_age_hours=None):
    """Cache downloads on disk. max_age_hours=None means keep forever (filings never change)."""
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if max_age_hours is None or age_h < max_age_hours:
            with open(path, "rb") as f:
                return f.read()
    raw = fetch()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return raw


def sec_json(url, cache_name, max_age_hours=None):
    return json.loads(cached(cache_name, lambda: http_get(url, sec=True), max_age_hours))


def strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def txt(el, path, default=""):
    if el is None:
        return default
    f = el.find(path)
    return f.text.strip() if f is not None and f.text else default


def num(s, default=0.0):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return default


# ---------------------------------------------------------------- EDGAR

def submissions(cik):
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    return sec_json(url, f"submissions/{int(cik)}.json", max_age_hours=6)


def recent_filings(sub):
    r = sub.get("filings", {}).get("recent", {})
    keys = ["accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "acceptanceDateTime"]
    n = len(r.get("accessionNumber", []))
    return [{k: (r.get(k) or [""] * n)[i] for k in keys} for i in range(n)]


def find_cik_by_name(name):
    """Fallback if a CIK in config.json is wrong: search EDGAR for a 13F filer by name."""
    q = urllib.parse.urlencode({"action": "getcompany", "company": name, "type": "13F-HR",
                                "dateb": "", "owner": "include", "count": "10", "output": "atom"})
    try:
        raw = http_get("https://www.sec.gov/cgi-bin/browse-edgar?" + q, sec=True).decode("utf-8", "ignore")
    except Exception:
        return None
    m = re.search(r"<cik>(\d+)</cik>", raw) or re.search(r"CIK=(\d{6,10})", raw)
    return int(m.group(1)) if m else None


def filing_base(cik, acc):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"


def filing_index(cik, acc):
    return sec_json(filing_base(cik, acc) + "/index.json", f"filings/{acc}/index.json")


def filing_file(cik, acc, name):
    return cached(f"filings/{acc}/{name.replace('/', '_')}",
                  lambda: http_get(filing_base(cik, acc) + "/" + name, sec=True))


def parse_13f(cik, filing):
    """Return (holdings list, amendment_type) for one 13F-HR filing."""
    acc = filing["accessionNumber"]
    idx = filing_index(cik, acc)
    items = idx.get("directory", {}).get("item", [])
    xmls = [i["name"] for i in items if i["name"].lower().endswith(".xml")]
    amend_type = ""
    if "primary_doc.xml" in xmls:
        try:
            p = strip_ns(ET.fromstring(filing_file(cik, acc, "primary_doc.xml")))
            amend_type = (txt(p, ".//amendmentType") or "").upper()
        except Exception:
            pass
    rows = []
    for name in sorted(xmls, key=lambda n: n == "primary_doc.xml"):
        if name == "primary_doc.xml":
            continue
        try:
            root = strip_ns(ET.fromstring(filing_file(cik, acc, name)))
        except Exception:
            continue
        tables = root.findall(".//infoTable")
        if not tables:
            continue
        mult = 1000.0 if (filing.get("reportDate") or "9999") < "2022-12-31" else 1.0
        for t in tables:
            rows.append({
                "name": txt(t, "nameOfIssuer"),
                "cls": txt(t, "titleOfClass"),
                "cusip": txt(t, "cusip").upper(),
                "value": num(txt(t, "value")) * mult,
                "shares": num(txt(t, "shrsOrPrnAmt/sshPrnamt")),
                "type": txt(t, "shrsOrPrnAmt/sshPrnamtType") or "SH",
                "putcall": (txt(t, "putCall") or "").upper(),
            })
        break
    # Some filers (e.g. Duquesne, Baupost) still report values in thousands after the 2023 switch to
    # dollars. Detect via implied share price: a median under $1/share means the units are thousands.
    if rows and (filing.get("reportDate") or "9999") >= "2022-12-31":
        px = sorted(r["value"] / r["shares"] for r in rows
                    if r["type"] == "SH" and r["shares"] > 0 and r["value"] > 0 and not r["putcall"])
        if px and px[len(px) // 2] < 1.0:
            for r in rows:
                r["value"] *= 1000.0
    return rows, amend_type


def aggregate(rows):
    """Combine duplicate lines (same CUSIP + option type) that funds often report per sub-manager."""
    agg = {}
    for r in rows:
        key = r["cusip"] + ("|" + r["putcall"] if r["putcall"] else "")
        a = agg.get(key)
        if a is None:
            agg[key] = dict(r, key=key)
        else:
            a["value"] += r["value"]
            a["shares"] += r["shares"]
    return agg


def quarter_holdings(cik, filings, n_quarters):
    """Build holdings for the latest n quarters, applying 13F-HR/A amendments."""
    by_period = defaultdict(list)
    for f in filings:
        if f["form"] in ("13F-HR", "13F-HR/A") and f["reportDate"]:
            by_period[f["reportDate"]].append(f)
    periods = sorted(by_period, reverse=True)[:n_quarters]
    out = []
    for p in periods:
        all_rows, amended, bases = [], [], []
        # a fund can report under more than one filer CIK (filings tagged "_cik"); combine them per period
        for fcik in sorted({f.get("_cik", cik) for f in by_period[p]}):
            fs = sorted([f for f in by_period[p] if f.get("_cik", cik) == fcik], key=lambda f: f["filingDate"])
            originals = [f for f in fs if f["form"] == "13F-HR"]
            if not originals:
                continue
            base = originals[-1]
            rows, _ = parse_13f(fcik, base)
            if not rows:  # could not read the holdings table; skip rather than show everything as "new"
                log(f"  could not read holdings for {p} ({base['accessionNumber']})")
                continue
            for a in fs:
                if a["form"] != "13F-HR/A" or a["filingDate"] < base["filingDate"]:
                    continue
                arows, atype = parse_13f(fcik, a)
                if not arows:
                    continue
                if "RESTATEMENT" in atype:
                    rows = arows
                else:  # "NEW HOLDINGS": confidential positions revealed later (Berkshire does this)
                    rows = rows + arows
                    amended.append(a["filingDate"])
            all_rows += rows
            bases.append((fcik, base))
        if not bases:
            continue
        bcik, base = max(bases, key=lambda b: (b[0] == cik, b[1]["filingDate"]))
        out.append({"period": p, "filed": max(b["filingDate"] for _, b in bases), "accession": base["accessionNumber"],
                    "url": filing_base(bcik, base["accessionNumber"]) + "/",
                    "amended": amended, "holdings": aggregate(all_rows)})
    return out


# ---------------------------------------------------------------- changes

def compare(cur, prev):
    """Quarter-over-quarter changes between two aggregated holding dicts."""
    tot_cur = sum(h["value"] for h in cur.values()) or 1
    tot_prev = sum(h["value"] for h in prev.values()) or 1
    changes = []
    for key in set(cur) | set(prev):
        c, p = cur.get(key), prev.get(key)
        sh_c = c["shares"] if c else 0.0
        sh_p = p["shares"] if p else 0.0
        val_c = c["value"] if c else 0.0
        val_p = p["value"] if p else 0.0
        ref = c or p
        if c and not p:
            action = "NEW"
        elif p and not c:
            action = "EXIT"
        else:
            if sh_p <= 0:
                continue
            ch = (sh_c - sh_p) / sh_p
            if ch >= 0.05:
                action = "ADD"
            elif ch <= -0.05:
                action = "TRIM"
            else:
                action = "HOLD"
        # price per share implied by the filing, used to estimate the size of the trade
        px = (val_c / sh_c) if sh_c else ((val_p / sh_p) if sh_p else 0)
        traded = (sh_c - sh_p) * px
        changes.append({
            "key": key, "cusip": ref["cusip"], "name": ref["name"], "cls": ref["cls"],
            "putcall": ref["putcall"], "action": action,
            "shares": sh_c, "shares_prev": sh_p,
            "share_change_pct": ((sh_c - sh_p) / sh_p * 100) if sh_p else None,
            "value": val_c, "value_prev": val_p,
            "weight": val_c / tot_cur * 100, "weight_prev": val_p / tot_prev * 100,
            "traded_value": traded, "move_pct": traded / tot_cur * 100,
        })
    return changes, tot_cur, tot_prev


# ---------------------------------------------------------------- tickers

def load_sec_tickers():
    data = json.loads(cached("sec/company_tickers.json",
                             lambda: http_get("https://www.sec.gov/files/company_tickers.json", sec=True), 24 * 7))
    by_cik, by_name = {}, {}
    for v in data.values():
        by_cik.setdefault(int(v["cik_str"]), v["ticker"])
        by_name.setdefault(norm_name(v["title"]), v["ticker"])
    return by_cik, by_name


SUFFIXES = r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|HLDGS|HOLDINGS|HOLDING|GROUP|NV|N V|SA|S A|AG|LP|L P|LLC|THE|NEW|DEL|CL A|CL B|CLASS A|CLASS B|COM|ORD|SHS|ADR|SPONSORED|SPON)\b"


def norm_name(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def map_cusips(cusips, names, sec_by_name):
    """CUSIP -> {ticker, type}. Uses OpenFIGI (free), cached forever. Falls back to name matching."""
    path = os.path.join(CACHE, "cusip_map.json")
    cmap = json.load(open(path)) if os.path.exists(path) else {}
    todo = [c for c in cusips if c and c not in cmap]
    key = os.environ.get("OPENFIGI_API_KEY", "").strip()
    batch = 100 if key else 10
    pause = 0.3 if key else 2.6  # free limits: 25 requests/min without a key
    if todo:
        log(f"  Looking up {len(todo)} new CUSIPs on OpenFIGI (first run is slow; cached after that)...")
    failures = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]).encode()
        hdr = {"Content-Type": "application/json"}
        if key:
            hdr["X-OPENFIGI-APIKEY"] = key
        try:
            res = json.loads(http_get("https://api.openfigi.com/v3/mapping", ua="HedgeFundTracker/1.0",
                                      data=body, headers=hdr, retries=4))
            for c, r in zip(chunk, res):
                d = (r.get("data") or [None])[0]
                if d and d.get("ticker"):
                    cmap[c] = {"ticker": d["ticker"].replace("/", "."), "type": " ".join(x for x in (d.get("securityType"), d.get("securityType2")) if x),
                               "src": "figi"}
                else:
                    cmap[c] = {"ticker": "", "type": "", "src": "none"}
        except Exception as e:
            failures += 1
            log(f"  OpenFIGI lookup failed ({e}); using name matching for these.")
            if failures >= 3:
                break
        time.sleep(pause)
        if i and i % (batch * 20) == 0:
            json.dump(cmap, open(path, "w"))
    # fallback: match by company name against SEC's ticker list
    for c in cusips:
        if c and (c not in cmap or (not cmap[c]["ticker"] and cmap[c]["src"] != "name")):
            t = sec_by_name.get(norm_name(names.get(c, "")), "")
            if t or c not in cmap:
                cmap[c] = {"ticker": t, "type": "", "src": "name"}
    os.makedirs(CACHE, exist_ok=True)
    json.dump(cmap, open(path, "w"))
    return cmap


ETF_WORDS = re.compile(r"\b(ETF|ISHARES|SPDR|VANGUARD|INVESCO QQQ|PROSHARES|DIREXION|SELECT SECTOR|TRUST UNIT|INDEX FD|FUND)\b")


def t212_flag(name, cls, figi_type, ticker, putcall):
    """Rough guess at whether a UK Trading 212 Invest/ISA account can buy this. Not authoritative."""
    if putcall:
        return "no", "Option contract (Trading 212 does not offer options)"
    t = (figi_type or "").upper()
    n = (name or "").upper() + " " + (cls or "").upper()
    if "ETP" in t or "ETF" in t or "FUND" in t or ETF_WORDS.search(n):
        return "no", "US-listed ETF: UK retail investors usually can't buy these (no UK KID). Look for a UCITS version on the LSE."
    if "WARRANT" in t or "WT" in (cls or "").upper().split() or "RIGHT" in t:
        return "no", "Warrant/right"
    if "PRN" in n or "NOTE" in n or "BOND" in t:
        return "no", "Bond/convertible note"
    if not ticker:
        return "unknown", "Ticker not found"
    if "PFD" in n or "PREF" in t:
        return "unlikely", "Preferred share"
    return "likely", "US-listed share: most are available on Trading 212"


# ---------------------------------------------------------------- prices

PRICES = {}
_yahoo_fail = [0]


def yahoo_symbol(t):
    return t.replace(".", "-").replace("/", "-")


def price_history(ticker):
    """Daily closes for ~2 years from Yahoo Finance (free, unofficial). Cached for 12 hours."""
    if ticker in PRICES:
        return PRICES[ticker]
    sym = yahoo_symbol(ticker)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=2y&interval=1d"
    path = os.path.join(CACHE, "prices", sym + ".json")
    if _yahoo_fail[0] >= 5 and not os.path.exists(path):
        PRICES[ticker] = []  # Yahoo is refusing us (rate limit): stop asking for the rest of this run
        return []
    try:
        raw = cached(f"prices/{sym}.json", lambda: http_get(url, ua=BROWSER_UA, retries=2), 12)
        j = json.loads(raw)
        r = j["chart"]["result"][0]
        ts = r.get("timestamp") or []
        closes = r["indicators"]["quote"][0].get("close") or []
        hist = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"), c) for t, c in zip(ts, closes) if c]
        _yahoo_fail[0] = 0
    except Exception as e:
        hist = []
        bad_ticker = isinstance(e, (KeyError, IndexError, TypeError, ValueError)) or (
            isinstance(e, urllib.error.HTTPError) and e.code == 404)
        if not bad_ticker:  # 429 / network failure, not just an unknown symbol
            _yahoo_fail[0] += 1
            if _yahoo_fail[0] == 5:
                log("  Yahoo Finance is not responding (rate limited?); skipping remaining price lookups this run.")
    PRICES[ticker] = hist
    return hist


def close_on(hist, date):
    """Close on or before the date (last trading day)."""
    best = None
    for d, c in hist:
        if d <= date:
            best = c
        else:
            break
    return best


def fx_history():
    """USD per 1 GBP."""
    hist = price_history("GBPUSD=X")
    if hist:
        return hist
    try:  # fallback: ECB rates via frankfurter.app
        start = (dt.date.today() - dt.timedelta(days=730)).isoformat()
        j = json.loads(http_get(f"https://api.frankfurter.app/{start}..?from=GBP&to=USD", ua="HedgeFundTracker/1.0"))
        hist = sorted((d, v["USD"]) for d, v in j["rates"].items())
    except Exception:
        hist = []
    PRICES["GBPUSD=X"] = hist
    return hist


def returns_since(ticker, date, fx):
    hist = price_history(ticker) if ticker else []
    if not hist:
        return None
    p0, p1 = close_on(hist, date), hist[-1][1]
    if not p0:
        return None
    out = {"px_then": p0, "px_now": p1, "ret_usd": (p1 / p0 - 1) * 100, "asof": hist[-1][0]}
    f0, f1 = close_on(fx, date), (fx[-1][1] if fx else None)
    if f0 and f1:
        out["px_now_gbp"] = p1 / f1
        out["ret_gbp"] = ((p1 / f1) / (p0 / f0) - 1) * 100
    return out


# ---------------------------------------------------------------- fast signals (Form 4, 13D/G)

FORM4 = {"4", "4/A"}
SCHED = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}


def parse_form4(cik, f):
    doc = f["primaryDocument"] or ""
    raw_name = doc.split("/")[-1]  # primaryDocument is the styled view (xslF345X05/...); raw XML sits beside it
    try:
        root = strip_ns(ET.fromstring(filing_file(cik, f["accessionNumber"], raw_name)))
    except Exception:
        return []
    issuer = txt(root, "issuer/issuerName")
    tick = txt(root, "issuer/issuerTradingSymbol").upper()
    if int(num(txt(root, "issuer/issuerCik"), 0)) == int(cik):
        return []  # the fund is the issuer (e.g. Buffett's gifts of BRK shares), not a trade by the fund
    out = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = txt(t, "transactionCoding/transactionCode")
        sh = num(txt(t, "transactionAmounts/transactionShares/value"))
        px = num(txt(t, "transactionAmounts/transactionPricePerShare/value"))
        ad = txt(t, "transactionAmounts/transactionAcquiredDisposedCode/value")
        after = num(txt(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
        if not sh:
            continue
        out.append({"date": txt(t, "transactionDate/value"), "code": code, "shares": sh, "price": px,
                    "value": sh * px, "side": "BUY" if ad == "A" else "SELL", "owned_after": after,
                    "issuer": issuer, "ticker": tick})
    return out


def subject_company(cik, f):
    acc = f["accessionNumber"]
    try:
        raw = filing_file(cik, acc, f"{acc}-index-headers.html").decode("utf-8", "ignore")
    except Exception:
        return "", None
    m = re.search(r"SUBJECT COMPANY:.*?COMPANY CONFORMED NAME:\s*([^\n<]+).*?CENTRAL INDEX KEY:\s*(\d+)", raw, re.S)
    return (m.group(1).strip(), int(m.group(2))) if m else ("", None)


def schedule_percent(cik, f):
    doc = f["primaryDocument"] or ""
    if not doc.lower().endswith(".xml"):
        return None
    try:
        raw = filing_file(cik, f["accessionNumber"], doc.split("/")[-1]).decode("utf-8", "ignore")
    except Exception:
        return None
    m = re.search(r"<(?:\w+:)?(?:percentOfClass|classPercent)>\s*([\d.]+)", raw)
    return float(m.group(1)) if m else None


def fast_signals(fund, filings, sec_by_cik, days=120, cap=40):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    f4, sched = [], []
    for f in filings:
        if f["filingDate"] < since:
            break
        if f["form"] in FORM4 and len(f4) < cap:
            for tx in parse_form4(fund["cik"], f):
                tx.update(filed=f["filingDate"], form=f["form"], url=filing_base(fund["cik"], f["accessionNumber"]) + "/",
                          fund=fund["id"], accession=f["accessionNumber"])
                f4.append(tx)
        elif f["form"] in SCHED and len(sched) < cap:
            name, scik = subject_company(fund["cik"], f)
            if scik == fund["cik"]:
                continue
            sched.append({"filed": f["filingDate"], "form": f["form"], "company": name,
                          "ticker": sec_by_cik.get(scik, "") if scik else "",
                          "pct": schedule_percent(fund["cik"], f), "fund": fund["id"],
                          "activist": "13D" in f["form"], "accession": f["accessionNumber"],
                          "url": filing_base(fund["cik"], f["accessionNumber"]) + "/"})
    return f4, sched


# ---------------------------------------------------------------- alerts

def load_env_file():
    p = os.path.join(ROOT, "alerts.env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_whatsapp(text):
    """Send via CallMeBot (free) or Twilio, whichever is configured. Returns True if sent."""
    sent = False
    phone, key = os.environ.get("CALLMEBOT_PHONE", ""), os.environ.get("CALLMEBOT_APIKEY", "")
    if phone and key:
        for part in split_message(text, 900):
            q = urllib.parse.urlencode({"phone": phone, "text": part, "apikey": key})
            try:
                http_get("https://api.callmebot.com/whatsapp.php?" + q, ua="HedgeFundTracker/1.0", retries=2)
                sent = True
            except Exception as e:
                log(f"  CallMeBot send failed: {e}")
            time.sleep(3)
    sid, tok = os.environ.get("TWILIO_ACCOUNT_SID", ""), os.environ.get("TWILIO_AUTH_TOKEN", "")
    frm, to = os.environ.get("TWILIO_WHATSAPP_FROM", ""), os.environ.get("TWILIO_WHATSAPP_TO", "")
    if sid and tok and frm and to:
        import base64
        auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
        for part in split_message(text, 1500):
            body = urllib.parse.urlencode({"From": f"whatsapp:{frm.replace('whatsapp:', '')}",
                                           "To": f"whatsapp:{to.replace('whatsapp:', '')}", "Body": part}).encode()
            try:
                http_get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", ua="HedgeFundTracker/1.0",
                         data=body, headers={"Authorization": "Basic " + auth,
                                             "Content-Type": "application/x-www-form-urlencoded"}, retries=1)
                sent = True
            except Exception as e:
                log(f"  Twilio send failed: {e}")
    return sent


def split_message(text, limit):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def alerts_configured():
    e = os.environ
    return bool((e.get("CALLMEBOT_PHONE") and e.get("CALLMEBOT_APIKEY")) or
                (e.get("TWILIO_ACCOUNT_SID") and e.get("TWILIO_AUTH_TOKEN") and e.get("TWILIO_WHATSAPP_TO")))


def fmt_usd(v):
    a = abs(v)
    s = f"${a/1e9:.1f}bn" if a >= 1e9 else f"${a/1e6:.0f}m" if a >= 1e6 else f"${a/1e3:.0f}k"
    return ("-" if v < 0 else "") + s


def build_alerts(data, state, dashboard_url):
    a = CFG["alerts"]
    seen = set(state.get("seen", []))
    msgs, new_seen = [], set(seen)
    funds = {f["id"]: f for f in data["funds"]}
    for f in data["funds"]:
        q = f.get("latest")
        if not q:
            continue
        new_seen.add(q["accession"])
        if q["accession"] in seen:
            continue
        big = [c for c in f["changes"] if c["action"] != "HOLD" and not c["putcall"] and (
            abs(c["move_pct"]) >= a["min_move_pct_of_portfolio"] or
            (c["action"] == "NEW" and c["weight"] >= a["min_new_position_pct"]) or
            (c["action"] == "EXIT" and c["weight_prev"] >= a["min_new_position_pct"]))]
        big.sort(key=lambda c: -abs(c["move_pct"]))
        if not big:
            continue
        lines = [f"*{f['name']}* ({f['manager']}) filed its {q['period']} 13F:"]
        for c in big[:a["max_items_per_message"]]:
            t = c.get("ticker") or c["name"]
            extra = f"{c['weight']:.1f}% of fund" if c["action"] != "EXIT" else f"was {c['weight_prev']:.1f}%"
            lines.append(f"{ICON[c['action']]} {c['action']} {t}: {fmt_usd(c['traded_value'])} ({extra})")
        lines.append(f"Signal weight: {f['signal']}")
        msgs.append("\n".join(lines))
    # consensus
    for c in data["consensus_buys"]:
        k = "cons:" + data["consensus_period"] + ":" + c["cusip"]
        new_seen.add(k)
        if k not in seen and c["count"] >= a["consensus_min_funds"]:
            msgs.append(f"*Consensus buy* {c.get('ticker') or c['name']}: {c['count']} funds bought in "
                        f"{data['consensus_period']} ({', '.join(funds[x]['name'] for x in c['funds'])})")
    # fast signals
    for s in data["form4"]:
        k = "f4:" + s["accession"]
        if k in new_seen:
            continue
        new_seen.add(k)
        if k not in seen and abs(s["value"]) >= a["form4_min_value_usd"]:
            msgs.append(f"*Form 4* {funds[s['fund']]['name']} {s['side']} {s['ticker'] or s['issuer']}: "
                        f"{s['shares']:,.0f} sh @ ${s['price']:.2f} ({fmt_usd(s['value'])}) on {s['date']}")
    for s in data["schedules"]:
        k = "sc:" + s["accession"]
        new_seen.add(k)
        if k not in seen and s["activist"] and not s["form"].endswith("/A"):
            pct = f" ({s['pct']:.1f}%)" if s.get("pct") else ""
            msgs.append(f"*New 13D (activist stake)* {funds[s['fund']]['name']} in {s['ticker'] or s['company']}{pct}")
    if msgs and dashboard_url:
        msgs.append(dashboard_url)
    return msgs, sorted(new_seen)


ICON = {"NEW": "🟢", "ADD": "🔼", "TRIM": "🔽", "EXIT": "🔴", "HOLD": "•"}


# ---------------------------------------------------------------- main build

def build(args):
    t0 = time.time()
    sec_by_cik, sec_by_name = load_sec_tickers()
    funds_out, all_f4, all_sched = [], [], []
    nq = max(3, CFG.get("lookback_quarters", 3))
    for fund in CFG["funds"]:
        log(f"- {fund['name']}")
        try:
            sub = submissions(fund["cik"])
        except Exception as e:
            sub = None
            log(f"  could not load CIK {fund['cik']} ({e})")
        if sub is None or fund.get("match", "").upper() not in sub.get("name", "").upper():
            found = find_cik_by_name(fund["name"])
            if found and found != fund["cik"]:
                log(f"  CIK {fund['cik']} looks wrong ({sub.get('name') if sub else 'n/a'}); using {found}")
                fund["cik"] = found
                sub = submissions(found)
        if not sub:
            funds_out.append(dict(fund, error="Not found on EDGAR"))
            continue
        filings = recent_filings(sub)
        extra = []
        for ocik in fund.get("also_ciks", []):  # older/other filer CIKs for the same fund (13F only)
            try:
                extra += [dict(f, _cik=int(ocik)) for f in recent_filings(submissions(ocik))
                          if f["form"] in ("13F-HR", "13F-HR/A")]
            except Exception as e:
                log(f"  could not load extra CIK {ocik} ({e})")
        try:
            qs = quarter_holdings(fund["cik"], filings + extra, nq)
        except Exception as e:
            log(f"  13F parse error: {e}")
            qs = []
        entry = dict(fund, edgar_name=sub.get("name"), quarters=[], changes=[], prev_changes=[])
        if qs:
            latest = qs[0]
            entry["latest"] = {k: latest[k] for k in ("period", "filed", "accession", "url", "amended")}
            entry["quarters"] = [{"period": q["period"], "filed": q["filed"],
                                  "total": sum(h["value"] for h in q["holdings"].values()),
                                  "positions": len(q["holdings"])} for q in qs]
            stale = latest["period"] < (dt.date.today() - dt.timedelta(days=200)).isoformat()
            entry["stale"] = stale
            if len(qs) >= 2:
                ch, tot, tot_prev = compare(latest["holdings"], qs[1]["holdings"])
                entry["changes"] = ch
                entry["prev_period"] = qs[1]["period"]
            else:  # first filing ever: everything is new
                entry["changes"] = compare(latest["holdings"], {})[0]
            if len(qs) >= 3:
                pch, _, _ = compare(qs[1]["holdings"], qs[2]["holdings"])
                entry["prev_changes"] = pch
                entry["prev_filed"] = qs[1]["filed"]
            entry["top"] = sorted([dict(h) for h in latest["holdings"].values()], key=lambda h: -h["value"])[:25]
            tv = entry["quarters"][0]["total"] or 1
            for h in entry["top"]:
                h["weight"] = h["value"] / tv * 100
            log(f"  {latest['period']}: {len(latest['holdings'])} positions, {fmt_usd(entry['quarters'][0]['total'])}")
        else:
            log("  no 13F filings found")
        f4, sched = fast_signals(fund, filings, sec_by_cik)
        all_f4 += f4
        all_sched += sched
        funds_out.append(entry)

    # keep the most significant changes per fund (Renaissance alone has thousands)
    cap = CFG.get("max_changes_per_fund", 150)
    for f in funds_out:
        for k in ("changes", "prev_changes"):
            ch = [c for c in f.get(k, []) if c["action"] != "HOLD"]
            ch.sort(key=lambda c: -abs(c["traded_value"]))
            f[k + "_total"] = len(ch)
            f[k] = ch[:cap]

    # consensus: same latest period, NEW or ADD (>=10% more shares) by 2+ funds, common stock only
    periods = [f["latest"]["period"] for f in funds_out if f.get("latest")]
    cons_period = max(set(periods), key=periods.count) if periods else ""
    buys, sells = defaultdict(list), defaultdict(list)
    names = {}
    for f in funds_out:
        if not f.get("latest") or f["latest"]["period"] != cons_period:
            continue
        for c in f["changes"]:
            if c["putcall"]:
                continue
            names[c["cusip"]] = c["name"]
            big_add = c["action"] == "ADD" and (c["share_change_pct"] or 0) >= 10
            big_trim = c["action"] == "TRIM" and (c["share_change_pct"] or 0) <= -10
            if c["action"] == "NEW" or big_add:
                buys[c["cusip"]].append((f, c))
            elif c["action"] == "EXIT" or big_trim:
                sells[c["cusip"]].append((f, c))
    W = {"high": 3, "medium": 2, "low": 1}

    def cons_list(d):
        out = []
        for cusip, lst in d.items():
            fids = sorted({f["id"] for f, _ in lst})
            if len(fids) < 2:
                continue
            out.append({"cusip": cusip, "name": names.get(cusip, ""), "count": len(fids), "funds": fids,
                        "score": sum(W.get(f["signal"], 1) for f, _ in lst),
                        "total_traded": sum(c["traded_value"] for _, c in lst),
                        "detail": [{"fund": f["id"], "action": c["action"], "traded_value": c["traded_value"],
                                    "weight": c["weight"]} for f, c in lst]})
        return sorted(out, key=lambda x: (-x["score"], -abs(x["total_traded"])))[:60]

    cons_buys, cons_sells = cons_list(buys), cons_list(sells)

    # tickers for everything we display
    all_names = {}
    for f in funds_out:
        for k in ("changes", "prev_changes", "top"):
            for c in f.get(k, []):
                all_names[c["cusip"]] = c["name"]
    cmap = map_cusips(sorted(all_names), all_names, sec_by_name)

    def tag(c):
        m = cmap.get(c["cusip"], {})
        c["ticker"] = m.get("ticker", "")
        c["t212"], c["t212_why"] = t212_flag(c["name"], c.get("cls"), m.get("type"), c["ticker"], c.get("putcall"))

    for f in funds_out:
        for k in ("changes", "prev_changes", "top"):
            for c in f.get(k, []):
                tag(c)
    for c in cons_buys + cons_sells:
        tag(c)

    # prices & returns since filing
    if not args.no_prices:
        log("- Prices and returns since filing")
        fx = fx_history()
        spy = price_history("SPY")
        priced = 0
        for f in funds_out:
            if not f.get("latest"):
                continue
            for k, date in (("changes", f["latest"]["filed"]), ("prev_changes", f.get("prev_filed"))):
                if not date:
                    continue
                for i, c in enumerate(f.get(k, [])):
                    if i >= 60 or not c["ticker"] or c["putcall"]:
                        continue
                    r = returns_since(c["ticker"], date, fx)
                    if r:
                        c.update(r)
                        priced += 1
            for h in f.get("top", [])[:15]:
                if h["ticker"] and not h["putcall"]:
                    r = returns_since(h["ticker"], f["latest"]["filed"], fx)
                    if r:
                        h.update(r)
            # scorecard: did copying last quarter's buys beat the S&P 500?
            for label, k, date in (("latest", "changes", f["latest"]["filed"]), ("previous", "prev_changes", f.get("prev_filed"))):
                if not date:
                    continue
                rets = [c.get("ret_gbp", c.get("ret_usd")) for c in f.get(k, [])
                        if c["action"] in ("NEW", "ADD") and c.get("ret_usd") is not None]
                rets = [r for r in rets if r is not None][:20]
                bench = returns_since("SPY", date, fx) if spy else None
                f.setdefault("scorecard", {})[label] = {
                    "filed": date, "n": len(rets),
                    "avg_ret_gbp": sum(rets) / len(rets) if rets else None,
                    "spy_ret_gbp": (bench or {}).get("ret_gbp"),
                    "hit_rate": (sum(1 for r in rets if r > (bench or {}).get("ret_gbp", 0)) / len(rets) * 100) if rets else None}
        for c in cons_buys + cons_sells:
            if c["ticker"]:
                fdates = [f["latest"]["filed"] for f in funds_out if f["id"] in c["funds"] and f.get("latest")]
                if fdates:
                    r = returns_since(c["ticker"], max(fdates), fx)
                    if r:
                        c.update(r)
        for s in all_f4:
            r = returns_since(s["ticker"], s["filed"], fx) if s["ticker"] else None
            if r:
                s.update(r)
        for s in all_sched:
            r = returns_since(s["ticker"], s["filed"], fx) if s["ticker"] else None
            if r:
                s.update(r)
        fx_now = fx[-1][1] if fx else None
        log(f"  priced {priced} moves; GBPUSD {fx_now}")
    else:
        fx_now = None

    all_f4.sort(key=lambda s: (s["filed"], s["date"]), reverse=True)
    all_sched.sort(key=lambda s: s["filed"], reverse=True)
    data = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "gbpusd": fx_now,
        "consensus_period": cons_period,
        "funds": funds_out,
        "consensus_buys": cons_buys,
        "consensus_sells": cons_sells,
        "form4": all_f4[:200],
        "schedules": all_sched[:200],
    }
    os.makedirs(DOCS, exist_ok=True)
    js = json.dumps(data, separators=(",", ":"), default=str)
    with open(os.path.join(DOCS, "data.js"), "w") as fh:
        fh.write("window.TRACKER_DATA = " + js + ";\n")
    log(f"Wrote docs/data.js ({len(js)//1024} KB) in {time.time()-t0:.0f}s")
    return data


def main():
    global CFG
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-alerts", action="store_true")
    ap.add_argument("--no-prices", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    args = ap.parse_args()
    CFG = json.load(open(os.path.join(ROOT, "config.json")))
    load_env_file()
    if os.environ.get("SEC_USER_AGENT"):
        CFG["user_agent"] = os.environ["SEC_USER_AGENT"]
    if args.test_alert:
        if not alerts_configured():
            sys.exit("No WhatsApp settings found. See README (alerts.env or GitHub secrets).")
        ok = send_whatsapp("✅ Hedge Fund Tracker test message. Alerts are working.")
        sys.exit(0 if ok else 1)
    data = build(args)
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    msgs, seen = build_alerts(data, state, os.environ.get("DASHBOARD_URL", ""))
    first_run = "seen" not in state
    if first_run:
        log("First run: recorded current filings as the starting point (no alerts sent).")
    elif msgs and not args.no_alerts and alerts_configured():
        log(f"Sending {len(msgs)} alert(s) to WhatsApp")
        send_whatsapp("📊 Hedge Fund Tracker\n\n" + "\n\n".join(msgs) + "\n\nResearch only, not advice.")
    elif msgs:
        log(f"{len(msgs)} alert(s) would be sent (WhatsApp not configured):")
        for m in msgs:
            log("   " + m.replace("\n", "\n   "))
    state["seen"] = seen
    state["last_run"] = data["generated"]
    json.dump(state, open(STATE_FILE, "w"), indent=0)


if __name__ == "__main__":
    main()
