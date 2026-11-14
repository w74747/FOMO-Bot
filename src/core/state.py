"""Fast, live state kept in Redis (watchlist, open positions, risk counters)."""
import json
import time
from datetime import datetime, timezone

import redis.asyncio as redis

P = "fb:"  # key prefix


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StateManager:
    def __init__(self, url: str | None = None, client=None):
        if client is not None:
            self.r = client
        else:
            self.r = redis.from_url(url, decode_responses=True)

    # ---------- generic json helpers ----------
    async def _hget_json(self, key: str, field: str) -> dict | None:
        raw = await self.r.hget(P + key, field)
        return json.loads(raw) if raw else None

    async def _hgetall_json(self, key: str) -> dict[str, dict]:
        raw = await self.r.hgetall(P + key)
        return {k: json.loads(v) for k, v in raw.items()}

    # ---------- watchlist ----------
    async def get_watchlist(self) -> dict[str, dict]:
        return await self._hgetall_json("watchlist")

    async def set_watch_item(self, mint: str, item: dict):
        await self.r.hset(P + "watchlist", mint, json.dumps(item))

    async def replace_watchlist(self, items: dict[str, dict]):
        pipe = self.r.pipeline()
        pipe.delete(P + "watchlist")
        if items:
            pipe.hset(P + "watchlist", mapping={k: json.dumps(v) for k, v in items.items()})
        await pipe.execute()

    async def remove_watch_item(self, mint: str):
        await self.r.hdel(P + "watchlist", mint)

    # ---------- positions ----------
    async def get_positions(self) -> dict[str, dict]:
        return await self._hgetall_json("positions")

    async def get_position(self, mint: str) -> dict | None:
        return await self._hget_json("positions", mint)

    async def save_position(self, mint: str, pos: dict):
        await self.r.hset(P + "positions", mint, json.dumps(pos))

    async def delete_position(self, mint: str):
        await self.r.hdel(P + "positions", mint)

    # ---------- paper balance & daily pnl ----------
    async def get_balance(self, default: float) -> float:
        val = await self.r.get(P + "paper:balance")
        if val is None:
            await self.r.set(P + "paper:balance", default)
            return default
        return float(val)

    async def add_balance(self, delta: float) -> float:
        return float(await self.r.incrbyfloat(P + "paper:balance", delta))

    async def get_daily_pnl(self) -> float:
        val = await self.r.get(P + f"pnl:{_today()}")
        return float(val) if val else 0.0

    async def add_daily_pnl(self, delta: float):
        key = P + f"pnl:{_today()}"
        await self.r.incrbyfloat(key, delta)
        await self.r.expire(key, 7 * 86400)

    # ---------- flags ----------
    async def is_paused(self) -> bool:
        return await self.r.get(P + "paused") == "1"

    async def set_paused(self, paused: bool):
        await self.r.set(P + "paused", "1" if paused else "0")

    async def set_cooldown(self, mint: str, hours: float):
        await self.r.set(P + f"cooldown:{mint}", "1", ex=max(1, int(hours * 3600)))

    async def in_cooldown(self, mint: str) -> bool:
        return bool(await self.r.exists(P + f"cooldown:{mint}"))

    # ---------- strategy parameter overrides ----------
    async def get_param_overrides(self) -> dict:
        raw = await self.r.get(P + "params")
        return json.loads(raw) if raw else {}

    async def set_param_overrides(self, params: dict | None):
        if params:
            await self.r.set(P + "params", json.dumps(params))
        else:
            await self.r.delete(P + "params")

    # ---------- caches / meta ----------
    async def cache_get(self, key: str) -> dict | None:
        raw = await self.r.get(P + "cache:" + key)
        return json.loads(raw) if raw else None

    async def cache_set(self, key: str, value: dict, ttl_sec: int):
        await self.r.set(P + "cache:" + key, json.dumps(value), ex=ttl_sec)

    async def set_meta(self, key: str, value: str):
        await self.r.set(P + "meta:" + key, value)

    async def get_meta(self, key: str) -> str | None:
        return await self.r.get(P + "meta:" + key)

    async def mark_scan(self):
        await self.set_meta("last_scan", str(int(time.time())))
