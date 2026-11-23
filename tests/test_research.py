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
    assert "عدد الصفقات: 2 " in txt and "15 لبدء" in txt and "+1.00$" in txt and "+8.0%" in txt
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
    assert "عدد الصفقات: 1 " in txt and "مستبعد: 2" in txt and "-1.67$" in txt


async def test_report_compares_live_and_paper():
    from datetime import timedelta
    import fakeredis.aioredis as fr
    state = StateManager(client=fr.FakeRedis(decode_responses=True))
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    rows = [{"pnl_usd": 5.0, "return_pct": 10.0, "closed_at": old, "mode": "paper"},   # before live: excluded
            {"pnl_usd": 2.0, "return_pct": 10.0, "closed_at": now, "mode": "live"},
            {"pnl_usd": -1.0, "return_pct": -5.0, "closed_at": now, "mode": "live",
             "entry_gap_pct": 3.0, "exit_gap_pct": -1.5},
            {"pnl_usd": 2.2, "return_pct": 11.0, "closed_at": now, "mode": "paper"},
            {"pnl_usd": -0.8, "return_pct": -4.0, "closed_at": now, "mode": "paper"},
            {"pnl_usd": 9.0, "return_pct": 45.0, "closed_at": now, "mode": "paper", "tier": "low_liq"}]
    txt = await build_report(FakeDB(rows), state, settings)
    assert "تجربة السيولة المنخفضة" in txt and "+45.0%" in txt
    assert txt.index("الحقيقي") < txt.index("الورقي في نفس الفترة")
    assert "+1.00$" in txt and "+1.40$" in txt and "الفرق في متوسط الصفقة: -1.0" in txt
    assert "مرجع صامت" in txt and "3 صفقة" in txt
    assert "أعلى من القاع بـ 3.0%" in txt and "تحت الوقف بـ 1.5%" in txt


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


async def test_unsafe_mints_do_not_use_analysis_slots(monkeypatch):
    import fakeredis.aioredis as fr
    from src.analysis.scanner import Scanner
    from src.data.models import PoolInfo
    monkeypatch.setattr(settings, "MAX_ANALYZE_PER_SCAN", 2)
    base = {"name": "", "price": 1, "liquidity": 3e5, "fdv": 2e6, "age_days": 40, "volume_24h": 5e5,
            "holders": 3000, "organic_score": 50, "top_holders_pct": 0.2, "mint_disabled": None,
            "freeze_disabled": None, "is_sus": False}

    class J:
        async def top_tokens(self, *a):
            return [{**base, "mint": m, "symbol": m, "volume_24h": v}
                    for m, v in (("STOCK1", 9e6), ("STOCK2", 8e6), ("MEME1", 6e5), ("MEME2", 5e5))]

    class G:
        async def best_pools(self, mints):
            return {m: PoolInfo("P" + m, m, m, m, "SOL", "d", 1, 3e5, 1, 1, 30, 1, 0) for m in mints}

    class S:
        async def mint_check(self, m):
            return {"ok": not m.startswith("STOCK"), "reasons": ["mint authority active (x)"] if m.startswith("STOCK") else []}

    sc = Scanner(G(), S(), StateManager(client=fr.FakeRedis(decode_responses=True)), None, settings, jupiter=J())
    n, kept, rejected = await sc.candidates()
    assert [p.mint for p, _ in kept] == ["MEME1", "MEME2"]
    assert rejected["mint authority active"] == 2


def test_optimizer_does_not_recommend_when_A_and_B_disagree():
    from src.analysis import optimizer as opt
    cur = opt.ConfigResult({}, opt.SetStats(277, 41, .56, 6.29, 0, -25, 4.2, 1176), opt.SetStats(195, 36, .55, 4.9, 0, -29, 3.3, 646))
    alt = opt.ConfigResult({}, opt.SetStats(379, 52, .56, 4.37, 0, -18, 2.9, 1094), opt.SetStats(264, 43, .59, 3.95, 0, -27, 2.9, 778))
    better = opt.ConfigResult({}, opt.SetStats(300, 45, .58, 6.5, 0, -20, 4.3, 1290), opt.SetStats(220, 40, .58, 5.2, 0, -25, 3.6, 792))

    def pick(top, current):
        for i, r in enumerate(top, 1):
            if (r.b.trades >= 15 and r.b.tokens_traded >= opt.MIN_TOKENS_B and r.b.avg_pct > 0
                    and r.b.total_lcb_pct > max(current.b.total_lcb_pct, 0) * 1.10
                    and r.a.total_lcb_pct >= current.a.total_lcb_pct):
                return i
        return None
    assert pick([alt], cur) is None          # better on B, worse on A -> keep current
    assert pick([alt, better], cur) == 2     # better on both -> recommend
    import inspect
    assert "r.a.total_lcb_pct >= current.a.total_lcb_pct" in inspect.getsource(opt.optimize)


async def test_known_coins_are_rechecked():
    from src.analysis.scanner import Scanner

    class DB:
        ready = True

        async def tracked_pools(self, min_candles=300):
            return [{"mint": "OLD1"}, {"mint": "OLD2"}, {"mint": "HOT"}]

    class J:
        asked = None

        async def top_tokens(self, cat, interval, limit):
            return [{"mint": "HOT", "symbol": "HOT"}]

        async def token_info(self, mints):
            J.asked = mints
            return [{"mint": m, "symbol": m} for m in mints]

    import fakeredis.aioredis as fr
    sc = Scanner(None, None, StateManager(client=fr.FakeRedis(decode_responses=True)), DB(), settings, jupiter=J())
    toks = await sc.discover_tokens()
    assert J.asked == ["OLD1", "OLD2"]                      # one batch, without coins already found
    assert [t["mint"] for t in toks].count("HOT") == 12 and {"OLD1", "OLD2"} <= {t["mint"] for t in toks}


async def test_gecko_discovery_adds_new_coins_through_jupiter():
    from types import SimpleNamespace as NS

    from src.analysis.scanner import Scanner

    class G:
        async def trending_pools(self, page):
            return [NS(mint="TREND"), NS(mint="HOT")] if page == 1 else []

        async def top_pools(self, page):
            return [NS(mint="VOL")] if page == 1 else []

    class J:
        asked = None

        async def token_info(self, mints):
            J.asked = mints
            return [{"mint": m, "symbol": m} for m in mints]

    import fakeredis.aioredis as fr
    sc = Scanner(G(), None, StateManager(client=fr.FakeRedis(decode_responses=True)), None, settings, jupiter=J())
    out = await sc.gecko_tokens({"HOT"})
    assert J.asked == ["TREND", "VOL"] and [t["mint"] for t in out] == ["TREND", "VOL"]


def test_backtest_acceptance_rule():
    from src.analysis.backtest import BacktestResult
    from src.analysis.scanner import bt_reject_reason

    def bt(rets):
        return BacktestResult(trades=len(rets), wins=sum(r > 0 for r in rets), win_rate=0, total_return_pct=sum(rets) * 100,
                              avg_return_pct=0, worst_trade_pct=0, test_days=30, returns=rets)
    assert "evidence" in bt_reject_reason(bt([0.2]), settings)                 # 1 trade
    assert bt_reject_reason(bt([0.1, 0.05]), settings) is None                 # 2 trades, median > 0
    assert "median" in bt_reject_reason(bt([-0.1, -0.1, 0.9]), settings)       # one big win only
    assert "negative" in bt_reject_reason(bt([0.1, -0.3]), settings)
