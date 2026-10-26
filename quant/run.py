# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/run.py
#  Start the engine for one job: today's universe (rebuilt if it isn't from today), the feeds, and a time budget
#  (QUANT_BUDGET_MIN, default 345 - a GitHub job may run 6 h; the workflow chains the next job if the session is
#  still open). On a VPS, set QUANT_BUDGET_MIN to cover the whole session.
# ============================================================
import asyncio
import os
import sys
import time

from . import market_hours as mh
from .config import CFG
from .engine import Engine
from .feed import FeedManager
from .universe import build, load


def main():
    s = mh.session_now()
    if not mh.day_status(mh.et_now().date())[0]:
        print(f"[run] {s['state']} - no session today")
        return 0
    uni = load()
    if str(uni.get("asOf", ""))[:10] != mh.et_now().date().isoformat():
        uni = build()
    eng = Engine(CFG, uni)
    stream = uni["symbols"] if CFG.feed == "sip" else uni["stream"][:CFG.stream_limit]
    feeds = FeedManager(eng.book, uni["symbols"], stream, CFG)
    live = [n for n in CFG.strategies if eng.live_for(n)]
    print(f"[run] {len(uni['symbols'])} symbols ({len(stream)} streamed, feed {CFG.feed}) · live: {', '.join(live) or 'none'} · "
          f"shadow: {', '.join(n for n in CFG.strategies if n not in live) or 'none'} · execution {CFG.execution}", flush=True)
    budget = float(os.environ.get("QUANT_BUDGET_MIN", "345")) * 60
    summ = asyncio.run(eng.run(until_epoch=time.time() + budget, feeds=feeds))
    lv = summ.get("live") or {}
    print(f"[run] done · trades today {len(eng.ledger.closed)} · day P&L ${eng.ledger.pnl:+.2f} · live overall n {lv.get('n')} win {lv.get('winRate')} "
          f"exp {lv.get('expectancyBps')} bps · counts {dict(eng.counts)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
