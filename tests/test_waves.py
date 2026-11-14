from src.analysis.backtest import backtest
from src.analysis.waves import score, wave_stats, zigzag
from src.trading.strategy import StrategyParams, build_plan
from tests.helpers import wave_candles


def test_zigzag_alternates():
    pivots, tentative = zigzag(wave_candles(), 0.10)
    kinds = [p.kind for p in pivots]
    assert len(pivots) >= 30
    assert all(a != b for a, b in zip(kinds, kinds[1:]))
    assert tentative is not None


def test_ranging_coin_is_accepted():
    st = wave_stats(wave_candles(), 0.10)
    assert st.n_cycles >= 15
    assert 0.22 < st.median_drop < 0.32
    assert st.recovery_rate > 0.9
    assert abs(st.trend_pct_day) < 1
    assert 30 < st.avg_cycle_hours < 42
    sc, reasons = score(st, 300_000)
    assert reasons == []
    assert sc >= 70


def test_downtrend_is_rejected():
    st = wave_stats(wave_candles(drift_per_day=-6), 0.10)
    sc, reasons = score(st, 300_000)
    assert any("downtrend" in r or "lower lows" in r for r in reasons)


def test_flat_coin_is_rejected():
    st = wave_stats(wave_candles(amp=0.02, noise=0.002), 0.10)
    _, reasons = score(st, 300_000)
    assert reasons


def test_plan_levels_and_costs():
    p = StrategyParams()
    plan = build_plan(1.0, 0.25, 0.35, 36, 300_000, 20, p)
    assert plan is not None
    assert plan.stop < plan.entry < plan.tp1 < plan.tp2 < 1.2
    assert abs(plan.entry - (1 - 0.25 * 0.85)) < 1e-9
    # tiny pool: price impact kills the trade
    assert build_plan(1.0, 0.25, 0.35, 36, 2_000, 20, p) is None


def test_backtest_profits_on_clean_waves_and_not_on_crash():
    p = StrategyParams()
    good = backtest(wave_candles(), 0.10, p, 300_000, 20)
    assert good.trades >= 5
    assert good.total_return_pct > 0
    assert backtest(wave_candles(drift_per_day=-15, amp=0.12), 0.10, p, 300_000, 20).trades == 0


def test_backtest_stop_limits_loss_on_rug():
    from src.data.models import Candle
    c = wave_candles(hours=600)
    last, t = c[-1].c, c[-1].ts
    for i in range(1, 121):
        px = last * (0.3 if i > 48 else 1 - 0.7 * i / 48)
        c.append(Candle(t + i * 3600, px, px * 1.01, px * 0.99, px, 1))
    r = backtest(c, 0.10, StrategyParams(), 300_000, 20)
    assert r.worst_trade_pct > -20   # stop caps the damage of a 70% collapse


def test_clean_candles_removes_spikes_but_keeps_real_waves():
    from src.analysis.waves import clean_candles
    from src.data.models import Candle
    c = wave_candles(hours=200)
    raw = list(c)
    raw[100] = raw[100]._replace(h=raw[100].h * 5)                       # absurd wick
    raw[120] = raw[120]._replace(c=raw[120].c * 4, h=raw[120].c * 4)     # one-candle close spike
    cleaned = clean_candles(raw)
    assert cleaned[100].h <= max(cleaned[100].o, cleaned[100].c) * 1.5 + 1e-12
    assert cleaned[120].c < raw[120].c / 2
    # genuine sine waves are untouched in their closes
    assert all(abs(a.c - b.c) < 1e-12 for a, b in zip(c[:99], cleaned[:99]))
    st_raw, st_clean = wave_stats(raw, 0.10), wave_stats(cleaned, 0.10)
    assert max(st_clean.rises) < max(st_raw.rises)
    assert isinstance(cleaned[0], Candle)


def test_network_fees_reduce_backtest_and_plan():
    p = StrategyParams()
    free = backtest(wave_candles(), 0.10, p, 300_000, 20)
    paid = backtest(wave_candles(), 0.10, p, 300_000, 20, tx_fee_usd=0.1)   # 0.5% of $20 per tx
    assert paid.trades == free.trades and paid.total_return_pct < free.total_return_pct
    # absurd fees make the setup unprofitable: no trades are taken at all
    assert backtest(wave_candles(), 0.10, p, 300_000, 20, tx_fee_usd=0.5).trades == 0
    a = build_plan(1.0, 0.25, 0.35, 36, 300_000, 20, p)
    b = build_plan(1.0, 0.25, 0.35, 36, 300_000, 20, p, tx_fee_usd=0.5)
    assert b is None or b.cost_pct > a.cost_pct
