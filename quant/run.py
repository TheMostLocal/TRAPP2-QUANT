# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/run.py
#  Start the engine for one job: today's universe (rebuilt if it isn't from today), the feeds, and a time budget
#  (QUANT_BUDGET_MIN, default 345 - a GitHub job may run 6 h; quant/engine_job.sh queues the next job).
#  z169: (1) no Alpaca keys -> stop at once with a clear error instead of sitting blind for six hours;
#  (2) started early (or after the close on a weeknight) it waits for 0920 ET of the next session inside its
#  budget, or sleeps to the end of the job and exits 3 = "queue the next job" - so the engine no longer depends
#  on GitHub starting the 0920 schedule (it created few scheduled runs on 10-26..10-30). Gaps over 20 h
#  (weekends, holidays) exit 0: the heartbeat or the schedule starts Monday.
#  Exit codes: 0 done · 2 not configured · 3 bridging - queue the next job.
# ============================================================
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from . import market_hours as mh
from .config import CFG

LEAD_MIN = 10                       # start 10 minutes before the open
BRIDGE_MAX_H = float(os.environ.get("QUANT_BRIDGE_MAX_H", "20"))   # a weeknight is 1600 -> 0920 = 17.3 h; a weekend 65 h


def seconds_to_start(now_utc=None):
    """0 if the engine should run now (a trading day, between 0920 ET and the close), else seconds until 0920 ET
    of the next session."""
    now_utc = now_utc or datetime.now(timezone.utc)
    et = mh.et_now(now_utc)
    ok, close, _ = mh.day_status(et.date())
    m = et.hour * 60 + et.minute
    if ok and mh.OPEN_MIN - LEAD_MIN <= m < close:
        return 0
    if ok and m < mh.OPEN_MIN - LEAD_MIN:
        start = et.replace(hour=9, minute=30, second=0, microsecond=0) - timedelta(minutes=LEAD_MIN)
    else:
        nxt = mh.next_open(et)
        if nxt is None:
            return None
        start = nxt - timedelta(minutes=LEAD_MIN)
    return max(0, int((start - et).total_seconds()))


def main():
    if not (os.environ.get("ALPACA_KEY_ID", "").strip() and os.environ.get("ALPACA_SECRET_KEY", "").strip()):
        print("::error::ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set in TRAPP2-QUANT's Actions secrets - the engine has "
              "no market data and no paper account. Add them (the same paper keys as the books) and re-run.", flush=True)
        return 2
    budget = float(os.environ.get("QUANT_BUDGET_MIN", "345")) * 60
    t0 = time.time()
    wait = seconds_to_start()
    if wait is None:
        print("[run] no session found in the next 20 days", flush=True)
        return 0
    if wait > 0:
        if wait > BRIDGE_MAX_H * 3600:
            print(f"[run] {mh.session_now()['text']} - the next session is {wait / 3600:.1f} h away (weekend / holiday): not waiting", flush=True)
            return 0
        if wait <= budget - 15 * 60:
            print(f"[run] {mh.session_now()['text']} - waiting {wait // 60} min for 0920 ET", flush=True)
            time.sleep(wait)
        else:
            nap = max(0, budget - 5 * 60)
            print(f"[run] {mh.session_now()['text']} - 0920 ET is {wait / 3600:.1f} h away: sleeping {nap / 3600:.1f} h, then the next job takes over", flush=True)
            time.sleep(nap)
            return 3
    from .engine import Engine
    from .feed import FeedManager
    from .universe import build, load
    uni = load()
    if str(uni.get("asOf", ""))[:10] != mh.et_now().date().isoformat():
        uni = build()
    eng = Engine(CFG, uni)
    stream = uni["symbols"] if CFG.feed == "sip" else uni["stream"][:CFG.stream_limit]
    feeds = FeedManager(eng.book, uni["symbols"], stream, CFG)
    live = [n for n in CFG.strategies if eng.live_for(n)]
    print(f"[run] {len(uni['symbols'])} symbols ({len(stream)} streamed, feed {CFG.feed}) · live: {', '.join(live) or 'none'} · "
          f"shadow: {', '.join(n for n in CFG.strategies if n not in live) or 'none'} · execution {CFG.execution}"
          f" · Supabase {'on' if os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_SERVICE_ROLE') else 'OFF (GitHub only)'}", flush=True)
    left = budget - (time.time() - t0)
    summ = asyncio.run(eng.run(until_epoch=time.time() + left, feeds=feeds))
    lv = summ.get("live") or {}
    print(f"[run] done · trades today {len(eng.ledger.closed)} · day P&L ${eng.ledger.pnl:+.2f} · live overall n {lv.get('n')} win {lv.get('winRate')} "
          f"exp {lv.get('expectancyBps')} bps · counts {dict(eng.counts)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
