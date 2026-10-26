# src/core/database.py
import asyncpg
from src.config import settings

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(settings.DATABASE_URL)
        await self._init_schema()

    async def _init_schema(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id SERIAL PRIMARY KEY,
                    trader_handle TEXT NOT NULL,
                    token_mint TEXT NOT NULL,
                    realized_pnl DOUBLE PRECISION NOT NULL,
                    roi_percent DOUBLE PRECISION NOT NULL,
                    closed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)

    async def log_trade(self, handle: str, token_mint: str, pnl: float, roi: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO closed_trades (trader_handle, token_mint, realized_pnl, roi_percent) VALUES ($1, $2, $3, $4)",
                handle, token_mint, pnl, roi
            )
