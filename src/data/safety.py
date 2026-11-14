"""Token safety checks.

On-chain via Solana RPC (Helius): mint authority, freeze authority, dangerous
Token-2022 extensions. Optional Birdeye: top-10 holder concentration."""
from loguru import logger

from src.data.http import Http, HttpError, RateLimiter

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
DANGEROUS_EXTENSIONS = {"transferFeeConfig", "permanentDelegate", "nonTransferable", "transferHook", "pausableConfig"}


def evaluate_mint_account(value: dict | None) -> dict:
    """Turn a jsonParsed getAccountInfo value into a safety verdict."""
    if not value:
        return {"ok": False, "reasons": ["mint account not found"]}
    info = (((value.get("data") or {}).get("parsed") or {}).get("info")) or {}
    reasons = []
    if info.get("mintAuthority"):
        reasons.append("mint authority active (supply can be inflated)")
    if info.get("freezeAuthority"):
        reasons.append("freeze authority active (wallets can be frozen)")
    is_2022 = value.get("owner") == TOKEN_2022
    if is_2022:
        for ext in info.get("extensions") or []:
            name = ext.get("extension")
            if name not in DANGEROUS_EXTENSIONS:
                continue
            if name == "transferFeeConfig":
                state = ext.get("state") or {}
                fee = ((state.get("newerTransferFee") or {}).get("transferFeeBasisPoints") or 0)
                if not fee:
                    continue
                reasons.append(f"transfer fee {fee} bps")
            else:
                reasons.append(f"Token-2022 extension: {name}")
    return {"ok": not reasons, "reasons": reasons, "token2022": is_2022, "decimals": info.get("decimals")}


class SafetyChecker:
    def __init__(self, http: Http, rpc_url: str | None, birdeye_key: str | None):
        self.http = http
        self.rpc_url = rpc_url
        self.birdeye_key = birdeye_key
        self.rpc_limiter = RateLimiter(300)
        self.birdeye_limiter = RateLimiter(50)  # free tier is ~1 req/s
        self._birdeye_disabled = False

    async def mint_check(self, mint: str) -> dict:
        if not self.rpc_url:
            return {"ok": True, "reasons": [], "skipped": "no SOLANA_RPC_URL"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}]}
        try:
            data = await self.http.post_json(self.rpc_url, body, limiter=self.rpc_limiter, retries=2)
        except HttpError as e:
            logger.warning(f"RPC mint check failed for {mint}: {e}")
            return {"ok": True, "reasons": [], "skipped": "rpc error"}
        return evaluate_mint_account((data.get("result") or {}).get("value"))

    async def top10_pct(self, mint: str) -> float | None:
        """Share of supply held by the top 10 holders (0..1), or None if unknown."""
        if not self.birdeye_key or self._birdeye_disabled:
            return None
        try:
            data = await self.http.get_json(
                "https://public-api.birdeye.so/defi/token_security",
                params={"address": mint},
                headers={"X-API-KEY": self.birdeye_key, "x-chain": "solana", "accept": "application/json"},
                limiter=self.birdeye_limiter, retries=1,
            )
        except HttpError as e:
            if e.status in (401, 403):
                logger.warning("Birdeye key rejected for token_security; disabling Birdeye checks.")
                self._birdeye_disabled = True
            else:
                logger.warning(f"Birdeye token_security failed for {mint}: {e}")
            return None
        d = (data or {}).get("data") or {}
        val = d.get("top10HolderPercent")
        if val is None:
            return None
        val = float(val)
        return val / 100 if val > 1 else val
