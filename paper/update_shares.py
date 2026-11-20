"""Refresh paper/lists/shares.csv from a new half-year market-cap list (run in January and July).
Usage: python paper/update_shares.py LIST.xlsx START END   (the list's averaging window, e.g. 2026-07-01 2026-12-31)
Accepts NSE 'Average MCAP' files (lakh) and AMFI files (crore)."""
import io, sys, urllib.request
from datetime import date, timedelta
import pandas as pd
f, a, b = sys.argv[1], date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
x = pd.read_excel(f, header=None)
if any("NSE Symbol" in str(v) for v in x.iloc[1].values):          # AMFI layout, values in crore
    x = pd.read_excel(f, header=1); x.columns = [str(c).strip() for c in x.columns]
    s = [c for c in x.columns if c.startswith("NSE Symbol")][0]; m = [c for c in x.columns if c.startswith("NSE 6 month")][0]
    L = pd.Series(pd.to_numeric(x[m], errors="coerce").values, index=x[s].astype(str).str.strip())
else:                                                               # NSE layout, values in lakh
    x = pd.read_excel(f).iloc[:, :4]; x.columns = ["n", "symbol", "name", "m"]
    L = pd.Series(pd.to_numeric(x.m, errors="coerce").values / 100, index=x.symbol.astype(str).str.strip())
L = L[L.index != "-"].dropna()
closes, d = [], a
while d <= b:
    if d.weekday() < 5:
        try:
            raw = urllib.request.urlopen(urllib.request.Request(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
                                                                headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read()
            t = pd.read_csv(io.BytesIO(raw), skipinitialspace=True); t.columns = [c.strip() for c in t.columns]
            t = t[t.SERIES.str.strip() == "EQ"]; closes.append(pd.Series(pd.to_numeric(t.CLOSE_PRICE, errors="coerce").values, index=t.SYMBOL.str.strip()))
        except Exception:
            pass
    d += timedelta(days=1)
avg = pd.concat(closes, axis=1).mean(axis=1)
sh = (L / avg.reindex(L.index)).dropna()
pd.DataFrame({"symbol": sh.index, "shares": sh.values}).to_csv("paper/lists/shares.csv", index=False)
print(f"shares.csv updated for {len(sh)} symbols from {f}")
