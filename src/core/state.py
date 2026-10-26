# src/core/state.py
import json
import redis.asyncio as redis
from src.config import settings

class StateManager:
    def __init__(self):
        self.r = redis.from_url(settings.REDIS_URL, decode_responses=True)

    async def get_active_trader(self) -> dict | None:
        val = await self.r.get("active_trader")
        return json.loads(val) if val else None

    async def set_active_trader(self, trader_data: dict):
        await self.r.set("active_trader", json.dumps(trader_data))

    async def get_consecutive_losses(self, handle: str) -> int:
        val = await self.r.get(f"losses:{handle}")
        return int(val) if val else 0

    async def increment_consecutive_losses(self, handle: str) -> int:
        return await self.r.incr(f"losses:{handle}")

    async def reset_consecutive_losses(self, handle: str):
        await self.r.set(f"losses:{handle}", 0)

    async def update_position(self, token_mint: str, data: dict):
        await self.r.hset("open_positions", token_mint, json.dumps(data))

    async def get_position(self, token_mint: str) -> dict | None:
        data = await self.r.hget("open_positions", token_mint)
        return json.loads(data) if data else None

    async def delete_position(self, token_mint: str):
        await self.r.hdel("open_positions", token_mint)
