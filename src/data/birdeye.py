"""Birdeye Data Services (research, 2026-11-28): top traders of a token, to discover wallets
that trade OUR wave coins profitably ("smart wallets"). Free Standard plan: 30k CU/month at
1 req/s, so the bot asks for at most BIRDEYE_DAILY_COINS coins a day."""
from loguru import logger

from src.data.http import Http, HttpError, RateLimiter

BASE = "https://public-api.birdeye.so"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_top_traders(payload: dict) -> list[dict]:
    d = (payload or {}).get("data") or {}
    items = d.get("items") if isinstance(d, dict) else d
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        owner = it.get("owner") or it.get("address") or it.get("wallet")
        if not owner:
            continue
        tags = it.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        out.append({
            "wallet": owner,
            "pnl": _f(it.get("total_pnl", it.get("totalPnl", it.get("pnl")))),
            "realized": _f(it.get("realized_pnl", it.get("realizedPnl"))),
            "volume": _f(it.get("volume_usd", it.get("volumeUsd", it.get("volume")))),
            "trades": int(_f(it.get("trade", it.get("trades")))),
            "tags": [str(t) for t in tags],
        })
    return out


class BirdeyeClient:
    def __init__(self, http: Http, api_key: str | None):
        self.http = http
        self.key = api_key
        self.limiter = RateLimiter(50)          # free plan: 1 req/s
        self.denied: str | None = None          # set when the plan does not allow the endpoint

    @property
    def ready(self) -> bool:
        return bool(self.key) and not self.denied

    async def top_traders(self, mint: str, time_frame: str = "30d", sort_by: str = "total_pnl",
                          limit: int = 10) -> list[dict]:
        if not self.ready:
            return []
        try:
            data = await self.http.get_json(
                f"{BASE}/defi/v2/tokens/top_traders",
                params={"address": mint, "time_frame": time_frame, "sort_by": sort_by, "sort_type": "desc",
                        "offset": 0, "limit": limit},
                headers={"X-API-KEY": self.key, "x-chain": "solana", "accept": "application/json"},
                limiter=self.limiter, retries=1)
        except HttpError as e:
            if e.status in (401, 403):
                self.denied = f"HTTP {e.status}"
                logger.warning(f"Birdeye top_traders refused ({e.status}): plan does not include it")
            elif e.status == 400 and sort_by != "volume":
                return await self.top_traders(mint, "24h", "volume", limit)   # older plans: volume only
            else:
                logger.warning(f"Birdeye top_traders {mint[:6]}: {e}")
            return []
        return parse_top_traders(data)
