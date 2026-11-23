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


QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}


def parse_multi_top_pools(payload: dict) -> dict[str, list[PoolInfo]]:
    """Parse /tokens/multi/{mints}?include=top_pools into {mint: [pools where mint is the base]}."""
    included_pools = [i for i in (payload.get("included") or []) if i.get("type") == "pool"]
    token_stub = []
    for item in included_pools:  # synthesise quote token records so symbols resolve
        qid = _rel_id(item, "quote_token")
        qmint = qid.split("_", 1)[-1]
        if qmint in QUOTE_MINTS:
            token_stub.append({"id": qid, "type": "token",
                               "attributes": {"address": qmint, "symbol": QUOTE_MINTS[qmint]}})
    pools = parse_pools({"data": included_pools, "included": token_stub})
    out: dict[str, list[PoolInfo]] = {}
    for p in pools:
        out.setdefault(p.mint, []).append(p)
    return out


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

    async def trending_pools(self, page: int = 1) -> list[PoolInfo]:
        data = await self._get(f"/networks/{NETWORK}/trending_pools",
                               {"page": page, "include": "base_token,quote_token"})
        return parse_pools(data)

    async def token_pools(self, mint: str) -> list[PoolInfo]:
        data = await self._get(
            f"/networks/{NETWORK}/tokens/{mint}/pools",
            {"page": 1, "include": "base_token,quote_token"},
        )
        return [p for p in parse_pools(data) if p.mint == mint]

    async def pool_base_mint(self, pool: str) -> str | None:
        """If `pool` is a pool/pair address, return its base token mint."""
        try:
            data = await self._get(f"/networks/{NETWORK}/pools/{pool}", {"include": "base_token,quote_token"})
        except Exception:
            return None
        payload = {"data": [data.get("data")] if isinstance(data.get("data"), dict) else [],
                   "included": data.get("included") or []}
        pools = parse_pools(payload)
        return pools[0].mint if pools else None

    async def best_pools(self, mints: list[str]) -> dict[str, PoolInfo]:
        """Most liquid SOL/USDC/USDT pool for each mint (30 mints per call)."""
        out: dict[str, PoolInfo] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            try:
                data = await self._get(f"/networks/{NETWORK}/tokens/multi/{','.join(chunk)}",
                                       {"include": "top_pools"})
                found = parse_multi_top_pools(data)
            except Exception:
                found = {}
            for mint in chunk:
                pools = [p for p in found.get(mint, []) if p.quote_symbol in QUOTE_SYMBOLS]
                if not pools:  # fallback: one call per token
                    try:
                        pools = [p for p in await self.token_pools(mint) if p.quote_symbol in QUOTE_SYMBOLS]
                    except Exception:
                        pools = []
                if pools:
                    out[mint] = max(pools, key=lambda p: p.liquidity_usd)
        return out

    async def ohlcv(self, pool: str, timeframe: str = "hour", aggregate: int = 1, limit: int = 720) -> list[Candle]:
        data = await self._get(
            f"/networks/{NETWORK}/pools/{pool}/ohlcv/{timeframe}",
            {"aggregate": aggregate, "limit": min(limit, 1000), "currency": "usd", "token": "base"},
        )
        return parse_ohlcv(data)
