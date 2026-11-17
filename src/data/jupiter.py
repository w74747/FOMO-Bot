"""Jupiter APIs: Price V3 (live prices, 50 mints per call) and Tokens V2
(discovery of active tokens with age, liquidity, holders and audit data)."""
from datetime import datetime, timezone

from loguru import logger

from src.data.http import Http, HttpError, PriorityLimiter

TOKEN_CATEGORIES = ("toptraded", "toporganicscore", "toptrending")


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_token(t: dict) -> dict | None:
    """Normalise one Tokens V2 record into the fields the scanner uses."""
    mint = t.get("id")
    if not mint:
        return None
    created = ((t.get("firstPool") or {}).get("createdAt")) or t.get("createdAt")
    age_days = 0.0
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except ValueError:
            pass
    audit = t.get("audit") or {}
    s24 = t.get("stats24h") or {}
    top = audit.get("topHoldersPercentage")
    return {
        "mint": mint,
        "symbol": t.get("symbol") or "?",
        "name": t.get("name") or "",
        "price": _num(t.get("usdPrice")),
        "liquidity": _num(t.get("liquidity")),
        "fdv": _num(t.get("fdv")) or _num(t.get("mcap")),
        "age_days": age_days,
        "volume_24h": _num(s24.get("buyVolume")) + _num(s24.get("sellVolume")),
        "holders": int(_num(t.get("holderCount"))),
        "organic_score": _num(t.get("organicScore")),
        "top_holders_pct": (_num(top) / 100) if top is not None else None,
        "mint_disabled": audit.get("mintAuthorityDisabled"),
        "freeze_disabled": audit.get("freezeAuthorityDisabled"),
        "is_sus": bool(audit.get("isSus")),
        "price_change_24h": _num(s24.get("priceChange")),
        "verified": bool(t.get("isVerified")),
    }


class JupiterClient:
    def __init__(self, http: Http, api_key: str | None, per_minute: int = 55, reserve: int = 15):
        """One budget for the whole account (prices, tokens and swap orders share Jupiter's
        main bucket). Free plan = 60 requests per 60-second sliding window."""
        self.http = http
        if api_key:
            self.base = "https://api.jup.ag"
            self.headers = {"x-api-key": api_key}
            self.limiter = PriorityLimiter(per_minute, reserve)
        else:
            logger.warning("JUPITER_API_KEY missing: using the low-limit lite endpoint.")
            self.base = "https://lite-api.jup.ag"
            self.headers = {}
            self.limiter = PriorityLimiter(28, 12)

    # ------------------------------------------------------------ prices
    async def prices(self, mints: list[str]) -> dict[str, dict]:
        """Returns {mint: {"price", "liquidity", "change24h"}}. Mints without a reliable
        price are simply missing from the result."""
        out: dict[str, dict] = {}
        mints = list(dict.fromkeys(m for m in mints if m))
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            try:
                data = await self.http.get_json(
                    f"{self.base}/price/v3", params={"ids": ",".join(chunk)},
                    headers=self.headers, limiter=self.limiter, retries=2, high=True, max_delay=3.0,
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

    # ------------------------------------------------------------ tokens
    async def _tokens_get(self, path: str, params: dict | None = None) -> list[dict]:
        data = await self.http.get_json(f"{self.base}/tokens/v2{path}", params=params,
                                        headers=self.headers, limiter=self.limiter, retries=2)
        if not isinstance(data, list):
            return []
        return [x for x in (parse_token(t) for t in data if isinstance(t, dict)) if x]

    async def top_tokens(self, category: str, interval: str = "24h", limit: int = 100) -> list[dict]:
        """category: toptraded | toporganicscore | toptrending."""
        return await self._tokens_get(f"/{category}/{interval}", {"limit": limit})

    async def token_info(self, mints: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(mints), 100):
            out += await self._tokens_get("/search", {"query": ",".join(mints[i:i + 100])})
        return out
