# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/segments.py
#  z175 - WHERE a strategy is judged. A strategy that loses on average can still have an edge in part of the market
#  (mid-size names, the middle of the day, short side, one sector). Every signal carries four tags; a segment is
#  one tag or two together ("liq:mid", "liq:mid|tod:mid") or "all". The lab (quant/lab.py) tests each strategy in
#  every segment, out of sample, and the engine trades live only in segments that passed.
# ============================================================
from itertools import combinations

DIMS = ("liq", "tod", "side", "sector")


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
    return {"liq": liq_bucket(liq), "tod": tod_bucket(ctx.minute), "side": side, "sector": sec}


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
