"""Builds the dashboard (README.md + charts/equity.png + dashboard.html) and the weekly report from state/."""
import html, json
from datetime import date, timedelta
from pathlib import Path
import pandas as pd


def rs(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    x = float(x); sgn = "-" if x < 0 else ""
    x = abs(round(x))
    s = str(int(x)); head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail; head = head[:-2]
    return f"{sgn}₹{(head + ',' if head else '') + tail}"


def pct(x, d=1):
    return "-" if x is None or pd.isna(x) else f"{x:+.{d}f}%"


def md_table(df):
    if df is None or not len(df):
        return "_None yet._\n"
    cols = list(df.columns)
    out = "| " + " | ".join(cols) + " |\n|" + "|".join("---" for _ in cols) + "|\n"
    for r in df.itertuples(index=False):
        out += "| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |\n"
    return out


def build(root, sd, today, R, weekly=False):
    root, sd = Path(root), Path(sd)
    meta = json.loads((sd / "meta.json").read_text()) if (sd / "meta.json").exists() else None
    if meta is None:
        return
    pos = json.loads((sd / "positions.json").read_text())
    tr = pd.read_csv(sd / "trades.csv") if (sd / "trades.csv").exists() and (sd / "trades.csv").stat().st_size > 5 else pd.DataFrame()
    eq = pd.read_csv(sd / "equity.csv") if (sd / "equity.csv").exists() else pd.DataFrame()
    sig = pd.read_csv(sd / "signals.csv") if (sd / "signals.csv").exists() and (sd / "signals.csv").stat().st_size > 5 else pd.DataFrame()
    start_cap, start = meta["start_capital"], meta["start_date"]
    equity = float(eq.equity.iloc[-1]) if len(eq) else start_cap
    days = max((today - date.fromisoformat(start)).days, 0)
    yearly = ((equity / start_cap) ** (365 / days) - 1) * 100 if days >= 90 else None
    idx = eq.dropna(subset=["smallcap250"]) if len(eq) and "smallcap250" in eq else pd.DataFrame()
    idx = idx[idx.date >= start] if len(idx) else idx
    idx_ret = (idx.smallcap250.iloc[-1] / idx.smallcap250.iloc[0] - 1) * 100 if len(idx) > 1 else None
    n, wins = len(tr), int((tr.pnl > 0).sum()) if len(tr) else 0
    in_use = sum(p["margin"] for p in pos)

    # ---- equity chart
    (root / "charts").mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(9, 3.4), dpi=110)
        if len(eq):
            e = eq[eq.date >= start] if (eq.date >= start).any() else eq
            x = pd.to_datetime(e.date)
            ax.plot(x, e.equity / 1e5, color="#1f6f5c", lw=2, label="Paper account")
            if len(idx) > 1:
                ii = idx[idx.date.isin(e.date)]
                ax.plot(pd.to_datetime(ii.date), start_cap / 1e5 * ii.smallcap250 / ii.smallcap250.iloc[0], color="#9aa5a0", lw=1.5, label="Nifty Smallcap 250 (same money)")
        ax.axhline(start_cap / 1e5, color="#cccccc", lw=1, ls="--")
        ax.set_ylabel("₹ lakh"); ax.grid(alpha=.25); ax.legend(frameon=False, loc="upper left")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        fig.tight_layout(); fig.savefig(root / "charts/equity.png"); plt.close(fig)
    except Exception as ex:
        print("chart skipped:", ex)

    # ---- tables
    open_df = pd.DataFrame([dict(Stock=p["symbol"], Broker=p["broker"], Bought=p["buy_date"], **{"Avg cost": f"{p['avg_cost']:.2f}"},
                                 **{"Last close": f"{p.get('last_close', p['avg_cost']):.2f}"},
                                 **{"Unrealized": rs(p.get("unrealized", 0))}, Stop=f"{p['stop']:.2f}", **{"Sell on": p["sell_date"]},
                                 **{"Dividend %": p["dividend_pct"]}, Position=rs(p["position"])) for p in pos])
    up = sig[sig.status.isin(["waiting for buy day", "buy today", "bought", "holding"])] if len(sig) else sig
    up_df = up[["symbol", "ex_date", "buy_day", "last_cum_day", "dividend_pct", "mcap_cr", "status"]].rename(columns=dict(
        symbol="Stock", ex_date="Ex-date", buy_day="Buy day", last_cum_day="Sell day", dividend_pct="Dividend %", mcap_cr="Mcap (₹ cr)", status="Status")) if len(up) else up
    sk = sig[sig.status == "skip"] if len(sig) else sig
    sk_df = sk[["symbol", "ex_date", "dividend_pct", "mcap_cr", "reason"]].rename(columns=dict(symbol="Stock", ex_date="Ex-date", dividend_pct="Dividend %",
                                                                                            mcap_cr="Mcap (₹ cr)", reason="Why skipped")) if len(sk) else sk
    if len(tr):
        bm = tr["buy_mode"].fillna("") if "buy_mode" in tr else pd.Series("", index=tr.index)
        sm = tr["sell_mode"].fillna("") if "sell_mode" in tr else pd.Series("", index=tr.index)
        lb, ls_ = bm.str.startswith("live"), sm.str.startswith("live")
        tr["how"] = ["live ticks" if a and b else "partly live" if a or b else "1-minute candles" for a, b in zip(lb, ls_)]
    tr_df = tr.sort_values("sell_date", ascending=False).head(30)[["symbol", "broker", "buy_date", "avg_cost", "sell_date", "exit_price", "exit_reason", "pnl", "pnl_pct_margin", "how"]] \
        .rename(columns=dict(symbol="Stock", broker="Broker", buy_date="Bought", avg_cost="Buy", sell_date="Sold", exit_price="Sell", exit_reason="Why",
                             pnl="Profit (₹)", pnl_pct_margin="On margin %", how="Traded on")) if len(tr) else tr
    month = tr.assign(m=tr.sell_date.str[:7]).groupby("m").agg(Trades=("pnl", "size"), Wins=("pnl", lambda s: int((s > 0).sum())),
                                                                Profit=("pnl", "sum")).reset_index().rename(columns={"m": "Month"}) if len(tr) else tr
    if len(month):
        month["Profit"] = month.Profit.map(rs)

    avg_pos = tr.pnl_pct_position.mean() if n else None
    summary = [("Started", f"{start} with {rs(start_cap)} (paper money)"),
               ("Equity now", f"{rs(equity)} ({pct((equity / start_cap - 1) * 100)})"),
               ("Closed trades", f"{n}" + (f", {wins} winners ({wins / n:.0%}), average {pct(avg_pos, 2)} per trade on the position" if n else "")),
               ("Yearly rate so far", pct(yearly) if yearly is not None else "shown after 3 months"),
               ("Nifty Smallcap 250 since start", pct(idx_ret)),
               ("Open positions", f"{len(pos)} using {rs(in_use)} of margin"),
               ("Per-share limit this quarter", f"{rs(meta.get('limit'))} ({meta.get('quarter')})"),
               ("Backtest expectation", "about +2.3% per trade on the position, about 60% winners")]
    S = "| | |\n|---|---|\n" + "".join(f"| {a} | {b} |\n" for a, b in summary)
    readme = f"""# Dividend run-up paper trading

**Dashboard: https://ajbhutada.github.io/dividend-paper-trader/**

_Updated after the close on {today:%d %b %Y}. Paper money only: no real orders are placed._

{S}
![Equity](charts/equity.png)

## Open positions
{md_table(open_df)}
## Dividends being watched (next 25 trading days)
{md_table(up_df)}
## Closed trades (latest 30)
{md_table(tr_df)}
## Month by month
{md_table(month)}
## Skipped dividends and why
{md_table(sk_df)}
## Rules in short
- NSE EQ shares of ₹{R['MIN_MCAP']}–{R['MAX_MCAP']:,} cr with a dividend of at least {R['MIN_YIELD']*100:g}% of the price, at least ₹{R['MIN_VALUE_CR']*100:g} lakh traded a day, no circuit locks, no bonus/split nearby.
- Buy on the later of 10 trading days before the ex-date and the first trading day after the record-date news, at the average of calm 1-minute candles (no volume spikes), in 5 orders.
- Sell at the close of the last cum day; 10% stop-loss below the buy price.
- Size: 10% of capital at the start of each quarter as margin per share (Kite, then Upstox, then Groww MTF; otherwise cash, 2x for dividends of 1.5% or more), never more than 10% of the day's traded value.
- Costs: per-order brokerage, STT, stamp duty, exchange and SEBI fees, GST, DP, pledge, MTF interest and an estimate of our own price impact.

**Live dashboard: https://ajbhutada.github.io/dividend-paper-trader/** (updated after every bot job). Example with a full year of past-data trades: https://ajbhutada.github.io/dividend-paper-trader/example-2026/. Research and paper trading only, not investment advice.
"""
    (root / "README.md").write_text(readme, encoding="utf-8")

    # ---- interactive-ish HTML (self-contained)
    def htable(df):
        if df is None or not len(df):
            return "<p class='muted'>None yet.</p>"
        return "<div class='tw'><table><thead><tr>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns) + "</tr></thead><tbody>" + \
            "".join("<tr>" + "".join(f"<td>{html.escape('' if pd.isna(v) else str(v))}</td>" for v in r) + "</tr>" for r in df.itertuples(index=False)) + "</tbody></table></div>"
    pts = ""
    if len(eq) > 1:
        e = eq[eq.date >= start] if (eq.date >= start).any() else eq
        lo, hi = float(e.equity.min()) * 0.98, float(e.equity.max()) * 1.02
        xs = [20 + 760 * k / max(len(e) - 1, 1) for k in range(len(e))]
        ys = [200 - 180 * (v - lo) / max(hi - lo, 1) for v in e.equity]
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dividend paper trading</title>
<style>body{{font-family:Segoe UI,system-ui,sans-serif;margin:0;padding:20px;background:#f6f7f5;color:#1a1f1c}}.w{{max-width:1000px;margin:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:6px 8px;border-bottom:1px solid #dde1dd;text-align:left;white-space:nowrap}}
th{{background:#eef1ee;cursor:pointer}}.tw{{overflow-x:auto;background:#fff;border:1px solid #dde1dd;border-radius:8px}}.muted{{color:#5b6560}}
@media (prefers-color-scheme:dark){{body{{background:#111513;color:#e5eae7}}.tw{{background:#181d1a;border-color:#2b322e}}th{{background:#1f2622}}td,th{{border-color:#2b322e}}}}</style></head>
<body><div class="w"><h1>Dividend run-up paper trading</h1><p class="muted">Updated after the close on {today:%d %b %Y}. Paper money only.</p>
{htable(pd.DataFrame(summary, columns=['', ' ']))}
<h2>Equity</h2><svg viewBox="0 0 800 220" width="100%" style="background:#fff0;border:1px solid #dde1dd;border-radius:8px"><polyline fill="none" stroke="#1f6f5c" stroke-width="2.5" points="{pts}"/></svg>
<h2>Open positions</h2>{htable(open_df)}<h2>Dividends being watched</h2>{htable(up_df)}<h2>Closed trades</h2>{htable(tr.sort_values('sell_date', ascending=False) if len(tr) else tr)}
<h2>Month by month</h2>{htable(month)}<h2>Skipped and why</h2>{htable(sk_df)}
<p class="muted">Research and paper trading only, not investment advice.</p></div>
<script>document.querySelectorAll('th').forEach(th=>th.addEventListener('click',()=>{{const t=th.closest('table'),i=[...th.parentNode.children].indexOf(th),rows=[...t.tBodies[0].rows];
const num=s=>parseFloat(s.replace(/[₹,%+]/g,''));const asc=th.dataset.a!=='1';th.dataset.a=asc?'1':'0';rows.sort((a,b)=>{{const x=a.cells[i].innerText,y=b.cells[i].innerText;
const nx=num(x),ny=num(y);return (isNaN(nx)||isNaN(ny)?x.localeCompare(y):nx-ny)*(asc?1:-1)}});rows.forEach(r=>t.tBodies[0].appendChild(r))}}))</script></body></html>"""
    (root / "dashboard.html").write_text(page, encoding="utf-8")
    if (root / "docs").exists() or root.resolve() == Path(__file__).resolve().parent.parent:
        dashboard_json(sd, root / "docs/data/dashboard.json")

    # ---- weekly report on the last trading day of the week
    if weekly:
        wk0 = today - timedelta(days=today.weekday())
        wtr = tr[tr.sell_date >= wk0.isoformat()] if len(tr) else tr
        body = f"""Weekly paper-trading report, week ending {today:%d %b %Y}

@AjBhutada here is this week's summary.

{S}
### Trades closed this week
{md_table(wtr[['symbol', 'buy_date', 'avg_cost', 'sell_date', 'exit_price', 'exit_reason', 'pnl', 'pnl_pct_margin']] if len(wtr) else wtr)}
### Open positions
{md_table(open_df)}
### Coming up
{md_table(up_df)}
Full dashboard: https://ajbhutada.github.io/dividend-paper-trader/ . Paper trading only, not investment advice.
"""
        (root / "reports").mkdir(parents=True, exist_ok=True)
        (root / "reports/weekly_pending.md").write_text(body, encoding="utf-8")
        (root / f"reports/weekly_{today.isoformat()}.md").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------- data for the live dashboard (GitHub Pages: docs/index.html)
def _rows(df):
    if df is None or not len(df):
        return []
    return json.loads(df.to_json(orient="records", force_ascii=False))


def dashboard_json(sd, out, title="Dividend run-up paper trading", note=""):
    """Everything the dashboard page shows, in one JSON file. Rebuilt after every bot job."""
    from datetime import datetime, timezone
    sd, out = Path(sd), Path(out)
    rd = lambda f: (sd / f).exists() and (sd / f).stat().st_size > 5
    meta = json.loads((sd / "meta.json").read_text()) if (sd / "meta.json").exists() else {}
    pos = json.loads((sd / "positions.json").read_text()) if rd("positions.json") else []
    tr = pd.read_csv(sd / "trades.csv") if rd("trades.csv") else pd.DataFrame()
    eq = pd.read_csv(sd / "equity.csv") if rd("equity.csv") else pd.DataFrame()
    sig = pd.read_csv(sd / "signals.csv") if rd("signals.csv") else pd.DataFrame()
    live_dir = sd / "live"
    plans = sorted(live_dir.glob("*_plan.json")) if live_dir.exists() else []
    plan = json.loads(plans[-1].read_text()) if plans else None
    if plan:
        plan.pop("signals", None)
    fills = []
    for f in sorted(live_dir.glob("*_fills.csv"))[-15:] if live_dir.exists() else []:
        if f.stat().st_size > 5:
            fills += _rows(pd.read_csv(f))
    logs = sorted(live_dir.glob("*_log.txt")) if live_dir.exists() else []
    log_tail = logs[-1].read_text(encoding="utf-8").splitlines()[-120:] if logs else []
    eod_log = (sd / "last_run_log.txt").read_text(encoding="utf-8").splitlines()[-60:] if (sd / "last_run_log.txt").exists() else []
    minutes = json.loads((live_dir / "github_minutes.json").read_text()) if (live_dir / "github_minutes.json").exists() else {}
    root = Path(__file__).resolve().parent.parent
    settings = json.loads((root / "paper/settings.json").read_text()) if (root / "paper/settings.json").exists() else {}
    ist = timezone(timedelta(hours=5, minutes=30))
    data = dict(title=title, note=note, generated=datetime.now(ist).strftime("%Y-%m-%d %H:%M"), meta=meta, positions=pos,
                trades=_rows(tr), equity=_rows(eq), signals=_rows(sig), plan=plan, fills=fills, live_log=log_tail,
                live_log_date=logs[-1].name[:10] if logs else None, eod_log=eod_log, minutes=minutes,
                settings={k: v for k, v in settings.items() if not k.startswith("_")})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Write the dashboard data file only")
    ap.add_argument("--state", default="state"); ap.add_argument("--out", default="docs/data/dashboard.json")
    ap.add_argument("--title", default="Dividend run-up paper trading"); ap.add_argument("--note", default="")
    a = ap.parse_args()
    dashboard_json(a.state, a.out, a.title, a.note)
    print("wrote", a.out)
