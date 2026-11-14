"""Jupiter Price API V3: live USD prices for up to 50 mints per call."""
from loguru import logger

from src.data.http import Http, HttpError, RateLimiter


class JupiterClient:
    def __init__(self, http: Http, api_key: str | None):
        self.http = http
        if api_key:
            self.base = "https://api.jup.ag"
            self.headers = {"x-api-key": api_key}
            self.limiter = RateLimiter(240)   # free key starts around 5 req/s
        else:
            logger.warning("JUPITER_API_KEY missing: using the low-limit lite endpoint.")
            self.base = "https://lite-api.jup.ag"
            self.headers = {}
            self.limiter = RateLimiter(50)

    async def prices(self, mints: list[str]) -> dict[str, dict]:
        """Returns {mint: {"price": float, "liquidity": float|None, "change24h": float|None}}.
        Mints without a reliable price are simply missing from the result."""
        out: dict[str, dict] = {}
        mints = list(dict.fromkeys(m for m in mints if m))
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            try:
                data = await self.http.get_json(
                    f"{self.base}/price/v3", params={"ids": ",".join(chunk)},
                    headers=self.headers, limiter=self.limiter, retries=1,
                )
            except HttpError as e:
                logger.warning(f"Jupiter price error: {e}")
                continue
            for mint, info in (data or {}).items():
                if not isinstance(info, dict) or info.get("usdPrice") is None:
                    continue
                out[mint] = {
                    "price": float(info["usdPrice"]),
                    "liquidity": float(info["liquidity"]) if info.get("liquidity") is not None else None,
                    "change24h": info.get("priceChange24h"),
                }
        return out
