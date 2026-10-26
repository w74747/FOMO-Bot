from loguru import logger
from src.core.state import StateManager
from src.core.database import Database

class DecisionEngine:
    def __init__(self, state: StateManager, db: Database):
        self.state = state
        self.db = db

    async def register_fill(self, handle: str, token_mint: str, action: str, amount: float, price: float) -> tuple[bool, str]:
        """
        تتبع المراكز بناءً على المتوسط المرجح (Weighted Average) وحساب خسارة المركز عند الإغلاق الكامل.
        تعيد الدالة: (هل يجب طرد التاجر؟, الرسالة التوضيحية)
        """
        position = await self.state.get_position(token_mint) or {
            "trader": handle,
            "total_tokens": 0.0,
            "total_cost": 0.0,
            "realized_revenue": 0.0,
        }

        if action == "BUY":
            position["total_tokens"] += amount
            position["total_cost"] += (amount * price)
            await self.state.update_position(token_mint, position)
            return False, f"Accumulated {amount} tokens at ${price}"

        elif action == "SELL":
            current_tokens = position["total_tokens"]
            if current_tokens <= 0:
                return False, "Sell on empty position ignored."

            revenue = amount * price
            position["realized_revenue"] += revenue
            remaining_tokens = max(0.0, current_tokens - amount)
            position["total_tokens"] = remaining_tokens

            # إغلاق كامل للمركز
            if remaining_tokens == 0.0:
                total_cost = position["total_cost"]
                total_revenue = position["realized_revenue"]
                net_pnl = total_revenue - total_cost
                roi = (net_pnl / total_cost) * 100 if total_cost > 0 else 0.0

                await self.state.delete_position(token_mint)
                await self.db.log_trade(handle, token_mint, net_pnl, roi)

                if net_pnl < 0:
                    consecutive = await self.state.increment_consecutive_losses(handle)
                    logger.warning(f"Trade closed at LOSS. Net: ${net_pnl:.2f}. Consecutive: {consecutive}")
                    if consecutive >= 2:
                        return True, f"Trader kicked: 2 consecutive losses reached (Last trade: {net_pnl:.2f}$)."
                    return False, f"Trade closed at loss (${net_pnl:.2f}). Total losses: {consecutive}/2"
                else:
                    await self.state.reset_consecutive_losses(handle)
                    return False, f"Trade closed in profit (+${net_pnl:.2f}). Loss counter reset."
            else:
                await self.state.update_position(token_mint, position)
                return False, f"Partial sell executed. Remaining: {remaining_tokens} tokens."

        return False, "Unknown action."
