# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/engine.py
#  THE INTRADAY ENGINE. One process for the session: real-time data into the Book; every minute each strategy
#  looks for signals, the risk check sizes and approves them, the broker fills them; every 2 seconds every open
#  position is checked against its stop, target, time stop and its strategy's own exit; at 1555 ET everything is
#  closed. A strategy trades LIVE (Alpaca paper fills) only once its latest backtest passed the quality gate
#  (data/quant/gate.json: enough trades, positive expectancy net of costs, t-stat >= 2); until then it runs in
#  SHADOW (same signals, simulated fills, its own statistics) - evidence first, then capital.
#  Survives a job hand-off: open positions are saved to data/quant/open.json and picked up by the next job.
# ============================================================
import asyncio
import json
import time
import uuid
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from . import market_hours as mh
from .book import Book
from .broker import AlpacaBroker, SimBroker
from .config import CFG
from .ledger import DATA, Ledger
from .risk import Position, Risk
from .strategies import ALL


def load_gate(data_dir):
    try:
        return json.loads((Path(data_dir) / "gate.json").read_text()).get("strategies") or {}
    except Exception:
        return {}


class Engine:
    def __init__(self, cfg=CFG, universe=None, clock=time.time, broker=None, data_dir=None, log=print):
        self.cfg, self.clock, self.log = cfg, clock, log
        self.dir = Path(data_dir) if data_dir else DATA
        self.uni = universe or {}
        self.book = Book(self.uni.get("symbols") or [])
        self.risk = Risk(cfg)
        self.sim = SimBroker(cfg)
        self.broker = broker or (AlpacaBroker(cfg, sim=self.sim) if cfg.execution == "alpaca" else self.sim)
        self.strategies = [ALL[n]() for n in cfg.strategies if n in ALL]
        self.gate = load_gate(self.dir)
        self.day = self.et(self.clock()).date().isoformat()
        self.ledger = Ledger(cfg, self.day, self.dir)
        self.positions = []
        self.traded_today = defaultdict(set)
        self.flattened = False
        self.counts = defaultdict(int)
        self._restore()

    # ---- helpers -----------------------------------------------------------------------------------
    def et(self, now):
        from datetime import datetime, timezone
        return mh.et_now(datetime.fromtimestamp(now, timezone.utc))

    def minute(self, now):
        e = self.et(now)
        return e.hour * 60 + e.minute

    def day_open_epoch(self, now):
        e = self.et(now)
        return e.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()

    def live_for(self, strategy):
        g = self.gate.get(strategy) or {}
        return bool(g.get("pass")) and self.cfg.execution in ("alpaca", "sim")

    def ctx(self, now):
        return SimpleNamespace(cfg=self.cfg, minute=self.minute(now), now=now, day_open_epoch=self.day_open_epoch(now),
                               open_symbols={p.symbol for p in self.positions}, traded_today=self.traded_today,
                               sector_etf=self.uni.get("sector_etf") or {}, etf_weights={k: v for k, v in (self.uni.get("etf_weights") or {}).items()},
                               momentum_universe=self.uni.get("momentum") or [])

    # ---- entries / exits ---------------------------------------------------------------------------
    def enter(self, sig, now):
        st = self.book.s.get(sig.symbol)
        day_pnl = self.ledger.pnl
        ok, q = self.risk.check(sig, st, self.minute(now), now, self.positions, day_pnl)
        if not ok:
            self.counts[f"skip:{q}"] += 1
            return None
        mode = "live" if self.live_for(sig.strategy) else "shadow"
        br = self.broker if mode == "live" else self.sim
        mid, spread = st.mid(), st.spread_bps()
        fq, px = br.fill(sig.symbol, "buy" if sig.side == "long" else "sell", q, st)
        if not fq or not px:
            self.counts["fill failed"] += 1
            return None
        p = Position(id=f"{self.day}-{sig.strategy[:3]}-{uuid.uuid4().hex[:8]}", strategy=sig.strategy, symbol=sig.symbol, side=sig.side, qty=fq,
                     entry_px=px, entry_mid=mid, entry_spread_bps=spread, entry_ts=now, stop_bps=sig.stop_bps, target_bps=sig.target_bps,
                     max_hold_min=sig.max_hold_min, mode=mode, reason=sig.reason, meta=dict(sig.meta))
        self.positions.append(p)
        self.traded_today[sig.strategy].add(sig.symbol)
        self.counts[f"open:{mode}"] += 1
        self.log(f"[engine] OPEN {mode} {p.side} {p.qty} {p.symbol} @ {px:.4f} ({sig.strategy}: {sig.reason}) stop {p.stop_bps} target {p.target_bps} bps")
        return p

    def exit(self, p, now, reason):
        st = self.book.s.get(p.symbol)
        br = self.broker if p.mode == "live" else self.sim
        fq, px = br.fill(p.symbol, "sell" if p.side == "long" else "buy", p.qty, st)
        if not fq or not px:
            self.counts["exit fill failed"] += 1
            return None
        self.positions.remove(p)
        t = self.ledger.close(p, px, st.mid() if st else None, now, reason)
        self.counts[f"close:{p.mode}"] += 1
        self.log(f"[engine] CLOSE {p.mode} {p.symbol} {t['net_bps']:+.1f} bps ${t['pnl']:+.2f} ({reason})")
        return t

    def check_exits(self, now, minute_tick=False):
        ctx = self.ctx(now) if minute_tick else None
        for p in list(self.positions):
            st = self.book.s.get(p.symbol)
            if not st or not st.mid():
                continue
            mv = p.move_bps(st.mid())
            held_min = (now - p.entry_ts) / 60
            reason = None
            if mv <= -p.stop_bps:
                reason = f"stop ({mv:+.1f} bps)"
            elif mv >= p.target_bps:
                reason = f"target ({mv:+.1f} bps)"
            elif held_min >= p.max_hold_min:
                reason = f"time ({held_min:.0f} min)"
            elif minute_tick:
                s = next((x for x in self.strategies if x.name == p.strategy), None)
                reason = s.should_exit(p, self.book, ctx) if s else None
            if reason:
                self.exit(p, now, reason)

    def flatten(self, now, reason="flatten 1555 ET"):
        for p in list(self.positions):
            self.exit(p, now, reason)
        if not self.flattened and self.cfg.execution == "alpaca" and hasattr(self.broker, "flatten_all"):
            self.broker.flatten_all()
        self.flattened = True

    def on_minute(self, now):
        m = self.minute(now)
        self.check_exits(now, minute_tick=True)
        if m >= self.cfg.flatten_at:
            return
        ctx = self.ctx(now)
        sigs = []
        for s in self.strategies:
            try:
                sigs += s.on_minute(self.book, ctx)
            except Exception as e:
                self.log(f"[engine] {s.name}: {type(e).__name__} {e}")
        sigs.sort(key=lambda x: -x.score)
        for sig in sigs:
            self.enter(sig, now)

    # ---- state across jobs -------------------------------------------------------------------------
    def _restore(self):
        p = self.dir / "open.json"
        try:
            doc = json.loads(p.read_text())
        except Exception:
            return
        if doc.get("day") != self.day:
            return
        for d in doc.get("positions") or []:
            self.positions.append(Position(**d))
        for k, v in (doc.get("traded") or {}).items():
            self.traded_today[k] = set(v)
        self.ledger.pnl = float(doc.get("pnl") or 0)
        self.log(f"[engine] picked up {len(self.positions)} open positions from the previous job")

    def save_open(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "open.json").write_text(json.dumps({"day": self.day, "pnl": self.ledger.pnl, "traded": {k: sorted(v) for k, v in self.traded_today.items()},
                                                         "positions": [p.__dict__ for p in self.positions]}, indent=1))

    def state(self, now, feed_status=None):
        return {"day": self.day, "at": now, "minute": self.minute(now), "open": [{"symbol": p.symbol, "side": p.side, "qty": p.qty, "strategy": p.strategy,
                "mode": p.mode, "entry": p.entry_px, "moveBps": round(p.move_bps((self.book.s.get(p.symbol) or self.book.get(p.symbol)).mid() or p.entry_px), 1)}
                for p in self.positions], "dayPnl": round(self.ledger.pnl, 2), "closedToday": len(self.ledger.closed),
                "counts": dict(self.counts), "feed": feed_status or {}, "gate": self.gate}

    def persist(self, now, feed_status=None):
        self.ledger.flush_files()
        self.ledger.write_summary()
        self.save_open()
        self.ledger.push_supabase(self.state(now, feed_status))

    # ---- the session loop --------------------------------------------------------------------------
    async def run(self, until_epoch=None, feeds=None, sleep=asyncio.sleep, tick=1.0):
        stop = asyncio.Event()
        task = asyncio.create_task(feeds.run(stop)) if feeds else None
        last_min, last_exit, last_save = None, 0.0, self.clock()
        sess = mh.day_status(self.et(self.clock()).date())
        close_min = sess[1] or 16 * 60
        flatten_at = min(self.cfg.flatten_at, close_min - 5)
        try:
            while True:
                now = self.clock()
                m = self.minute(now)
                if (until_epoch and now >= until_epoch) or m >= close_min + 2:
                    break
                self.book.roll(now)
                if m >= flatten_at and not self.flattened:
                    self.flatten(now)
                mk = int(now // 60)
                if mk != last_min and 9 * 60 + 30 <= m < close_min:
                    last_min = mk
                    self.on_minute(now)
                elif now - last_exit >= 2:
                    self.check_exits(now)
                    last_exit = now
                if now - last_save >= 300:
                    self.persist(now, feeds.status() if feeds else None)
                    last_save = now
                await sleep(tick)
        finally:
            stop.set()
            if task:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except Exception:
                    pass
            now = self.clock()
            if self.minute(now) >= flatten_at and self.positions:
                self.flatten(now)
            self.persist(now, feeds.status() if feeds else None)
        return self.ledger.summary()
