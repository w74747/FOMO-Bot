"""Solana wallet: key loading, transaction signing and the few RPC calls the live broker needs.

The private key never leaves this process and is never logged."""
import asyncio
import base64
import json

from loguru import logger
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from src.data.http import Http, HttpError, RateLimiter

LAMPORTS = 1_000_000_000
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
)
CLOSE_ACCOUNT_IX = 9


def load_keypair(secret: str) -> Keypair:
    """Accepts the base58 string exported by Phantom/Solflare or a JSON byte array."""
    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


def sign_versioned(tx_b64: str, kp: Keypair) -> str:
    """Sign a base64 VersionedTransaction returned by Jupiter. Keeps any signatures already
    present (e.g. when Jupiter pays the fee) and fills in ours at our signer index."""
    raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = raw.message
    n = msg.header.num_required_signatures
    signers = list(msg.account_keys)[:n]
    me = kp.pubkey()
    if me not in signers:
        raise ValueError("wallet is not a required signer of this transaction")
    sigs = list(raw.signatures)
    sigs[signers.index(me)] = kp.sign_message(to_bytes_versioned(msg))
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


class SolanaWallet:
    def __init__(self, http: Http, rpc_url: str, secret: str):
        self.http = http
        self.rpc_url = rpc_url
        self.kp = load_keypair(secret)
        self.address = str(self.kp.pubkey())
        self.limiter = RateLimiter(300)

    async def rpc(self, method: str, params: list):
        data = await self.http.post_json(
            self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            limiter=self.limiter, retries=2)
        if "error" in data:
            raise HttpError(0, method, json.dumps(data["error"])[:300])
        return data.get("result")

    async def sol_balance(self) -> int:
        """Lamports, at 'confirmed' commitment."""
        res = await self.rpc("getBalance", [self.address, {"commitment": "confirmed"}])
        return int(res["value"])

    async def token_accounts(self, mint: str) -> list[dict]:
        """All token accounts of this wallet for `mint`: [{pubkey, program, amount(raw int), decimals}]."""
        res = await self.rpc("getTokenAccountsByOwner", [
            self.address, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        out = []
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            out.append({
                "pubkey": acc["pubkey"],
                "program": acc["account"]["owner"],
                "amount": int(info["tokenAmount"]["amount"]),
                "decimals": int(info["tokenAmount"]["decimals"]),
            })
        return out

    async def token_balance(self, mint: str) -> tuple[int, int | None]:
        accs = await self.token_accounts(mint)
        return sum(a["amount"] for a in accs), (accs[0]["decimals"] if accs else None)

    async def wait_balance_change(self, before: int, timeout: float = 20.0) -> int:
        """Poll the SOL balance until it differs from `before` (the swap has settled)."""
        waited = 0.0
        bal = before
        while waited < timeout:
            await asyncio.sleep(1.5)
            waited += 1.5
            bal = await self.sol_balance()
            if bal != before:
                return bal
        return bal

    async def close_empty_token_accounts(self, mint: str) -> int:
        """Close every EMPTY token account for `mint` to reclaim its rent (~0.002 SOL each).
        Returns the number of accounts closed. Accounts that still hold tokens are untouched."""
        empty = [a for a in await self.token_accounts(mint)
                 if a["amount"] == 0 and a["program"] in TOKEN_PROGRAMS]
        if not empty:
            return 0
        me = self.kp.pubkey()
        ixs = [set_compute_unit_limit(10_000 * len(empty) + 5_000), set_compute_unit_price(50_000)]
        for a in empty:
            ixs.append(Instruction(
                Pubkey.from_string(a["program"]), bytes([CLOSE_ACCOUNT_IX]),
                [AccountMeta(Pubkey.from_string(a["pubkey"]), False, True),   # account
                 AccountMeta(me, False, True),                                 # rent destination
                 AccountMeta(me, True, False)],                                # owner (signer)
            ))
        bh = await self.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        msg = MessageV0.try_compile(me, ixs, [], Hash.from_string(bh["value"]["blockhash"]))
        tx = VersionedTransaction(msg, [self.kp])
        sig = await self.rpc("sendTransaction", [
            base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}])
        for _ in range(20):
            await asyncio.sleep(1.5)
            st = await self.rpc("getSignatureStatuses", [[sig]])
            v = (st or {}).get("value", [None])[0]
            if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
                if v.get("err"):
                    logger.warning(f"close account tx failed: {v['err']}")
                    return 0
                return len(empty)
        logger.warning(f"close account tx not confirmed in time: {sig}")
        return 0
