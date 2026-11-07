# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/macro.py
#  z177 - THE QUAD for the quant book. TRAPP2-1/data/macro/quad.json carries the OFFICIAL growth / inflation quad
#  (FRED GDP + CPI, ~1-2 months behind) and the daily MARKET NOWCAST, and whether they agree. Every quant trade
#  records both; the lab segments by the OFFICIAL quad as it was known on each session's date (the full history is
#  in quad_history_full.json; a month's CPI is out ~45 days after the month starts), so a live trade and the lab's
#  history use one definition. Evidence first: the quad tags trades and segments; it does not tilt anything until
#  the lab shows a segment that earns it.
# ============================================================
import json
import urllib.request
from datetime import date, datetime, timedelta

RAW = "https://raw.githubusercontent.com/TheMostLocal/TRAPP2-1/main/data/macro"
KNOWN_LAG_DAYS = 45


def _get(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-quant"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def current_quads(doc=None):
    """{'official': 1-4|None, 'nowcast': 1-4|None, 'agree': bool|None, 'officialAsOf': ..., 'nowcastAsOf': ...}"""
    doc = doc if doc is not None else (_get(f"{RAW}/quad.json") or {})
    cur = doc.get("current") or {}
    off = doc.get("official") or doc.get("officialLatest") or (cur if cur.get("method") != "market-nowcast" else {}) or {}
    now = doc.get("nowcast") or (cur if cur.get("method") == "market-nowcast" else {}) or {}
    ag = doc.get("agree") or {}
    return {"official": off.get("quad"), "nowcast": now.get("quad"), "agree": ag.get("sameQuad"),
            "officialAsOf": off.get("asOf"), "nowcastAsOf": now.get("asOf"), "note": ag.get("note")}


def official_on(day, rows):
    """The official quad as it was KNOWN on `day`: the latest monthly row dated at least KNOWN_LAG_DAYS earlier."""
    if not rows:
        return None
    if isinstance(day, str):
        day = datetime.strptime(day[:10], "%Y-%m-%d").date()
    cutoff = (day - timedelta(days=KNOWN_LAG_DAYS)).isoformat()
    best = None
    for r in rows:
        if str(r.get("date")) <= cutoff and r.get("quad") in (1, 2, 3, 4):
            best = r["quad"]
    return best


def history_rows():
    d = _get(f"{RAW}/quad_history_full.json", timeout=30) or {}
    return sorted(d.get("rows") or [], key=lambda r: str(r.get("date")))
