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


# z172: a US stock / ETF symbol as Alpaca knows it - 1-5 letters, an optional class suffix (BRK-B). Anything else in a
# holdings file (cash lines, futures like ESZ6, internal codes, blanks) made Alpaca answer 400 to the whole request.
SYMBOL_RE = re.compile(r"[A-Z]{1,5}(-[A-Z]{1,2})?")


def us_symbol(t):
    return bool(t) and bool(SYMBOL_RE.fullmatch(t))


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
        if us_symbol(t) and w > 0:
            out.append([t, round(w, 6)])
    out.sort(key=lambda x: -x[1])
    return out[:top]


def _kind(t):
    if t.startswith("^"):
        return "index"
    if t.endswith("=X"):
        return "fx"
    if t.endswith("=F"):
        return "future"
    if t.endswith("-USD"):
        return "crypto"
    if "." in t:
        return "foreign listing"
    return "us" if us_symbol(t) else "other"


def build(n_stream=None, max_symbols=None, momentum_min_dv=None):
    """z174: EVERY US-listed stock and ETF in the four books (739 of 906 on 10-31; new imports join automatically the
    next morning), plus the sector / index ETFs and the sector ETFs' holdings. Nothing is cut by size any more: the
    engine's liquidity floors (price, spread, dollar volume) decide what may trade. What Alpaca's stock API can't
    trade (indices, FX, futures, crypto, foreign listings) is counted, not silently dropped."""
    from .config import CFG
    n_stream = n_stream or CFG.stream_limit
    max_symbols = max_symbols or CFG.max_symbols
    momentum_min_dv = momentum_min_dv if momentum_min_dv is not None else CFG.momentum_min_dollar_volume
    prev = {}
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        pass
    rows, skipped, seen = {}, {}, set()
    for b in BOOKS:
        try:
            data = json.loads(get(f"{RAW}/{b}/main/data/master.json").decode())
            for r in (data if isinstance(data, list) else data.get("rows") or []):
                t = str(r.get("ticker") or "").upper()
                if not t or t in seen:
                    continue
                seen.add(t)
                k = _kind(t)
                if k != "us":
                    skipped[k] = skipped.get(k, 0) + 1
                    continue
                try:
                    px = float(r.get("price") or r.get("closeyest") or 0)
                    vol = float(r.get("volume") or 0)
                except ValueError:
                    px, vol = 0.0, 0.0
                etf = SECTOR_ETF.get(str(r.get("sector") or "").strip().lower())
                fund = bool(str(r.get("expenseRatio") or "").strip())      # z176: only funds carry an expense ratio
                rows[t] = {"price": px, "dv": px * vol, "etf": etf, "book": b, "fund": fund}
        except Exception as e:
            log(f"{b}: {e}")
    stocks = sorted((t for t in rows if t not in ETFS + INDEX_ETFS), key=lambda t: -rows[t]["dv"])
    weights = {}
    for etf in ETFS:
        try:
            weights[etf] = holdings(etf)
        except Exception as e:
            log(f"{etf} holdings: {e} - keeping the previous file's")
            if prev.get("etf_weights", {}).get(etf):
                weights[etf] = prev["etf_weights"][etf]
    members = sorted({t for w in weights.values() for t, _ in w})
    symbols = [t for t in dict.fromkeys(ETFS + INDEX_ETFS + stocks + members) if us_symbol(t)][:max_symbols]
    stream = list(dict.fromkeys(INDEX_ETFS + ETFS + stocks))[:n_stream]
    sector_etf = {t: rows[t]["etf"] for t in stocks if rows[t]["etf"]}
    momentum = [t for t in stocks if rows[t]["dv"] >= momentum_min_dv and rows[t]["price"] >= 5]
    liquidity = {t: round(rows[t]["dv"]) for t in rows}
    kinds = {t: ("fund" if (rows.get(t, {}).get("fund") or t in ETFS + INDEX_ETFS) else "stock") for t in symbols}
    doc = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": symbols, "stream": stream,
           "sector_etf": sector_etf, "etf_weights": weights, "momentum": momentum, "liquidity": liquidity, "kinds": kinds,
           "counts": {"bookTickers": len(seen), "usTradable": len(rows), "symbols": len(symbols), "stocks": len(stocks),
                      "withSector": len(sector_etf), "momentum": len(momentum), "etfsWithHoldings": len(weights),
                      "stocksInUniverse": sum(1 for v in kinds.values() if v == "stock"), "fundsInUniverse": sum(1 for v in kinds.values() if v == "fund"),
                      "stream": len(stream), "notOnAlpacaStocks": skipped}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))
    log(f"{len(seen)} book tickers: {len(rows)} US stocks / ETFs (all kept) · not tradable through Alpaca's stock API: "
        + (", ".join(f"{v} {k}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])) or "none")
        + f" · {len(symbols)} symbols with the ETF holdings · {len(sector_etf)} with a sector ETF · {len(momentum)} momentum-liquid · {len(stream)} streamed")
    return doc


def load():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return build()


if __name__ == "__main__":
    build()
    sys.exit(0)
