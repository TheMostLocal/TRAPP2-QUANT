# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/stats.py
#  How good is the book, honestly: win rate with its 95 % interval (Wilson), whether it beats a benchmark win
#  rate (one-sided binomial test), expectancy per trade in bps net of costs, profit factor, per-trade Sharpe
#  and its t-statistic. A win rate means little alone - a strategy can win 70 % of trades and lose money.
# ============================================================
import math


def wilson(wins, n, z=1.96):
    if n <= 0:
        return (None, None)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def binom_sf(k, n, p):
    """P(X >= k) for X ~ Binomial(n, p): exact for n <= 2000, normal approximation (continuity) beyond."""
    if n <= 0:
        return None
    if k <= 0:
        return 1.0
    if n <= 2000:
        lp, lq = math.log(p), math.log(1 - p)
        terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq for i in range(k, n + 1)]
        m = max(terms)
        return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))
    mu, sd = n * p, math.sqrt(n * p * (1 - p))
    zz = (k - 0.5 - mu) / sd
    return 0.5 * math.erfc(zz / math.sqrt(2))


def summarize(trades, benchmark=0.5075):
    """trades: dicts with net_bps (float). Flat trades (|net| < 0.05 bps) count in n but not as wins or losses."""
    r = [float(t["net_bps"]) for t in trades if t.get("net_bps") is not None]
    n = len(r)
    if not n:
        return {"n": 0}
    wins = sum(1 for x in r if x >= 0.05)
    losses = sum(1 for x in r if x <= -0.05)
    decided = wins + losses
    mean = sum(r) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    gw = sum(x for x in r if x > 0)
    gl = -sum(x for x in r if x < 0)
    lo, hi = wilson(wins, decided)
    return {"n": n, "wins": wins, "losses": losses, "winRate": round(wins / decided, 4) if decided else None,
            "winRateCI95": [round(lo, 4), round(hi, 4)] if decided else None,
            "pBeatsBenchmark": round(binom_sf(wins, decided, benchmark), 4) if decided else None,
            "benchmark": benchmark, "expectancyBps": round(mean, 3), "sdBps": round(sd, 3),
            "tStat": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None,
            "avgWinBps": round(gw / wins, 2) if wins else None, "avgLossBps": round(-gl / losses, 2) if losses else None,
            "profitFactor": round(gw / gl, 3) if gl > 0 else None, "sharpePerTrade": round(mean / sd, 4) if sd > 0 else None}


def trades_needed(edge_win_rate, benchmark=0.5075, z=1.645):
    """Rough count of trades to show a true win rate edge_win_rate beats benchmark at one-sided 95 %."""
    d = edge_win_rate - benchmark
    if d <= 0:
        return None
    return math.ceil((z * math.sqrt(benchmark * (1 - benchmark)) / d) ** 2)
