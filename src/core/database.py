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
