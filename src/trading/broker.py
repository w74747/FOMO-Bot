"""Paper broker: simulates fills with realistic costs (fees + price impact) and keeps
positions, balance and daily PnL in Redis, history in Postgres.

A live broker (Jupiter Swap API V2 + wallet signing) will implement the same
`buy` / `sell` interface in the next phase."""
import time

from loguru import logger

from src.trading.strategy import StrategyParams, side_cost, trail_pct


class RiskBlocked(Exception):
    pass


async def check_risk(state, s, mint: str) -> str | None:
    """Return a reason string if a new position is NOT allowed, else None."""
    if await state.is_paused():
        return "البوت متوقف مؤقتاً (/resume)"
    positions = await state.get_positions()
    if mint in positions:
        return "يوجد مركز مفتوح على هذه العملة"
    if len(positions) >= s.MAX_OPEN_POSITIONS:
        return f"الحد الأقصى للمراكز المفتوحة ({s.MAX_OPEN_POSITIONS})"
    if await state.get_daily_pnl() <= -abs(s.DAILY_LOSS_LIMIT_USD):
        return f"تم بلوغ حد الخسارة اليومي ({s.DAILY_LOSS_LIMIT_USD}$)"
    if await state.in_cooldown(mint):
        return "العملة في فترة تهدئة بعد وقف خسارة"
    return None


class PaperBroker:
    mode = "paper"

    def __init__(self, state, db, settings):
        self.state = state
        self.db = db
        self.s = settings
        self.sol_usd = settings.SOL_USD_FALLBACK

    @property
    def tx_fee_usd(self) -> float:
        """Network fee (base + priority) per transaction, in USD."""
        return self.s.TX_FEE_SOL * self.sol_usd

    async def buy(self, item: dict, price: float, liquidity: float | None, params=None) -> dict:
        reason = await check_risk(self.state, self.s, item["mint"])
        if reason:
            raise RiskBlocked(reason)
        balance = await self.state.get_balance(self.s.PAPER_START_BALANCE_USD)
        size = min(self.s.MAX_POSITION_USD, balance)
        if size < 1:
            raise RiskBlocked("الرصيد الورقي غير كافٍ")
        liq = liquidity or item.get("liquidity") or 0
        cost = side_cost(size, liq)
        impact = cost - side_cost(0, liq)
        if impact > self.s.max_slippage:
            raise RiskBlocked(f"الانزلاق المتوقع {impact:.1%} أعلى من الحد")

        fill = price * (1 + cost)
        qty = (size - self.tx_fee_usd) / fill   # network fee is paid out of the position budget
        stop_pct = item.get("stop_pct", 0.1)
        rise = item.get("median_rise", 0.2)
        pos = {
            "mint": item["mint"],
            "symbol": item.get("symbol", "?"),
            "qty": qty,
            "remaining_qty": qty,
            "entry_price": fill,
            "cost_usd": size,
            "stop": fill * (1 - stop_pct),
            "tp1": fill * (1 + rise * (params.tp1_factor if params else self.s.TP1_FACTOR)),
            "tp2": fill * (1 + rise * (params.tp2_factor if params else self.s.TP2_FACTOR)),
            "tp1_hit": False,
            "peak": fill,
            "trail_pct": trail_pct(item.get("median_drop", 0.2), params or StrategyParams()),
            "opened_at": int(time.time()),
            "time_stop_at": int(time.time() + item.get("time_stop_hours", 24) * 3600),
            "liquidity_at_entry": liq,
            "realized_usd": 0.0,
            "expected_pct": item.get("bt_avg"),
            "mode": self.mode,
        }
        await self.state.add_balance(-size)
        await self.state.save_position(item["mint"], pos)
        await self.db.log_trade(pos["mint"], pos["symbol"], "BUY", qty, fill, size, None, "entry", self.mode)
        logger.info(f"[paper] BUY {pos['symbol']} {size:.2f}$ @ {fill:.10g}")
        return pos

    async def sell(self, mint: str, fraction: float, price: float, reason: str,
                   liquidity: float | None = None) -> tuple[dict, float, bool]:
        """Sell `fraction` of the ORIGINAL quantity (capped at what remains).
        Returns (position, pnl_usd_of_this_sale, fully_closed)."""
        pos = await self.state.get_position(mint)
        if not pos:
            raise RiskBlocked("لا يوجد مركز")
        qty = min(pos["remaining_qty"], pos["qty"] * fraction)
        if fraction >= 1:
            qty = pos["remaining_qty"]
        liq = liquidity or pos.get("liquidity_at_entry") or 0
        value = qty * price
        fill = price * (1 - side_cost(value, liq))
        proceeds = qty * fill - self.tx_fee_usd
        basis = pos["cost_usd"] * (qty / pos["qty"])
        pnl = proceeds - basis

        pos["remaining_qty"] -= qty
        pos["realized_usd"] += pnl
        closed = pos["remaining_qty"] <= pos["qty"] * 1e-9
        await self.state.add_balance(proceeds)
        await self.state.add_daily_pnl(pnl)
        if closed:
            await self.state.delete_position(mint)
        else:
            await self.state.save_position(mint, pos)
        await self.db.log_trade(mint, pos["symbol"], "SELL", qty, fill, proceeds, pnl, reason, self.mode)
        logger.info(f"[paper] SELL {pos['symbol']} {qty:.4g} @ {fill:.10g} pnl={pnl:+.2f}$ ({reason})")
        return pos, pnl, closed

    async def record_close(self, pos: dict, reason: str):
        await self.db.save_closed_position(pos, reason)
