# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/broker.py
#  Execution. AlpacaBroker sends market orders to the Alpaca PAPER account (same keys as the market data), so
#  every live trade is filled by Alpaca against real quotes - the honest test of an intraday strategy. SimBroker
#  fills at the far side of the current quote plus slippage: used for shadow strategies, backtests, and when the
#  paper API is unavailable. Both return (filled_qty, avg_price) or (0, None).
# ============================================================
import json
import os
import time
import urllib.error
import urllib.request
import uuid


class SimBroker:
    def __init__(self, cfg):
        self.cfg = cfg

    def fill(self, symbol, side, qty, st):
        if not st or qty <= 0:
            return 0, None
        bid, ask = st.bid, st.ask
        mid = st.mid()
        if not mid:
            return 0, None
        if not (bid and ask):
            hs = mid * self.cfg.backtest_half_spread_bps / 1e4
            bid, ask = mid - hs, mid + hs
        slip = self.cfg.slippage_bps / 1e4
        px = ask * (1 + slip) if side == "buy" else bid * (1 - slip)
        return qty, round(px, 6)

    def flatten_all(self):
        return True


class AlpacaBroker:
    def __init__(self, cfg, key=None, secret=None, sim=None):
        self.cfg = cfg
        self.key = (key or os.environ.get("ALPACA_KEY_ID") or "").strip()
        self.secret = (secret or os.environ.get("ALPACA_SECRET_KEY") or "").strip()
        self.base = cfg.alpaca_trading.rstrip("/")
        self.sim = sim or SimBroker(cfg)
        self.errors = 0

    def _req(self, method, path, body=None, timeout=15):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else {}

    def enabled(self):
        return bool(self.key and self.secret)

    def fill(self, symbol, side, qty, st, wait=10.0, poll=0.4, sleep=time.sleep):
        if not self.enabled() or qty <= 0:
            return 0, None
        coid = "q-" + uuid.uuid4().hex[:20]
        try:
            o = self._req("POST", "/v2/orders", {"symbol": symbol.replace("-", "."), "qty": str(int(qty)), "side": side,
                                                 "type": "market", "time_in_force": "day", "client_order_id": coid})
        except urllib.error.HTTPError as e:
            self.errors += 1
            print(f"[broker] {symbol} {side} {qty}: rejected HTTP {e.code} {e.read().decode('utf-8', 'replace')[:160]}", flush=True)
            return 0, None
        except Exception as e:
            self.errors += 1
            print(f"[broker] {symbol} {side} {qty}: {e}", flush=True)
            return 0, None
        oid, t0 = o.get("id"), time.time()
        while True:
            st_ = o.get("status")
            if st_ == "filled":
                return int(float(o.get("filled_qty") or 0)), float(o.get("filled_avg_price"))
            if st_ in ("canceled", "expired", "rejected", "suspended"):
                print(f"[broker] {symbol} {side} {qty}: {st_}", flush=True)
                return 0, None
            if time.time() - t0 > wait:
                fq = int(float(o.get("filled_qty") or 0))
                try:
                    self._req("DELETE", f"/v2/orders/{oid}")
                except Exception:
                    pass
                return (fq, float(o["filled_avg_price"])) if fq and o.get("filled_avg_price") else (0, None)
            sleep(poll)
            try:
                o = self._req("GET", f"/v2/orders/{oid}")
            except Exception:
                pass

    def flatten_all(self):
        """Safety net at the close: cancel everything open and close every position on the paper account."""
        if not self.enabled():
            return False
        try:
            self._req("DELETE", "/v2/positions?cancel_orders=true")
            return True
        except Exception as e:
            print(f"[broker] flatten_all: {e}", flush=True)
            return False
