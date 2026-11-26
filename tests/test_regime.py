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



def test_strategy_health_uses_only_the_past():
    from src.analysis.regime import DAY, strategy_health
    # bad first 10 days, good after: the filter must switch on only after a lag
    trades = [{"ts": d * DAY + h * 3600, "ret": (-5.0 if d < 10 else 5.0), "side": "A" if h % 2 else "B",
               "pool": "p"} for d in range(20) for h in range(0, 24, 4)]
    h = strategy_health(trades, windows=(4,))[4]
    full, filt = h["levels"][0][1], h["levels"][2][1]
    assert filt["avg"] > full["avg"] and h["off"]["avg"] < 0
    assert h["now"] == 5.0


def test_split_message_keeps_lines_whole():
    from src.bot.controller import split_message
    text = "\n".join(f"<b>line {i}</b> " + "x" * 50 for i in range(300))
    parts = split_message(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and "\n".join(parts) == text
    assert all(p.count("<b>") == p.count("</b>") for p in parts)


async def test_health_gate_blocks_live_only_when_weak():
    import time
    import fakeredis.aioredis as fr
    from src.core.state import StateManager
    st = StateManager(client=fr.FakeRedis(decode_responses=True))
    assert "غير متوفر" in await st.health_block(settings)              # unknown -> blocked
    await st.set_health({"value": 0.3, "n": 47, "prev": 6.0, "ts": time.time()})
    assert "ضعيفة" in await st.health_block(settings)
    await st.set_health({"value": 6.0, "n": 89, "prev": 1.0, "ts": time.time()})
    assert await st.health_block(settings) is None
    await st.set_health({"value": 6.0, "n": 89, "prev": 1.0, "ts": time.time() - 13 * 3600})
    assert await st.health_block(settings) is not None                  # stale reading


def test_health_now_window():
    from src.analysis.regime import health_now
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(6)]
    last = max(c[0] for _, cs, _ in ds for c in cs)
    h = health_now(ds, asdict(StrategyParams.from_settings(settings)), 20, 0.02, {}, last, 10, 0, 1)
    assert h["n"] >= 1 and h["value"] is not None and h["ts"] == last


def test_trail_only_exit_and_exit_models():
    from src.analysis.backtest import backtest
    from src.analysis.regime import exit_models, format_exit_models
    p = StrategyParams.from_settings(settings)
    cs = wave_candles(hours=700, period=30)
    r = backtest(cs, p.zigzag_pct, p, 300_000, 20, trail_only=0.08, bounce_pct=0.02, body_only=True)
    assert r.trades > 0
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(4)]
    rows = exit_models(ds, asdict(p), 20, 0.02, {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True})
    assert len(rows) == 6 and "هدف واحد متحرك" in format_exit_models(rows)


async def test_health_line_shows_live_state():
    import time
    import fakeredis.aioredis as fr
    from src.analysis.scanner import Scanner
    from src.core.state import StateManager
    st = StateManager(client=fr.FakeRedis(decode_responses=True))
    sc = Scanner(None, None, st, None, settings)
    await st.set_health({"value": 1.69, "n": 47, "prev": 7.7, "ts": time.time()})
    assert "لا بدء تلقائي" in await sc.health_line()
    await st.set_live_enabled(True)
    line = await sc.health_line()
    assert "الحقيقي ما زال يعمل" in line and "+0.5%" in line


def test_selection_test_is_out_of_sample():
    from src.analysis.regime import format_selection, selection_test
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=900, period=26 + i)], 300_000) for i in range(6)]
    r = selection_test(ds, asdict(StrategyParams.from_settings(settings)), 20, 0.02,
                       {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True})
    assert r["cutoffs"] > 0 and len(r["rules"]) == 7
    assert "شرط قبول العملة" in format_selection(r)


def test_backtest_cooldowns_only_remove_trades():
    from src.analysis.backtest import backtest
    from src.analysis.regime import cooldown_models
    p = StrategyParams.from_settings(settings)
    cs = wave_candles(hours=900, period=26)
    free = backtest(cs, p.zigzag_pct, p, 300_000, 20, bounce_pct=0.02, body_only=True)
    rest = backtest(cs, p.zigzag_pct, p, 300_000, 20, bounce_pct=0.02, body_only=True,
                    cooldowns={"loss_h": 48, "strike_limit": 1, "strike_h": 200})
    assert rest.trades <= free.trades
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(3)]
    assert len(cooldown_models(ds, asdict(p), 20, 0.02, {"bounce_pct": 0.02, "body_only": True})) == 5


def test_skip_gates_default_is_live_gate_and_downtrend_can_be_skipped():
    from src.analysis.backtest import backtest
    from src.analysis.regime import coin_gate_models, format_gate_models, gate_models
    p = StrategyParams(zigzag_pct=0.14, entry_drop_factor=1.0, tp1_factor=0.7, tp2_factor=1.1, stop_drop_factor=0.7)
    flat = wave_candles(hours=700, period=30)
    assert backtest(flat, p.zigzag_pct, p, 300_000, 20).returns == \
        backtest(flat, p.zigzag_pct, p, 300_000, 20, skip_gates=()).returns
    falling = wave_candles(hours=700, period=30, drift_per_day=-6)
    live = backtest(falling, p.zigzag_pct, p, 300_000, 20)
    nogate = backtest(falling, p.zigzag_pct, p, 300_000, 20, skip_gates=("downtrend", "lower lows"))
    assert live.trades == 0 and nogate.trades > 0          # the gate is what blocked every entry
    rows = coin_gate_models([tuple(c) for c in falling], 300_000, asdict(p), 20, 0.0, {})
    assert len(rows) == 4 and rows[0]["all"]["n"] == 0 and rows[3]["all"]["n"] == nogate.trades
    assert rows[3]["a"]["n"] + rows[3]["b"]["n"] == nogate.trades
    ds = [("poolA", [tuple(c) for c in flat], 300_000), ("poolB", [tuple(c) for c in falling], 300_000)]
    allrows = gate_models(ds, asdict(p), 20, 0.0, {})
    assert allrows[3]["all"]["n"] >= allrows[0]["all"]["n"] > 0
    txt = format_gate_models(allrows, "t")
    assert "بدون الفلترين" in txt and "A:" in txt
