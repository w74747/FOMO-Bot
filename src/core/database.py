"""Durable history in Postgres. Every method is a no-op if the database is unavailable,
so a database outage never stops the bot."""
import json

import asyncpg
from loguru import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_analyses (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    pool TEXT,
    score DOUBLE PRECISION,
    stats JSONB,
    backtest JSONB,
    plan JSONB,
    scanned_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_analyses_mint ON token_analyses (mint, scanned_at DESC);

CREATE TABLE IF NOT EXISTS paper_trades (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    side TEXT NOT NULL,
    qty DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    usd DOUBLE PRECISION NOT NULL,
    pnl_usd DOUBLE PRECISION,
    reason TEXT,
    mode TEXT DEFAULT 'paper',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_time ON paper_trades (created_at DESC);

CREATE TABLE IF NOT EXISTS candles (
    pool TEXT NOT NULL,
    ts BIGINT NOT NULL,
    o DOUBLE PRECISION, h DOUBLE PRECISION, l DOUBLE PRECISION, c DOUBLE PRECISION, v DOUBLE PRECISION,
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS tracked_pools (
    pool TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    liquidity_usd DOUBLE PRECISION,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS closed_positions (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ DEFAULT NOW(),
    cost_usd DOUBLE PRECISION,
    pnl_usd DOUBLE PRECISION,
    return_pct DOUBLE PRECISION,
    exit_reason TEXT,
    expected_pct DOUBLE PRECISION,
    mode TEXT DEFAULT 'paper'
);
CREATE INDEX IF NOT EXISTS idx_closed_time ON closed_positions (closed_at);
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS entry_gap_pct DOUBLE PRECISION;
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS exit_gap_pct DOUBLE PRECISION;
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS tier TEXT;
CREATE TABLE IF NOT EXISTS candles_5m (
    pool TEXT NOT NULL,
    ts BIGINT NOT NULL,
    o DOUBLE PRECISION, h DOUBLE PRECISION, l DOUBLE PRECISION, c DOUBLE PRECISION, v DOUBLE PRECISION,
    PRIMARY KEY (pool, ts)
);
CREATE TABLE IF NOT EXISTS pool_trades (
    pool TEXT NOT NULL,
    tx TEXT NOT NULL,
    ts BIGINT NOT NULL,
    kind TEXT NOT NULL,
    usd DOUBLE PRECISION,
    trader TEXT,
    price DOUBLE PRECISION,
    PRIMARY KEY (pool, tx, kind)
);
CREATE INDEX IF NOT EXISTS idx_pool_trades_ts ON pool_trades (pool, ts);
CREATE TABLE IF NOT EXISTS holders (
    mint TEXT NOT NULL,
    ts BIGINT NOT NULL,
    holders INTEGER,
    PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS smart_wallets (
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    ts BIGINT NOT NULL,
    pnl DOUBLE PRECISION,
    realized DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    trades INTEGER,
    tags TEXT,
    PRIMARY KEY (wallet, mint)
);
CREATE TABLE IF NOT EXISTS jup_prices (
    mint TEXT NOT NULL,
    ts BIGINT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (mint, ts)
);
"""


class Database:
    def __init__(self, url: str | None):
        self.url = url
        self.pool: asyncpg.Pool | None = None

    @property
    def ready(self) -> bool:
        return self.pool is not None

    async def connect(self):
        if not self.url:
            logger.warning("DATABASE_URL not set: history will not be stored.")
            return
        try:
            self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                await conn.execute(SCHEMA)
            logger.info("Postgres connected and schema ready.")
        except Exception as e:
            self.pool = None
            logger.error(f"Postgres unavailable, continuing without history: {e}")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def save_analysis(self, mint, symbol, pool, score, stats: dict, backtest: dict, plan: dict | None):
        if not self.pool:
            return
        try:
            await self.pool.execute(
                "INSERT INTO token_analyses (mint, symbol, pool, score, stats, backtest, plan) "
                "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb)",
                mint, symbol, pool, score, json.dumps(stats), json.dumps(backtest), json.dumps(plan),
            )
        except Exception as e:
            logger.error(f"save_analysis failed: {e}")

    async def log_trade(self, mint, symbol, side, qty, price, usd, pnl_usd=None, reason="", mode="paper"):
        if not self.pool:
            return
        try:
            await self.pool.execute(
                "INSERT INTO paper_trades (mint, symbol, side, qty, price, usd, pnl_usd, reason, mode) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                mint, symbol, side, qty, price, usd, pnl_usd, reason, mode,
            )
        except Exception as e:
            logger.error(f"log_trade failed: {e}")

    # ------------------------------------------------------------ candles
    async def save_candles(self, pool: str, mint: str, symbol: str, liquidity: float, candles) -> None:
        if not self.pool or not candles:
            return
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(
                        "INSERT INTO candles (pool, ts, o, h, l, c, v) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                        "ON CONFLICT (pool, ts) DO UPDATE SET o=EXCLUDED.o, h=EXCLUDED.h, l=EXCLUDED.l, "
                        "c=EXCLUDED.c, v=EXCLUDED.v",
                        [(pool, c.ts, c.o, c.h, c.l, c.c, c.v) for c in candles],
                    )
                    await conn.execute(
                        "INSERT INTO tracked_pools (pool, mint, symbol, liquidity_usd) VALUES ($1,$2,$3,$4) "
                        "ON CONFLICT (pool) DO UPDATE SET last_seen=NOW(), liquidity_usd=EXCLUDED.liquidity_usd, "
                        "symbol=EXCLUDED.symbol",
                        pool, mint, symbol, liquidity,
                    )
        except Exception as e:
            logger.error(f"save_candles failed: {e}")

    async def load_candles(self, pool: str, since_ts: int = 0):
        from src.data.models import Candle
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT ts, o, h, l, c, v FROM candles WHERE pool=$1 AND ts >= $2 ORDER BY ts", pool, since_ts)
        return [Candle(r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows]

    async def last_candle_ts(self, pool: str) -> int | None:
        if not self.pool:
            return None
        return await self.pool.fetchval("SELECT MAX(ts) FROM candles WHERE pool=$1", pool)

    # ------------------------------------------------------------ 5-minute candles (research)
    async def save_fine(self, pool: str, candles) -> None:
        if not self.pool or not candles:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO candles_5m (pool, ts, o, h, l, c, v) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                    "ON CONFLICT (pool, ts) DO UPDATE SET o=EXCLUDED.o, h=EXCLUDED.h, l=EXCLUDED.l, "
                    "c=EXCLUDED.c, v=EXCLUDED.v",
                    [(pool, c.ts, c.o, c.h, c.l, c.c, c.v) for c in candles])
        except Exception as e:
            logger.error(f"save_fine failed: {e}")

    async def load_fine(self, pool: str, since_ts: int = 0):
        from src.data.models import Candle
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT ts, o, h, l, c, v FROM candles_5m WHERE pool=$1 AND ts >= $2 ORDER BY ts", pool, since_ts)
        return [Candle(r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows]

    async def fine_span(self, pool: str) -> tuple[int | None, int | None]:
        if not self.pool:
            return None, None
        r = await self.pool.fetchrow("SELECT MIN(ts) AS a, MAX(ts) AS b FROM candles_5m WHERE pool=$1", pool)
        return (r["a"], r["b"]) if r else (None, None)

    async def fine_pools(self) -> list[str]:
        if not self.pool:
            return []
        return [r["pool"] for r in await self.pool.fetch("SELECT DISTINCT pool FROM candles_5m")]

    async def active_pools(self, within_hours: float = 48, min_candles: int = 300) -> list[dict]:
        """Tracked pools whose hourly candles are recent (the coins the bot still looks at)."""
        if not self.pool:
            return []
        import time as _t
        rows = await self.pool.fetch(
            "SELECT t.pool, t.mint, t.symbol, t.liquidity_usd, MAX(c.ts) AS last, COUNT(c.ts) AS n "
            "FROM tracked_pools t JOIN candles c ON c.pool = t.pool "
            "GROUP BY t.pool, t.mint, t.symbol, t.liquidity_usd "
            "HAVING MAX(c.ts) >= $1 AND COUNT(c.ts) >= $2 ORDER BY MAX(c.ts) DESC",
            int(_t.time() - within_hours * 3600), min_candles)
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ order flow / holders / wallets
    async def save_trades(self, pool: str, trades: list[dict]) -> int:
        if not self.pool or not trades:
            return 0
        try:
            async with self.pool.acquire() as conn:
                before = await conn.fetchval("SELECT COUNT(*) FROM pool_trades WHERE pool=$1", pool)
                await conn.executemany(
                    "INSERT INTO pool_trades (pool, tx, ts, kind, usd, trader, price) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                    "ON CONFLICT DO NOTHING",
                    [(pool, t["tx"], t["ts"], t["kind"], t["usd"], t["trader"], t["price"]) for t in trades])
                after = await conn.fetchval("SELECT COUNT(*) FROM pool_trades WHERE pool=$1", pool)
                return int(after - before)
        except Exception as e:
            logger.error(f"save_trades failed: {e}")
            return 0

    async def last_trade_ts(self, pool: str) -> int | None:
        if not self.pool:
            return None
        return await self.pool.fetchval("SELECT MAX(ts) FROM pool_trades WHERE pool=$1", pool)

    async def load_trades(self, pool: str, since_ts: int = 0) -> list[dict]:
        if not self.pool:
            return []
        rows = await self.pool.fetch("SELECT ts, kind, usd, trader, price FROM pool_trades "
                                     "WHERE pool=$1 AND ts >= $2 ORDER BY ts", pool, since_ts)
        return [dict(r) for r in rows]

    async def save_holders(self, ts: int, counts: dict[str, int]) -> None:
        if not self.pool or not counts:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.executemany("INSERT INTO holders (mint, ts, holders) VALUES ($1,$2,$3) "
                                       "ON CONFLICT DO NOTHING", [(m, ts, int(n)) for m, n in counts.items() if n])
        except Exception as e:
            logger.error(f"save_holders failed: {e}")

    async def holders_series(self, mint: str, since_ts: int = 0) -> list[tuple[int, int]]:
        if not self.pool:
            return []
        rows = await self.pool.fetch("SELECT ts, holders FROM holders WHERE mint=$1 AND ts >= $2 ORDER BY ts",
                                     mint, since_ts)
        return [(r["ts"], r["holders"]) for r in rows]

    async def save_smart(self, mint: str, symbol: str, ts: int, traders: list[dict]) -> None:
        if not self.pool or not traders:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO smart_wallets (wallet, mint, symbol, ts, pnl, realized, volume, trades, tags) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (wallet, mint) DO UPDATE SET ts=EXCLUDED.ts, "
                    "pnl=EXCLUDED.pnl, realized=EXCLUDED.realized, volume=EXCLUDED.volume, trades=EXCLUDED.trades, "
                    "tags=EXCLUDED.tags",
                    [(t["wallet"], mint, symbol, ts, t["pnl"], t["realized"], t["volume"], t["trades"],
                      ",".join(t["tags"])) for t in traders])
        except Exception as e:
            logger.error(f"save_smart failed: {e}")

    async def smart_checked(self, since_ts: int) -> set[str]:
        if not self.pool:
            return set()
        return {r["mint"] for r in await self.pool.fetch(
            "SELECT DISTINCT mint FROM smart_wallets WHERE ts >= $1", since_ts)}

    async def smart_wallets(self, min_coins: int = 1) -> list[dict]:
        """Wallets with positive PnL in our coins, bots excluded, best first."""
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT wallet, COUNT(*) AS coins, SUM(pnl) AS pnl, SUM(volume) AS volume, "
            "string_agg(symbol, ',') AS symbols FROM smart_wallets "
            "WHERE pnl > 0 AND tags NOT ILIKE '%bot%' AND tags NOT ILIKE '%sniper%' AND tags NOT ILIKE '%arbitrage%' "
            "GROUP BY wallet HAVING COUNT(*) >= $1 ORDER BY COUNT(*) DESC, SUM(pnl) DESC", min_coins)
        return [dict(r) for r in rows]

    async def tracked_pools(self, min_candles: int = 300) -> list[dict]:
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT t.pool, t.mint, t.symbol, t.liquidity_usd, COUNT(c.ts) AS n "
            "FROM tracked_pools t JOIN candles c ON c.pool = t.pool "
            "GROUP BY t.pool, t.mint, t.symbol, t.liquidity_usd HAVING COUNT(c.ts) >= $1", min_candles)
        return [dict(r) for r in rows]

    async def candle_stats(self) -> dict:
        if not self.pool:
            return {}
        row = await self.pool.fetchrow("SELECT COUNT(DISTINCT pool) AS pools, COUNT(*) AS candles FROM candles")
        return dict(row) if row else {}

    # ------------------------------------------------------------ closed positions / report
    async def save_closed_position(self, pos: dict, exit_reason: str):
        if not self.pool:
            return
        from datetime import datetime, timezone
        try:
            ret = pos["realized_usd"] / pos["cost_usd"] * 100 if pos.get("cost_usd") else 0.0
            await self.pool.execute(
                "INSERT INTO closed_positions (mint, symbol, opened_at, cost_usd, pnl_usd, return_pct, "
                "exit_reason, expected_pct, mode, entry_gap_pct, exit_gap_pct, tier) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                pos["mint"], pos.get("symbol"),
                datetime.fromtimestamp(pos.get("opened_at", 0), tz=timezone.utc),
                pos.get("cost_usd"), pos["realized_usd"], ret, exit_reason,
                pos.get("expected_pct"), pos.get("mode", "paper"),
                pos.get("entry_gap_pct"), pos.get("exit_gap_pct"),
                "low_liq" if pos.get("paper_only") else None,
            )
        except Exception as e:
            logger.error(f"save_closed_position failed: {e}")

    async def closed_positions(self, since_hours: float | None = None) -> list[dict]:
        if not self.pool:
            return []
        if since_hours is None:
            rows = await self.pool.fetch("SELECT * FROM closed_positions ORDER BY closed_at")
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM closed_positions WHERE closed_at >= NOW() - make_interval(secs => $1) "
                "ORDER BY closed_at", float(since_hours * 3600))
        return [dict(r) for r in rows]

    async def count_stop_losses(self, mint: str, days: float = 7) -> int | None:
        """Stop-loss closes on a coin in the last `days` (history-based, survives restarts)."""
        if not self.pool:
            return None
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM closed_positions WHERE mint=$1 AND exit_reason='وقف خسارة' "
            "AND COALESCE(mode, 'paper') = 'paper' "       # one row per signal (live mirrors paper)
            "AND closed_at >= NOW() - make_interval(days => $2)", mint, int(days))

    async def mints_by_symbol(self, symbol: str) -> list[str]:
        if not self.pool:
            return []
        rows = await self.pool.fetch("SELECT DISTINCT mint FROM closed_positions WHERE symbol=$1", symbol)
        return [r["mint"] for r in rows]

    async def save_jup_prices(self, ts: int, prices: dict[str, float]):
        if not self.pool or not prices:
            return
        try:
            await self.pool.executemany(
                "INSERT INTO jup_prices (mint, ts, price) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                [(m, ts, p) for m, p in prices.items()])
        except Exception as e:
            logger.error(f"save_jup_prices failed: {e}")

    async def analyses_since(self, since) -> list[dict]:
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT mint, symbol, score, stats, backtest, plan, scanned_at FROM token_analyses WHERE scanned_at >= $1 "
            "ORDER BY scanned_at", since)
        out = []
        for r in rows:
            d = dict(r)
            for k in ("stats", "backtest", "plan"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        d[k] = None
            out.append(d)
        return out

    async def jup_mints(self, min_rows: int = 1440) -> list[str]:
        """Mints with at least `min_rows` 1-minute Jupiter samples (1440 = one day)."""
        if not self.pool:
            return []
        rows = await self.pool.fetch("SELECT mint FROM jup_prices GROUP BY mint HAVING COUNT(*) >= $1", min_rows)
        return [r["mint"] for r in rows]

    async def jup_hourly(self, mint: str) -> list[tuple]:
        """Hourly OHLC built from our 1-minute Jupiter samples: (ts, o, h, l, c, samples)."""
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT ts / 3600 * 3600 AS h, (array_agg(price ORDER BY ts))[1] AS o, MAX(price) AS hi, "
            "MIN(price) AS lo, (array_agg(price ORDER BY ts DESC))[1] AS c, COUNT(*) AS n "
            "FROM jup_prices WHERE mint=$1 GROUP BY 1 ORDER BY 1", mint)
        return [(r["h"], r["o"], r["hi"], r["lo"], r["c"], r["n"]) for r in rows]

    async def all_trades(self, mode: str | None = None) -> list[dict]:
        if not self.pool:
            return []
        if mode:
            rows = await self.pool.fetch("SELECT * FROM paper_trades WHERE mode=$1 ORDER BY created_at", mode)
        else:
            rows = await self.pool.fetch("SELECT * FROM paper_trades ORDER BY created_at")
        return [dict(r) for r in rows]

    async def recent_trades(self, limit: int = 10) -> list[dict]:
        if not self.pool:
            return []
        rows = await self.pool.fetch(
            "SELECT symbol, side, usd, pnl_usd, reason, created_at FROM paper_trades "
            "ORDER BY created_at DESC LIMIT $1", limit,
        )
        return [dict(r) for r in rows]

    async def pnl_summary(self) -> dict:
        if not self.pool:
            return {}
        row = await self.pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE pnl_usd IS NOT NULL) AS closes, "
            "COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins, "
            "COALESCE(SUM(pnl_usd),0) AS total FROM paper_trades"
        )
        return dict(row) if row else {}
