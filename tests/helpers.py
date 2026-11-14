import math

from src.data.models import Candle


def wave_candles(hours=720, amp=0.15, period=36, drift_per_day=0.0, base=1.0, noise=0.01):
    """Hourly candles of a sine wave around `base` (amp=0.15 -> ~26% peak-to-trough drop)."""
    out = []
    t0 = 1_700_000_000
    prev = base
    for i in range(hours):
        trend = (1 + drift_per_day / 100) ** (i / 24)
        c = base * trend * (1 + amp * math.sin(2 * math.pi * i / period))
        o = prev
        h = max(o, c) * (1 + noise)
        l = min(o, c) * (1 - noise)
        out.append(Candle(t0 + i * 3600, o, h, l, c, 1000.0))
        prev = c
    return out
