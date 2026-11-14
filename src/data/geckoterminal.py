"""GeckoTerminal (CoinGecko on-chain) client: pool discovery and OHLCV candles.

Free keyless API, rate-limited. If COINGECKO_DEMO_API_KEY is set the CoinGecko
on-chain endpoints are used instead (same data, higher limit)."""
from datetime import datetime, timezone

from src.data.http import Http, RateLimiter
from src.data.models import Candle, PoolInfo

NETWORK = "solana"
QUOTE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT"}


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _age_days(created: str | None) -> float:
    if not created:
        return 0.0
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except ValueError:
        return 0.0


def _rel_id(item: dict, rel: str) -> str:
    return ((item.get("relationships") or {}).get(rel) or {}).get("data", {}).get("id", "") or ""


def parse_pools(payload: dict) -> list[PoolInfo]:
    """Parse a /pools style response (with include=base_token,quote_token)."""
    tokens = {}
    for inc in payload.get("included") or []:
        if inc.get("type") == "token":
            tokens[inc["id"]] = inc.get("attributes") or {}

    pools = []
    for item in payload.get("data") or []:
        a = item.get("attributes") or {}
        base_id = _rel_id(item, "base_token")
        quote_id = _rel_id(item, "quote_token")
        base = tokens.get(base_id, {})
        quote = tokens.get(quote_id, {})
        mint = base.get("address") or base_id.split("_", 1)[-1]
        if not mint:
            continue
        tx = ((a.get("transactions") or {}).get("h24") or {})
        name = a.get("name") or ""
        pools.append(PoolInfo(
            pool_address=a.get("address", ""),
            mint=mint,
            symbol=base.get("symbol") or name.split(" / ")[0],
            name=base.get("name") or name,
            quote_symbol=(quote.get("symbol") or (name.split(" / ")[1] if " / " in name else "")).upper(),
            dex=_rel_id(item, "dex"),
            price_usd=_f(a.get("base_token_price_usd")),
            liquidity_usd=_f(a.get("reserve_in_usd")),
            volume_24h_usd=_f((a.get("volume_usd") or {}).get("h24")),
            fdv_usd=_f(a.get("fdv_usd")),
            age_days=_age_days(a.get("pool_created_at")),
            tx_24h=int(_f(tx.get("buys")) + _f(tx.get("sells"))),
            price_change_24h=_f((a.get("price_change_percentage") or {}).get("h24")),
        ))
    return pools


def parse_ohlcv(payload: dict) -> list[Candle]:
    rows = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    candles = [Candle(int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5])) for r in rows if len(r) >= 6]
    candles = [c for c in candles if c.h > 0 and c.l > 0]
    candles.sort(key=lambda c: c.ts)  # API returns newest first
    return candles


class GeckoClient:
    def __init__(self, http: Http, calls_per_min: int = 20, demo_key: str | None = None):
        self.http = http
        if demo_key:
            self.base = "https://api.coingecko.com/api/v3/onchain"
            self.headers = {"accept": "application/json", "x-cg-demo-api-key": demo_key}
            calls_per_min = max(calls_per_min, 60)
        else:
            self.base = "https://api.geckoterminal.com/api/v2"
            self.headers = {"accept": "application/json"}
        self.limiter = RateLimiter(calls_per_min)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self.http.get_json(self.base + path, params=params, headers=self.headers, limiter=self.limiter)

    async def top_pools(self, page: int = 1) -> list[PoolInfo]:
        data = await self._get(
            f"/networks/{NETWORK}/pools",
            {"page": page, "sort": "h24_volume_usd_desc", "include": "base_token,quote_token"},
        )
        return parse_pools(data)

    async def token_pools(self, mint: str) -> list[PoolInfo]:
        data = await self._get(
            f"/networks/{NETWORK}/tokens/{mint}/pools",
            {"page": 1, "include": "base_token,quote_token"},
        )
        return [p for p in parse_pools(data) if p.mint == mint]

    async def ohlcv(self, pool: str, timeframe: str = "hour", aggregate: int = 1, limit: int = 720) -> list[Candle]:
        data = await self._get(
            f"/networks/{NETWORK}/pools/{pool}/ohlcv/{timeframe}",
            {"aggregate": aggregate, "limit": min(limit, 1000), "currency": "usd", "token": "base"},
        )
        return parse_ohlcv(data)
