"""Starts the morning live job on time without relying on GitHub's own timer.

On 20 Nov 2026 GitHub started this repo's scheduled runs 4-7 hours late. That would leave the live trader idle all
morning, so wake.yml starts this script early (spare starts from 2:57 am India time). It
  1. starts the end-of-day run at once if the last one was missed (that run only catches up the missed day), then
  2. sleeps until 8:25 am and starts live.yml (job=morning) through workflow_dispatch, which GitHub starts within seconds.
A later spare start finds the morning job already started and stops. Standard library and the gh command only.
  python paper/wake.py                    what wake.yml runs
  python paper/wake.py --dry --at 07:10   show what it would do at that time of day; starts nothing
  python paper/wake.py --test             start live.yml's connection test now (checks that this job may start workflows)"""
import argparse, json, subprocess, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
GO, LATEST = "08:25", "14:55"               # start the morning job at GO; after LATEST the 9 pm backup handles the day
BUDGET_S = 5 * 3600 + 40 * 60               # GitHub stops a job after 6 hours: before that, hand over to a fresh wake-up


def gh(*args, dry=False):
    if dry:
        print("  would run: gh " + " ".join(args)); return ""
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def live_started(day):
    """True if a morning or afternoon live job was already started today (by GitHub's timer or by a wake-up)."""
    rows = json.loads(gh("run", "list", "--workflow", "live.yml", "-L", "50", "--json", "createdAt,displayTitle"))
    for r in rows:
        t = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).astimezone(IST)
        title = r["displayTitle"]
        if t.date() == day and t.strftime("%H:%M") >= "07:30" and ("morning" in title or "afternoon" in title):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true"); ap.add_argument("--at"); ap.add_argument("--day"); ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        gh("workflow", "run", "live.yml", "-f", "job=test"); print("started live.yml (connection test)"); return 0
    if (a.at or a.day) and not a.dry:
        raise SystemExit("--at / --day are for --dry checks only")
    t0 = time.time()

    def now():
        n = datetime.now(IST)
        d = datetime.strptime(a.day, "%Y-%m-%d").date() if a.day else n.date()
        return n if not a.at else datetime.combine(d, datetime.strptime(a.at, "%H:%M").time(), IST)

    today = now().date()
    if today.weekday() >= 5:
        print(f"{today}: weekend, nothing to do."); return 0

    # 1. the last end-of-day run missed? (it should be for the previous weekday or later) -> start it now
    meta = json.loads((ROOT / "state/meta.json").read_text())
    prev = today - timedelta(days=3 if today.weekday() == 0 else 1)
    if now().strftime("%H:%M") < "08:00" and (meta.get("last_run") or "") < prev.isoformat():
        print(f"the last end-of-day run was for {meta.get('last_run')}; starting it now to catch up")
        gh("workflow", "run", "daily.yml", dry=a.dry)

    # 2. the morning live job
    while True:
        hm = now().strftime("%H:%M")
        if hm >= LATEST:
            print(f"{hm}: too late to start the live trader today; the 9 pm run trades the day on 1-minute candles."); return 0
        if live_started(today):
            print(f"{hm}: the live trader was already started today; nothing to do."); return 0
        if hm >= GO:
            gh("workflow", "run", "live.yml", "-f", "job=morning", dry=a.dry)
            print(f"{hm}: started the morning live job."); return 0
        if a.dry:
            print(f"{hm}: would sleep until {GO}, then start the morning live job"); a.at = GO; continue
        wait = (datetime.combine(today, datetime.strptime(GO, "%H:%M").time(), IST) - now()).total_seconds()
        left = BUDGET_S - (time.time() - t0)
        if wait > left:                       # started very early: sleep what this job may, then hand over
            time.sleep(max(left, 0))
            gh("workflow", "run", "wake.yml"); print("started a fresh wake-up (a GitHub job may run at most 6 hours)"); return 0
        print(f"{hm}: sleeping until {GO}", flush=True)
        time.sleep(max(wait, 0) + 5)


if __name__ == "__main__":
    raise SystemExit(main())
