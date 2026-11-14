from dataclasses import asdict
from datetime import datetime, timezone

import fakeredis.aioredis

from src.analysis.optimizer import optimize, split_of
from src.bot.report import _max_drawdown_pct, build_report
from src.config import settings
from src.core.state import StateManager
from src.trading.strategy import StrategyParams
from tests.helpers import wave_candles


def test_optimizer_small_grid_and_split():
    ds = [(f"pool{i}", [tuple(c) for c in wave_candles(hours=700, period=30 + i)], 300_000) for i in range(8)]
    grid = {"entry_drop_factor": [0.7, 1.0], "stop_drop_factor": [0.45]}
    r = optimize(ds, asdict(StrategyParams()), 20, grid)
    assert r["configs_tested"] == 2 and r["tokens"] == 8
    assert r["tokens_a"] + r["tokens_b"] == 8
    assert split_of("pool1") == split_of("pool1")  # deterministic
    assert r["current"]["a"]["trades"] + r["current"]["b"]["trades"] > 0


def test_drawdown():
    assert _max_drawdown_pct([10, -20, -30, 5], 100) == (110 - 60) / 110 * 100


class FakeDB:
    ready = True

    def __init__(self, rows):
        self.rows = rows

    async def closed_positions(self, since_hours=None):
        return self.rows

    async def candle_stats(self):
        return {"pools": 12, "candles": 9000}


async def test_report_renders():
    state = StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    now = datetime.now(timezone.utc)
    rows = [{"pnl_usd": 2.0, "return_pct": 10.0, "expected_pct": 8.0, "closed_at": now},
            {"pnl_usd": -1.0, "return_pct": -5.0, "expected_pct": 8.0, "closed_at": now}]
    txt = await build_report(FakeDB(rows), state, settings)
    assert "2/30" in txt and "+1.00$" in txt and "+8.0%" in txt
    empty = await build_report(FakeDB([]), state, settings)
    assert "0/30" in empty


async def test_scanner_merges_stored_and_fresh_candles():
    import time
    from src.analysis.scanner import Scanner
    from src.data.models import PoolInfo

    now = int(time.time()) // 3600 * 3600
    stored = [c._replace(ts=now - (500 - i) * 3600) for i, c in enumerate(wave_candles(hours=498))]
    fresh = [c._replace(ts=now - (2 - i) * 3600, c=9.0) for i, c in enumerate(wave_candles(hours=3))]

    class DB:
        ready = True
        saved = None

        async def last_candle_ts(self, pool):
            return stored[-1].ts

        async def load_candles(self, pool, since):
            return stored

        async def save_candles(self, pool, mint, sym, liq, cs):
            DB.saved = cs

    class G:
        limit = None

        async def ohlcv(self, pool, tf, agg, limit):
            G.limit = limit
            return fresh

    sc = Scanner(G(), None, None, DB(), settings)
    out = await sc.get_candles(PoolInfo("P", "M", "S", "S", "SOL", "d", 1, 1e5, 1, 1, 30, 1, 0))
    assert G.limit <= 6                      # only the missing hours were requested
    assert len(out) == 501 and out[-1].c == 9.0
    assert DB.saved == fresh
