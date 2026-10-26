import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant.book import Book
from quant.feed import AlpacaStream


class StreamProtocol(unittest.TestCase):
    """A local websocket server speaking Alpaca's market-data protocol: connected -> auth -> subscribe -> data;
    it drops the connection once to prove the client reconnects and re-subscribes."""

    def test_auth_subscribe_data_and_reconnect(self):
        import websockets
        seen = {"auth": 0, "subs": [], "conns": 0}

        async def handler(ws):
            seen["conns"] += 1
            await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
            a = json.loads(await ws.recv())
            seen["auth"] += a.get("action") == "auth" and a.get("key") == "k"
            await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
            sub = json.loads(await ws.recv())
            seen["subs"].append(sub)
            await ws.send(json.dumps([{"T": "subscription", "quotes": sub["quotes"], "trades": sub["trades"]}]))
            px = 230.0 + seen["conns"]
            await ws.send(json.dumps([{"T": "q", "S": "AAPL", "bp": px, "ap": px + 0.02, "bs": 3, "as": 4, "t": "2026-10-26T14:30:05.123Z"},
                                      {"T": "t", "S": "BRK.B", "p": 480.5, "s": 10, "t": "2026-10-26T14:30:06Z"}]))
            if seen["conns"] == 1:
                await ws.close()                                   # drop the first connection
                return
            await asyncio.sleep(5)

        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                port = srv.sockets[0].getsockname()[1]
                b = Book()
                s = AlpacaStream(b, ["AAPL", "BRK-B"], "iex", "k", "s", url=f"ws://127.0.0.1:{port}")
                stop = asyncio.Event()
                t = asyncio.create_task(s.run(stop))
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if seen["conns"] >= 2 and abs((b.get("AAPL").bid or 0) - 232.0) < 1e-9:
                        break
                stop.set()
                await asyncio.wait_for(t, 10)
                return b, s

        b, s = asyncio.run(main())
        self.assertEqual(seen["conns"], 2, "reconnected after the drop")
        self.assertEqual(seen["auth"], 2)
        self.assertEqual(seen["subs"][1], {"action": "subscribe", "quotes": ["AAPL", "BRK.B"], "trades": ["AAPL", "BRK.B"]})
        self.assertAlmostEqual(b.get("AAPL").mid(), 232.01)                # data from the second connection
        self.assertEqual(b.get("BRK-B").last, 480.5)
        self.assertGreaterEqual(s.msgs, 4)


if __name__ == "__main__":
    unittest.main()
