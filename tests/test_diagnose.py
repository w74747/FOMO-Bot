from dataclasses import asdict

from src.analysis.diagnose import compare_data, diagnose, era_of, format_diagnose
from src.config import settings
from src.data.models import Candle
from src.trading.strategy import StrategyParams
from tests.helpers import wave_candles

KW = {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True}


def test_compare_data_detects_pool_wicks():
    g = {"M": [Candle(0, 1.0, 1.10, 0.90, 1.0, 1), Candle(3600, 1.0, 1.0, 1.0, 1.0, 1)]}
    j = {"M": [(0, 1.0, 1.02, 0.98, 1.0, 60), (3600, 1.0, 1.0, 1.0, 1.0, 60)]}
    d = compare_data(g, j)
    assert d["hours"] == 2 and d["high_excess_3"] == 50.0 and d["low_excess_3"] == 50.0


def test_diagnose_end_to_end():
    raw = wave_candles(hours=800, period=30)
    jrows = [(c.ts, c.o, c.h, c.l, c.c, 60) for c in raw[-100:]]
    datasets = {"M": ([tuple(c) for c in raw], 300_000)}
    t0 = raw[600].ts
    closes = [{"opened_at": t0, "ret": -10.0, "pnl": -2.0, "reason": "وقف خسارة", "mode": "live", "tier": None,
               "entry_gap": 2.5, "exit_gap": -1.0},
              {"opened_at": t0, "ret": 12.0, "pnl": 2.4, "reason": "وقف متحرك", "mode": "paper", "tier": None,
               "entry_gap": None, "exit_gap": None}]
    actual = [{"mint": "M", "symbol": "M", "opened_at": t0, "closed_at": t0 + 7200, "ret": 12.0,
               "reason": "وقف متحرك", "mode": "paper", "entry_price": raw[600].c, "exit_price": raw[602].c}]
    eras = [("old", 0.0), ("shadow", raw[500].ts)]
    r = diagnose(closes, actual, datasets, {"M": jrows}, asdict(StrategyParams.from_settings(settings)),
                 20, 0.02, KW, eras)
    assert era_of(t0, eras) == "shadow" and r["data"]["hours"] > 0 and r["bt_both"]["coins"] == 1
    assert r["reconcile"]["n"] == 1
    txt = format_diagnose(r)
    assert "تشخيص شامل" in txt and "Jupiter" in txt


def test_missed_opportunities_groups():
    from datetime import datetime, timezone

    from src.analysis.diagnose import format_missed, missed_opportunities
    p = StrategyParams.from_settings(settings)
    raw = wave_candles(hours=800, period=30)
    since = raw[300].ts
    r = missed_opportunities({"M": ([tuple(c) for c in raw], 300_000)}, {"M": "MM"}, [], [], p, 20, 0.02, KW,
                             since, settings.MIN_SCORE)
    assert set(r) <= {"not_scanned"} and r["not_scanned"]["n"] > 0
    an = [{"mint": "M", "score": 90, "scanned_at": datetime.fromtimestamp(c.ts - 3600, timezone.utc)} for c in raw[300:]]
    r2 = missed_opportunities({"M": ([tuple(c) for c in raw], 300_000)}, {"M": "MM"}, an, [], p, 20, 0.02, KW,
                              since, settings.MIN_SCORE)
    assert "analysed_ok" in r2 and "أين كانت أرباح الاختبار" in format_missed(r2)
