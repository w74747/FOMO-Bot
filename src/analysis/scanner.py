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
from src.data.jupiter import TOKEN_CATEGORIES, JupiterClient
from src.data.safety import SafetyChecker
from src.trading.strategy import StrategyParams, TradePlan, build_plan, load_params

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


def prefilter_tokens(tokens: list[dict], s) -> tuple[list[dict], dict[str, int]]:
    """Cheap filters on Jupiter Tokens V2 data (no extra API calls)."""
    rejected: dict[str, int] = {}
    kept: dict[str, dict] = {}

    def rej(key):
        rejected[key] = rejected.get(key, 0) + 1

    seen: set[str] = set()
    for t in tokens:
        if t["mint"] in seen:  # the same token appears in several Jupiter categories
            continue
        seen.add(t["mint"])
        sym = (t["symbol"] or "").upper()
        if t["mint"] in EXCLUDED_MINTS or sym in QUOTE_SYMBOLS or sym in EXCLUDED_SYMBOLS or "USD" in sym:
            rej("major/stable")
        elif t["is_sus"]:
            rej("flagged suspicious")
        elif t["mint_disabled"] is False:
            rej("mint authority active")
        elif t["freeze_disabled"] is False:
            rej("freeze authority active")
        elif t["age_days"] < s.MIN_TOKEN_AGE_DAYS:
            rej("too young")
        elif t["liquidity"] < s.MIN_LIQUIDITY_USD:
            rej("low liquidity")
        elif t["volume_24h"] < s.MIN_VOLUME_24H_USD:
            rej("low volume")
        elif t["fdv"] and t["fdv"] > s.MAX_FDV_USD:
            rej("fdv too high")
        elif t["holders"] and t["holders"] < s.MIN_HOLDERS:
            rej("few holders")
        elif t["organic_score"] < s.MIN_ORGANIC_SCORE:
            rej("low organic score")
        elif t["top_holders_pct"] is not None and t["top_holders_pct"] > s.MAX_TOP10_HOLDER_PCT:
            rej("concentrated holders")
        else:
            kept[t["mint"]] = t
    ranked = sorted(kept.values(), key=lambda t: t["volume_24h"], reverse=True)
    return ranked, rejected


def merge_token_into_pool(pool: PoolInfo, t: dict) -> PoolInfo:
    """Use Jupiter's aggregated numbers (liquidity across all pools, real token age)."""
    pool.symbol = t["symbol"] or pool.symbol
    pool.name = t["name"] or pool.name
    pool.liquidity_usd = max(t["liquidity"], pool.liquidity_usd)
    pool.volume_24h_usd = t["volume_24h"] or pool.volume_24h_usd
    pool.fdv_usd = t["fdv"] or pool.fdv_usd
    pool.age_days = max(t["age_days"], pool.age_days)
    return pool


class Scanner:
    def __init__(self, gecko: GeckoClient, safety: SafetyChecker, state, db, settings,
                 notify: Callable[[str], Awaitable[None]] | None = None,
                 jupiter: JupiterClient | None = None):
        self.gecko = gecko
        self.jupiter = jupiter
        self.safety = safety
        self.state = state
        self.db = db
        self.s = settings
        self.params = StrategyParams.from_settings(settings)
        self.notify = notify
        self.running = False

    async def discover_tokens(self) -> list[dict]:
        tokens: list[dict] = []
        for cat in TOKEN_CATEGORIES:
            try:
                tokens += await self.jupiter.top_tokens(cat, "24h", 100)
            except HttpError as e:
                logger.warning(f"Jupiter {cat} failed: {e}")
        return tokens

    async def candidates(self) -> tuple[int, list[tuple[PoolInfo, dict | None]], dict[str, int]]:
        """Returns (n_seen, [(pool, jupiter_token_or_None)], rejected_counts)."""
        if self.jupiter:
            tokens = await self.discover_tokens()
            if tokens:
                kept, rejected = prefilter_tokens(tokens, self.s)
                kept = kept[: self.s.MAX_ANALYZE_PER_SCAN]
                pools = await self.gecko.best_pools([t["mint"] for t in kept])
                out = []
                for t in kept:
                    if t["mint"] in pools:
                        out.append((merge_token_into_pool(pools[t["mint"]], t), t))
                    else:
                        rejected["no supported pool"] = rejected.get("no supported pool", 0) + 1
                return len({t["mint"] for t in tokens}), out, rejected
            logger.warning("Jupiter discovery returned nothing; falling back to GeckoTerminal pools.")
        pools = await self.discover()
        kept, rejected = prefilter(pools, self.s)
        return len(pools), [(p, None) for p in kept[: self.s.MAX_ANALYZE_PER_SCAN]], rejected

    async def discover(self) -> list[PoolInfo]:
        pools: list[PoolInfo] = []
        for page in range(1, self.s.SCAN_POOL_PAGES + 1):
            try:
                pools.extend(await self.gecko.top_pools(page))
            except HttpError as e:
                logger.warning(f"pool discovery page {page} failed: {e}")
                break
        return pools

    async def analyze_pool(self, pool: PoolInfo, token: dict | None = None) -> Analysis:
        a = Analysis(pool=pool)
        if token is not None:
            a.top10 = token.get("top_holders_pct")
        a.safety = await self.safety.mint_check(pool.mint)
        if not a.safety.get("ok", True):
            a.reasons.extend(a.safety["reasons"])
            return a
        try:
            candles = await self.get_candles(pool)
        except HttpError as e:
            a.reasons.append(f"no candles ({e.status})")
            return a
        recent = candles[-self.s.HISTORY_HOURS:]
        a.stats = wave_stats(recent, self.params.zigzag_pct)
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
        a.bt = backtest(candles, self.params.zigzag_pct, self.params, pool.liquidity_usd, self.s.MAX_POSITION_USD)
        if a.bt.trades >= 3 and a.bt.total_return_pct < 0:
            a.reasons.append(f"backtest negative ({a.bt.total_return_pct:+.1f}% over {a.bt.trades} trades)")
        if a.score < self.s.MIN_SCORE and not a.reasons:
            a.reasons.append(f"score {a.score:.0f} < {self.s.MIN_SCORE:.0f}")
        if a.top10 is not None and a.top10 > self.s.MAX_TOP10_HOLDER_PCT:
            a.reasons.append(f"top holders own {a.top10:.0%}")
        return a

    async def get_candles(self, pool: PoolInfo):
        """Hourly candles: stored history from Postgres + only the missing recent hours from the API.
        Every fetched candle is stored, including for rejected tokens (avoids survivorship bias
        when optimising later)."""
        addr = pool.pool_address
        now = time.time()
        last = await self.db.last_candle_ts(addr) if self.db.ready else None
        if last and now - last < 900 * 3600:
            need = int((now - last) / 3600) + 3
            fresh = await self.gecko.ohlcv(addr, "hour", 1, need)
            stored = await self.db.load_candles(addr, int(now - 1000 * 3600))
            merged = {c.ts: c for c in stored}
            merged.update({c.ts: c for c in fresh})
            candles = [merged[k] for k in sorted(merged)]
        else:
            fresh = await self.gecko.ohlcv(addr, "hour", 1, 1000)
            candles = fresh
        await self.db.save_candles(addr, pool.mint, pool.symbol, pool.liquidity_usd, fresh)
        return candles

    async def collect(self, limit: int = 60) -> tuple[int, int]:
        """Backfill candle history for many tokens (winners AND losers) to give the optimiser
        a broad, less biased dataset. Returns (new_pools_stored, candles_stored)."""
        if not (self.jupiter and self.db.ready):
            return 0, 0
        tokens = await self.discover_tokens()
        seen, loose = set(), []
        for t in tokens:
            sym = (t["symbol"] or "").upper()
            if t["mint"] in seen or t["mint"] in EXCLUDED_MINTS or sym in EXCLUDED_SYMBOLS or "USD" in sym:
                continue
            seen.add(t["mint"])
            if t["age_days"] >= 10 and t["liquidity"] >= 20_000:
                loose.append(t)
        tracked = {r["mint"] for r in await self.db.tracked_pools(min_candles=1)}
        todo = [t for t in loose if t["mint"] not in tracked][:limit]
        pools = await self.gecko.best_pools([t["mint"] for t in todo])
        n_pools = n_candles = 0
        for t in todo:
            p = pools.get(t["mint"])
            if not p:
                continue
            try:
                candles = await self.gecko.ohlcv(p.pool_address, "hour", 1, 1000)
            except HttpError as e:
                logger.warning(f"collect: candles failed for {t['symbol']}: {e}")
                continue
            await self.db.save_candles(p.pool_address, t["mint"], t["symbol"], t["liquidity"], candles)
            n_pools += 1
            n_candles += len(candles)
        return n_pools, n_candles

    async def analyze_mint(self, mint: str) -> Analysis | None:
        self.params = await load_params(self.state, self.s)
        token = None
        if self.jupiter:
            try:
                info = await self.jupiter.token_info([mint])
                token = next((t for t in info if t["mint"] == mint), None)
            except HttpError:
                token = None
        pools = await self.gecko.best_pools([mint])
        if mint not in pools:
            return None
        pool = merge_token_into_pool(pools[mint], token) if token else pools[mint]
        return await self.analyze_pool(pool, token)

    async def run(self) -> list[Analysis]:
        if self.running:
            logger.info("Scan already running; skipped.")
            return []
        self.running = True
        started = time.time()
        try:
            self.params = await load_params(self.state, self.s)
            n_seen, kept, rejected = await self.candidates()
            logger.info(f"Scan: {n_seen} tokens/pools seen, {len(kept)} to analyse, rejected={rejected}")

            results: list[Analysis] = []
            for pool, token in kept:
                try:
                    a = await self.analyze_pool(pool, token)
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
                await self.notify(self._report(n_seen, len(kept), results, accepted, time.time() - started, rejected))
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
                "bt_avg": a.bt.avg_return_pct if a.bt else 0.0,
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

    def _report(self, n_pools, n_kept, results, accepted, secs, prefilter_rejected=None) -> str:
        from html import escape
        lines = [
            "🔎 <b>اكتمل المسح</b>",
            f"عملات مفحوصة: {n_pools} · اجتازت الفلاتر: {n_kept} · حُللت: {len(results)} · "
            f"في قائمة المراقبة: <b>{len(accepted)}</b> ({secs:.0f} ث)",
        ]
        for a in accepted[:10]:
            st, bt = a.stats, a.bt
            lines.append(
                f"• <b>{escape(a.pool.symbol)}</b> تقييم {a.score:.0f} | موجات {st.n_cycles} | "
                f"هبوط {st.median_drop:.0%} / صعود {st.median_rise:.0%} | "
                f"اختبار تاريخي: {bt.trades} صفقات {bt.total_return_pct:+.1f}%"
            )
        if prefilter_rejected:
            lines.append("مستبعدة في الفلترة الأولية: " + "، ".join(
                f"{escape(k)} ×{v}" for k, v in sorted(prefilter_rejected.items(), key=lambda x: -x[1])[:6]))
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
