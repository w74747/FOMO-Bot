from dataclasses import asdict

from src.analysis.regime import HOUR, analyze, format_report, market_index, regime_at
from src.config import settings
from src.data.models import Candle
from src.trading.strategy import StrategyParams
from tests.helpers import wave_candles


def test_index_uses_only_the_previous_closed_hour():
    t0 = 1_000 * HOUR
    series = {f"p{i}": [Candle(t0 + h * HOUR, 1, 1, 1, 1 + (h >= 30) * 1.0, 1) for h in range(48)]
              for i in range(10)}
    idx = market_index(series)
    # at hour 30 every coin doubled vs 24h before
    assert abs(idx[t0 + 30 * HOUR]["m24"] - 100) < 1e-9 and idx[t0 + 30 * HOUR]["breadth"] == 100
    # a trade inside hour 30 sees hour 29 (before the jump): no look-ahead
    assert regime_at(idx, t0 + 30 * HOUR + 600)["m24"] == 0


def test_analyze_runs_end_to_end():
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(10)]
    real = [{"opened_at": ds[0][1][500][0] + 100, "ret": -5.0, "mode": "live", "symbol": "X"}]
    r = analyze(ds, asdict(StrategyParams.from_settings(settings)), 20, 0.02, real)
    assert r["trades"] > 0 and r["with_regime"] > 0 and r["real"]
    txt = format_report(r)
    assert "بدون فلتر" in txt and "صفقاتنا الفعلية" in txt
    from src.analysis.regime import format_age
    assert sum(st["n"] for _, st in r["by_age"]) == r["trades"] and "آخر 3 أيام" in format_age(r)


def test_bounce_entry_model():
    from src.analysis.backtest import _bounce_entry, backtest
    from src.analysis.regime import entry_models
    lv = {"entry": 1.0, "stop": 0.85}
    B = lambda o, h, l, c: Candle(0, o, h, l, c, 1)  # noqa: E731
    assert _bounce_entry(B(1.1, 1.1, 0.95, 0.96), lv, 0.02, 0, None) == (None, 0.95)     # armed, no bounce
    f, _ = _bounce_entry(B(1.1, 1.1, 0.95, 0.98), lv, 0.02, 0, None)
    assert abs(f - 0.969) < 1e-9                                                          # low, then bounce
    assert _bounce_entry(B(0.96, 0.96, 0.80, 0.81), lv, 0.02, 0, 0.95)[0] == "cancel"     # stop first
    f, low = _bounce_entry(B(0.96, 0.98, 0.90, 0.91), lv, 0.02, 0, 0.95)                  # ambiguous bar
    assert abs(f - 0.969) < 1e-9 and low == 0.90
    cs = wave_candles(hours=700, period=30)
    p = StrategyParams.from_settings(settings)
    assert backtest(cs, p.zigzag_pct, p, 300_000, 20, bounce_pct=0.02, entry_slip=0.01).trades > 0
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(4)]
    rows = entry_models(ds, asdict(p), 20, 0.02)
    assert len(rows) == 9 and rows[0]["all"]["n"] > 0


def test_body_only_runs():
    from src.analysis.backtest import backtest
    p = StrategyParams.from_settings(settings)
    for period in (28, 33, 40):
        cs = wave_candles(hours=700, period=period)
        full = backtest(cs, p.zigzag_pct, p, 300_000, 20, bounce_pct=0.02, entry_slip=0.007)
        body = backtest(cs, p.zigzag_pct, p, 300_000, 20, bounce_pct=0.02, entry_slip=0.007, body_only=True)
        assert full.trades > 0 and body.trades >= 0
