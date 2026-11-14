import fakeredis.aioredis
import pytest

from src.config import settings
from src.core.state import StateManager
from src.trading.broker import PaperBroker
from src.trading.monitor import Monitor
from src.trading.strategy import StrategyParams, levels

MINT = "MINT1"


class FakeDB:
    def __init__(self):
        self.trades = []
        self.closed = []

    async def log_trade(self, *a, **k):
        self.trades.append(a)

    async def save_closed_position(self, pos, reason):
        self.closed.append((pos, reason))


class FakeJup:
    def __init__(self):
        self.price = 1.0

    async def prices(self, mints):
        return {m: {"price": self.price, "liquidity": 500_000} for m in mints}


def watch_item(ref_high=1.0):
    lv = levels(ref_high, 0.25, 0.35, StrategyParams.from_settings(settings))
    return {"mint": MINT, "symbol": "TEST", "liquidity": 500_000, "score": 80, "median_drop": 0.25,
            "median_rise": 0.35, "avg_cycle_hours": 36, "time_stop_hours": 54, "reward_risk": 2,
            "status": "waiting", "local_min": None, **lv}


@pytest.fixture
async def env():
    state = StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    db, jup, msgs = FakeDB(), FakeJup(), []

    async def notify(t):
        msgs.append(t)

    mon = Monitor(state, jup, PaperBroker(state, db, settings), settings, notify)
    await state.set_watch_item(MINT, watch_item())
    return state, db, jup, mon, msgs


async def step(mon, jup, price):
    jup.price = price
    await mon.tick()


async def test_full_winning_cycle(env):
    state, db, jup, mon, msgs = env
    start = await state.get_balance(settings.PAPER_START_BALANCE_USD)
    await step(mon, jup, 0.95)
    assert (await state.get_watchlist())[MINT]["status"] == "waiting"
    await step(mon, jup, 0.78)                     # into buy zone (entry = 0.7875)
    assert (await state.get_watchlist())[MINT]["status"] == "armed"
    await step(mon, jup, 0.76)                     # new local low
    await step(mon, jup, 0.78)                     # +2.6% bounce -> buy
    pos = await state.get_position(MINT)
    assert pos and pos["cost_usd"] == settings.MAX_POSITION_USD
    await step(mon, jup, pos["tp1"] * 1.001)       # half out, stop to breakeven
    pos = await state.get_position(MINT)
    assert pos["tp1_hit"] and pos["stop"] >= pos["entry_price"]
    await step(mon, jup, pos["tp2"] * 1.001)       # rest out
    assert await state.get_position(MINT) is None
    end = await state.get_balance(0)
    assert end > start
    assert await state.get_daily_pnl() > 0
    assert (await state.get_watchlist())[MINT]["status"] == "waiting"
    assert len(db.trades) == 3
    (closed_pos, reason), = db.closed
    assert closed_pos["realized_usd"] > 0 and reason == "الهدف 2"


async def test_param_overrides_change_entry(env):
    state, db, jup, mon, msgs = env
    await state.set_param_overrides({"entry_drop_factor": 1.0, "bogus": 5})
    await step(mon, jup, 1.2)          # new high -> plan recomputed with override
    item = (await state.get_watchlist())[MINT]
    assert abs(item["entry"] - 1.2 * (1 - 0.25 * 1.0)) < 1e-9


async def test_stop_loss_and_cooldown(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)                     # bounce -> buy
    pos = await state.get_position(MINT)
    await step(mon, jup, pos["stop"] * 0.99)
    assert await state.get_position(MINT) is None
    assert await state.get_daily_pnl() < 0
    assert await state.in_cooldown(MINT)


async def test_broken_wave_before_entry(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.60)                     # through the stop while armed
    assert MINT not in await state.get_watchlist()
    assert await state.get_position(MINT) is None


async def test_risk_limits(env):
    state, db, jup, mon, msgs = env
    await state.add_daily_pnl(-settings.DAILY_LOSS_LIMIT_USD)
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    assert await state.get_position(MINT) is None
    assert any("حد الخسارة" in m for m in msgs)


async def test_new_high_moves_plan_up(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 1.2)
    item = (await state.get_watchlist())[MINT]
    assert item["ref_high"] == 1.2 and abs(item["entry"] - 1.2 * (1 - 0.25 * 0.85)) < 1e-9
