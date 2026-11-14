"""Scanner: discovers live Solana meme pools, filters them, analyses their waves,
back-tests the strategy on each, and builds the watchlist."""
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

from src.analysis.backtest import BacktestResult, backtest
from src.analysis.waves import WaveStats, score, wave_stats
from src.data.geckoterminal import QUOTE_SYMBOLS, GeckoClient
from src.data.http import HttpError
from src.data.models import PoolInfo
from src.data.safety import SafetyChecker
from src.trading.strategy import StrategyParams, TradePlan, build_plan

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",  # PYTH
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",   # JTO
}
EXCLUDED_SYMBOLS = {"SOL", "WSOL", "JITOSOL", "MSOL", "BSOL", "JUPSOL", "INF", "JUP", "RAY", "PYTH", "JTO",
                    "WBTC", "CBBTC", "BTC", "ETH", "WETH", "EURC"}


@dataclass
class Analysis:
    pool: PoolInfo
    stats: WaveStats | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    plan: TradePlan | None = None
    bt: BacktestResult | None = None
    safety: dict = field(default_factory=dict)
    top10: float | None = None

    @property
    def accepted(self) -> bool:
        return not self.reasons and self.plan is not None


def prefilter(pools: list[PoolInfo], s) -> tuple[list[PoolInfo], dict[str, int]]:
    """Cheap filters on pool metadata only (no extra API calls)."""
    rejected: dict[str, int] = {}
    best: dict[str, PoolInfo] = {}

    def rej(key):
        rejected[key] = rejected.get(key, 0) + 1

    for p in pools:
        sym = (p.symbol or "").upper()
        if p.mint in EXCLUDED_MINTS or sym in QUOTE_SYMBOLS or sym in EXCLUDED_SYMBOLS or "USD" in sym:
            rej("major/stable")
            continue
        if p.quote_symbol not in QUOTE_SYMBOLS:
            rej("unsupported quote")
            continue
        if p.age_days < s.MIN_TOKEN_AGE_DAYS:
            rej("too young")
            continue
        if p.liquidity_usd < s.MIN_LIQUIDITY_USD:
            rej("low liquidity")
            continue
        if p.volume_24h_usd < s.MIN_VOLUME_24H_USD:
            rej("low volume")
            continue
        if p.fdv_usd and p.fdv_usd > s.MAX_FDV_USD:
            rej("fdv too high")
            continue
        if p.mint not in best or p.liquidity_usd > best[p.mint].liquidity_usd:
            best[p.mint] = p
    kept = sorted(best.values(), key=lambda p: p.volume_24h_usd, reverse=True)
    return kept, rejected


class Scanner:
    def __init__(self, gecko: GeckoClient, safety: SafetyChecker, state, db, settings,
                 notify: Callable[[str], Awaitable[None]] | None = None):
        self.gecko = gecko
        self.safety = safety
        self.state = state
        self.db = db
        self.s = settings
        self.params = StrategyParams.from_settings(settings)
        self.notify = notify
        self.running = False

    async def discover(self) -> list[PoolInfo]:
        pools: list[PoolInfo] = []
        for page in range(1, self.s.SCAN_POOL_PAGES + 1):
            try:
                pools.extend(await self.gecko.top_pools(page))
            except HttpError as e:
                logger.warning(f"pool discovery page {page} failed: {e}")
                break
        return pools

    async def analyze_pool(self, pool: PoolInfo, check_holders: bool = True) -> Analysis:
        a = Analysis(pool=pool)
        a.safety = await self.safety.mint_check(pool.mint)
        if not a.safety.get("ok", True):
            a.reasons.extend(a.safety["reasons"])
            return a
        try:
            candles = await self.gecko.ohlcv(pool.pool_address, "hour", 1, self.s.HISTORY_HOURS)
        except HttpError as e:
            a.reasons.append(f"no candles ({e.status})")
            return a
        a.stats = wave_stats(candles, self.s.ZIGZAG_PCT)
        if a.stats is None:
            a.reasons.append("not enough candles")
            return a
        a.score, reasons = score(a.stats, pool.liquidity_usd)
        a.reasons.extend(reasons)
        a.plan = build_plan(
            a.stats.last_high, a.stats.median_drop, a.stats.median_rise, a.stats.avg_cycle_hours,
            pool.liquidity_usd, self.s.MAX_POSITION_USD, self.params,
        )
        if a.plan is None and not a.reasons:
            a.reasons.append("reward/risk too low after costs")
        a.bt = backtest(candles, self.s.ZIGZAG_PCT, self.params, pool.liquidity_usd, self.s.MAX_POSITION_USD)
        if a.bt.trades >= 3 and a.bt.total_return_pct < 0:
            a.reasons.append(f"backtest negative ({a.bt.total_return_pct:+.1f}% over {a.bt.trades} trades)")
        if a.score < self.s.MIN_SCORE and not a.reasons:
            a.reasons.append(f"score {a.score:.0f} < {self.s.MIN_SCORE:.0f}")
        if check_holders and not a.reasons:
            a.top10 = await self._top10(pool.mint)
            if a.top10 is not None and a.top10 > self.s.MAX_TOP10_HOLDER_PCT:
                a.reasons.append(f"top-10 holders own {a.top10:.0%}")
        return a

    async def _top10(self, mint: str) -> float | None:
        cached = await self.state.cache_get(f"top10:{mint}")
        if cached is not None:
            return cached.get("v")
        val = await self.safety.top10_pct(mint)
        await self.state.cache_set(f"top10:{mint}", {"v": val}, 24 * 3600)
        return val

    async def analyze_mint(self, mint: str) -> Analysis | None:
        pools = await self.gecko.token_pools(mint)
        pools = [p for p in pools if p.quote_symbol in QUOTE_SYMBOLS]
        if not pools:
            return None
        best = max(pools, key=lambda p: p.liquidity_usd)
        return await self.analyze_pool(best)

    async def run(self) -> list[Analysis]:
        if self.running:
            logger.info("Scan already running; skipped.")
            return []
        self.running = True
        started = time.time()
        try:
            pools = await self.discover()
            kept, rejected = prefilter(pools, self.s)
            kept = kept[: self.s.MAX_ANALYZE_PER_SCAN]
            logger.info(f"Scan: {len(pools)} pools, {len(kept)} pass prefilter, rejected={rejected}")

            results: list[Analysis] = []
            for pool in kept:
                try:
                    a = await self.analyze_pool(pool)
                except Exception as e:  # one bad token must not kill the scan
                    logger.exception(f"analysis failed for {pool.symbol}: {e}")
                    continue
                results.append(a)
                await self.db.save_analysis(
                    pool.mint, pool.symbol, pool.pool_address, a.score,
                    a.stats.to_dict() if a.stats else {},
                    a.bt.to_dict() if a.bt else {},
                    a.plan.to_dict() if a.plan else None,
                )

            accepted = sorted((a for a in results if a.accepted), key=lambda a: a.score, reverse=True)
            accepted = accepted[: self.s.WATCHLIST_SIZE]
            await self._update_watchlist(accepted)
            await self.state.mark_scan()
            logger.info(f"Scan done in {time.time() - started:.0f}s: {len(accepted)} on watchlist")
            if self.notify:
                await self.notify(self._report(len(pools), len(kept), results, accepted, time.time() - started))
            return results
        finally:
            self.running = False

    async def _update_watchlist(self, accepted: list[Analysis]):
        old = await self.state.get_watchlist()
        positions = await self.state.get_positions()
        new: dict[str, dict] = {}
        now = int(time.time())
        for a in accepted:
            p, st, pl = a.pool, a.stats, a.plan
            prev = old.get(p.mint, {})
            new[p.mint] = {
                "mint": p.mint,
                "symbol": p.symbol,
                "pool": p.pool_address,
                "liquidity": p.liquidity_usd,
                "score": a.score,
                "median_drop": st.median_drop,
                "median_rise": st.median_rise,
                "avg_cycle_hours": st.avg_cycle_hours,
                "ref_high": pl.ref_high,
                "entry": pl.entry,
                "stop": pl.stop,
                "tp1": pl.tp1,
                "tp2": pl.tp2,
                "stop_pct": pl.stop_pct,
                "time_stop_hours": pl.time_stop_hours,
                "reward_risk": pl.reward_risk,
                "bt_trades": a.bt.trades if a.bt else 0,
                "bt_return": a.bt.total_return_pct if a.bt else 0.0,
                "status": prev.get("status", "waiting") if prev.get("status") == "armed" else "waiting",
                "local_min": prev.get("local_min") if prev.get("status") == "armed" else None,
                "added_at": prev.get("added_at", now),
                "updated_at": now,
            }
        # keep items that have an open position even if they dropped off the list
        for mint in positions:
            if mint not in new and mint in old:
                new[mint] = {**old[mint], "status": "holding"}
        await self.state.replace_watchlist(new)

    def _report(self, n_pools, n_kept, results, accepted, secs) -> str:
        from html import escape
        lines = [
            "🔎 <b>اكتمل المسح</b>",
            f"مجمعات مفحوصة: {n_pools} · اجتازت الفلاتر: {n_kept} · حُللت: {len(results)} · "
            f"في قائمة المراقبة: <b>{len(accepted)}</b> ({secs:.0f} ث)",
        ]
        for a in accepted[:10]:
            st, bt = a.stats, a.bt
            lines.append(
                f"• <b>{escape(a.pool.symbol)}</b> تقييم {a.score:.0f} | موجات {st.n_cycles} | "
                f"هبوط {st.median_drop:.0%} / صعود {st.median_rise:.0%} | "
                f"اختبار تاريخي: {bt.trades} صفقات {bt.total_return_pct:+.1f}%"
            )
        if not accepted:
            top_reasons: dict[str, int] = {}
            for a in results:
                for r in a.reasons[:1]:
                    key = r.split("(")[0].strip()
                    top_reasons[key] = top_reasons.get(key, 0) + 1
            if top_reasons:
                lines.append("أهم أسباب الرفض: " + "، ".join(
                    f"{escape(k)} ×{v}" for k, v in sorted(top_reasons.items(), key=lambda x: -x[1])[:5]))
        return "\n".join(lines)
