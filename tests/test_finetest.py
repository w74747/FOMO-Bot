import math
from dataclasses import asdict

from src.analysis.finetest import backtest_fine, fine_models, format_fine_models
from src.config import settings
from src.data.models import Candle
from src.trading.strategy import StrategyParams


def series(hours=700, period=30, amp=0.15):
    """5-minute sine-wave candles and the hourly candles built from them."""
    t0 = 1_700_000_000
    fine, prev = [], 1.0
    for i in range(hours * 12):
        x = 1.0 + amp * math.sin(2 * math.pi * (i / 12) / period)
        o, c = prev, x
        fine.append(Candle(t0 + i * 300, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 100))
        prev = c
    hourly = []
    for k in range(hours):
        g = fine[k * 12:(k + 1) * 12]
        hourly.append(Candle(g[0].ts, g[0].o, max(b.h for b in g), min(b.l for b in g), g[-1].c, 1200))
    return hourly, fine


def test_fine_backtest_trades_the_waves_without_future_hours():
    p = StrategyParams.from_settings(settings)
    hourly, fine = series()
    det = []
    rets = backtest_fine(hourly, fine[300 * 12:], p, 300_000, 20, 0.02, mode="close", detail=det)
    assert len(rets) >= 3 and det[0]["entry_ts"] >= fine[300 * 12].ts
    closes = {b.ts: b.c for b in fine}
    assert all(abs(d["fill"] - closes[d["entry_ts"]] * 1.007) < 1e-9 for d in det)   # bought at a 5-min close
    intra = backtest_fine(hourly, fine[300 * 12:], p, 300_000, 20, 0.02, mode="intra")
    assert intra


def test_fine_models_and_format():
    p = StrategyParams.from_settings(settings)
    ds = []
    for i, per in enumerate((28, 32, 36)):
        h, f = series(period=per)
        ds.append((f"pool{i}", [tuple(c) for c in h], [tuple(c) for c in f[-12 * 24 * 6:]], 300_000))
    res = fine_models(ds, asdict(p), 20, 0.02, {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True})
    assert res["pools"] == 3 and len(res["rows"]) == 4
    assert res["rows"][3]["all"]["n"] > 0
    assert "5 دقائق" in format_fine_models(res)
    assert "لا توجد" in format_fine_models({"pools": 0, "days": 0, "rows": []})
