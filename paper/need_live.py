"""Quick check (standard library only) whether this part of the day needs the live trader, so GitHub installs nothing on quiet
afternoons. Prints need=true / need=false for the workflow."""
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

job = sys.argv[1] if len(sys.argv) > 1 else "morning"
ROOT = Path(__file__).resolve().parent.parent
today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
need = True
if job in ("afternoon", "sells"):
    st = ROOT / "state"
    pos = json.loads((st / "positions.json").read_text()) if (st / "positions.json").exists() else []
    cfg = json.loads((ROOT / "paper/settings.json").read_text())
    sells = any(p["sell_date"] <= today for p in pos)
    pf = st / "live" / f"{today}_plan.json"
    if job == "sells":
        need = sells
    elif pf.exists():
        plan = json.loads(pf.read_text())
        need = any(b["status"] == "open" for b in plan["buys"]) or bool(cfg.get("watch_holding_days") and pos)
    # afternoon without a morning plan: let the live trader make the plan now
print(f"need={'true' if need else 'false'}")
