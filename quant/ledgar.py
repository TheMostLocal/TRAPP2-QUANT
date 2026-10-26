# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/ledger.py
#  The quant book's own ledger - separate from the swing bot's (one book, one trader each): every closed trade
#  with its fills, the mid and spread at entry, fees, net bps and dollars, hold time and why it closed. Closed
#  trades append to data/quant/trades/<ET date>.jsonl (committed at the end of each job); summary.json carries
#  the statistics overall, per strategy, live vs shadow, per day. Supabase (Bot project) gets the same rows live.
# ============================================================
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .stats import summarize

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "quant"


class Ledger:
    def __init__(self, cfg, day, data_dir=None):
        self.cfg, self.day = cfg, day
        self.dir = Path(data_dir) if data_dir else DATA
        self.closed = []
        self.pnl = 0.0
        self.pending_sb = []

    def close(self, pos, exit_px, exit_mid, exit_ts, reason):
        sign = 1 if pos.side == "long" else -1
        gross = (exit_px / pos.entry_px - 1) * 1e4 * sign
        sell_px = exit_px if pos.side == "long" else pos.entry_px
        fee = pos.qty * sell_px * self.cfg.fee_bps_sell / 1e4
        pnl = pos.qty * (exit_px - pos.entry_px) * sign - fee
        net_bps = gross - self.cfg.fee_bps_sell
        t = {"id": pos.id, "day": self.day, "strategy": pos.strategy, "mode": pos.mode, "symbol": pos.symbol, "side": pos.side, "qty": pos.qty,
             "entry_ts": datetime.fromtimestamp(pos.entry_ts, timezone.utc).isoformat(timespec="seconds"),
             "exit_ts": datetime.fromtimestamp(exit_ts, timezone.utc).isoformat(timespec="seconds"),
             "hold_sec": int(exit_ts - pos.entry_ts), "entry_px": round(pos.entry_px, 4), "exit_px": round(exit_px, 4),
             "entry_mid": round(pos.entry_mid, 4), "exit_mid": round(exit_mid, 4) if exit_mid else None,
             "entry_spread_bps": round(pos.entry_spread_bps, 2) if pos.entry_spread_bps is not None else None,
             "slip_entry_bps": round((pos.entry_px / pos.entry_mid - 1) * 1e4 * sign, 2) if pos.entry_mid else None,
             "gross_bps": round(gross, 3), "fee": round(fee, 4), "net_bps": round(net_bps, 3), "pnl": round(pnl, 2),
             "stop_bps": pos.stop_bps, "target_bps": pos.target_bps, "reason_in": pos.reason, "reason_out": reason, "meta": pos.meta}
        self.closed.append(t)
        if pos.mode == "live":
            self.pnl += pnl
        self.pending_sb.append(t)
        return t

    # ---- persistence -------------------------------------------------------------------------------
    def flush_files(self):
        if not self.closed:
            return 0
        d = self.dir / "trades"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.day}.jsonl"
        have = set()
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    have.add(json.loads(line)["id"])
                except Exception:
                    pass
        new = [t for t in self.closed if t["id"] not in have]
        with p.open("a") as f:
            for t in new:
                f.write(json.dumps(t, separators=(",", ":")) + "\n")
        return len(new)

    def summary(self, days=60):
        rows = []
        d = self.dir / "trades"
        if d.exists():
            for p in sorted(d.glob("*.jsonl"))[-days:]:
                for line in p.read_text().splitlines():
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        ids = {r["id"] for r in rows}
        rows += [t for t in self.closed if t["id"] not in ids]
        b = self.cfg.benchmark_win_rate
        live = [r for r in rows if r.get("mode") == "live"]
        out = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "benchmarkWinRate": b,
               "live": summarize(live, b), "shadow": summarize([r for r in rows if r.get("mode") == "shadow"], b),
               "byStrategy": {}, "byDay": {}, "trades": len(rows)}
        for s in sorted({r["strategy"] for r in rows}):
            rr = [r for r in rows if r["strategy"] == s]
            out["byStrategy"][s] = {"live": summarize([r for r in rr if r.get("mode") == "live"], b),
                                    "shadow": summarize([r for r in rr if r.get("mode") == "shadow"], b)}
        for dday in sorted({r["day"] for r in rows})[-30:]:
            rr = [r for r in live if r["day"] == dday]
            out["byDay"][dday] = {"n": len(rr), "pnl": round(sum(r["pnl"] for r in rr), 2), **{k: v for k, v in summarize(rr, b).items() if k in ("winRate", "expectancyBps")}}
        return out

    def write_summary(self):
        s = self.summary()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "summary.json").write_text(json.dumps(s, indent=1))
        return s

    # ---- Supabase (Bot project): quant_trades rows + quant_state (the live picture) ---------------------
    def push_supabase(self, state=None):
        url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/").replace("/rest/v1", "")
        key = (os.environ.get("SUPABASE_SERVICE_ROLE") or os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
        if not (url and key):
            return False

        def post(table, rows, conflict):
            req = urllib.request.Request(f"{url}/rest/v1/{table}?on_conflict={conflict}", method="POST", data=json.dumps(rows).encode(),
                                         headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                  "Prefer": "resolution=merge-duplicates,return=minimal"})
            with urllib.request.urlopen(req, timeout=30):
                pass
        try:
            if self.pending_sb:
                post("quant_trades", [{"id": t["id"], "day": t["day"], "strategy": t["strategy"], "mode": t["mode"], "symbol": t["symbol"],
                                       "net_bps": t["net_bps"], "pnl": t["pnl"], "closed_at": t["exit_ts"], "trade": t} for t in self.pending_sb], "id")
                self.pending_sb = []
            if state is not None:
                post("quant_state", [{"id": "live", "doc": state, "updated_at": datetime.now(timezone.utc).isoformat()}], "id")
            return True
        except Exception as e:
            print(f"[ledger] Supabase: {e}", flush=True)
            return False
