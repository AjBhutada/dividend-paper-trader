import http.cookiejar, json, urllib.request, urllib.parse
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
op.addheaders = [("User-Agent", UA), ("Accept", "application/json"), ("Accept-Language", "en-US,en;q=0.9"), ("Referer", "https://www.nseindia.com/")]
tests = {"nse_home": "https://www.nseindia.com/",
         "nse_corp_actions": "https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=01-11-2026&to_date=30-11-2026",
         "nse_announcements": "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=16-11-2026&to_date=20-11-2026",
         "nse_holidays": "https://www.nseindia.com/api/holiday-master?type=trading",
         "nse_archive_bhav": "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_19112026.csv",
         "nse_archive_index": "https://nsearchives.nseindia.com/content/indices/ind_close_all_19112026.csv",
         "upstox_intraday": "https://api.upstox.com/v3/historical-candle/intraday/" + urllib.parse.quote("NSE_EQ|INE965W01036", safe="") + "/minutes/1",
         "upstox_hist": "https://api.upstox.com/v3/historical-candle/" + urllib.parse.quote("NSE_EQ|INE965W01036", safe="") + "/minutes/1/2026-11-19/2026-11-19",
         "kite_mtf": "https://public.zrd.sh/crux/approved-mtf-securities.json",
         "upstox_instruments": "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"}
for k, u in tests.items():
    try:
        r = op.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=40); b = r.read()
        print(f"{k}: OK {r.status} {len(b)} bytes")
    except Exception as e:
        print(f"{k}: FAIL {e}")
