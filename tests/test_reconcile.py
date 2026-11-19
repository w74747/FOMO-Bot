from dataclasses import asdict

from src.analysis.backtest import backtest
from src.analysis.reconcile import format_reconcile, reconcile
from src.analysis.waves import clean_candles
from src.config import settings
from src.data.models import Candle
from src.trading.strategy import StrategyParams
from tests.helpers import wave_candles

KW = {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True}


def test_reconcile_matches_backtest_trades_and_flags_data_gaps():
    p = StrategyParams.from_settings(settings)
    raw = wave_candles(hours=700, period=30)
    cs = clean_candles(raw)
    detail = []
    backtest(cs, p.zigzag_pct, p, 300_000, 20, tx_fee_usd=0.02, trade_detail=detail, **KW)
    assert detail and {"entry_ts", "exit_ts", "fill", "ret", "reason"} <= set(detail[0])
    d = detail[0]
    lows = min(c.l for c in cs if d["entry_ts"] <= c.ts <= d["exit_ts"])
    actual = [
        # same trade as the back-test, but we stopped out at a price the candles never showed
        {"mint": "M", "symbol": "A", "opened_at": d["entry_ts"] + 600, "closed_at": d["exit_ts"] + 1800,
         "ret": -15.0, "reason": "وقف خسارة", "entry_price": d["fill"] * 1.01, "exit_price": lows * 0.9},
        # a trade where the back-test was flat
        {"mint": "M", "symbol": "B", "opened_at": cs[60].ts + 100, "closed_at": cs[62].ts,
         "ret": -5.0, "reason": "وقف زمني", "entry_price": cs[60].c, "exit_price": cs[62].c},
        {"mint": "X", "symbol": "C", "opened_at": cs[100].ts, "closed_at": cs[101].ts, "ret": 1.0,
         "reason": "الهدف 1", "entry_price": 1, "exit_price": 1},
    ]
    r = reconcile(actual, {"M": ([tuple(c) for c in raw], 300_000)}, asdict(p), 20, 0.02, KW)
    assert r["matched"] >= 1 and r["no_data"] == 1 and r["stops_unseen"] == 1
    assert abs(r["rows"][0]["entry_vs_bt"] - 1.0) < 1e-6
    txt = format_reconcile(r)
    assert "مطابقة" in txt and "لم تصل إليه شموع المجمع" in txt
    assert isinstance(Candle, type)
