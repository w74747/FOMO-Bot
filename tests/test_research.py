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


async def test_scanner_requires_backtest_evidence_and_skips_banned():
    import fakeredis.aioredis as fr
    from src.analysis.scanner import Scanner
    from src.data.models import PoolInfo

    flat_waves = wave_candles(hours=300, period=40)   # short history -> few/no back-test trades

    class G:
        async def best_pools(self, mints):
            return {m: PoolInfo("P" + m, m, m, m, "SOL", "d", 1, 3e5, 1, 1, 30, 1, 0) for m in mints}

        async def ohlcv(self, pool, *a):
            return wave_candles() if pool == "PGOOD" else flat_waves

    class S:
        async def mint_check(self, m):
            return {"ok": True, "reasons": []}

    class DB:
        ready = False

        async def save_analysis(self, *a):
            pass

        async def save_candles(self, *a):
            pass

    base = {"name": "", "price": 1, "liquidity": 3e5, "fdv": 2e6, "age_days": 40, "volume_24h": 5e5,
            "holders": 3000, "organic_score": 50, "top_holders_pct": 0.2, "mint_disabled": True,
            "freeze_disabled": True, "is_sus": False}

    class J:
        async def top_tokens(self, *a):
            return [{**base, "mint": m, "symbol": m} for m in ("GOOD", "NOEVID", "BANNED")]

    async def ohlcv_for(pool, *a):
        return wave_candles() if pool in ("PGOOD", "PBANNED") else flat_waves
    g = G()
    g.ohlcv = ohlcv_for
    state = StateManager(client=fr.FakeRedis(decode_responses=True))
    await state.set_cooldown("BANNED", 72)
    sc = Scanner(g, S(), state, DB(), settings, jupiter=J())
    res = {a.pool.mint: a for a in await sc.run()}
    assert any("evidence" in r for r in res["NOEVID"].reasons)
    assert set(await state.get_watchlist()) == {"GOOD"}


async def test_report_excludes_manual_closes():
    import fakeredis.aioredis as fr
    state = StateManager(client=fr.FakeRedis(decode_responses=True))
    now = datetime.now(timezone.utc)
    rows = [{"pnl_usd": -1.67, "return_pct": -8.0, "expected_pct": 6.0, "closed_at": now, "exit_reason": "وقف خسارة"},
            {"pnl_usd": -1.41, "return_pct": -7.0, "expected_pct": 6.0, "closed_at": now, "exit_reason": "إغلاق يدوي"},
            {"pnl_usd": 0.0, "return_pct": 0.0, "expected_pct": 6.0, "closed_at": now, "exit_reason": "إغلاق يدوي"}]
    txt = await build_report(FakeDB(rows), state, settings)
    assert "1/30" in txt and "مستبعد: 2" in txt and "-1.67$" in txt


def test_arabic_reasons_and_address_extraction():
    from src.analysis.scanner import extract_address
    from src.bot.reasons import ar_reason
    assert ar_reason("only 2 complete waves") == "موجات مكتملة قليلة (2)"
    assert ar_reason("lower lows -3.5%/day") == "قيعان متناقصة (العملة تنزف) (-3.5%/day)"
    assert ar_reason("not enough backtest evidence (1 trades)") == "دليل تاريخي غير كافٍ (1 trades)"
    assert ar_reason("too young") == "عمرها أقل من 7 أيام"
    mint = "4CxjZyMNoF1p6eXHXHv1TWpS2X47CuUmLwtGe18spump"
    assert extract_address(mint) == mint
    assert extract_address(f"https://birdeye.so/token/{mint}?chain=solana") == mint
    assert extract_address(f"https://pump.fun/coin/{mint}") == mint
    assert extract_address("hello") is None


async def test_manual_coin_is_rechecked_every_scan_and_never_capped_out(monkeypatch):
    import fakeredis.aioredis as fr
    from src.analysis.scanner import Scanner
    from src.data.models import PoolInfo
    monkeypatch.setattr(settings, "WATCHLIST_SIZE", 1)

    base = {"name": "", "price": 1, "liquidity": 3e5, "fdv": 2e6, "age_days": 40, "volume_24h": 5e5,
            "holders": 3000, "organic_score": 50, "top_holders_pct": 0.2, "mint_disabled": True,
            "freeze_disabled": True, "is_sus": False}

    class J:
        async def top_tokens(self, *a):
            return [{**base, "mint": m, "symbol": m} for m in ("AUTO1", "AUTO2")]

        async def token_info(self, mints):
            return [{**base, "mint": m, "symbol": m} for m in mints]

    class G:
        async def best_pools(self, mints):
            return {m: PoolInfo("P" + m, m, m, m, "SOL", "d", 1, 3e5, 1, 1, 30, 1, 0) for m in mints}

        async def ohlcv(self, *a):
            return wave_candles()

    class S:
        async def mint_check(self, m):
            return {"ok": True, "reasons": []}

    class DB:
        ready = False

        async def save_analysis(self, *a):
            pass

        async def save_candles(self, *a):
            pass

    state = StateManager(client=fr.FakeRedis(decode_responses=True))
    await state.add_manual("MINE")
    sc = Scanner(G(), S(), state, DB(), settings, jupiter=J())
    await sc.run()
    wl = await state.get_watchlist()
    assert "MINE" in wl and wl["MINE"]["manual"] is True     # user's coin kept despite size 1
    assert len(wl) == 2                                       # + best automatic coin
    a, why = await sc.analyze_manual("MINE")
    assert a.accepted and why is None
