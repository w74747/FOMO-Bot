"""Wave analysis: find the swing highs/lows of a coin and measure its typical
drop / rebound, how often drops recover, and whether it is ranging or trending down.

Pure functions, no I/O, so they can be tested and back-tested offline."""
import math
import statistics
from dataclasses import asdict, dataclass, field

from src.data.models import Candle


def clean_candles(candles: list[Candle], max_wick: float = 0.5, max_jump: float = 0.6) -> list[Candle]:
    """Remove data glitches that fake huge waves:
    * wicks more than `max_wick` beyond the candle body are clipped;
    * a single close that jumps more than `max_jump` away from BOTH neighbours, while the
      neighbours agree with each other, is replaced by their average (one-candle spike)."""
    if len(candles) < 3:
        return list(candles)
    out = []
    for c in candles:
        top, bot = max(c.o, c.c), min(c.o, c.c)
        h = min(c.h, top * (1 + max_wick))
        l = max(c.l, bot / (1 + max_wick))
        out.append(c._replace(h=max(h, top), l=min(l, bot)))
    for i in range(1, len(out) - 1):
        prev, cur, nxt = out[i - 1].c, out[i].c, out[i + 1].c
        if prev <= 0 or nxt <= 0:
            continue
        neighbours_agree = abs(nxt / prev - 1) < 0.2
        if neighbours_agree and abs(cur / prev - 1) > max_jump and abs(cur / nxt - 1) > max_jump:
            fix = (prev + nxt) / 2
            c = out[i]
            out[i] = c._replace(o=prev, c=fix, h=max(prev, fix), l=min(prev, fix))
            out[i + 1] = out[i + 1]._replace(o=fix, h=max(out[i + 1].h, fix), l=min(out[i + 1].l, fix))
    return out


@dataclass
class Pivot:
    idx: int
    ts: int
    price: float
    kind: str  # "H" or "L"


def zigzag(candles: list[Candle], pct: float) -> tuple[list[Pivot], Pivot | None]:
    """Confirmed swing pivots (a reversal of at least `pct`) plus the current,
    not-yet-confirmed extreme of the running leg."""
    if not candles:
        return [], None
    pivots: list[Pivot] = []
    hi = (0, candles[0].h)
    lo = (0, candles[0].l)
    direction = 0  # 0 unknown, 1 up-leg (tracking high), -1 down-leg (tracking low)

    for i, c in enumerate(candles):
        if direction == 0:
            if c.h > hi[1]:
                hi = (i, c.h)
            if c.l < lo[1]:
                lo = (i, c.l)
            if hi[1] >= lo[1] * (1 + pct) and lo[0] < hi[0]:
                pivots.append(Pivot(lo[0], candles[lo[0]].ts, lo[1], "L"))
                direction = 1
            elif lo[1] <= hi[1] * (1 - pct) and hi[0] < lo[0]:
                pivots.append(Pivot(hi[0], candles[hi[0]].ts, hi[1], "H"))
                direction = -1
        elif direction == 1:
            if c.h > hi[1]:
                hi = (i, c.h)
            elif c.l <= hi[1] * (1 - pct):
                pivots.append(Pivot(hi[0], candles[hi[0]].ts, hi[1], "H"))
                direction = -1
                lo = (i, c.l)
        else:
            if c.l < lo[1]:
                lo = (i, c.l)
            elif c.h >= lo[1] * (1 + pct):
                pivots.append(Pivot(lo[0], candles[lo[0]].ts, lo[1], "L"))
                direction = 1
                hi = (i, c.h)

    tentative = None
    if direction == 1:
        tentative = Pivot(hi[0], candles[hi[0]].ts, hi[1], "H")
    elif direction == -1:
        tentative = Pivot(lo[0], candles[lo[0]].ts, lo[1], "L")
    return pivots, tentative


def _slope_pct_per_day(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of ln(price) vs time(days), as % per day."""
    pts = [(t, math.log(p)) for t, p in points if p > 0]
    if len(pts) < 2:
        return 0.0
    n = len(pts)
    mx = sum(t for t, _ in pts) / n
    my = sum(y for _, y in pts) / n
    var = sum((t - mx) ** 2 for t, _ in pts)
    if var == 0:
        return 0.0
    slope = sum((t - mx) * (y - my) for t, y in pts) / var
    return (math.exp(slope) - 1) * 100


@dataclass
class WaveStats:
    n_candles: int
    span_days: float
    n_cycles: int
    median_drop: float          # typical high->low drop, fraction (0.25 = 25%)
    p75_drop: float
    median_rise: float          # typical low->high rise, fraction
    recovery_rate: float        # share of drops that won back >= 70% of the loss
    avg_cycle_hours: float      # average time between swing highs
    trend_pct_day: float        # overall price drift % per day
    lows_trend_pct_day: float   # drift of swing lows % per day (falling lows = bad)
    daily_volatility: float
    last_high: float            # reference high for the next entry
    last_low: float
    current_price: float
    current_leg: str            # "up" / "down" / "?"
    drops: list[float] = field(default_factory=list)
    rises: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["drops"] = [round(x, 4) for x in self.drops]
        d["rises"] = [round(x, 4) for x in self.rises]
        return d


def wave_stats(candles: list[Candle], pct: float) -> WaveStats | None:
    if len(candles) < 48:
        return None
    pivots, tentative = zigzag(candles, pct)
    drops, rises, recov = [], [], []
    for a, b in zip(pivots, pivots[1:]):
        if a.kind == "H" and b.kind == "L":
            drops.append((a.price - b.price) / a.price)
        elif a.kind == "L" and b.kind == "H":
            rises.append((b.price - a.price) / a.price)
    # recovery: H1 -> L -> H2, how much of the H1-L loss did H2 win back
    for a, b, c in zip(pivots, pivots[1:], pivots[2:]):
        if a.kind == "H" and b.kind == "L" and c.kind == "H" and a.price > b.price:
            recov.append(1.0 if (c.price - b.price) / (a.price - b.price) >= 0.7 else 0.0)

    highs = [p for p in pivots if p.kind == "H"]
    lows = [p for p in pivots if p.kind == "L"]
    t0 = candles[0].ts
    day = 86400.0
    span_days = (candles[-1].ts - t0) / day

    cycle_hours = 0.0
    if len(highs) >= 2:
        cycle_hours = statistics.mean((b.ts - a.ts) / 3600 for a, b in zip(highs, highs[1:]))

    trend = _slope_pct_per_day([((c.ts - t0) / day, c.c) for c in candles])
    lows_trend = _slope_pct_per_day([((p.ts - t0) / day, p.price) for p in lows[-6:]])

    # daily volatility from hourly log returns
    rets = [math.log(b.c / a.c) for a, b in zip(candles, candles[1:]) if a.c > 0 and b.c > 0]
    vol = statistics.pstdev(rets) * math.sqrt(24) if len(rets) > 2 else 0.0

    # Reference high = the most recent peak. In an up-leg that is the running (unconfirmed)
    # high; in a down-leg it is the last confirmed swing high.
    last_high = highs[-1].price if highs else max(c.h for c in candles)
    current_leg = "?"
    if tentative is not None:
        current_leg = "up" if tentative.kind == "H" else "down"
        if tentative.kind == "H":
            last_high = tentative.price
    last_low = lows[-1].price if lows else min(c.l for c in candles)

    return WaveStats(
        n_candles=len(candles),
        span_days=round(span_days, 2),
        n_cycles=min(len(drops), len(rises)),
        median_drop=statistics.median(drops) if drops else 0.0,
        p75_drop=(statistics.quantiles(drops, n=4)[2] if len(drops) >= 4 else (max(drops) if drops else 0.0)),
        median_rise=statistics.median(rises) if rises else 0.0,
        recovery_rate=(sum(recov) / len(recov)) if recov else 0.0,
        avg_cycle_hours=round(cycle_hours, 1),
        trend_pct_day=round(trend, 3),
        lows_trend_pct_day=round(lows_trend, 3),
        daily_volatility=round(vol, 4),
        last_high=last_high,
        last_low=last_low,
        current_price=candles[-1].c,
        current_leg=current_leg,
        drops=drops,
        rises=rises,
    )


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


MAX_CYCLE_HOURS = 72.0   # the strategy trades waves of hours to ~3 days, not weekly swings


def score(stats: WaveStats, liquidity_usd: float, max_cycle_hours: float = MAX_CYCLE_HOURS) -> tuple[float, list[str]]:
    """0-100 score of how well a coin fits the 'repeating waves' pattern.
    Returns (score, reasons-for-rejection). A non-empty reasons list means rejected."""
    reasons = []
    if stats.span_days < 5:
        reasons.append(f"history too short ({stats.span_days:.1f}d)")
    if stats.n_cycles < 3:
        reasons.append(f"only {stats.n_cycles} complete waves")
    if stats.median_drop < 0.08:
        reasons.append(f"waves too small ({stats.median_drop:.0%})")
    if stats.median_drop > 0.60:
        reasons.append(f"waves too violent ({stats.median_drop:.0%})")
    if stats.avg_cycle_hours > max_cycle_hours:
        reasons.append(f"waves too slow ({stats.avg_cycle_hours:.0f}h cycle)")
    if stats.trend_pct_day < -4:
        reasons.append(f"downtrend {stats.trend_pct_day:.1f}%/day")
    if stats.lows_trend_pct_day < -3:
        reasons.append(f"lower lows {stats.lows_trend_pct_day:.1f}%/day")

    s = 0.0
    s += 25 * _clamp(stats.n_cycles / 8)
    d = stats.median_drop
    if 0.15 <= d <= 0.35:
        s += 20
    elif 0.10 <= d < 0.15 or 0.35 < d <= 0.50:
        s += 12
    elif d >= 0.08:
        s += 5
    s += 20 * stats.recovery_rate
    s += 15 * _clamp(1 - abs(stats.trend_pct_day) / 5)
    s += 10 * _clamp(1 + stats.lows_trend_pct_day / 3)
    if liquidity_usd > 0:
        s += 10 * _clamp(math.log10(liquidity_usd / 50_000) / math.log10(20))
    return round(s, 1), reasons
