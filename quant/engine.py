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
#  z180: only a lab v2 verdict (session-clustered statistics) can send a signal live - an older models.json is read as
#  "nothing approved" (everything shadow) until the lab re-runs; and an approval that is split by the macro quad trades
#  live only while the regime is dialed in and the nowcast agrees with the official quad (STATUS 1b) - else shadow.
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
from .segments import matches
from .strategies import ALL


MIN_LAB_VERSION = 2                     # z180: approvals from per-trade statistics (lab v1) are not trusted


def load_models(data_dir, log=print):
    """z175: the lab's verdict (data/quant/models.json) - per strategy, the parameter variants it found and the segments
    where a variant passed out of sample. None when the lab hasn't run (the older all-or-nothing gate then applies).
    z180: a verdict from an older lab counts as "nothing approved" ({}): every strategy shadow until the lab re-runs."""
    try:
        doc = json.loads((Path(data_dir) / "models.json").read_text())
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    if int(doc.get("labVersion") or 1) < MIN_LAB_VERSION:
        n = sum(len((m or {}).get("live") or []) for m in (doc.get("strategies") or {}).values())
        log(f"[engine] models.json is from lab v{doc.get('labVersion') or 1} (per-trade statistics): its {n} approval(s) are not trusted "
            f"for live - every strategy runs in shadow until the lab (v{MIN_LAB_VERSION}, judged by session) re-runs")
        return {}
    return doc.get("strategies") or {}


def regime_ok(uni):
    """z180 (STATUS 1b): regime-split trading needs the regime dialed in AND the nowcast agreeing with the official
    quad. Missing = not dialed in."""
    g = (uni or {}).get("regime") or {}
    return bool(g.get("ok"))


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
        self.gate = load_gate(self.dir)
        self.models = load_models(self.dir, log=log)
        # z175: every strategy runs its original rules everywhere (live where approved, shadow elsewhere - the evidence
        # keeps coming); each variant the lab approved somewhere runs too, but only trades in its approved segments
        self.strategies = []
        for n in cfg.strategies:
            if n not in ALL:
                continue
            self.strategies.append(ALL[n]())
            m = (self.models or {}).get(n) or {}
            for vid in sorted({a.get("variant") for a in (m.get("live") or []) if a.get("variant") not in (None, "default")}):
                params = (m.get("variants") or {}).get(vid)
                if params is not None:
                    self.strategies.append(ALL[n](params=params, variant=vid))
        self.day = self.et(self.clock()).date().isoformat()
        self.ledger = Ledger(cfg, self.day, self.dir)
        self.positions = []
        self.traded_today = defaultdict(set)
        self.last_exit = {}                     # z176: "strategy|symbol" -> epoch of its last close (the re-entry cooldown)
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

    def _approvals(self, sig):
        """The lab's approvals this signal falls inside (its variant, a segment it belongs to)."""
        m = (self.models or {}).get(sig.strategy) or {}
        return [a for a in (m.get("live") or []) if a.get("variant", "default") == (sig.variant or "default") and matches(sig.tags or {}, a.get("segment", ""))]

    def regime_blocked(self, sig):
        """z180: True when the signal's only approvals are quad-split and the regime gate is shut (STATUS 1b)."""
        if self.cfg.paper_all or self.models is None:
            return False
        ap = self._approvals(sig)
        return bool(ap) and all("quad:" in a.get("segment", "") for a in ap) and not regime_ok(self.uni)

    def live_signal(self, sig):
        """z175: live only if the lab approved this strategy's variant in a segment this signal belongs to.
        z180: a quad-split approval counts only while the regime gate is open."""
        if self.cfg.execution not in ("alpaca", "sim"):
            return False
        if self.cfg.paper_all:                    # z176: every signal to the paper account when asked to
            return (sig.variant or "default") == "default"
        if self.models is not None:
            return any("quad:" not in a.get("segment", "") or regime_ok(self.uni) for a in self._approvals(sig))
        return (sig.variant or "default") == "default" and self.live_for(sig.strategy)

    def live_for(self, strategy):
        if self.models is not None:
            return bool((self.models.get(strategy) or {}).get("live"))
        g = self.gate.get(strategy) or {}
        return bool(g.get("pass")) and self.cfg.execution in ("alpaca", "sim")

    def ctx(self, now):
        return SimpleNamespace(cfg=self.cfg, minute=self.minute(now), now=now, day_open_epoch=self.day_open_epoch(now),
                               open_symbols={p.symbol for p in self.positions}, traded_today=self.traded_today,
                               sector_etf=self.uni.get("sector_etf") or {}, etf_weights={k: v for k, v in (self.uni.get("etf_weights") or {}).items()},
                               momentum_universe=self.uni.get("momentum") or [], liquidity=self.uni.get("liquidity") or {},
                               kinds=self.uni.get("kinds") or {}, quad=self.uni.get("quad") or {})

    # ---- entries / exits ---------------------------------------------------------------------------
    def enter(self, sig, now):
        st = self.book.s.get(sig.symbol)
        le = self.last_exit.get(f"{sig.strategy}|{sig.symbol}")
        if le is not None and now - le < self.cfg.cooldown_min * 60:          # z176: no churning one name
            self.counts["skip:cooldown"] += 1
            return None
        liq = (self.uni.get("liquidity") or {})
        if liq and sig.symbol in liq and liq[sig.symbol] < self.cfg.min_dollar_volume:      # z174: every symbol is watched, not every one is traded
            self.counts["skip:illiquid"] += 1
            return None
        day_pnl = self.ledger.pnl
        ok, q = self.risk.check(sig, st, self.minute(now), now, self.positions, day_pnl)
        if not ok:
            self.counts[f"skip:{q}"] += 1
            return None
        mode = "live" if self.live_signal(sig) else "shadow"
        if mode == "shadow" and self.regime_blocked(sig):
            self.counts["shadow:regime gate"] += 1               # z180: approved, but only for a regime that isn't dialed in
        br = self.broker if mode == "live" else self.sim
        mid, spread = st.mid(), st.spread_bps()
        fq, px = br.fill(sig.symbol, "buy" if sig.side == "long" else "sell", q, st)
        if not fq or not px:
            self.counts["fill failed"] += 1
            return None
        p = Position(id=f"{self.day}-{sig.strategy[:3]}-{uuid.uuid4().hex[:8]}", strategy=sig.strategy, symbol=sig.symbol, side=sig.side, qty=fq,
                     entry_px=px, entry_mid=mid, entry_spread_bps=spread, entry_ts=now, stop_bps=sig.stop_bps, target_bps=sig.target_bps,
                     max_hold_min=sig.max_hold_min, mode=mode, reason=sig.reason, meta=dict(sig.meta, variant=sig.variant or "default", tags=dict(sig.tags or {})))
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
        self.last_exit[f"{p.strategy}|{p.symbol}"] = now
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
                s = next((x for x in self.strategies if x.name == p.strategy and x.variant == (p.meta or {}).get("variant", "default")), None)
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
        # z175: a non-default variant only acts where it was approved; live signals first, then by strength
        # z180: ... and where its approval waits on the regime gate it keeps trading in shadow (the evidence keeps coming)
        sigs = [x for x in sigs if (x.variant or "default") == "default" or self.live_signal(x) or self.regime_blocked(x)]
        sigs.sort(key=lambda x: (not self.live_signal(x), -x.score))
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
        self.last_exit = {k: float(v) for k, v in (doc.get("lastExit") or {}).items()}
        self.ledger.pnl = float(doc.get("pnl") or 0)
        self.log(f"[engine] picked up {len(self.positions)} open positions from the previous job")

    def save_open(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "open.json").write_text(json.dumps({"day": self.day, "pnl": self.ledger.pnl, "traded": {k: sorted(v) for k, v in self.traded_today.items()},
                                                         "lastExit": self.last_exit,
                                                         "positions": [p.__dict__ for p in self.positions]}, indent=1))

    def state(self, now, feed_status=None):
        return {"day": self.day, "at": now, "minute": self.minute(now), "open": [{"symbol": p.symbol, "side": p.side, "qty": p.qty, "strategy": p.strategy,
                "mode": p.mode, "entry": p.entry_px, "moveBps": round(p.move_bps((self.book.s.get(p.symbol) or self.book.get(p.symbol)).mid() or p.entry_px), 1)}
                for p in self.positions], "dayPnl": round(self.ledger.pnl, 2), "closedToday": len(self.ledger.closed),
                "counts": dict(self.counts), "feed": feed_status or {}, "gate": self.gate, "regime": (self.uni or {}).get("regime") or {}}

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
