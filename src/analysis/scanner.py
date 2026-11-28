"""Scanner: discovers live Solana meme pools, filters them, analyses their waves,
back-tests the strategy on each, and builds the watchlist."""
import statistics
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

from src.analysis.backtest import BacktestResult, backtest, bt_kwargs
from src.analysis.waves import WaveStats, clean_candles, score, wave_stats
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
    paper_only: bool = False     # low-liquidity experiment: paper book only

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
        elif t["liquidity"] < min(s.MIN_LIQUIDITY_USD, getattr(s, "PAPER_ONLY_MIN_LIQUIDITY_USD", 0) or s.MIN_LIQUIDITY_USD):
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
            kept[t["mint"]] = {**t, "paper_only": t["liquidity"] < s.MIN_LIQUIDITY_USD}
    # real-money candidates first, then the paper-only experiment, each by volume
    ranked = sorted(kept.values(), key=lambda t: (t["paper_only"], -t["volume_24h"]))
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


BASE58_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def extract_address(text: str) -> str | None:
    """Pull a Solana address out of a raw address or a pasted link
    (Birdeye, Solscan, pump.fun, Jupiter, DexScreener…)."""
    found = BASE58_RE.findall(text or "")
    return max(found, key=len) if found else None


def bt_reject_reason(bt, s) -> str | None:
    """Per-coin acceptance on the realistic back-test. Out-of-sample test (2026-11-23, 89 coins,
    17 cutoffs): '>= 2 trades + positive median' gave more trades (142 vs 123) and a higher
    total in both halves (A +729 vs +707, B +428 vs +251) than '>= 3 trades + positive sum'."""
    if bt.trades < s.MIN_BT_TRADES:
        return f"not enough backtest evidence ({bt.trades} trades)"
    if bt.total_return_pct < 0:
        return f"backtest negative ({bt.total_return_pct:+.1f}% over {bt.trades} trades)"
    if getattr(s, "BT_ACCEPT_MEDIAN", False) and bt.returns and statistics.median(bt.returns) <= 0:
        return f"backtest median trade not positive ({statistics.median(bt.returns) * 100:+.1f}%)"
    return None


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
        self._manual_results: list[Analysis] = []

    async def discover_tokens(self) -> list[dict]:
        """Union of Jupiter's top lists over several windows. Short windows (6h, 1h) surface
        older coins that are active right now and never make the 24h top 100."""
        tokens: list[dict] = []
        intervals = [i.strip() for i in self.s.DISCOVERY_INTERVALS.split(",") if i.strip()]
        for interval in intervals:
            for cat in TOKEN_CATEGORIES:
                try:
                    tokens += await self.jupiter.top_tokens(cat, interval, 100)
                except HttpError as e:
                    logger.warning(f"Jupiter {cat}/{interval} failed: {e}")
        seen = {t["mint"] for t in tokens}
        extra = await self.gecko_tokens(seen)
        seen |= {t["mint"] for t in extra}
        return tokens + extra + await self.known_tokens(seen)

    async def gecko_tokens(self, exclude: set[str]) -> list[dict]:
        """Second discovery source: GeckoTerminal trending and top-volume Solana pools. Their
        base mints get fresh Jupiter data (one request per 100) and go through the same filters."""
        pages = getattr(self.s, "GECKO_DISCOVERY_PAGES", 0)
        if not (pages and self.gecko and self.jupiter):
            return []
        mints: list[str] = []
        for name in ("trending_pools", "top_pools"):
            fetch = getattr(self.gecko, name, None)
            if fetch is None:
                continue
            for page in range(1, pages + 1):
                try:
                    mints += [p.mint for p in await fetch(page) if p.mint]
                except Exception as e:
                    logger.warning(f"gecko discovery {name} p{page}: {e}")
                    break
        mints = [m for m in dict.fromkeys(mints) if m not in exclude]
        try:
            out = await self.jupiter.token_info(mints) if mints else []
        except Exception as e:
            logger.warning(f"gecko discovery token info failed: {e}")
            return []
        logger.info(f"gecko discovery: {len(mints)} new mints, {len(out)} with Jupiter data")
        return [t for t in out if t["mint"] not in exclude]

    async def known_tokens(self, exclude: set[str]) -> list[dict]:
        """Coins we already hold candle history for. Many drop out of Jupiter's top lists only
        because trading went quiet for a while; their waves may still be fine. Fresh Jupiter
        data (liquidity, holders, audit) for up to 100 mints costs one request."""
        if not (getattr(self.s, "KNOWN_COINS_RECHECK", False) and getattr(self.db, "ready", False) and self.jupiter):
            return []
        try:
            mints = list(dict.fromkeys(r["mint"] for r in await self.db.tracked_pools(min_candles=300)
                                       if r["mint"] not in exclude))
            out = await self.jupiter.token_info(mints) if mints else []
        except Exception as e:
            logger.warning(f"known-coins recheck failed: {e}")
            return []
        out = [t for t in out if t["mint"] not in exclude]
        logger.info(f"known coins rechecked: {len(out)} of {len(mints)} still listed")
        return out

    async def candidates(self) -> tuple[int, list[tuple[PoolInfo, dict | None]], dict[str, int]]:
        """Returns (n_seen, [(pool, jupiter_token_or_None)], rejected_counts)."""
        if self.jupiter:
            tokens = await self.discover_tokens()
            if tokens:
                kept, rejected = prefilter_tokens(tokens, self.s)
                # Cheap on-chain safety check first (RPC, no candle quota): tokens with an active
                # mint/freeze authority (tokenised stocks, bridged assets…) must not use up analysis slots.
                safe = []
                for t in kept:
                    if len(safe) >= self.s.MAX_ANALYZE_PER_SCAN:
                        break
                    chk = await self.safety.mint_check(t["mint"])
                    if chk.get("ok", True):
                        safe.append(t)
                    else:
                        key = (chk.get("reasons") or ["unsafe mint"])[0].split(" (")[0]
                        rejected[key] = rejected.get(key, 0) + 1
                kept = safe
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
            a.paper_only = bool(token.get("paper_only"))
        a.safety = await self.safety.mint_check(pool.mint)
        if not a.safety.get("ok", True):
            a.reasons.extend(a.safety["reasons"])
            return a
        try:
            candles = await self.get_candles(pool)
        except HttpError as e:
            a.reasons.append(f"no candles ({e.status})")
            return a
        candles = clean_candles(candles)
        recent = candles[-self.s.HISTORY_HOURS:]
        a.stats = wave_stats(recent, self.params.zigzag_pct)
        if a.stats is None:
            a.reasons.append("not enough candles")
            return a
        a.score, reasons = score(a.stats, pool.liquidity_usd, self.params.max_cycle_hours)
        a.reasons.extend(reasons)
        fee_usd = await self.state.tx_fee_usd(self.s)
        a.plan = build_plan(
            a.stats.last_high, a.stats.median_drop, a.stats.median_rise, a.stats.avg_cycle_hours,
            pool.liquidity_usd, self.s.MAX_POSITION_USD, self.params, fee_usd,
        )
        if a.plan is None and not a.reasons:
            a.reasons.append("reward/risk too low after costs")
        a.bt = backtest(candles, self.params.zigzag_pct, self.params, pool.liquidity_usd, self.s.MAX_POSITION_USD,
                        tx_fee_usd=fee_usd, **bt_kwargs(self.s))
        why = bt_reject_reason(a.bt, self.s)
        if why:
            a.reasons.append(why)
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

    async def collect_fine(self) -> tuple[int, int]:
        """5-minute candles of the active pools (research). First time: page back
        FINE_BACKFILL_PAGES x 1000 bars; afterwards only the missing recent bars.
        Returns (pools touched, candles stored)."""
        import asyncio
        if not getattr(self.db, "ready", False):
            return 0, 0
        pools = (await self.db.active_pools(self.s.FINE_ACTIVE_HOURS))[: self.s.FINE_MAX_POOLS]
        touched = stored = 0
        now = time.time()
        for p in pools:
            if getattr(self.gecko, "fallback", False):
                break                              # demo quota used up: candles/shadow first
            addr = p["pool"]
            try:
                first, last = await self.db.fine_span(addr)
                if last and now - last < 3 * 86400:
                    need = int((now - last) / 300) + 3
                    cs = await self.gecko.ohlcv(addr, "minute", 5, need)
                    await self.db.save_fine(addr, cs)
                    stored += len(cs)
                    await asyncio.sleep(self.s.FINE_CALL_GAP_SEC)
                else:
                    before = None
                    for _ in range(self.s.FINE_BACKFILL_PAGES):
                        cs = await self.gecko.ohlcv(addr, "minute", 5, 1000, before=before)
                        await self.db.save_fine(addr, cs)
                        stored += len(cs)
                        await asyncio.sleep(self.s.FINE_CALL_GAP_SEC)
                        if len(cs) < 900:
                            break
                        before = cs[0].ts
                touched += 1
            except Exception as e:
                logger.warning(f"fine candles {p.get('symbol')}: {e}")
                await asyncio.sleep(self.s.FINE_CALL_GAP_SEC)
        logger.info(f"fine candles: {touched} pools, {stored} candles stored")
        return touched, stored

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

    async def shadow_refresh(self) -> dict:
        """Refresh candles of watched coins and apply the back-test's verdict and levels."""
        if not getattr(self.s, "SHADOW_GATE", False) or not getattr(self.db, "ready", False):
            return {}
        import asyncio
        from concurrent.futures import ProcessPoolExecutor
        from dataclasses import asdict

        from src.analysis.shadow import shadow_states
        self.params = await load_params(self.state, self.s)
        items = await self.state.get_watchlist()
        datasets = {}
        for mint, it in items.items():
            if it.get("status") == "holding" or not it.get("pool"):
                continue
            try:
                from types import SimpleNamespace
                pool = SimpleNamespace(pool_address=it["pool"], mint=mint, symbol=it.get("symbol", "?"),
                                       liquidity_usd=float(it.get("liquidity") or 0))
                candles = await self.get_candles(pool)
                datasets[mint] = ([tuple(c) for c in candles], float(it.get("liquidity") or 100_000))
            except Exception as e:
                logger.warning(f"shadow candles {it.get('symbol')}: {e}")
        if not datasets:
            return {}
        fee = await self.state.tx_fee_usd(self.s)
        with ProcessPoolExecutor(max_workers=1) as ex:
            res = await asyncio.get_running_loop().run_in_executor(
                ex, shadow_states, datasets, asdict(self.params), self.s.MAX_POSITION_USD, fee, bt_kwargs(self.s),
                time.time())
        now = int(time.time())
        cur = await self.state.get_watchlist()           # re-read: the monitor may have changed items
        for mint, v in res.items():
            it = cur.get(mint)
            if not it or it.get("status") == "holding":
                continue
            it["shadow"] = {"ok": v["ok"], "why": v["why"], "ts": now}
            if v.get("signal"):
                it["bt_signal"] = v["signal"]        # ENTRY_ON_CLOSE: the back-test bought at this close
            else:
                it.pop("bt_signal", None)
            if v["ok"]:
                moved = abs(v["entry"] - it.get("entry", 0)) / max(it.get("entry", 1e-12), 1e-12) > 0.001
                for k in ("ref_high", "entry", "stop", "tp1", "tp2", "stop_pct", "time_stop_hours",
                          "median_drop", "median_rise", "avg_cycle_hours"):
                    it[k] = v[k]
                if moved and it.get("status") == "armed" and it.get("local_min", 0) > it["entry"]:
                    it.update(status="waiting", local_min=None)   # the zone moved below the price
            elif it.get("status") == "armed":
                it.update(status="waiting", local_min=None)
            await self.state.set_watch_item(mint, it)
        logger.info("shadow: " + ", ".join(f"{cur.get(m, {}).get('symbol', m[:6])}="
                                           f"{'ok' if v['ok'] else v['why']}" for m, v in res.items()))
        return res

    async def update_health(self):
        """Strategy health: realistic back-test of the current params on every stored coin,
        averaged over recent (closed) trades. Gates live entries; shown in the scan report."""
        if not getattr(self.db, "ready", False) or not getattr(self.s, "HEALTH_GATE", False):
            return
        import asyncio
        from concurrent.futures import ProcessPoolExecutor
        from dataclasses import asdict

        from src.analysis.regime import health_now
        try:
            self.params = await load_params(self.state, self.s)
            datasets = []
            for p in await self.db.tracked_pools(min_candles=300):
                candles = await self.db.load_candles(p["pool"])
                datasets.append((p["pool"], [tuple(c) for c in candles], float(p["liquidity_usd"] or 100_000)))
            fee = await self.state.tx_fee_usd(self.s)
            with ProcessPoolExecutor(max_workers=1) as ex:
                h = await asyncio.get_running_loop().run_in_executor(
                    ex, health_now, datasets, asdict(self.params), self.s.MAX_POSITION_USD, fee,
                    bt_kwargs(self.s), time.time(), self.s.HEALTH_WINDOW_DAYS, self.s.HEALTH_LAG_DAYS,
                    self.s.HEALTH_MIN_TRADES)
            await self.state.set_health(h)
            logger.info(f"strategy health: {h}")
        except Exception as e:
            logger.exception(f"health update failed: {e}")

    async def run(self) -> list[Analysis]:
        if self.running:
            logger.info("Scan already running; skipped.")
            return []
        self.running = True
        self._manual_results = []
        started = time.time()
        try:
            self.params = await load_params(self.state, self.s)
            n_seen, kept, rejected = await self.candidates()
            manual = await self.state.get_manual()
            for mint in manual - {p.mint for p, _ in kept}:
                try:
                    a, why = await self.analyze_manual(mint)
                except Exception as e:
                    logger.warning(f"manual coin {mint} failed: {e}")
                    continue
                if a is not None:
                    if why and why not in a.reasons:
                        a.reasons.insert(0, why)
                    self._manual_results.append(a)
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

            results.extend(self._manual_results)
            for a in self._manual_results:
                await self.db.save_analysis(
                    a.pool.mint, a.pool.symbol, a.pool.pool_address, a.score,
                    a.stats.to_dict() if a.stats else {}, a.bt.to_dict() if a.bt else {},
                    a.plan.to_dict() if a.plan else None)
            manual_ok = [a for a in self._manual_results if a.accepted]
            auto_ok = sorted((a for a in results if a.accepted and a not in manual_ok),
                             key=lambda a: a.score, reverse=True)
            real_ok = [a for a in auto_ok if not a.paper_only]
            paper_ok = [a for a in auto_ok if a.paper_only]
            accepted = (manual_ok + real_ok[: self.s.WATCHLIST_SIZE]   # user coins come on top
                        + paper_ok[: getattr(self.s, "PAPER_ONLY_WATCH_SIZE", 0)])
            accepted = await self._update_watchlist(accepted)
            await self.state.mark_scan()
            await self.shadow_refresh()
            await self.update_health()
            logger.info(f"Scan done in {time.time() - started:.0f}s: {len(accepted)} on watchlist")
            if self.notify:
                text = self._report(n_seen, len(kept), results, accepted, time.time() - started, rejected)
                text += "\n" + await self.health_line()
                await self.notify(text)
            return results
        finally:
            self.running = False

    @staticmethod
    def watch_item(a: Analysis, prev: dict, positions: set[str], now: int, manual: bool = False) -> dict:
        p, st, pl = a.pool, a.stats, a.plan
        return {
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
            "status": "holding" if p.mint in positions else (
                "armed" if prev.get("status") == "armed" else "waiting"),
            "local_min": prev.get("local_min") if prev.get("status") == "armed" else None,
            "added_at": prev.get("added_at", now),
            "liq_baseline": prev.get("liq_baseline"),
            "manual": manual or prev.get("manual", False),
            "paper_only": bool(a.paper_only) and not (manual or prev.get("manual", False)),
            "updated_at": now,
        }

    async def resolve_mint(self, text: str) -> str | None:
        """Accept a mint or a pasted link; if it is a pool/pair address, map it to the token."""
        addr = extract_address(text)
        if not addr:
            return None
        if self.jupiter:
            try:
                if any(t["mint"] == addr for t in await self.jupiter.token_info([addr])):
                    return addr
            except HttpError:
                pass
        return await self.gecko.pool_base_mint(addr) or addr

    async def analyze_manual(self, mint: str) -> tuple[Analysis | None, str | None]:
        """Analyse a user-suggested coin with the SAME rules as the scanner.
        Returns (analysis, prefilter_rejection_reason)."""
        self.params = await load_params(self.state, self.s)
        token = None
        if self.jupiter:
            try:
                token = next((t for t in await self.jupiter.token_info([mint]) if t["mint"] == mint), None)
            except HttpError:
                token = None
        if token:
            _, rejected = prefilter_tokens([token], self.s)
            if rejected:
                reason = next(iter(rejected))
                pools = await self.gecko.best_pools([mint])
                a = await self.analyze_pool(merge_token_into_pool(pools[mint], token), token) if mint in pools else None
                return a, reason
        pools = await self.gecko.best_pools([mint])
        if mint not in pools:
            return None, "no supported pool"
        pool = merge_token_into_pool(pools[mint], token) if token else pools[mint]
        return await self.analyze_pool(pool, token), None

    async def _update_watchlist(self, accepted: list[Analysis]) -> list[Analysis]:
        """Write the watchlist; returns the analyses that were actually added."""
        old = await self.state.get_watchlist()
        positions = await self.state.held_mints()   # paper + live
        new: dict[str, dict] = {}
        added: list[Analysis] = []
        now = int(time.time())
        for a in accepted:
            p = a.pool
            if await self.state.in_cooldown(p.mint) and p.mint not in positions:
                continue  # recently stopped out or liquidity collapsed
            added.append(a)
            new[p.mint] = self.watch_item(a, old.get(p.mint, {}), positions, now,
                                          manual=a in self._manual_results)
        # keep items that have an open position even if they dropped off the list
        for mint in positions:
            if mint not in new and mint in old:
                new[mint] = {**old[mint], "status": "holding"}
        await self.state.replace_watchlist(new)
        return added

    async def health_line(self) -> str:
        if not getattr(self.s, "HEALTH_GATE", False):
            return ""
        h = await self.state.get_health()
        if not h or h.get("value") is None:
            return "🩺 صحة الاستراتيجية: غير متوفرة بعد"
        v = h["value"]
        trend = ""
        if h.get("prev") is not None:
            trend = " ↗" if v > h["prev"] else " ↘"
        try:
            live_on = await self.state.live_enabled()
        except Exception:
            live_on = False
        stop_at = getattr(self.s, "AUTO_STOP_HEALTH", self.s.HEALTH_MIN_PCT)
        if v <= self.s.HEALTH_MIN_PCT:
            verdict = "⛔ الحقيقي محجوب (الورقي مستمر)"
        elif v > getattr(self.s, "AUTO_START_HEALTH", 2.0):
            verdict = "✅ جيدة" + (" · 💰 الحقيقي يعمل" if live_on else "")
        elif live_on:
            verdict = f"⚠️ ضعيفة · 💰 الحقيقي ما زال يعمل (يتوقف تلقائياً عند ≤ {stop_at:+.1f}%)"
        else:
            verdict = "⚠️ ضعيفة: لا بدء تلقائي للحقيقي"
        return (f"🩺 صحة الاستراتيجية: متوسط الصفقة في الاختبار مؤخراً <b>{v:+.2f}%</b>{trend} "
                f"({h['n']} صفقة) — {verdict}")

    def _report(self, n_pools, n_kept, results, accepted, secs, prefilter_rejected=None) -> str:
        from html import escape

        from src.bot.reasons import ar_reason
        lines = [
            "🔎 <b>اكتمل المسح</b>",
            f"عملات مفحوصة: {n_pools} · اجتازت الفلاتر: {n_kept} · حُللت: {len(results)} · "
            f"في قائمة المراقبة: <b>{len(accepted)}</b> ({secs:.0f} ث)",
        ]
        for a in accepted[:16]:
            st, bt = a.stats, a.bt
            star = "⭐ " if a in self._manual_results else ("🧪 " if a.paper_only else "")
            lines.append(
                f"• {star}<b>{escape(a.pool.symbol)}</b> تقييم {a.score:.0f} | موجات {st.n_cycles} | دورة ~{st.avg_cycle_hours:.0f}س | "
                f"هبوط {st.median_drop:.0%} / صعود {st.median_rise:.0%} | "
                f"اختبار تاريخي: {bt.trades} صفقات {bt.total_return_pct:+.1f}%"
            )
        if any(a.paper_only for a in accepted):
            lines.append("🧪 = سيولة أقل من الحد: تُتداول ورقياً فقط (تجربة)")
        if prefilter_rejected:
            lines.append("مستبعدة في الفلترة الأولية: " + "، ".join(
                f"{escape(ar_reason(k))} ×{v}" for k, v in sorted(prefilter_rejected.items(), key=lambda x: -x[1])[:6]))
        top_reasons: dict[str, int] = {}
        for a in results:
            for r in a.reasons[:1]:
                key = r.split("(")[0].strip()
                top_reasons[key] = top_reasons.get(key, 0) + 1
        if top_reasons:
            lines.append("رُفضت بعد التحليل: " + "، ".join(
                f"{escape(ar_reason(k))} ×{v}" for k, v in sorted(top_reasons.items(), key=lambda x: -x[1])[:6]))
        return "\n".join(lines)
