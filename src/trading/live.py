"""Live broker: real swaps through Jupiter Swap API V2 (GET /order -> sign -> POST /execute).

Accounting uses the wallet's real SOL balance before/after every swap, so network fees,
priority fees and token-account rent are all included automatically. After the final sell
the empty token account is closed and its rent (~0.002 SOL) comes back to the wallet.

Same interface as PaperBroker: buy / sell / record_close."""
import time

from loguru import logger

from src.data.http import Http, HttpError
from src.trading.broker import RiskBlocked, check_risk
from src.trading.strategy import StrategyParams, trail_pct
from src.trading.wallet import LAMPORTS, SolanaWallet, sign_versioned

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_SWAP = "https://api.jup.ag/swap/v2"


class SwapFailed(Exception):
    pass


class LiveBroker:
    mode = "live"
    book = "live"

    def __init__(self, state, db, settings, http: Http, wallet: SolanaWallet, jupiter_key: str):
        self.state = state
        self.db = db
        self.s = settings
        self.http = http
        self.wallet = wallet
        self.headers = {"x-api-key": jupiter_key}
        self.sol_usd = settings.SOL_USD_FALLBACK

    async def enabled(self) -> bool:
        return await self.state.live_enabled()

    # ------------------------------------------------------------ swap primitive
    async def swap(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
                   check_impact: bool = True) -> dict:
        """One Jupiter swap. Returns {"signature", "in", "out", "sol_delta"} where sol_delta is the
        real change of the wallet's SOL balance in lamports (negative when SOL was spent)."""
        order = await self.http.get_json(f"{JUP_SWAP}/order", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount),
            "taker": self.wallet.address, "slippageBps": str(slippage_bps),
        }, headers=self.headers, retries=1)
        if not order.get("transaction"):
            raise SwapFailed(f"order rejected: {order.get('errorCode')} {order.get('errorMessage') or order.get('error')}")
        impact = order.get("priceImpact")   # percent (negative = cost); ambiguous fields are ignored
        if isinstance(impact, (int, float)):
            logger.info(f"quote {input_mint[:6]}->{output_mint[:6]} impact={float(impact):.2f}%")
        # Entries are refused on a bad quote. Exits are never blocked by this estimate (it proved
        # noisy: -11.6% on a $10 sell that filled fine seconds later); slippageBps still caps them.
        if check_impact and isinstance(impact, (int, float)) and abs(float(impact)) > slippage_bps / 100:
            raise SwapFailed(f"price impact {float(impact):.2f}% above limit")

        signed = sign_versioned(order["transaction"], self.wallet.kp)
        before = await self.wallet.sol_balance()
        res = await self.http.post_json(f"{JUP_SWAP}/execute",
                                        {"signedTransaction": signed, "requestId": order["requestId"]},
                                        headers=self.headers, retries=0)
        if res.get("status") != "Success":
            raise SwapFailed(f"execute failed: {res.get('code')} {res.get('error')} sig={res.get('signature')}")
        after = await self.wallet.wait_balance_change(before)
        return {
            "signature": res.get("signature"),
            "in": int(res.get("inputAmountResult") or res.get("totalInputAmount") or amount),
            "out": int(res.get("outputAmountResult") or res.get("totalOutputAmount") or order.get("outAmount") or 0),
            "sol_delta": after - before,
        }

    # ------------------------------------------------------------ broker interface
    async def buy(self, item: dict, price: float, liquidity: float | None, params=None) -> dict:
        reason = await check_risk(self.state, self.s, item["mint"], "live",
                                  self.s.LIVE_MAX_OPEN_POSITIONS, self.s.LIVE_DAILY_LOSS_USD)
        if reason:
            raise RiskBlocked(reason)
        size_usd = self.s.LIVE_POSITION_USD
        lamports = int(size_usd / self.sol_usd * LAMPORTS)
        reserve = int(self.s.LIVE_SOL_RESERVE * LAMPORTS)
        balance = await self.wallet.sol_balance()
        if balance - lamports < reserve:
            raise RiskBlocked(f"رصيد SOL غير كافٍ ({balance / LAMPORTS:.4f} SOL، الاحتياطي {self.s.LIVE_SOL_RESERVE} SOL)")

        existing = {a["pubkey"] for a in await self.wallet.token_accounts(item["mint"])}
        try:
            r = await self.swap(SOL_MINT, item["mint"], lamports, self.s.MAX_SLIPPAGE_BPS)
        except (SwapFailed, HttpError) as e:
            raise RiskBlocked(f"فشل الشراء الحقيقي: {e}") from e
        accounts = await self.wallet.token_accounts(item["mint"])
        tokens = sum(a["amount"] for a in accounts) or r["out"]
        decimals = accounts[0]["decimals"] if accounts else None
        if not tokens or decimals is None:
            raise RiskBlocked("تم التنفيذ لكن رصيد العملة غير ظاهر بعد؛ راجع المحفظة")
        # rent locked in a newly created token account comes back when it is closed:
        # it counts in the cost (PnL) but not in the entry price used for stop/targets
        rent = 0
        for a in accounts:
            if a["pubkey"] not in existing:
                rent += await self.wallet.account_lamports(a["pubkey"])
        cost_sol = -r["sol_delta"] / LAMPORTS            # includes fees + token-account rent
        cost_usd = cost_sol * self.sol_usd
        fill = (cost_sol - rent / LAMPORTS) * self.sol_usd / (tokens / 10 ** decimals)
        p = params or StrategyParams()
        stop_pct = item.get("stop_pct", 0.1)
        rise = item.get("median_rise", 0.2)
        pos = {
            "mint": item["mint"], "symbol": item.get("symbol", "?"),
            "qty": tokens, "remaining_qty": tokens, "decimals": decimals,
            "entry_price": fill, "cost_usd": cost_usd, "cost_sol": cost_sol,
            "stop": fill * (1 - stop_pct), "tp1": fill * (1 + rise * p.tp1_factor),
            "tp2": fill * (1 + rise * p.tp2_factor), "tp1_hit": False, "peak": fill,
            "trail_pct": trail_pct(item.get("median_drop", 0.2), p),
            "opened_at": int(time.time()),
            "time_stop_at": int(time.time() + item.get("time_stop_hours", 24) * 3600),
            "liquidity_at_entry": liquidity or item.get("liq_baseline"),
            "realized_usd": 0.0, "realized_sol": 0.0, "expected_pct": item.get("bt_avg"),
            "mode": self.mode, "buy_sig": r["signature"], "rent_sol": rent / LAMPORTS,
            "paper_ref_price": price,
        }
        await self.state.save_position(item["mint"], pos, self.book)
        await self.db.log_trade(pos["mint"], pos["symbol"], "BUY", tokens, fill, cost_usd, None, "entry", self.mode)
        logger.info(f"[LIVE] BUY {pos['symbol']} {cost_sol:.5f} SOL ({cost_usd:.2f}$) sig={r['signature']}")
        return pos

    async def sell(self, mint: str, fraction: float, price: float, reason: str,
                   liquidity: float | None = None) -> tuple[dict, float, bool]:
        pos = await self.state.get_position(mint, self.book)
        if not pos:
            raise RiskBlocked("لا يوجد مركز حقيقي")
        on_chain, _ = await self.wallet.token_balance(mint)
        full = fraction >= 1 or on_chain <= pos["qty"] * fraction * 1.01
        amount = on_chain if full else min(on_chain, int(pos["qty"] * fraction))
        if amount <= 0:
            await self.state.delete_position(mint, self.book)
            raise RiskBlocked("لا يوجد رصيد من العملة في المحفظة؛ أُغلق السجل")
        urgent = reason not in ("الهدف 1",)
        slippage = self.s.LIVE_EXIT_SLIPPAGE_BPS if urgent else self.s.MAX_SLIPPAGE_BPS
        r = await self.swap(mint, SOL_MINT, amount, slippage, check_impact=False)
        proceeds_sol = r["sol_delta"] / LAMPORTS
        closed = full
        if closed:
            before = await self.wallet.sol_balance()
            n = await self.wallet.close_empty_token_accounts(mint)
            if n:
                proceeds_sol += (await self.wallet.sol_balance() - before) / LAMPORTS  # rent back
        share = amount / pos["qty"] if not closed else pos["remaining_qty"] / pos["qty"]
        basis_usd = pos["cost_usd"] * share
        proceeds_usd = proceeds_sol * self.sol_usd
        pnl = proceeds_usd - basis_usd
        pos["remaining_qty"] = 0 if closed else pos["remaining_qty"] - amount
        pos["realized_usd"] += pnl
        pos["realized_sol"] += proceeds_sol - pos["cost_sol"] * share
        await self.state.add_daily_pnl(pnl, self.book)
        if closed:
            await self.state.delete_position(mint, self.book)
        else:
            await self.state.save_position(mint, pos, self.book)
        fill = proceeds_usd / (amount / 10 ** pos["decimals"]) if amount else price
        await self.db.log_trade(mint, pos["symbol"], "SELL", amount, fill, proceeds_usd, pnl, reason, self.mode)
        logger.info(f"[LIVE] SELL {pos['symbol']} {proceeds_sol:.5f} SOL pnl={pnl:+.2f}$ ({reason}) sig={r['signature']}")
        return pos, pnl, closed

    async def record_close(self, pos: dict, reason: str):
        await self.db.save_closed_position(pos, reason)

    # ------------------------------------------------------------ round-trip self test
    async def self_test(self, usd: float = 2.0) -> str:
        """Buy `usd` of USDC with SOL, sell it all back, close the USDC account.
        Proves signing, execution, balance accounting and rent reclaim end to end."""
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        start = await self.wallet.sol_balance()
        lam = int(usd / self.sol_usd * LAMPORTS)
        r1 = await self.swap(SOL_MINT, usdc, lam, 100)
        bal, _ = await self.wallet.token_balance(usdc)
        r2 = await self.swap(usdc, SOL_MINT, bal or r1["out"], 100)
        closed = await self.wallet.close_empty_token_accounts(usdc)
        end = await self.wallet.sol_balance()
        cost = (start - end) / LAMPORTS
        return (f"شراء: {r1['signature']}\nبيع: {r2['signature']}\n"
                f"حسابات أُغلقت واسترد إيجارها: {closed}\n"
                f"التكلفة الكاملة للجولة: {cost:.6f} SOL ≈ {cost * self.sol_usd:.3f}$ "
                f"({cost * self.sol_usd / usd:.2%} من {usd}$)")
