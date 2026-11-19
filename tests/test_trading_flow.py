import time
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
            "status": "waiting", "local_min": None, **lv,
            "shadow": {"ok": True, "why": "", "ts": time.time()}}


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
    await step(mon, jup, pos["tp1"] * 1.001)       # half out, trailing stop >= breakeven
    pos = await state.get_position(MINT)
    assert pos["tp1_hit"] and pos["stop"] >= pos["entry_price"]
    run_up = pos["tp2"] * 1.30                      # rally far beyond the old fixed target 2
    await step(mon, jup, run_up)
    pos = await state.get_position(MINT)
    assert pos is not None, "must NOT sell at target 2 any more"
    assert pos["stop"] >= run_up * (1 - pos["trail_pct"] / 2) - 1e-12   # tightened above TP2
    await step(mon, jup, run_up * 0.9)              # pull-back hits the trailing stop
    assert await state.get_position(MINT) is None
    end = await state.get_balance(0)
    assert end > start
    assert await state.get_daily_pnl() > 0
    assert (await state.get_watchlist())[MINT]["status"] == "waiting"
    assert len(db.trades) == 3
    (closed_pos, reason), = db.closed
    assert closed_pos["realized_usd"] > 0 and reason == "وقف متحرك"


async def test_weak_bounce_after_tp1_keeps_profit(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)                     # buy
    pos = await state.get_position(MINT)
    await step(mon, jup, pos["tp1"] * 1.06)        # tp1 + a little more, below target 2
    pos = await state.get_position(MINT)
    stop = pos["stop"]
    assert stop > pos["entry_price"]               # profit locked, not just breakeven
    await step(mon, jup, stop * 0.999)
    assert await state.get_position(MINT) is None
    (closed_pos, reason), = db.closed
    assert closed_pos["realized_usd"] > 0 and reason == "وقف متحرك"


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


async def test_liquidity_collapse_bans_coin(env, monkeypatch):
    state, db, jup, mon, msgs = env
    monkeypatch.setattr(settings, "LIQUIDITY_CONFIRM_SEC", 0)
    await step(mon, jup, 0.95)                         # baseline reading: 500k

    async def thin(mints):
        return {m: {"price": 0.95, "liquidity": 200_000} for m in mints}   # -60%
    jup.prices = thin
    for _ in range(settings.LIQUIDITY_CONFIRM_READS):  # must persist several readings
        await mon.tick()
    assert MINT not in await state.get_watchlist()
    assert await state.in_cooldown(MINT)
    await state.set_watch_item(MINT, watch_item())    # e.g. re-added by a scan
    await mon.tick()
    assert MINT not in await state.get_watchlist()    # banned coins are dropped again


async def test_single_liquidity_blip_does_not_exit(env, monkeypatch):
    state, db, jup, mon, msgs = env
    monkeypatch.setattr(settings, "LIQUIDITY_CONFIRM_SEC", 0)
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)                         # buy (baseline 500k)
    assert await state.get_position(MINT)
    orig = jup.prices

    async def blip(mints):
        return {m: {"price": 0.80, "liquidity": 100_000} for m in mints}
    jup.prices = blip
    await mon.tick()                                   # one bad reading
    jup.prices = orig
    await step(mon, jup, 0.80)                         # back to normal
    jup.prices = blip
    await mon.tick()                                   # another isolated blip
    assert await state.get_position(MINT) is not None  # never exited on blips
    for _ in range(settings.LIQUIDITY_CONFIRM_READS):
        await mon.tick()                               # now it persists -> exit
    assert await state.get_position(MINT) is None


async def test_stop_loss_removes_from_watchlist(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    pos = await state.get_position(MINT)
    await step(mon, jup, pos["stop"] * 0.99)
    assert MINT not in await state.get_watchlist()


async def test_fast_tick_only_prices_hot_coins(env):
    state, db, jup, mon, msgs = env
    calls = []
    orig = jup.prices

    async def spy(mints):
        calls.append(set(mints))
        return await orig(mints)
    jup.prices = spy
    await mon.tick(full=False)                     # nothing armed, nothing held -> no API call
    assert calls == []
    jup.price = 0.78
    await mon.tick(full=True)                      # full tick arms the coin
    jup.price = 0.80
    await mon.tick(full=False)                     # fast tick sees the bounce and buys
    assert MINT in calls[-1]
    assert await state.get_position(MINT) is not None


async def test_second_stop_loss_bans_coin_longer(env):
    state, db, jup, mon, msgs = env
    for _ in range(2):
        await state.set_watch_item(MINT, watch_item())
        await state.r.delete(f"fb:cooldown:{MINT}")
        await step(mon, jup, 0.78)
        await step(mon, jup, 0.80)                    # buy
        pos = await state.get_position(MINT)
        await step(mon, jup, pos["stop"] * 0.99)      # stop-loss
    ttl = await state.r.ttl(f"fb:cooldown:{MINT}")
    assert ttl > 13 * 3600                            # 72h ban, not the 12h cooldown
    assert any("مُنعت" in m for m in msgs)


async def test_strikes_use_trade_history_when_available(env):
    state, db, jup, mon, msgs = env

    async def count_stop_losses(mint, days):
        return 2            # e.g. one earlier stop recorded before this code existed + this one
    db.count_stop_losses = count_stop_losses
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    pos = await state.get_position(MINT)
    await step(mon, jup, pos["stop"] * 0.99)
    assert await state.r.ttl(f"fb:cooldown:{MINT}") > 13 * 3600


async def test_no_entry_shortly_after_a_liquidity_blip(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.95)                         # baseline 500k
    orig = jup.prices

    async def blip(mints):
        return {m: {"price": 0.78, "liquidity": 200_000} for m in mints}
    jup.prices = blip
    await mon.tick()                                   # one unconfirmed drop reading (+ armed)
    jup.prices = orig
    await step(mon, jup, 0.78)                         # liquidity back to normal
    await step(mon, jup, 0.80)                         # bounce: would buy, but blocked
    assert await state.get_position(MINT) is None
    mon._liq_unstable_at[MINT] -= 3601                 # an hour later…
    await step(mon, jup, 0.80)
    assert await state.get_position(MINT) is not None


async def test_losing_time_stop_rests_the_coin(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)                     # buy
    pos = await state.get_position(MINT)
    pos["time_stop_at"] = 0                        # time is up
    await state.save_position(MINT, pos)
    await step(mon, jup, pos["entry_price"] * 0.97)  # below entry, above the stop
    assert await state.get_position(MINT) is None
    assert db.closed[-1][1] == "وقف زمني"
    assert MINT not in await state.get_watchlist()  # no instant re-buy
    assert await state.r.ttl(f"fb:cooldown:{MINT}") > 0


async def test_entry_and_exit_gaps_are_recorded(env):
    state, db, jup, mon, msgs = env
    await step(mon, jup, 0.78)                     # armed, low = 0.78
    await step(mon, jup, 0.80)                     # bought 2.56% above the low
    pos = await state.get_position(MINT)
    assert abs(pos["entry_gap_pct"] - (0.80 / 0.78 - 1) * 100) < 1e-9
    await step(mon, jup, pos["stop"] * 0.97)       # reading 3% under the stop
    closed, reason = db.closed[-1]
    assert reason == "وقف خسارة" and abs(closed["exit_gap_pct"] + 3.0) < 1e-6


async def test_low_liquidity_coins_trade_on_paper_only_and_stay_silent(env):
    state, db, jup, mon, msgs = env

    class Live:
        book = "live"
        bought = 0

        async def enabled(self):
            return True

        async def buy(self, *a, **k):
            Live.bought += 1
            raise AssertionError("live must not buy paper-only coins")

    mon.brokers["live"] = Live()
    await state.set_watch_item(MINT, {**watch_item(), "paper_only": True})
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    pos = await state.get_position(MINT)
    assert pos and pos["paper_only"] and Live.bought == 0
    assert msgs == []                                  # armed + buy messages are silent
    await step(mon, jup, pos["stop"] * 0.97)
    assert db.closed[-1][0]["paper_only"] and msgs == []


def test_prefilter_marks_low_liquidity_as_paper_only():
    from src.analysis.scanner import prefilter_tokens
    base = {"symbol": "X", "name": "", "price": 1, "age_days": 30, "volume_24h": 1e6, "holders": 5000,
            "organic_score": 90, "top_holders_pct": 0.1, "mint_disabled": True, "freeze_disabled": True,
            "is_sus": False, "fdv": 1e6, "price_change_24h": 0, "verified": False}
    toks = [{**base, "mint": "BIG", "liquidity": 200_000}, {**base, "mint": "MID", "liquidity": 30_000},
            {**base, "mint": "TINY", "liquidity": 5_000}]
    kept, rej = prefilter_tokens(toks, settings)
    by = {t["mint"]: t for t in kept}
    assert set(by) == {"BIG", "MID"} and by["MID"]["paper_only"] and not by["BIG"]["paper_only"]
    assert [t["mint"] for t in kept] == ["BIG", "MID"] and rej.get("low liquidity") == 1


async def test_weak_strategy_health_blocks_live_but_not_paper(env):
    import time
    state, db, jup, mon, msgs = env

    class Live:
        book = "live"
        bought = 0

        async def enabled(self):
            return True

        async def buy(self, *a, **k):
            Live.bought += 1
            raise AssertionError("health gate must block live")

    mon.brokers["live"] = Live()
    await state.set_health({"value": 0.5, "n": 40, "prev": 6.0, "ts": time.time()})
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    assert await state.get_position(MINT) and Live.bought == 0
    assert any("صحة الاستراتيجية ضعيفة" in m for m in msgs)



async def test_shadow_gate_blocks_what_the_backtest_would_not_trade(env):
    state, db, jup, mon, msgs = env
    await state.set_watch_item(MINT, {**watch_item(), "shadow": {"ok": False, "why": "bt_blocked", "ts": time.time()}})
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    assert await state.get_position(MINT) is None
    item = (await state.get_watchlist())[MINT]
    assert item["status"] == "waiting"
    # stale verdict = unknown = no entry
    await state.set_watch_item(MINT, {**watch_item(), "shadow": {"ok": True, "why": "", "ts": time.time() - 4 * 3600}})
    await step(mon, jup, 0.78)
    await step(mon, jup, 0.80)
    assert await state.get_position(MINT) is None
