# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/universe.py
#  What the quant book watches, rebuilt each morning: the most liquid US stocks in the four books (by dollar
#  volume, price >= $5, with a sector), every SPDR sector ETF plus SPY / QQQ / IWM / DIA, and each sector ETF's
#  top holdings with their weights from State Street's daily holdings file (for the ETF arbitrage). The names
#  that stream (the free plan allows 30) are the ETFs and the largest stocks; the rest are polled every few
#  seconds. Falls back to the previous day's file for anything that can't be fetched.
#    -> data/quant/universe.json
# ============================================================
import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "quant" / "universe.json"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3")
SECTOR_ETF = {"technology": "XLK", "information technology": "XLK", "communication services": "XLC", "consumer cyclical": "XLY",
              "consumer discretionary": "XLY", "consumer defensive": "XLP", "consumer staples": "XLP", "energy": "XLE",
              "financial services": "XLF", "financials": "XLF", "financial": "XLF", "healthcare": "XLV", "health care": "XLV",
              "industrials": "XLI", "basic materials": "XLB", "materials": "XLB", "real estate": "XLRE", "utilities": "XLU"}
ETFS = sorted(set(SECTOR_ETF.values()))
INDEX_ETFS = ["SPY", "QQQ", "IWM", "DIA"]
SSGA = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{}.xlsx"
UA = {"User-Agent": "Mozilla/5.0 (valuatio quant)"}


def log(*a):
    print("[universe]", *a, flush=True)


def get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def xlsx_rows(blob):
    """The first worksheet of an .xlsx as lists of cell strings - enough for a holdings file, no pandas needed."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
            shared.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
    sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    xml = z.read(sheet).decode("utf-8", "replace")
    rows = []
    for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
        cells = {}
        for ref, attrs, body in re.findall(r'<c r="([A-Z]+)\d+"([^>]*)>(.*?)</c>', row, re.S):
            v = re.search(r"<v>(.*?)</v>", body, re.S)
            t = re.search(r"<t[^>]*>(.*?)</t>", body, re.S)
            val = v.group(1) if v else (t.group(1) if t else "")
            if 't="s"' in attrs and val.isdigit():
                val = shared[int(val)] if int(val) < len(shared) else ""
            cells[ref] = val
        if cells:
            cols = sorted(cells, key=lambda c: (len(c), c))
            rows.append([cells[c] for c in cols])
    return rows


def holdings(etf, top=25):
    rows = xlsx_rows(get(SSGA.format(etf.lower())))
    hdr = next(i for i, r in enumerate(rows[:20]) if "Ticker" in [x.strip() for x in r])
    names = [x.strip() for x in rows[hdr]]
    ti, wi = names.index("Ticker"), names.index("Weight")
    out = []
    for r in rows[hdr + 1:]:
        if len(r) <= max(ti, wi):
            continue
        t = r[ti].strip().upper().replace(".", "-").replace("/", "-")
        try:
            w = float(r[wi]) / 100
        except ValueError:
            continue
        if t and t not in ("-", "CASH_USD", "NAN") and w > 0:
            out.append([t, round(w, 6)])
    out.sort(key=lambda x: -x[1])
    return out[:top]


def build(n_stocks=120, n_stream=30):
    prev = {}
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        pass
    rows = {}
    for b in BOOKS:
        try:
            data = json.loads(get(f"{RAW}/{b}/main/data/master.json").decode())
            for r in (data if isinstance(data, list) else data.get("rows") or []):
                t = str(r.get("ticker") or "").upper()
                if not t or t.startswith("^") or t.endswith(("=X", "=F", "-USD")) or "." in t:
                    continue
                try:
                    px, vol = float(r.get("price") or 0), float(r.get("volume") or 0)
                except ValueError:
                    continue
                etf = SECTOR_ETF.get(str(r.get("sector") or "").strip().lower())
                rows.setdefault(t, {"price": px, "dv": px * vol, "etf": etf})
        except Exception as e:
            log(f"{b}: {e}")
    stocks = sorted((t for t, v in rows.items() if v["etf"] and v["price"] >= 5 and t not in ETFS + INDEX_ETFS), key=lambda t: -rows[t]["dv"])[:n_stocks]
    weights = {}
    for etf in ETFS:
        try:
            weights[etf] = holdings(etf)
        except Exception as e:
            log(f"{etf} holdings: {e} - keeping the previous file's")
            if prev.get("etf_weights", {}).get(etf):
                weights[etf] = prev["etf_weights"][etf]
    members = sorted({t for w in weights.values() for t, _ in w})
    symbols = list(dict.fromkeys(ETFS + INDEX_ETFS + stocks + members))
    stream = list(dict.fromkeys(INDEX_ETFS + ETFS + stocks))[:n_stream]
    doc = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": symbols, "stream": stream,
           "sector_etf": {t: rows[t]["etf"] for t in stocks}, "etf_weights": weights, "momentum": stocks[:80],
           "counts": {"symbols": len(symbols), "stocks": len(stocks), "etfsWithHoldings": len(weights), "stream": len(stream)}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))
    log(f"{len(symbols)} symbols · {len(stocks)} stocks · {len(weights)} ETFs with holdings · {len(stream)} streamed")
    return doc


def load():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return build()


if __name__ == "__main__":
    build()
    sys.exit(0)
