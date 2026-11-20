"""Real-time paper trader for the dividend run-up strategy on the Upstox live market feed. No real orders are placed.

Morning (from about 8:10 am India time) it builds today's plan from yesterday's NSE prices plus the newest NSE dividend /
record-date announcements and corporate actions (fresh download, so a dividend announced yesterday evening is included):
  - which dividends are bought today, with the planned position size (all the strategy's conditions are checked here),
  - which open positions are watched for the 10% stop-loss, and which are sold today (their last cum day).
From 9:15 am to 3:30 pm it reads every tick of those shares (last price, 5 best bids and offers, volume traded) and
  - buys in 5 slices (9:30, 10:30, 11:30, 12:45, 2:00 pm). A slice is bought only in a calm minute: neither the last
    minute's volume nor the current minute's volume so far is above 4x the previous day's median minute volume.
    The slice is filled against the live offers (walking up the 5 levels), and our total never exceeds 10% of the volume
    traded so far today. Slices not filled by 3:00 pm are dropped.
  - sells a whole position against the live bids if the price falls 10% below its buy price (from the day after buying),
  - on the last cum day sells half at 3:05 pm and the rest at 3:20 pm against the live bids (any remainder at 3:27 pm).
It saves its state every few minutes, so a restart (or the afternoon job on GitHub) continues where it stopped.

Usage (one call per part of the day; GitHub runs them at 8:50 am, 12:15 pm and 2:45 pm):
  python paper/live.py --job morning      plan the day; live trading until 12:20 if a dividend is bought today (or a sale is late)
  python paper/live.py --job afternoon    live buying 12:20 - 3:00 pm on buy days (until 3:30 pm if a sale is also due)
  python paper/live.py --job sells        live selling at 3:05 / 3:20 pm on a last cum day
  python paper/live.py --test-feed 60     connection test only: listen for up to 60 s and print what arrives
paper/settings.json: "watch_holding_days" (true = also watch held shares all day for the stop; needs many GitHub minutes.
false = on holding days the stop is checked at 9 pm from the day's low, as in the 5-year test) and "monthly_minute_budget"
(stop using the live feed for the month once the live jobs have used this many GitHub minutes; null = no limit).
The Upstox token is read from the UPSTOX_ACCESS_TOKEN environment variable (a GitHub secret) or a local .env file.
It is never printed or saved.
"""
import argparse, json, math, os, sys, threading, time
from datetime import date, datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E

R = E.RULES
BUY_SLOTS = ["09:30", "10:30", "11:30", "12:45", "14:00"]
LAST_BUY = "15:00"
SELL_SLOTS = ["15:05", "15:20"]
FORCE_SELL = "15:27"
LATE_SELL = "09:20"
MODE = "live ticks"


class Clock:
    """Real time, or the time of the message being replayed in tests."""
    def __init__(self):
        self.t = None

    def now(self):
        return self.t or datetime.now(E.IST)


CLOCK = Clock()
hhmm = lambda: CLOCK.now().strftime("%H:%M")


def token():
    t = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not t:
        for f in (E.ROOT / ".env", E.ROOT.parent / ".env"):
            if f.exists():
                for line in f.read_text().splitlines():
                    if line.startswith("UPSTOX_ACCESS_TOKEN="):
                        t = line.split("=", 1)[1].strip()
    return t


# ---------------------------------------------------------------- live feed
class Feed:
    """Keeps the latest snapshot of each share from the Upstox 'full' feed, plus our own per-minute volume."""
    def __init__(self):
        self.snap, self.mins, self.lock = {}, {}, threading.Lock()
        self.last_rx, self.n = 0.0, 0

    def on_message(self, msg):
        if isinstance(msg, (bytes, bytearray)):
            msg = msg.decode()
        if isinstance(msg, str):
            msg = json.loads(msg)
        now = CLOCK.now()
        with self.lock:
            self.last_rx, self.n = now.timestamp(), self.n + 1
            for k, v in (msg.get("feeds") or {}).items():
                ff = (v.get("fullFeed") or {}).get("marketFF")
                if not ff:
                    continue
                lt = ff.get("ltpc") or {}
                q = (ff.get("marketLevel") or {}).get("bidAskQuote") or []
                s = self.snap.setdefault(k, {})
                if q:
                    s["bids"] = [(float(x["bidP"]), float(x["bidQ"])) for x in q if x.get("bidP") and float(x.get("bidQ") or 0) > 0]
                    s["asks"] = [(float(x["askP"]), float(x["askQ"])) for x in q if x.get("askP") and float(x.get("askQ") or 0) > 0]
                if lt.get("ltp"):
                    s["ltp"] = float(lt["ltp"])
                vtt = float(ff.get("vtt") or s.get("vtt") or 0)
                s.update(vtt=vtt, atp=float(ff.get("atp") or s.get("atp") or 0), rx=now.timestamp())
                cur_start = int(now.timestamp() // 60 * 60 * 1000)
                for o in (ff.get("marketOHLC") or {}).get("ohlc") or []:
                    if o.get("interval") == "I1" and o.get("ts"):
                        ts, vol = int(o["ts"]), float(o.get("vol") or 0)
                        if ts == cur_start - 60000:
                            s["i1_last"] = (ts, vol)            # the minute that just finished
                # our own minute bookkeeping from the running day volume
                mn = now.strftime("%H:%M")
                m = self.mins.setdefault(k, dict(min=mn, start=vtt, prev=vtt, done=[]))
                if m["min"] != mn:
                    m["done"].append(max(m["prev"] - m["start"], 0.0))
                    m["min"], m["start"] = mn, m["prev"]
                m["prev"] = vtt
                s["cur_min_vol"] = max(vtt - m["start"], 0.0)

    def last_minute_vol(self, k):
        s, m = self.snap.get(k, {}), self.mins.get(k)
        cur_start = int(CLOCK.now().timestamp() // 60 * 60 * 1000)
        if s.get("i1_last") and s["i1_last"][0] == cur_start - 60000:
            return s["i1_last"][1]
        if m and m["done"] and m["min"] == CLOCK.now().strftime("%H:%M"):
            return m["done"][-1]
        return 0.0                                   # no trade at all in the last minute: calm

    def today_median_minute(self, k):
        m = self.mins.get(k)
        d = [x for x in (m["done"] if m else []) if x > 0]
        return float(np.median(d)) if len(d) >= 10 else None


def walk(levels, qty, side):
    """Average fill price for qty against the visible book (best level first); anything beyond the 5 levels at the worst level -/+ 0.2%."""
    if not levels or qty <= 0:
        return None
    got, cost = 0.0, 0.0
    for p, q in levels:
        take = min(q, qty - got)
        got += take; cost += take * p
        if got >= qty - 1e-9:
            break
    if got < qty:
        cost += (qty - got) * levels[-1][0] * (1.002 if side == "buy" else 0.998)
    return cost / qty


# ---------------------------------------------------------------- the morning plan
def build_plan(today, meta, pos):
    prev = E.tdays(today, -1)
    H = E.history(prev)
    if prev.isoformat() not in set(H.date):
        raise RuntimeError(f"NSE prices for {prev} are not available")
    T = H[H.date == prev.isoformat()].set_index("symbol")
    kite, ups, groww, ISIN = E.mtf_lists(today)
    E.update_quarter(today, meta, pos)
    rows, buys = E.evaluate_signals(today, H, T, meta, pos, kite, ups, groww, ISIN)
    plan = dict(date=today.isoformat(), made_at=CLOCK.now().strftime("%Y-%m-%d %H:%M"), buys=[], signals=rows)
    for b in buys:
        if not b["isin"]:
            b["signal"].update(status="skip", reason="no Upstox instrument"); continue
        c = E.candles(b["isin"], prev, today)
        base = float(c.v[c.v > 0].median()) if len(c) and (c.v > 0).any() else None
        plan["buys"].append(dict(symbol=b["symbol"], isin=b["isin"], key=f"NSE_EQ|{b['isin']}", broker=b["broker"], mfrac=b["mfrac"], target=round(b["target"]),
                                 sigma=b["sigma"], ex=b["ex"], cum=b["cum"], dividend_pct=b["dividend_pct"], mcap_cr=b["mcap_cr"], base_minute_vol=base,
                                 slices=0, bought_qty=0, bought_value=0.0, status="open", note=""))
        E.log(f"PLAN buy {b['symbol']} ({b['broker']}): target position Rs {b['target']:,.0f}, dividend {b['dividend_pct']}%, "
              f"sell on {b['cum']}, previous day's median minute volume {base}")
    for p in pos:
        if not p.get("isin"):
            p["isin"] = ISIN.get(p["symbol"])
        what = "sell today (last cum day)" if p["sell_date"] == today.isoformat() else \
            "sell at the open (missed last cum day)" if p["sell_date"] < today.isoformat() else f"hold, 10% stop at {p['stop']}"
        E.log(f"PLAN {p['symbol']}: {what}")
    rows = [r for r in rows if r["status"] != "skip" or r.get("reason") not in ("", None)]
    E.log(f"PLAN {today}: {len(plan['buys'])} to buy, {len(pos)} open position(s), {len(rows)} dividends checked")
    return plan


# ---------------------------------------------------------------- trading decisions
class Trader:
    def __init__(self, today, sd, meta, pos, trades, plan, feed):
        self.today, self.sd, self.meta, self.pos, self.trades, self.plan, self.feed = today, sd, meta, pos, trades, plan, feed
        self.fills = []
        self.live_dir = sd / "live"; self.live_dir.mkdir(parents=True, exist_ok=True)
        f = self.live_dir / f"{today}_fills.csv"
        if f.exists() and f.stat().st_size > 5:
            self.fills = pd.read_csv(f).to_dict("records")

    def keys(self):
        k = {b["key"] for b in self.plan["buys"] if b["status"] == "open"}
        k |= {f"NSE_EQ|{p['isin']}" for p in self.pos if p.get("isin")}
        return sorted(k)

    def fill(self, sym, side, qty, px, s, why):
        t = CLOCK.now()
        row = dict(time=t.strftime("%Y-%m-%d %H:%M:%S"), symbol=sym, side=side, qty=round(qty, 2), price=round(px, 2), value=round(qty * px),
                   ltp=s.get("ltp"), best_bid=(s.get("bids") or [[None]])[0][0], best_ask=(s.get("asks") or [[None]])[0][0],
                   day_volume=s.get("vtt"), why=why)
        self.fills.append(row)
        E.log(f"{row['time'][11:]} {side} {sym} {qty:.0f} @ {px:.2f} ({why})")

    def step(self):
        t = hhmm()
        if t < "09:15" or t >= "15:30":
            return
        for p in self.pos:                         # how much of the session each position was watched live (the 9 pm run uses this)
            if self.feed.snap.get(f"NSE_EQ|{p.get('isin')}") is not None:
                w = p.get("watched") or {}
                if w.get("date") != self.today.isoformat():
                    w = dict(date=self.today.isoformat(), minutes=0, last="")
                if w["last"] != t:
                    w["minutes"] += 1; w["last"] = t
                p["watched"] = w
        for b in self.plan["buys"]:
            self.try_buy(b, t)
        for p in list(self.pos):
            self.try_exit(p, t)

    def try_buy(self, b, t):
        if b["status"] != "open":
            return
        if t >= LAST_BUY:
            b["status"] = "done" if b["bought_qty"] else "not bought"
            b["note"] = f"{b['slices']} of {R['ORDERS']} slices filled by 3 pm" if b["bought_qty"] else "no calm minute with offers before 3 pm"
            E.log(f"{b['symbol']}: buying closed, {b['note']}")
            return
        due = sum(1 for x in BUY_SLOTS if t >= x)
        now = CLOCK.now().timestamp()
        if b["slices"] >= due or now - b.get("last_order_ts", 0) < 60:
            return
        s = self.feed.snap.get(b["key"])
        if not s or not s.get("asks"):
            return
        base = b["base_minute_vol"] or self.feed.today_median_minute(b["key"])
        if base is None:
            return
        lim = R["SPIKE_K"] * base
        if self.feed.last_minute_vol(b["key"]) > lim or s.get("cur_min_vol", 0) > lim:
            if not b.get("spike_noted_slice") == b["slices"]:
                b["spike_noted_slice"] = b["slices"]; E.log(f"{t} {b['symbol']}: volume spike, waiting for a calm minute")
            return
        remaining = b["target"] - b["bought_value"]
        value = remaining / (R["ORDERS"] - b["slices"])
        best = s["asks"][0][0]
        qty = math.floor(value / best)
        qty = min(qty, math.floor(R["PARTICIPATION"] * s.get("vtt", 0) - b["bought_qty"]))      # never more than 10% of today's volume so far
        qty = min(qty, math.floor(self.meta["cash"] / (best * b["mfrac"] * 1.003)))            # free capital
        if qty < 1:
            return
        px = walk(s["asks"], qty, "buy")
        self.fill(b["symbol"], "BUY", qty, px, s, f"slice {b['slices'] + 1} of {R['ORDERS']}")
        p = next((p for p in self.pos if p["symbol"] == b["symbol"]), None)
        if p is None:
            p = E.open_position(b, px, qty, self.today, self.meta, buy_impact=0.0, mode=MODE)
            self.pos.append(p)
        else:
            E.add_to_position(p, qty, px, self.meta)
        b["slices"] += 1; b["bought_qty"] += qty; b["bought_value"] += qty * px; b["last_order_ts"] = now
        p["buy_orders"] = b["slices"]
        if b["slices"] >= R["ORDERS"] or b["bought_value"] >= b["target"] * 0.98:
            b["status"] = "done"; b["note"] = f"{b['slices']} slices, average {p['avg_cost']}"

    def try_exit(self, p, t):
        s = self.feed.snap.get(f"NSE_EQ|{p.get('isin')}")
        if not s:
            return
        ex = p.setdefault("selling", dict(sold_qty=0.0, sold_value=0.0, orders=0, reason=None))
        left = p["qty"] - ex["sold_qty"]
        d = self.today.isoformat()
        qty, why = 0.0, None
        if p["buy_date"] < d and ex["orders"] == 0 and s.get("ltp") and s["ltp"] <= p["stop"]:
            qty, why = left, "10% stop"
        elif p["sell_date"] < d and t >= LATE_SELL:
            qty, why = left, "late sell (missed last cum day)"
        elif p["sell_date"] == d:
            if t >= FORCE_SELL:
                qty, why = left, "last cum day"
            elif t >= SELL_SLOTS[1] and ex["orders"] < 2:
                qty, why = left, "last cum day"
            elif t >= SELL_SLOTS[0] and ex["orders"] < 1:
                qty, why = left / 2, "last cum day"
        if qty <= 0 or not s.get("bids"):
            return
        px = walk(s["bids"], qty, "sell")
        self.fill(p["symbol"], "SELL", qty, px, s, why)
        ex["sold_qty"] += qty; ex["sold_value"] += qty * px; ex["orders"] += 1; ex["reason"] = ex["reason"] or why
        if p["qty"] - ex["sold_qty"] < 1e-6:
            self.trades = E.close_position(p, ex["sold_value"] / p["qty"], ex["reason"], self.today, self.meta, self.trades,
                                           n_sell_orders=ex["orders"], day_value=None, mode=MODE)
            self.pos.remove(p)

    def save(self):
        sd = self.sd
        (sd / "meta.json").write_text(json.dumps(self.meta, indent=1, default=str))
        (sd / "positions.json").write_text(json.dumps(self.pos, indent=1, default=str))
        self.trades.to_csv(sd / "trades.csv", index=False)
        pl = {k: v for k, v in self.plan.items()}
        (self.live_dir / f"{self.today}_plan.json").write_text(json.dumps(pl, indent=1, default=str))
        pd.DataFrame(self.fills, columns=["time", "symbol", "side", "qty", "price", "value", "ltp", "best_bid", "best_ask", "day_volume", "why"]) \
            .to_csv(self.live_dir / f"{self.today}_fills.csv", index=False)
        with open(self.live_dir / f"{self.today}_log.txt", "a", encoding="utf-8") as f:
            f.write("\n".join(E.LOG) + ("\n" if E.LOG else ""))
        E.LOG.clear()


# ---------------------------------------------------------------- connection
def connect(keys, feed):
    import upstox_client
    cfg = upstox_client.Configuration()
    cfg.access_token = token()
    st = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg), keys, "full")
    st.on("message", feed.on_message)
    st.on("open", lambda: E.log(f"{hhmm()} connected to the Upstox live feed ({len(keys)} shares)"))
    st.on("error", lambda e: E.log(f"{hhmm()} feed error: {str(e)[:200]}"))
    st.auto_reconnect(True, 5, 50)
    st.connect()
    return st


def test_feed(seconds):
    """Connection check: subscribe to a few large shares and the Nifty 50 index, print what arrives."""
    if not token():
        print("No UPSTOX_ACCESS_TOKEN found (GitHub secret or .env)."); return 1
    E.HOL = E.holidays()
    _, _, _, ISIN = E.mtf_lists(datetime.now(E.IST).date())
    keys = [f"NSE_EQ|{ISIN[s]}" for s in ("RELIANCE", "TCS", "BMWVENTLTD") if s in ISIN]
    feed = Feed()
    st = connect(keys, feed)
    t0 = time.time()
    while time.time() - t0 < seconds and not (feed.n >= 3 and time.time() - t0 > 15):
        time.sleep(1)
    print(f"messages received: {feed.n}")
    for k, s in feed.snap.items():
        print(f"  {k}: last price {s.get('ltp')}, best bid {(s.get('bids') or [[None]])[0][0]}, best offer {(s.get('asks') or [[None]])[0][0]}, "
              f"volume today {s.get('vtt')}")
    print("\n".join(E.LOG))
    try:
        st.disconnect()
    except Exception:
        pass
    sys.stdout.flush(); os._exit(0 if feed.snap else 1)


def settings():
    f = E.ROOT / "paper/settings.json"
    return json.loads(f.read_text()) if f.exists() else dict(watch_holding_days=False, monthly_minute_budget=1650)


def minutes_used(sd, month, add=0.0):
    f = sd / "live" / "github_minutes.json"
    d = json.loads(f.read_text()) if f.exists() else {}
    if add:
        d[month] = round(d.get(month, 0) + add, 1)
        f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(d, indent=1))
    return d.get(month, 0)


def window(job, plan, pos, today, cfg):
    """(start, end) of live watching for this part of the day, or None when nothing needs the live feed."""
    d = today.isoformat()
    buys_open = any(b["status"] == "open" for b in plan["buys"])
    late = any(p["sell_date"] < d for p in pos)
    sells = any(p["sell_date"] <= d for p in pos)
    watch_all = bool(cfg.get("watch_holding_days")) and bool(pos)
    if job == "morning" and (buys_open or late or watch_all):
        return "09:13", "12:20"
    if job == "afternoon" and (buys_open or watch_all):
        return "09:13", ("15:31" if (sells or watch_all) else "15:01")
    if job == "sells" and sells:
        return "14:58", "15:31"
    return None


def main():
    t_start = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", choices=["morning", "afternoon", "sells"], default="morning")
    ap.add_argument("--state", default=str(E.ROOT / "state")); ap.add_argument("--test-feed", type=int)
    a = ap.parse_args()
    if a.test_feed:
        return test_feed(a.test_feed)
    today = datetime.now(E.IST).date()
    sd = Path(a.state); (sd / "live").mkdir(parents=True, exist_ok=True)
    E.HOL = E.holidays()
    if not E.is_trading(today):
        print(f"{today}: not a trading day."); return 0
    meta, pos, trades, _ = E.load_state(sd)
    if today.isoformat() < meta["start_date"]:
        print(f"{today}: paper trading starts on {meta['start_date']}."); return 0
    cfg, month = settings(), today.strftime("%Y-%m")
    pf = sd / "live" / f"{today}_plan.json"
    if pf.exists():
        plan = json.loads(pf.read_text()); E.log(f"{hhmm()} {a.job}: continuing today's plan (made at {plan['made_at']})")
    elif hhmm() >= "15:00":
        print("Too late to plan today; the 9 pm run will handle the day on 1-minute candles."); return 0
    else:
        plan = build_plan(today, meta, pos)
    feed = Feed()
    tr = Trader(today, sd, meta, pos, trades, plan, feed)
    w = window(a.job, plan, pos, today, cfg)
    if w is None or hhmm() >= w[1]:
        E.log(f"{hhmm()} {a.job}: nothing needs the live feed in this part of the day.")
        tr.save(); minutes_used(sd, month, (time.time() - t_start) / 60 + 1); return 0
    start, end = w
    budget = cfg.get("monthly_minute_budget")
    t0 = datetime.strptime(max(start, hhmm()), "%H:%M"); t1 = datetime.strptime(end, "%H:%M")
    need_min = (t1 - t0).total_seconds() / 60 + (t0 - datetime.strptime(hhmm(), "%H:%M")).total_seconds() / 60
    if budget and minutes_used(sd, month) + need_min > budget:
        E.log(f"{hhmm()} {a.job}: this month's GitHub minutes are nearly used ({minutes_used(sd, month):.0f} of {budget}); "
              f"the 9 pm run will handle today on 1-minute candles.")
        tr.save(); return 0
    if not token():
        E.log("No UPSTOX_ACCESS_TOKEN found; the 9 pm run will handle today on 1-minute candles.")
        tr.save(); return 1
    while hhmm() < start:
        time.sleep(15)
    keys = tr.keys()
    st, last_save, reconnects = connect(keys, feed), time.time(), 0
    while hhmm() < end:
        tr.step()
        if time.time() - last_save > 300:
            tr.save(); last_save = time.time()
        if "09:15" <= hhmm() < "15:30" and time.time() - max(feed.last_rx, t_start) > 180 and reconnects < 20:
            E.log(f"{hhmm()} no data for 3 minutes, reconnecting"); reconnects += 1
            try:
                st.disconnect()
            except Exception:
                pass
            st, feed.last_rx = connect(tr.keys() or keys, feed), time.time()
        time.sleep(1)
    if hhmm() >= LAST_BUY:
        for b in plan["buys"]:
            if b["status"] == "open":
                b["status"] = "done" if b["bought_qty"] else "not bought"
                b["note"] = b["note"] or ("partly bought by 3 pm" if b["bought_qty"] else "no calm minute with offers before 3 pm")
    tr.save()
    minutes_used(sd, month, (time.time() - t_start) / 60 + 2)
    try:
        st.disconnect()
    except Exception:
        pass
    print(f"{hhmm()} {a.job} stopped; {len(tr.fills)} paper fills today, {len(tr.pos)} open position(s).", flush=True)
    sys.stdout.flush(); os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
