# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/segments.py
#  z175 - WHERE a strategy is judged. A strategy that loses on average can still have an edge in part of the market
#  (mid-size names, the middle of the day, short side, one sector). Every signal carries four tags; a segment is
#  one tag or two together ("liq:mid", "liq:mid|tod:mid") or "all". The lab (quant/lab.py) tests each strategy in
#  every segment, out of sample, and the engine trades live only in segments that passed.
#  z202: + the MARKET TAPE (quant/tape.py): "mkt" - the sell-off read as known at the open (calm / pressure / selloff)
#  and "spx" - the S&P 500 today at the signal's minute (SPY vs its prior close: dn / flat / up). Recorded, not
#  segmented: mktNow (the live Today's Trends label), mktRun (the S&P's run through the prior close), spxBps.
#  z206: + flowNow (recorded, not segmented) - the positioning label of the day (short-covering rally, de-grossing ...).
#  z207: + shortPct / dtc (recorded, not segmented) - a crowded name's short % of shares and days to cover (FINRA).
#  z209: + gammaNow (recorded, not segmented) - dealer gamma's sign from SPY's open interest: negative gamma is when
#        breakouts tend to run and reversion to fail; the lab gets the record before it gets a segment.
# ============================================================
from itertools import combinations

DIMS = ("liq", "tod", "side", "sector", "quad", "mkt", "spx")    # z177: + the official quad as known that day; z202: + the tape


def liq_bucket(dollar_volume):
    """Dollar volume traded a day: mega >= $1B, large >= $200M, mid >= $50M, small below."""
    if dollar_volume is None:
        return "unknown"
    v = float(dollar_volume)
    return "mega" if v >= 1e9 else "large" if v >= 2e8 else "mid" if v >= 5e7 else "small"


def tod_bucket(minute):
    """Eastern time of the entry: open (before 1030), mid (1030-1430), late (after)."""
    return "open" if minute < 10 * 60 + 30 else "mid" if minute < 14 * 60 + 30 else "late"


def tags_for(symbol, side, ctx, sector=None):
    liq = (getattr(ctx, "liquidity", None) or {}).get(symbol)
    sec = sector or (getattr(ctx, "sector_etf", None) or {}).get(symbol) or "none"
    kind = (getattr(ctx, "kinds", None) or {}).get(symbol) or ("fund" if sector == symbol else "stock")   # z176: shown, not segmented
    q = getattr(ctx, "quad", None) or {}
    out = {"liq": liq_bucket(liq), "tod": tod_bucket(ctx.minute), "side": side, "sector": sec, "kind": kind}
    if q.get("official"):
        out["quad"] = f"Q{q['official']}"                 # z177: a segment dimension (official, as known)
    if q.get("nowcast"):
        out["quadNow"] = f"Q{q['nowcast']}"               # z177: recorded, not segmented
    m = getattr(ctx, "mkt", None) or {}                   # z202: the market tape (quant/tape.py)
    if m.get("state"):
        out["mkt"] = m["state"]                           # segmented: the read as known at the open
        out["mktRun"] = m.get("run")                      # recorded: the S&P's run through the prior close
    mv = getattr(ctx, "spx_move", None)
    if mv is not None:
        from .tape import day_bucket
        out["spx"] = day_bucket(mv)                       # segmented: SPY vs its prior close at this minute
        out["spxBps"] = round(mv * 1e4, 1)                # recorded
    now = m.get("now") or {}
    if now.get("label"):
        out["mktNow"] = now["label"]                      # recorded: the live Today's Trends read (7 criteria)
    if now.get("flow"):
        out["flowNow"] = now["flow"]                      # z206: recorded - the tape's positioning label (not segmented)
    if now.get("gex") is not None:                        # z209: recorded - dealer gamma's sign (SPY's open interest x gamma)
        out["gammaNow"] = "neg" if now["gex"] < 0 else "pos"
    cr = (m.get("crowded") or {}).get(symbol)
    if cr:                                                # z207: recorded - the name's short % / days to cover (FINRA)
        out["shortPct"] = cr.get("si")
        if cr.get("dtc") is not None:
            out["dtc"] = cr["dtc"]
    return out


def keys_for(tags):
    """Every segment a trade with these tags belongs to: 'all', each single tag, each pair of tags."""
    parts = [f"{d}:{tags.get(d)}" for d in DIMS if tags.get(d) is not None]
    out = ["all"] + parts
    out += ["|".join(p) for p in combinations(parts, 2)]
    return out


def matches(tags, key):
    if key == "all":
        return True
    for part in key.split("|"):
        d, v = part.split(":", 1)
        if str(tags.get(d)) != v:
            return False
    return True
