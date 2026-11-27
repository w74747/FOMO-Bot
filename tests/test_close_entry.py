"""ENTRY_ON_CLOSE: live and back-test decide entries at the hourly close from the same candle."""
import time
from dataclasses import asdict

import fakeredis.aioredis

from src.analysis.backtest import _close_entry, backtest
from src.analysis.shadow import shadow_states
from src.analysis.waves import clean_candles
from src.config import settings
from src.core.state import StateManager
from src.data.models import Candle
from src.trading.broker import PaperBroker
from src.trading.monitor import Monitor
from src.trading.strategy import StrategyParams
from tests.helpers import wave_candles
from tests.test_trading_flow import MINT, FakeDB, FakeJup, watch_item

KW = {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True, "close_entry": True}
LV = {"entry": 1.0, "stop": 0.8}


def bar(o, h, l, c):
    return Candle(0, o, h, l, c, 1)


def test_close_entry_rules():
    assert _close_entry(bar(1.1, 1.1, 0.95, 1.05), LV, 0.02, 0.007, None) == (None, None)    # closed above
    assert _close_entry(bar(1.1, 1.1, 0.95, 0.97), LV, 0.02, 0.007, None) == (None, 0.97)    # armed at the close
    assert _close_entry(bar(1.1, 1.1, 0.79, 0.97), LV, 0.02, 0.007, None)[0] == "cancel"     # wick broke the stop
    assert _close_entry(bar(0.97, 0.98, 0.9, 0.92), LV, 0.02, 0.007, 0.97) == (None, 0.92)   # lower close
    fill, low = _close_entry(bar(0.92, 0.99, 0.91, 0.95), LV, 0.02, 0.007, 0.92)              # +3.3% close
    assert abs(fill - 0.95 * 1.007) < 1e-12 and low is None
    assert _close_entry(bar(0.92, 1.1, 0.91, 1.06), LV, 0.02, 0.007, 0.92)[0] == "missed"    # ran > 5% above


def test_backtest_close_mode_fills_at_a_close():
    p = StrategyParams.from_settings(settings)
    cs = clean_candles(wave_candles(hours=700, period=30))
    detail = []
    r = backtest(cs, p.zigzag_pct, p, 300_000, 20, tx_fee_usd=0.02, trade_detail=detail, **KW)
    assert r.trades >= 3
    closes = {c.ts: c.c for c in cs}
    for d in detail:
        assert abs(d["fill"] - closes[d["entry_ts"]] * 1.007) < 1e-9


def test_shadow_signal_on_the_entry_close_only():
    p = StrategyParams.from_settings(settings)
    raw = wave_candles(hours=700, period=30)
    cs = clean_candles(raw)
    detail = []
    backtest(cs, p.zigzag_pct, p, 300_000, 20, tx_fee_usd=0.02, trade_detail=detail, **KW)
    d = detail[-1]
    upto = [tuple(c) for c in raw if c.ts <= d["entry_ts"]]
    res = shadow_states({"M": (upto, 300_000)}, asdict(p), 20, 0.02, KW, now=d["entry_ts"] + 3600 + 120)
    m = res["M"]
    assert m["ok"] and m["signal"] == {"ts": d["entry_ts"], "fill": d["fill"]}
    # while that candle is still forming it is ignored -> no signal yet
    res = shadow_states({"M": (upto, 300_000)}, asdict(p), 20, 0.02, KW, now=d["entry_ts"] + 1800)
    assert not res["M"].get("signal")
    # one hour later the back-test is in the trade -> no new entry
    nxt = [tuple(c) for c in raw if c.ts <= d["entry_ts"] + 3600]
    res = shadow_states({"M": (nxt, 300_000)}, asdict(p), 20, 0.02, KW, now=d["entry_ts"] + 7200 + 120)
    assert res["M"]["why"] == "bt_in_trade" or not res["M"].get("signal")


async def _env(**over):
    s = settings.model_copy(update={"ENTRY_ON_CLOSE": True, **over})
    state = StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    jup, msgs = FakeJup(), []

    async def notify(t):
        msgs.append(t)
    mon = Monitor(state, jup, PaperBroker(state, FakeDB(), s), s, notify)
    return state, jup, mon


async def test_ticks_never_enter_in_close_mode():
    state, jup, mon = await _env()
    await state.set_watch_item(MINT, watch_item())
    for px in (0.78, 0.76, 0.80):              # would arm + bounce-buy in tick mode
        jup.price = px
        await mon.tick()
    assert await state.get_position(MINT) is None


async def test_enter_signals_buys_fresh_signal_once():
    state, jup, mon = await _env()
    hour = time.time() - 3600 - 120               # a candle that closed 2 minutes ago
    it = watch_item()
    it["bt_signal"] = {"ts": hour, "fill": 0.80}
    await state.set_watch_item(MINT, it)
    jup.price = 0.805
    assert await mon.enter_signals() == ["TEST"]
    pos = await state.get_position(MINT)
    assert pos and abs(pos["entry_gap_pct"] - 0.625) < 1e-6
    assert await mon.enter_signals() == []        # not twice


async def test_enter_signals_guards():
    state, jup, mon = await _env()
    hour = time.time() - 3600 - 120
    it = watch_item()
    it["bt_signal"] = {"ts": hour, "fill": 0.80}
    await state.set_watch_item(MINT, it)
    jup.price = 0.80 * 1.05                       # ran away
    assert await mon.enter_signals() == []
    it = watch_item()
    it["bt_signal"] = {"ts": hour - 3 * 3600, "fill": 0.80}   # stale signal
    await state.set_watch_item(MINT, it)
    jup.price = 0.80
    assert await mon.enter_signals() == []
    assert await state.get_position(MINT) is None


def test_entry_mode_models_runs():
    from src.analysis.regime import ENTRY_MODE_MODELS, entry_mode_models, format_gate_models
    p = StrategyParams.from_settings(settings)
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(4)]
    rows = entry_mode_models(ds, asdict(p), 20, 0.02, {"bounce_pct": 0.02, "entry_slip": 0.007, "body_only": True})
    assert len(rows) == len(ENTRY_MODE_MODELS) and rows[1]["all"]["n"] > 0
    assert "توقيت" in format_gate_models(rows, "توقيت")
