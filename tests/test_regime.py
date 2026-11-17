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
