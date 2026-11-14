"""Fast loop: polls Jupiter prices for the watchlist and open positions every few
seconds, arms entries when a coin drops into its buy zone, buys on a confirmed
bounce, and manages targets / stops / time-stops."""
import asyncio
import time
from html import escape
from typing import Awaitable, Callable

from loguru import logger

from src.trading.broker import RiskBlocked
from src.trading.strategy import StrategyParams, levels, load_params


def fmt_price(p: float) -> str:
    return f"{p:.4g}" if p >= 0.01 else f"{p:.3e}"


class Monitor:
    def __init__(self, state, jupiter, broker, settings,
                 notify: Callable[[str], Awaitable[None]]):
        self.state = state
        self.jupiter = jupiter
        self.broker = broker
        self.s = settings
        self.params = StrategyParams.from_settings(settings)
        self.notify = notify
        self.last_tick = 0.0

    async def run(self):
        logger.info("Price monitor started.")
        while True:
            try:
                await self.tick()
            except Exception as e:
                logger.exception(f"monitor tick failed: {e}")
            await asyncio.sleep(self.s.PRICE_POLL_SEC)

    async def tick(self):
        self.params = await load_params(self.state, self.s)
        watch = await self.state.get_watchlist()
        positions = await self.state.get_positions()
        mints = list(set(watch) | set(positions))
        if not mints:
            self.last_tick = time.time()
            return
        prices = await self.jupiter.prices(mints)
        for mint, pos in positions.items():
            info = prices.get(mint)
            if info:
                await self._manage(pos, info, watch.get(mint))
        for mint, item in watch.items():
            if mint in positions:
                continue
            info = prices.get(mint)
            if info:
                await self._watch(item, info)
        self.last_tick = time.time()

    # ------------------------------------------------------------ entries
    async def _watch(self, item: dict, info: dict):
        price, liq = info["price"], info.get("liquidity")
        sym = escape(item.get("symbol", "?"))
        mint = item["mint"]
        changed = False

        if liq and item.get("liquidity") and liq < item["liquidity"] * (1 - self.s.LIQUIDITY_DROP_EXIT_PCT):
            await self.state.remove_watch_item(mint)
            await self.notify(f"⚠️ <b>{sym}</b>: السيولة انخفضت بشدة ({item['liquidity']:,.0f}$ ← {liq:,.0f}$). أُزيلت من المراقبة.")
            return

        # new high -> move the whole plan up
        if price > item["ref_high"]:
            lv = levels(price, item["median_drop"], item["median_rise"], self.params)
            item.update(ref_high=price, entry=lv["entry"], stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"],
                        status="waiting", local_min=None)
            changed = True

        status = item.get("status", "waiting")
        if status == "holding":  # position was closed elsewhere
            status = "waiting"
            item.update(status="waiting", local_min=None)
            changed = True
        if status == "waiting" and price <= item["entry"]:
            item.update(status="armed", local_min=price)
            changed = True
            await self.notify(
                f"🎯 <b>{sym}</b> دخلت منطقة الشراء عند {fmt_price(price)} "
                f"(هبوط {1 - price / item['ref_high']:.0%} من القمة). بانتظار تأكيد الارتداد…")
        elif status == "armed":
            if price < (item.get("local_min") or price):
                item["local_min"] = price
                changed = True
            if price <= item["stop"]:
                await self.state.remove_watch_item(mint)
                await self.state.set_cooldown(mint, self.s.STOP_COOLDOWN_HOURS)
                await self.notify(f"🧊 <b>{sym}</b> كسرت مستوى الوقف قبل الدخول، الموجة غير صالحة. تهدئة {self.s.STOP_COOLDOWN_HOURS:.0f} ساعة.")
                return
            if price >= item["local_min"] * (1 + self.s.BOUNCE_CONFIRM_PCT):
                if price > item["entry"] * 1.05:
                    item.update(status="waiting", local_min=None)  # bounced too far, missed it
                    changed = True
                else:
                    await self._enter(item, price, liq)
                    return
        if changed:
            await self.state.set_watch_item(mint, item)

    async def _enter(self, item: dict, price: float, liq: float | None):
        sym = escape(item.get("symbol", "?"))
        try:
            pos = await self.broker.buy(item, price, liq, self.params)
        except RiskBlocked as e:
            item.update(status="waiting", local_min=None)
            await self.state.set_watch_item(item["mint"], item)
            await self.notify(f"⛔ إشارة شراء <b>{sym}</b> أُلغيت: {escape(str(e))}")
            return
        item.update(status="holding", local_min=None)
        await self.state.set_watch_item(item["mint"], item)
        mode = "📝 ورقي" if pos.get("mode") == "paper" else "💰 حقيقي"
        await self.notify(
            f"🟢 <b>شراء {sym}</b> ({mode})\n"
            f"المبلغ: {pos['cost_usd']:.2f}$ · السعر: {fmt_price(pos['entry_price'])}\n"
            f"الهدف 1: {fmt_price(pos['tp1'])} · الهدف 2: {fmt_price(pos['tp2'])}\n"
            f"الوقف: {fmt_price(pos['stop'])} · وقف زمني بعد {item.get('time_stop_hours', 24):.0f} ساعة")

    # ------------------------------------------------------------ exits
    async def _manage(self, pos: dict, info: dict, item: dict | None):
        price, liq = info["price"], info.get("liquidity")
        mint = pos["mint"]
        now = time.time()

        if liq and pos.get("liquidity_at_entry") and liq < pos["liquidity_at_entry"] * (1 - self.s.LIQUIDITY_DROP_EXIT_PCT):
            await self._exit(mint, 1.0, price, "سحب سيولة", liq, item, cooldown=True)
            return
        if price <= pos["stop"]:
            reason = "وقف خسارة" if not pos["tp1_hit"] else "وقف عند نقطة الدخول"
            await self._exit(mint, 1.0, price, reason, liq, item, cooldown=not pos["tp1_hit"])
            return
        if not pos["tp1_hit"] and price >= pos["tp1"]:
            p, _, closed = await self._exit(mint, 0.5, price, "الهدف 1", liq, item)
            if not closed:
                p["tp1_hit"] = True
                p["stop"] = max(p["stop"], p["entry_price"])  # protect the rest at breakeven
                await self.state.save_position(mint, p)
            return
        if pos["tp1_hit"] and price >= pos["tp2"]:
            await self._exit(mint, 1.0, price, "الهدف 2", liq, item)
            return
        if now >= pos["time_stop_at"]:
            await self._exit(mint, 1.0, price, "وقف زمني", liq, item)

    async def _exit(self, mint, fraction, price, reason, liq, item, cooldown=False):
        pos, pnl, closed = await self.broker.sell(mint, fraction, price, reason, liq)
        if closed:
            await self.broker.record_close(pos, reason)
        sym = escape(pos.get("symbol", "?"))
        icon = "✅" if pnl >= 0 else "🔻"
        msg = f"{icon} <b>بيع {sym}</b> ({escape(reason)}) عند {fmt_price(price)} · ربح/خسارة: {pnl:+.2f}$"
        if closed:
            msg += f"\nأُغلق المركز. الإجمالي: {pos['realized_usd']:+.2f}$"
            if cooldown:
                await self.state.set_cooldown(mint, self.s.STOP_COOLDOWN_HOURS)
            if item:
                # start a fresh cycle from the current price
                lv = levels(price, item["median_drop"], item["median_rise"], self.params)
                item.update(ref_high=price, entry=lv["entry"], stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"],
                            status="waiting", local_min=None)
                await self.state.set_watch_item(mint, item)
        await self.notify(msg)
        return pos, pnl, closed

    async def close_all(self, reason: str = "إغلاق يدوي") -> int:
        positions = await self.state.get_positions()
        if not positions:
            return 0
        prices = await self.jupiter.prices(list(positions))
        n = 0
        for mint, pos in positions.items():
            info = prices.get(mint)
            if not info:
                continue
            watch = await self.state.get_watchlist()
            await self._exit(mint, 1.0, info["price"], reason, info.get("liquidity"), watch.get(mint))
            n += 1
        return n
