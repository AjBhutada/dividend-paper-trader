"""Daily paper trading of the NSE dividend run-up strategy (no real orders).

Runs once per trading day after the close. For day D it:
  1. sells open positions whose last cum day is D (at the close) or whose price hit the 10% stop-loss,
  2. buys dividends whose buy day is D, at the average price of D's calm 1-minute candles,
  3. records everything in state/ and rebuilds the dashboard (README.md, charts/, dashboard.html),
  4. on the last trading day of a week writes reports/weekly_pending.md (the workflow turns it into an issue = email).
Rules are the ones chosen in the 5-year backtest (see RULES below). Data: NSE public files/APIs and Upstox public candles.
Usage: python paper/engine.py [--date YYYY-MM-DD] [--state DIR]
"""
import argparse, gzip, http.cookiejar, io, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd

RULES = dict(START_CAPITAL=500000.0, START_DATE="2026-11-23", MIN_MCAP=500, MAX_MCAP=2500, MIN_YIELD=0.005, BIG_YIELD=0.015,
             MIN_VALUE_CR=0.2, MIN_PRICE=10.0, MAX_LOCKED=3, BUY_WINDOW=10, SPIKE_K=4.0, ORDERS=5, STOP=0.10,
             LIMIT_FRAC=0.10, PARTICIPATION=0.10, IMPACT_Y=0.5, MAX_LEVERAGE=3.0)
if os.environ.get("PAPER_MIN_YIELD"):          # tests of other dividend cuts only
    RULES["MIN_YIELD"] = float(os.environ["PAPER_MIN_YIELD"])
ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
LOG = []


def log(m):
    print(m, flush=True); LOG.append(m)


# ---------------------------------------------------------------- data access
_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
_op.addheaders = [("User-Agent", UA), ("Accept", "application/json,text/csv,*/*"), ("Accept-Language", "en-US,en;q=0.9"), ("Referer", "https://www.nseindia.com/")]
_warm = False


def fetch(url, tries=4, binary=True):
    global _warm
    if "www.nseindia.com/api" in url and not _warm:
        try:
            _op.open("https://www.nseindia.com/", timeout=30).read()
        except Exception:
            pass
        _warm = True
    for k in range(tries):
        try:
            b = _op.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60).read()
            return b if binary else b.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 + 3 * k)
        except Exception:
            time.sleep(2 + 3 * k)
    raise RuntimeError(f"could not download {url}")


CACHE = ROOT / ".cache"


def cached(name, maker, max_age_h=None):
    p = CACHE / name
    if p.exists() and (max_age_h is None or (time.time() - p.stat().st_mtime) < max_age_h * 3600):
        return p.read_bytes()
    b = maker()
    if b is not None:
        p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b)
    return b


def holidays():
    b = cached("holidays.json", lambda: fetch("https://www.nseindia.com/api/holiday-master?type=trading"), max_age_h=24 * 7)
    js = json.loads(b)
    return {datetime.strptime(h["tradingDate"], "%d-%b-%Y").date() for h in js.get("CM", [])}


HOL = None


def is_trading(d):
    return d.weekday() < 5 and d not in HOL


def tdays(start, n):
    """n-th trading day after start (n>0) or before (n<0); start itself counts as 0."""
    d, k, step = start, 0, (1 if n > 0 else -1)
    while k != n:
        d += timedelta(days=step)
        if is_trading(d):
            k += step
    return d


def bhav(d):
    """NSE full bhavcopy for trading day d (EQ/BE/BZ only), or None."""
    def make():
        b = fetch(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv")
        if b is None:
            return None
        x = pd.read_csv(io.BytesIO(b), skipinitialspace=True); x.columns = [c.strip() for c in x.columns]
        if pd.to_datetime(x.DATE1.iloc[0].strip(), format="%d-%b-%Y").date() != d:          # holiday copy of an older file
            return None
        x = x[x.SERIES.str.strip().isin(["EQ", "BE", "BZ"])]
        num = lambda c: pd.to_numeric(x[c], errors="coerce")
        out = pd.DataFrame(dict(symbol=x.SYMBOL.str.strip(), series=x.SERIES.str.strip(), prev_close=num("PREV_CLOSE"), open=num("OPEN_PRICE"),
                                high=num("HIGH_PRICE"), low=num("LOW_PRICE"), close=num("CLOSE_PRICE"), vwap=num("AVG_PRICE"),
                                value_cr=num("TURNOVER_LACS") / 100, trades=num("NO_OF_TRADES")))
        return out.to_csv(index=False).encode()
    b = cached(f"bhav/{d.isoformat()}.csv", make)
    return None if b is None else pd.read_csv(io.BytesIO(b))


def history(d, n=90):
    days, x = [], d
    while len(days) < n:
        if is_trading(x):
            days.append(x)
        x -= timedelta(days=1)
    frames = []
    for t in reversed(days):
        b = bhav(t)
        if b is not None:
            b["date"] = t.isoformat(); frames.append(b)
    return pd.concat(frames, ignore_index=True)


REAL_TODAY = datetime.now(IST).date()
FRESH_H = 3        # today's dividend lists are downloaded again when older than this (hours); the live trader lowers it before the open


def candles(isin, d, today):
    key = urllib.parse.quote(f"NSE_EQ|{isin}", safe="")
    if d == REAL_TODAY:
        b = fetch(f"https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/1")
        if not json.loads(b or b"{}").get("data", {}).get("candles"):
            b = fetch(f"https://api.upstox.com/v3/historical-candle/{key}/minutes/1/{d}/{d}")
    else:
        b = cached(f"candles/{isin}_{d:%Y%m%d}.json", lambda: fetch(f"https://api.upstox.com/v3/historical-candle/{key}/minutes/1/{d}/{d}"))
    c = json.loads(b or b"{}").get("data", {}).get("candles", [])
    m = pd.DataFrame(c, columns=["t", "o", "h", "l", "c", "v", "oi"])
    if len(m):
        m = m[pd.to_datetime(m.t).dt.date == d]
    return m


def calm_average(isin, d, prev_d, today, day_vwap):
    m, p = candles(isin, d, today), candles(isin, prev_d, today)
    if not len(m) or m.v.sum() <= 0:
        return np.nan
    cv = float((((m.h + m.l + m.c) / 3) * m.v).sum() / m.v.sum())
    if np.isfinite(day_vwap) and abs(day_vwap / cv - 1) > 0.03:       # adjusted candles: rescale to NSE's VWAP
        m[["o", "h", "l", "c"]] *= day_vwap / cv
    base = p.v[p.v > 0].median() if len(p) else np.nan
    if not np.isfinite(base) or base <= 0:
        base = m.v[m.v > 0].median()
    k = m[(m.v > 0) & (m.v <= RULES["SPIKE_K"] * base)]
    return float(((k.h + k.l + k.c) / 3).mean()) if len(k) else np.nan


def corp_actions(d):
    def make():
        f, t = d - timedelta(days=130), d + timedelta(days=45)
        return fetch(f"https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={f:%d-%m-%Y}&to_date={t:%d-%m-%Y}")
    ca = pd.DataFrame(json.loads(cached(f"ca/{d.isoformat()}.json", make, max_age_h=None if d < REAL_TODAY else FRESH_H)))   # today: refresh, new dividends appear all day
    if not len(ca):
        return ca
    ca["ex"] = pd.to_datetime(ca.exDate, format="%d-%b-%Y", errors="coerce").dt.date

    def amount(s):
        tot = 0.0
        for seg in re.split(r"/|\band\b", str(s)):
            if "ivid" in seg.lower():
                m = re.findall(r"(?:Rs|Re|INR)\.?\s*([\d]+(?:\.\d+)?)", seg)
                if m:
                    tot += float(m[0])
        return tot
    ca["div"] = ca.subject.map(amount)
    return ca


def announcements(d, symbols):
    """Earliest dividend / record-date news per symbol in the last 75 days (time as IST datetime)."""
    rows, start = [], d - timedelta(days=75)
    a = start - timedelta(days=start.weekday())          # Monday-to-Sunday weeks, so past weeks are downloaded once and reused
    old = d < REAL_TODAY - timedelta(days=7)            # a test of a past day: whole weeks (downloaded once), later news dropped below
    while a <= d:
        b = a + timedelta(days=6) if old else min(d, a + timedelta(days=6))
        name = f"ann/{a.isoformat()}_{b.isoformat()}.json"
        maker = lambda a=a, b=b: fetch(f"https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}")
        raw = cached(name, maker, max_age_h=None if (old or b < d - timedelta(days=2)) else FRESH_H)
        for x in json.loads(raw or b"[]"):
            if x.get("symbol") not in symbols:
                continue
            txt = (x.get("desc") or "") + " " + (x.get("attchmntText") or "")
            if "dividend" in txt.lower() or "record date" in txt.lower():
                t = pd.to_datetime(x.get("exchdisstime") or x.get("an_dt"), format="%d-%b-%Y %H:%M:%S", errors="coerce")
                if pd.notna(t) and (not old or t.date() <= d):      # never news from after the day being run
                    rows.append((x["symbol"], t))
        a = b + timedelta(days=1)
    return pd.DataFrame(rows, columns=["symbol", "t"])


def mtf_lists(d):
    """Brokers publish only their current lists, so every run (also a test of a past day) uses today's lists, downloaded once a day."""
    k = REAL_TODAY.isoformat()
    kite = pd.DataFrame(json.loads(cached(f"lists/kite_{k}.json", lambda: fetch("https://public.zrd.sh/crux/approved-mtf-securities.json"))))
    kite = dict(zip(kite.tradingsymbol, kite.margin / 100))
    up = json.loads(gzip.decompress(cached(f"lists/upstox_{k}.json.gz", lambda: fetch("https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"))))
    isin = {x["trading_symbol"]: x["isin"] for x in up if x.get("segment") == "NSE_EQ"}
    ups = {x["trading_symbol"]: (x.get("mtf_bracket") or 33.3) / 100 for x in up if x.get("segment") == "NSE_EQ" and x.get("mtf_enabled")}
    gw = pd.read_csv(ROOT / "paper/lists/groww_mtf_list.csv")
    groww = dict(zip(gw.symbol, gw.req))
    return kite, ups, groww, isin


def broker_for(sym, kite, ups, groww):
    if sym in kite:
        return "Kite MTF", max(kite[sym], 1 / RULES["MAX_LEVERAGE"])
    if sym in ups:
        return "Upstox MTF", max(ups[sym], 1 / RULES["MAX_LEVERAGE"])
    if sym in groww:
        return "Groww MTF", max(groww[sym], 1 / RULES["MAX_LEVERAGE"])
    return "Cash", 1.0


# ---------------------------------------------------------------- money
def charges(broker, buy_val, sell_val, n_buy_orders, n_sell_orders, n_buy_days):
    stt, stamp = 0.001 * (buy_val + sell_val), 0.00015 * buy_val
    exch = (0.0000307 if broker == "Groww MTF" else 0.0000297) * (buy_val + sell_val); sebi = 0.000001 * (buy_val + sell_val)
    if broker == "Kite MTF":
        brok = n_buy_orders * min(20, 0.003 * buy_val / n_buy_orders) + n_sell_orders * min(20, 0.003 * sell_val / n_sell_orders)
        other = 15.34 + 15 * 1.18 * (n_buy_days + 1)
    elif broker == "Upstox MTF":
        brok = n_buy_orders * min(20, 0.025 * buy_val / n_buy_orders) + n_sell_orders * min(20, 0.025 * sell_val / n_sell_orders)
        other = 23.6 + 20 * 1.18 * (n_buy_days + 1)
    elif broker == "Groww MTF":
        brok = 0.001 * (buy_val + sell_val); other = 23.6 + 20 * 1.18 * (n_buy_days + 1)
    else:
        brok = 0.0; other = 15.34
    return stt + stamp + exch + sebi + brok + 0.18 * (brok + exch + sebi) + other


def interest(broker, funded, days):
    if broker == "Kite MTF":
        return 0.0004 * days * funded
    if broker == "Upstox MTF":
        return 20 * math.ceil(funded / 40000) * days
    if broker == "Groww MTF":
        return 0.1495 / 365 * days * funded
    return 0.0


# ---------------------------------------------------------------- state
def load_state(sd, start=None):
    sd.mkdir(parents=True, exist_ok=True)
    meta = json.loads((sd / "meta.json").read_text()) if (sd / "meta.json").exists() else \
        dict(start_capital=RULES["START_CAPITAL"], start_date=start or RULES["START_DATE"], cash=RULES["START_CAPITAL"], quarter=None, limit=None, last_run=None)
    pos = json.loads((sd / "positions.json").read_text()) if (sd / "positions.json").exists() else []
    readable = lambda f: (sd / f).exists() and (sd / f).stat().st_size > 5
    trades = pd.read_csv(sd / "trades.csv") if readable("trades.csv") else pd.DataFrame()
    eq = pd.read_csv(sd / "equity.csv") if readable("equity.csv") else pd.DataFrame()
    return meta, pos, trades, eq


def save_state(sd, meta, pos, trades, eq, signals):
    (sd / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    (sd / "positions.json").write_text(json.dumps(pos, indent=1, default=str))
    trades.to_csv(sd / "trades.csv", index=False); eq.to_csv(sd / "equity.csv", index=False); signals.to_csv(sd / "signals.csv", index=False)
    (sd / "last_run_log.txt").write_text("\n".join(LOG))


# ---------------------------------------------------------------- shared steps (used by the end-of-day run and the live trader)
def update_quarter(today, meta, pos):
    """Per-share limit = 10% of capital at the start of each quarter (Jan/Apr/Jul/Oct)."""
    q = f"{today.year}Q{(today.month - 1) // 3 + 1}"
    if meta.get("quarter") != q:
        eq = meta["cash"] + sum(p["margin"] for p in pos)
        meta["quarter"], meta["limit"] = q, RULES["LIMIT_FRAC"] * eq
        log(f"New quarter {q}: per-share limit Rs {meta['limit']:,.0f} (10% of Rs {eq:,.0f})")


def evaluate_signals(today, H, T, meta, pos, kite, ups, groww, ISIN):
    """Check every dividend with an ex-date in the next 25 trading days against all the rules.
    H = daily history up to the reference day, T = the reference day's prices: today after the close (end-of-day run)
    or yesterday before the open (live trader's morning plan). New dividends are picked up because the NSE corporate-action
    and announcement lists are downloaded fresh each run.
    Returns (one row per dividend for the dashboard, the buys due today with their planned position size)."""
    CA = corp_actions(today)
    divs = CA[(CA["div"] > 0) & (CA.ex > today) & (CA.ex <= tdays(today, 25))].copy() if len(CA) else pd.DataFrame()
    if not len(divs):
        return [], []
    other = CA[~CA.subject.str.contains("ivid", case=False, na=False)]
    ann = announcements(today, set(divs.symbol))
    shares = pd.read_csv(ROOT / "paper/lists/shares.csv").set_index("symbol").shares
    held = {p["symbol"] for p in pos}
    rows, due = [], []
    for e in divs.groupby(["symbol", "ex"], as_index=False).agg(div=("div", "sum"), subject=("subject", " | ".join)).itertuples():
        s, X = e.symbol, e.ex
        C = tdays(X, -1)
        g = ann[ann.symbol == s]
        if len(g):
            t = g.t.min()
            news_day = t.date() if (t.hour, t.minute) < (9, 15) and is_trading(t.date()) else tdays(t.date() if is_trading(t.date()) else tdays(t.date(), -1), 1)
        else:
            news_day = tdays(X, -4)
        buy_day = max(tdays(X, -RULES["BUY_WINDOW"]), news_day)
        st = dict(symbol=s, ex_date=X.isoformat(), last_cum_day=C.isoformat(), dividend=e.div, buy_day=buy_day.isoformat(), status="", reason="")
        h = H[H.symbol == s].sort_values("date")
        win = h[h.date <= tdays(X, -21).isoformat()].tail(60)
        c21 = win.close.iloc[-1] if len(win) else (h.close.iloc[-1] if len(h) else np.nan)
        y = e.div / c21 if np.isfinite(c21) and c21 > 0 else np.nan
        mc = shares.get(s, np.nan) * c21
        st.update(dividend_pct=round(100 * y, 2) if np.isfinite(y) else None, mcap_cr=round(mc) if np.isfinite(mc) else None)
        reasons = []
        if not len(h) or s not in T.index:
            reasons.append("no recent price")
        else:
            if not (RULES["MIN_MCAP"] <= mc <= RULES["MAX_MCAP"]):
                reasons.append("market cap outside 500-2,500 cr")
            if not (np.isfinite(y) and y >= RULES["MIN_YIELD"]):
                reasons.append("dividend < 0.5%")
            if (h.series != "EQ").any():
                reasons.append("not normal EQ series")
            if not len(win) or win.value_cr.median() < RULES["MIN_VALUE_CR"]:
                reasons.append("too little trading")
            ret = win.close / win.prev_close - 1
            locked = int(((win.high == win.low) | ((ret > 0.0485) & (win.close >= win.high)) | ((ret < -0.0485) & (win.close <= win.low))).sum())
            if locked > RULES["MAX_LOCKED"]:
                reasons.append("circuit-locked days")
            if T.loc[s].close < RULES["MIN_PRICE"]:
                reasons.append("price below Rs 10")
            o = other[(other.symbol == s) & (other.ex >= tdays(X, -80)) & (other.ex <= tdays(X, 20))]
            if len(o):
                reasons.append("bonus/split/rights nearby")
        if buy_day > tdays(X, -2):
            reasons.append("record date announced too late")
        st["reason"] = "; ".join(reasons)
        if reasons:
            st["status"] = "skip"
        elif s in held:
            st["status"] = "holding"
        elif buy_day > today:
            st["status"] = "waiting for buy day"
        elif buy_day < today:
            st["status"] = "buy day passed"
        else:
            st["status"] = "buy today"
            sig = float(np.nanstd((win.close / win.prev_close - 1).tail(20))) or 0.03
            due.append(dict(symbol=s, isin=ISIN.get(s), y=y, med_value=float(win.value_cr.median()) * 1e7, sigma=sig, ex=X.isoformat(), cum=C.isoformat(),
                            dividend_pct=st["dividend_pct"], mcap_cr=st["mcap_cr"], signal=st))
        rows.append(st)
    # same-day signals: bigger dividend first; size each one and check the free capital
    buys, cash_left = [], meta["cash"]
    for b in sorted(due, key=lambda b: -b["y"]):
        broker, mfrac = broker_for(b["symbol"], kite, ups, groww)
        limit = meta["limit"] * (2 if (broker == "Cash" and b["y"] >= RULES["BIG_YIELD"]) else 1)
        V = min(limit / mfrac, RULES["PARTICIPATION"] * b["med_value"])
        if V < 10000:
            b["signal"].update(status="skip", reason="too small"); continue
        if V * mfrac > cash_left:
            b["signal"].update(status="skip", reason="capital busy"); continue
        cash_left -= V * mfrac
        b.update(broker=broker, mfrac=mfrac, target=V)
        buys.append(b)
    return rows, buys


def open_position(b, avg_px, qty, today, meta, buy_impact=0.0, mode="1-minute candles"):
    V = qty * avg_px
    margin = V * b["mfrac"]
    meta["cash"] -= margin
    p = dict(symbol=b["symbol"], isin=b.get("isin"), broker=b["broker"], margin_frac=b["mfrac"], buy_date=today.isoformat(), sell_date=b["cum"], ex_date=b["ex"],
             avg_cost=round(avg_px, 2), qty=qty, position=V, margin=margin, sigma=b["sigma"], buy_impact=buy_impact, dividend_pct=b["dividend_pct"],
             mcap_cr=b["mcap_cr"], stop=round(avg_px * (1 - RULES["STOP"]), 2), buy_mode=mode, buy_orders=RULES["ORDERS"])
    log(f"BUY {p['symbol']} ({p['broker']}) at {avg_px:.2f}, position Rs {V:,.0f}, margin Rs {margin:,.0f}, sell on {p['sell_date']}, stop {p['stop']} [{mode}]")
    return p


def add_to_position(p, qty, px, meta):
    """Another slice bought for an open position (live trader)."""
    v = qty * px
    meta["cash"] -= v * p["margin_frac"]
    p["qty"] += qty; p["position"] += v; p["margin"] += v * p["margin_frac"]
    p["avg_cost"] = round(p["position"] / p["qty"], 2); p["stop"] = round(p["avg_cost"] * (1 - RULES["STOP"]), 2)


def close_position(p, exit_px, how, today, meta, trades, n_sell_orders=2, day_value=None, mode="1-minute candles"):
    """Sell the whole position at exit_px (average if sold in slices). day_value given = add the modelled price impact (no live order book)."""
    sell_val = p["qty"] * exit_px
    days = (today - date.fromisoformat(p["buy_date"])).days
    imp = RULES["IMPACT_Y"] * p["sigma"] * math.sqrt(p["position"] / max(day_value, 1)) if day_value is not None else 0.0
    ch = charges(p["broker"], p["position"], sell_val, p.get("buy_orders", RULES["ORDERS"]), max(n_sell_orders, 1), 1)
    intr = interest(p["broker"], p["position"] * (1 - p["margin_frac"]), days)
    pnl = sell_val - p["position"] - ch - intr - (p.get("buy_impact", 0.0) + imp) * p["position"]
    meta["cash"] += p["margin"] + pnl
    row = dict(symbol=p["symbol"], broker=p["broker"], dividend_pct=p["dividend_pct"], mcap_cr=p["mcap_cr"], buy_date=p["buy_date"],
               avg_cost=p["avg_cost"], sell_date=today.isoformat(), exit_price=round(exit_px, 2), exit_reason=how, position=round(p["position"]),
               margin=round(p["margin"]), charges=round(ch + intr), impact=round((p.get("buy_impact", 0.0) + imp) * p["position"]), pnl=round(pnl),
               pnl_pct_position=round(100 * pnl / p["position"], 2), pnl_pct_margin=round(100 * pnl / p["margin"], 2), days=days, ex_date=p["ex_date"],
               buy_mode=p.get("buy_mode", "1-minute candles"), sell_mode=mode)
    log(f"SELL {p['symbol']} at {exit_px:.2f} ({how}): P&L Rs {pnl:,.0f} ({row['pnl_pct_margin']:+.1f}% on margin) [{mode}]")
    return pd.concat([trades, pd.DataFrame([row])], ignore_index=True)


# ---------------------------------------------------------------- the day, after the close
def run(today, sd, start=None):
    """End-of-day run (9 pm). If the live trader already handled today's buys and sells on real-time ticks, this only marks the
    positions at NSE's official close and refreshes the dividend list. Otherwise (live trader off or failed, and in tests)
    it trades the day on 1-minute candles as a backup."""
    global HOL
    HOL = holidays()
    meta, pos, trades, eqdf = load_state(sd, start)
    if not is_trading(today):
        log(f"{today}: not a trading day, nothing to do."); return None
    if meta.get("last_run") == today.isoformat():
        log(f"{today}: already processed."); return None
    H = history(today)
    if today.isoformat() not in set(H.date):
        raise RuntimeError(f"NSE bhavcopy for {today} is not published yet; try again later")
    T = H[H.date == today.isoformat()].set_index("symbol")
    prev_day = tdays(today, -1)
    kite, ups, groww, ISIN = mtf_lists(today)
    started = today.isoformat() >= meta["start_date"]
    MODE = "1-minute candles (backup)"
    update_quarter(today, meta, pos)
    lp = sd / "live" / f"{today}_plan.json"
    live_plan = {b["symbol"]: b for b in json.loads(lp.read_text())["buys"]} if lp.exists() else {}

    # ---- 1. exits. A position the live trader watched for the whole session is left alone (it already applied the stop and
    #         the last-cum-day sale on real-time ticks). Otherwise use the day's prices: stop on the day's low, else the close.
    keep = []
    for p in pos:
        if p["symbol"] not in T.index or not started:
            keep.append(p); continue
        w = p.get("watched") or {}
        if w.get("date") == today.isoformat() and w.get("minutes", 0) >= 360:
            keep.append(p); continue
        r = T.loc[p["symbol"]]
        exit_px, how = None, None
        stop = p["avg_cost"] * (1 - RULES["STOP"])
        if today.isoformat() > p["buy_date"]:
            if r.open <= stop:
                exit_px, how = r.open * 0.998, "10% stop (gap)"
            elif r.low <= stop:
                exit_px, how = stop * 0.998, "10% stop"
        if exit_px is None and today.isoformat() >= p["sell_date"]:
            exit_px, how = r.close * 0.9995, "last cum day close"
        if exit_px is None:
            keep.append(p); continue
        sold = p.get("selling") or {}
        if sold.get("sold_qty", 0) > 0:                       # part already sold live before the live job stopped
            exit_px = (sold["sold_value"] + (p["qty"] - sold["sold_qty"]) * exit_px) / p["qty"]
        trades = close_position(p, exit_px, how, today, meta, trades, n_sell_orders=1 if "stop" in how else 2, day_value=r.value_cr * 1e7, mode=MODE)
    pos = keep

    # ---- 2. signals and buys (a dividend the live trader already tried today is not bought again here)
    sig_rows, buys = evaluate_signals(today, H, T, meta, pos, kite, ups, groww, ISIN)
    for b in buys:
        st = b["signal"]
        lb = live_plan.get(b["symbol"])
        if lb and lb["status"] != "open":
            st.update(status="skip", reason=f"live trader: {lb.get('note') or lb['status']}"); continue
        if not started:
            continue
        r = T.loc[b["symbol"]]
        px = calm_average(b["isin"], today, prev_day, today, r.vwap) if b["isin"] else np.nan
        V = min(b["target"], RULES["PARTICIPATION"] * r.value_cr * 1e7)
        if not np.isfinite(px):
            st.update(status="skip", reason="no 1-minute candles"); continue
        if V < 10000:
            st.update(status="skip", reason="too small"); continue
        buy_imp = RULES["IMPACT_Y"] * b["sigma"] * math.sqrt(V / max(r.value_cr * 1e7, 1))
        pos.append(open_position(b, px, V / px, today, meta, buy_impact=buy_imp, mode=MODE))
        st["status"] = "bought"
    signals = pd.DataFrame(sig_rows)

    # ---- 3. equity (open positions marked at today's official close)
    open_val = 0.0
    for p in pos:
        c = T.loc[p["symbol"]].close if p["symbol"] in T.index else p["avg_cost"]
        p["last_close"] = float(c)
        p["unrealized"] = round(p["qty"] * c - p["position"], 0)
        open_val += p["margin"] + p["unrealized"]
    equity = meta["cash"] + open_val
    idx = index_close(today)
    eqdf = pd.concat([eqdf, pd.DataFrame([dict(date=today.isoformat(), equity=round(equity), cash=round(meta["cash"]), open_positions=len(pos),
                                               invested=round(sum(p["margin"] for p in pos)), smallcap250=idx)])], ignore_index=True)
    meta["last_run"] = today.isoformat()
    save_state(sd, meta, pos, trades, eqdf, signals)
    log(f"{today}: equity Rs {equity:,.0f}, cash Rs {meta['cash']:,.0f}, {len(pos)} open, {len(trades)} closed trades so far")
    return meta, pos, trades, eqdf, signals


def index_close(d):
    b = cached(f"index/{d.isoformat()}.csv", lambda: fetch(f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"))
    if b is None:
        return None
    x = pd.read_csv(io.BytesIO(b)); x.columns = [c.strip() for c in x.columns]
    v = x[x["Index Name"] == "Nifty Smallcap 250"]["Closing Index Value"]
    return float(v.iloc[0]) if len(v) else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date"); ap.add_argument("--state", default=str(ROOT / "state")); ap.add_argument("--start"); ap.add_argument("--out", default=str(ROOT)); ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args()
    today = date.fromisoformat(a.date) if a.date else datetime.now(IST).date()
    sd = Path(a.state)
    for k in range(4):                       # NSE publishes the bhavcopy in the evening; wait for it if needed
        try:
            out = run(today, sd, a.start); break
        except RuntimeError as ex:
            if "not published" in str(ex) and k < 3:
                log(str(ex) + "; waiting 15 minutes"); time.sleep(900)
            else:
                raise
    if HOL is not None and is_trading(today) and not a.no_report:
        weekly = out is not None and tdays(today, 1).isocalendar()[1] != today.isocalendar()[1]   # once per week, only when the day was processed
        sys.path.insert(0, str(Path(__file__).parent))
        from report import build
        build(Path(a.out), sd, today, RULES, weekly)
