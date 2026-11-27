"""Fast loop: polls Jupiter prices for the watchlist and open positions every few
seconds, arms entries when a coin drops into its buy zone, buys on a confirmed
bounce, and manages targets / stops / time-stops."""
import asyncio
import time
from html import escape
from typing import Awaitable, Callable

from loguru import logger

from src.trading.broker import RiskBlocked
from src.trading.strategy import StrategyParams, levels, load_params, trailing_stop


SOL_MINT = "So11111111111111111111111111111111111111112"


def fmt_price(p: float) -> str:
    return f"{p:.4g}" if p >= 0.01 else f"{p:.3e}"


def stale_level(item: dict, price: float) -> bool:
    """The coin is not in a wave at all: the price is already at/below the stop, or more than
    twice its usual drop below the reference high (an old high from before a collapse, e.g.
    BLOB -73% vs a usual 23%). Don't arm - silently; the back-test skips these bars too."""
    if price <= item.get("stop", 0):
        return True
    ref, drop = item.get("ref_high"), item.get("median_drop")
    return bool(ref and drop and price < ref * (1 - 2 * drop))


class Monitor:
    def __init__(self, state, jupiter, broker, settings,
                 notify: Callable[[str], Awaitable[None]], live_broker=None):
        self.state = state
        self.jupiter = jupiter
        self.broker = broker                      # paper (always on)
        self.brokers = {"paper": broker}
        if live_broker is not None:
            self.brokers["live"] = live_broker    # real money, only when /live on
        self._err_at: dict[str, float] = {}
        self._liq_alarm: dict[str, tuple[float, int]] = {}
        self._liq_unstable_at: dict[str, float] = {}   # mint -> last time a big drop was seen
        self.s = settings
        self.params = StrategyParams.from_settings(settings)
        self.notify = notify
        self.last_tick = 0.0

    async def run(self):
        """Two speeds: every FAST_POLL_SEC for open positions and armed coins (where seconds
        matter), and a full watchlist refresh every PRICE_POLL_SEC."""
        logger.info("Price monitor started.")
        last_full = 0.0
        while True:
            full = time.time() - last_full >= self.s.PRICE_POLL_SEC
            try:
                await self.tick(full=full)
                if full:
                    last_full = time.time()
            except Exception as e:
                logger.exception(f"monitor tick failed: {e}")
            await asyncio.sleep(self.s.FAST_POLL_SEC)

    async def tick(self, full: bool = True):
        self.params = await load_params(self.state, self.s)
        watch = await self.state.get_watchlist()
        books = {name: await self.state.get_positions(name) for name in self.brokers}
        held = set().union(*books.values()) if books else set()
        if not full:  # fast tick: only what is time-critical
            watch = {m: w for m, w in watch.items() if w.get("status") == "armed"}
            if not watch and not held:
                return
        mints = list(set(watch) | held)
        if not mints:
            self.last_tick = time.time()
            return
        prices = await self.jupiter.prices(mints + [SOL_MINT])
        sol = prices.get(SOL_MINT, {}).get("price")
        if sol:
            await self.state.set_meta("sol_usd", str(sol))
            for b in self.brokers.values():
                b.sol_usd = sol
        for name, positions in books.items():
            for mint, pos in positions.items():
                info = prices.get(mint)
                if info:
                    await self._manage(pos, info, watch.get(mint), self.brokers[name])
        for mint, item in watch.items():
            if mint in held:
                continue
            info = prices.get(mint)
            if info:
                await self._watch(item, info)
            elif full and time.time() - self._err_at.get("noprice:" + mint, 0) > 3600:
                self._err_at["noprice:" + mint] = time.time()
                logger.warning(f"no Jupiter price for watchlist coin {item.get('symbol')} ({mint})")
        if full:
            self.last_tick = time.time()

    def _liquidity_collapsed(self, key: str, liq: float | None, base: float | None) -> bool:
        """True only when liquidity stays below the threshold for several readings over
        LIQUIDITY_CONFIRM_SEC. One bad API reading must not trigger an exit; a real LP
        withdrawal persists."""
        if not liq or not base:
            return False
        if liq >= base * (1 - self.s.LIQUIDITY_DROP_EXIT_PCT):
            self._liq_alarm.pop(key, None)
            return False
        self._liq_unstable_at[key.split(":", 1)[-1]] = time.time()
        first, count = self._liq_alarm.get(key, (time.time(), 0))
        count += 1
        self._liq_alarm[key] = (first, count)
        if count == 1:
            logger.warning(f"liquidity drop seen for {key}: {base:,.0f} -> {liq:,.0f} (waiting for confirmation)")
        if count >= self.s.LIQUIDITY_CONFIRM_READS and time.time() - first >= self.s.LIQUIDITY_CONFIRM_SEC:
            self._liq_alarm.pop(key, None)
            logger.warning(f"liquidity collapse confirmed for {key}: {base:,.0f} -> {liq:,.0f}")
            return True
        return False

    # ------------------------------------------------------------ entries
    async def _watch(self, item: dict, info: dict):
        price, liq = info["price"], info.get("liquidity")
        sym = escape(item.get("symbol", "?"))
        mint = item["mint"]
        changed = False

        if await self.state.in_cooldown(mint):
            await self.state.remove_watch_item(mint)
            return

        # Compare like with like: the baseline is the first Jupiter price-API liquidity reading
        # for this coin (the scanner's number comes from a different source).
        if liq and not item.get("liq_baseline"):
            item["liq_baseline"] = liq
            changed = True
        base = item.get("liq_baseline")
        if self._liquidity_collapsed(f"w:{mint}", liq, base):
            await self.state.remove_watch_item(mint)
            await self.state.set_cooldown(mint, self.s.LIQUIDITY_BAN_HOURS)
            await self._notify_item(item, f"⚠️ <b>{sym}</b>: السيولة انخفضت بشدة ({base:,.0f}$ ← {liq:,.0f}$). أُزيلت ومُنعت لمدة {self.s.LIQUIDITY_BAN_HOURS:.0f} ساعة.")
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
        if getattr(self.s, "ENTRY_ON_CLOSE", False):
            # entries come only from the hourly back-test signal (enter_signals), never from
            # intra-hour Jupiter ticks, so live takes exactly the back-test's trades
            if status == "armed":
                item.update(status="waiting", local_min=None)
                changed = True
            if changed:
                await self.state.set_watch_item(mint, item)
            return
        shadow_ok = self._shadow_ok(item)
        if item.get("paper_only") and not getattr(self.s, "PAPER_ONLY_MIN_LIQUIDITY_USD", 0):
            shadow_ok = False             # low-liquidity experiment switched off: never enter
        if not shadow_ok and status == "armed":
            item.update(status="waiting", local_min=None)     # the back-test would not trade it now
            status, changed = "waiting", True
        if status == "waiting" and price <= item["entry"] and shadow_ok and not stale_level(item, price):
            item.update(status="armed", local_min=price)
            changed = True
            await self._notify_item(item, 
                f"🎯 <b>{sym}</b> دخلت منطقة الشراء عند {fmt_price(price)} "
                f"(هبوط {1 - price / item['ref_high']:.0%} من القمة). بانتظار تأكيد الارتداد…")
        elif status == "armed":
            if price < (item.get("local_min") or price):
                item["local_min"] = price
                changed = True
            if price <= item["stop"]:
                if getattr(self.s, "REST_AFTER_LOSS", True):
                    await self.state.remove_watch_item(mint)
                    await self.state.set_cooldown(mint, self.s.STOP_COOLDOWN_HOURS)
                    await self._notify_item(item, f"🧊 <b>{sym}</b> كسرت مستوى الوقف قبل الدخول، الموجة غير صالحة. تهدئة {self.s.STOP_COOLDOWN_HOURS:.0f} ساعة.")
                else:
                    # same as the back-test: the wave is broken, wait for a new high (the hourly
                    # shadow verdict blocks it); no time ban
                    item.update(status="waiting", local_min=None,
                                shadow={"ok": False, "why": "bt_blocked", "ts": time.time()})
                    await self.state.set_watch_item(mint, item)
                    await self._notify_item(item, f"🧊 <b>{sym}</b> كسرت مستوى الوقف قبل الدخول، الموجة غير صالحة. بانتظار موجة جديدة.")
                return
            if price >= item["local_min"] * (1 + self.s.BOUNCE_CONFIRM_PCT):
                if price > item["entry"] * 1.05:
                    item.update(status="waiting", local_min=None)  # bounced too far, missed it
                    changed = True
                elif time.time() - self._liq_unstable_at.get(mint, 0) < self.s.LIQUIDITY_ENTRY_BLOCK_MIN * 60:
                    logger.info(f"entry on {item.get('symbol')} skipped: liquidity was unstable recently")
                else:
                    await self._enter(item, price, liq)
                    return
        if changed:
            await self.state.set_watch_item(mint, item)

    async def enter_signals(self) -> list[str]:
        """ENTRY_ON_CLOSE: right after the hourly shadow refresh, buy every coin whose back-test
        bought at the close of the last candle. Guards: the signal is fresh, the price has not
        run more than ENTRY_CLOSE_MAX_GAP above the back-test's fill, and is above the stop."""
        if not getattr(self.s, "ENTRY_ON_CLOSE", False):
            return []
        watch = await self.state.get_watchlist()
        books = {name: await self.state.get_positions(name) for name in self.brokers}
        held = set().union(*books.values()) if books else set()
        todo = {}
        now = time.time()
        for mint, it in watch.items():
            sig = it.get("bt_signal")
            if not sig or mint in held or it.get("status") == "holding" or it.get("signal_done") == sig["ts"]:
                continue
            if now - (sig["ts"] + 3600) > self.s.ENTRY_CLOSE_MAX_AGE_MIN * 60 or not self._shadow_ok(it):
                continue
            if it.get("paper_only") and not getattr(self.s, "PAPER_ONLY_MIN_LIQUIDITY_USD", 0):
                continue
            todo[mint] = it
        if not todo:
            return []
        prices = await self.jupiter.prices(list(todo))
        done = []
        for mint, it in todo.items():
            it["signal_done"] = it["bt_signal"]["ts"]
            info = prices.get(mint)
            fill = it["bt_signal"]["fill"]
            if not info or not info.get("price"):
                logger.warning(f"signal {it.get('symbol')}: no Jupiter price")
                await self.state.set_watch_item(mint, it)
                continue
            price, liq = info["price"], info.get("liquidity")
            gap = price / fill - 1
            if price <= it["stop"] or gap > self.s.ENTRY_CLOSE_MAX_GAP:
                logger.info(f"signal {it.get('symbol')} skipped: price {price:.4g} vs back-test fill "
                            f"{fill:.4g} ({gap:+.1%}), stop {it['stop']:.4g}")
                await self.state.set_watch_item(mint, it)
                continue
            if self._liquidity_collapsed(f"w:{mint}", liq, it.get("liq_baseline")):
                await self.state.set_watch_item(mint, it)
                continue
            it["local_min"] = fill                 # entry gap = live price vs the back-test's fill
            await self.state.set_watch_item(mint, it)
            logger.info(f"signal {it.get('symbol')}: back-test bought at {fill:.4g}, live {price:.4g} ({gap:+.2%})")
            await self._enter(it, price, liq)
            if it.get("status") == "holding":
                done.append(it.get("symbol") or mint[:6])
        return done

    def _shadow_ok(self, item: dict) -> bool:
        """Only trade what the back-test would trade right now (fresh shadow verdict required)."""
        if not getattr(self.s, "SHADOW_GATE", False):
            return True
        sh = item.get("shadow")
        if not sh or time.time() - sh.get("ts", 0) > self.s.SHADOW_MAX_AGE_MIN * 60:
            return False
        return bool(sh.get("ok"))

    async def _enter(self, item: dict, price: float, liq: float | None):
        sym = escape(item.get("symbol", "?"))
        opened, blocked = [], {}
        for name, broker in self.brokers.items():
            if not await broker.enabled():
                continue
            if item.get("paper_only") and name != "paper":
                continue                      # low-liquidity experiment: never real money
            if name == "live":
                why = await self.state.health_block(self.s)
                if why:
                    blocked[name] = why
                    continue
            try:
                opened.append(await broker.buy(item, price, liq, self.params))
            except RiskBlocked as e:
                blocked[name] = str(e)
            except Exception as e:  # never let a live error stop the paper book
                logger.exception(f"{name} buy failed for {sym}")
                blocked[name] = f"خطأ: {e}"
        if not opened:
            item.update(status="waiting", local_min=None)
            await self.state.set_watch_item(item["mint"], item)
            why = blocked.get("live") if await self._quiet("paper") and "live" in blocked else blocked.get("paper", "")
            await self._notify_item(item, f"⛔ إشارة شراء <b>{sym}</b> أُلغيت: {escape(why or '')}")
            return
        low = item.get("local_min")
        if low:
            # how far above the bounce low we actually bought (ideal = BOUNCE_CONFIRM_PCT);
            # measures what polling speed costs at entry
            gap = (price / low - 1) * 100
            for pos in opened:
                pos["entry_gap_pct"] = gap
                await self.state.save_position(item["mint"], pos, pos.get("mode") or "paper")
            logger.info(f"entry {item.get('symbol')}: {gap:.2f}% above the bounce low")
        item.update(status="holding", local_min=None)
        await self.state.set_watch_item(item["mint"], item)
        for pos in opened:
            if await self._quiet(pos.get("mode") or "paper"):
                logger.info(f"[paper] buy {sym} {pos['cost_usd']:.2f}$ @ {pos['entry_price']}")
                continue
            mode = "📝 ورقي" if pos.get("mode") == "paper" else "💰 <b>حقيقي</b>"
            await self.notify(
                f"🟢 <b>شراء {sym}</b> ({mode})\n"
                f"المبلغ: {pos['cost_usd']:.2f}$ · السعر: {fmt_price(pos['entry_price'])}\n"
                f"الهدف 1 (بيع النصف): {fmt_price(pos['tp1'])} · بعده وقف متحرك {pos.get('trail_pct', 0.08):.0%} "
                f"يُشدَّد إلى النصف فوق {fmt_price(pos['tp2'])}\n"
                f"الوقف: {fmt_price(pos['stop'])} · وقف زمني بعد {item.get('time_stop_hours', 24):.0f} ساعة")
        if "live" in blocked and not item.get("paper_only") and await self.brokers["live"].enabled():
            await self.notify(f"ℹ️ الصفقة الحقيقية على <b>{sym}</b> لم تُفتح: {escape(blocked['live'])}")

    # ------------------------------------------------------------ exits
    async def _manage(self, pos: dict, info: dict, item: dict | None, broker=None):
        """Before target 1: fixed stop + time stop. At target 1: sell half, stop to breakeven.
        After that the rest rides with a trailing stop (no fixed second target), so a weak
        bounce still locks in profit and a strong one is not cut short."""
        broker = broker or self.broker
        book = broker.book
        price, liq = info["price"], info.get("liquidity")
        mint = pos["mint"]
        now = time.time()
        dirty = False

        if liq and not pos.get("liquidity_at_entry"):
            pos["liquidity_at_entry"] = liq      # first same-source reading becomes the baseline
            dirty = True
        base = pos.get("liquidity_at_entry")
        if self._liquidity_collapsed(f"{book}:{mint}", liq, base):
            await self._say(book, f"⚠️ <b>{escape(pos.get('symbol', '?'))}</b>: سحب سيولة مؤكد "
                              f"({base:,.0f}$ ← {liq:,.0f}$ لمدة {self.s.LIQUIDITY_CONFIRM_SEC:.0f}+ ث). خروج فوري.")
            await self._exit(mint, 1.0, price, "سحب سيولة", liq, item, broker, cooldown=True)
            await self.state.set_cooldown(mint, self.s.LIQUIDITY_BAN_HOURS)
            await self.state.remove_watch_item(mint)
            return

        if price > pos.get("peak", 0):
            pos["peak"] = price
            dirty = True
        if pos["tp1_hit"]:
            new_stop = trailing_stop(pos["stop"], pos["entry_price"], pos["peak"], pos["tp2"],
                                     pos.get("trail_pct", 0.08))
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
                dirty = True

        if price <= pos["stop"]:
            # how far below the stop the reading that triggered the exit was (polling delay)
            pos["exit_gap_pct"] = (price / pos["stop"] - 1) * 100
            await self.state.save_position(mint, pos, book)
            if not pos["tp1_hit"]:
                reason, cooldown = "وقف خسارة", True
            elif pos["stop"] > pos["entry_price"] * 1.001:
                reason, cooldown = "وقف متحرك", False
            else:
                reason, cooldown = "وقف عند نقطة الدخول", False
            await self._exit(mint, 1.0, price, reason, liq, item, broker, cooldown=cooldown)
            return
        if not pos["tp1_hit"] and price >= pos["tp1"]:
            await self.state.save_position(mint, pos, book)
            res = await self._exit(mint, 0.5, price, "الهدف 1", liq, item, broker)
            if res is None:
                return
            p, _, closed = res
            if not closed:
                p["tp1_hit"] = True
                p["peak"] = max(p.get("peak", price), price)
                p["stop"] = trailing_stop(max(p["stop"], p["entry_price"]), p["entry_price"], p["peak"],
                                          p["tp2"], p.get("trail_pct", 0.08))
                await self.state.save_position(mint, p, book)
                await self._say(
                    book, f"🔒 <b>{escape(p.get('symbol', '?'))}</b>: الباقي يتبع السعر بوقف متحرك "
                    f"{p.get('trail_pct', 0.08):.0%} تحت أعلى سعر (لا يقل عن سعر الدخول). الوقف الآن {fmt_price(p['stop'])}")
            return
        if not pos["tp1_hit"] and now >= pos["time_stop_at"]:
            # a losing time stop means the wave did not come back: rest the coin like a stop-loss
            # (mihao was re-bought one minute after a losing time stop and then hit its stop)
            losing = price < pos["entry_price"]
            await self._exit(mint, 1.0, price, "وقف زمني", liq, item, broker, cooldown=losing)
            return
        if dirty:
            await self.state.save_position(mint, pos, book)

    async def _exit(self, mint, fraction, price, reason, liq, item, broker=None, cooldown=False):
        if cooldown and reason != "سحب سيولة" and not getattr(self.s, "REST_AFTER_LOSS", True):
            cooldown = False      # no rest after losses: the back-test takes the next wave too
        broker = broker or self.broker
        try:
            pos, pnl, closed = await broker.sell(mint, fraction, price, reason, liq)
        except Exception as e:
            # live sells can fail (slippage, network); keep the position and retry next tick
            logger.exception(f"{broker.book} sell failed for {mint}")
            if time.time() - self._err_at.get(mint, 0) > 300:
                self._err_at[mint] = time.time()
                await self.notify(f"⚠️ فشل البيع ({broker.book}) لـ {escape(mint[:8])}…: {escape(str(e))[:300]}. سأعيد المحاولة.")
            return None
        if closed:
            await broker.record_close(pos, reason)
        sym = escape(pos.get("symbol", "?"))
        icon = "✅" if pnl >= 0 else "🔻"
        tag = "" if broker.book == "paper" else " 💰 <b>حقيقي</b>"
        msg = f"{icon} <b>بيع {sym}</b>{tag} ({escape(reason)}) عند {fmt_price(price)} · ربح/خسارة: {pnl:+.2f}$"
        if closed:
            msg += f"\nأُغلق المركز. الإجمالي: {pos['realized_usd']:+.2f}$"
            still_held = mint in await self.state.held_mints()
            if cooldown:
                hours = self.s.STOP_COOLDOWN_HOURS
                if reason == "وقف خسارة" and broker.book == "paper":
                    counted = None
                    if getattr(broker, "db", None) is not None and hasattr(broker.db, "count_stop_losses"):
                        counted = await broker.db.count_stop_losses(mint, 7)
                    strikes = counted if counted else await self.state.add_strike(mint)
                    if strikes >= self.s.STRIKE_LIMIT:
                        hours = self.s.STRIKE_BAN_HOURS
                        if strikes >= getattr(self.s, "STRIKE_LONG_LIMIT", 99):
                            hours = self.s.STRIKE_LONG_BAN_HOURS
                        msg += (f"\n🚫 وقف الخسارة رقم {strikes} على هذه العملة خلال 7 أيام: "
                                f"موجاتها تنكسر، مُنعت {hours:.0f} ساعة.")
                await self.state.set_cooldown(mint, max(hours, await self._remaining_cooldown_h(mint)))
                if not still_held:
                    await self.state.remove_watch_item(mint)
            elif item and not still_held:
                # start a fresh cycle from the current price
                lv = levels(price, item["median_drop"], item["median_rise"], self.params)
                item.update(ref_high=price, entry=lv["entry"], stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"],
                            status="waiting", local_min=None)
                await self.state.set_watch_item(mint, item)
        if await self._quiet(broker.book):
            logger.info(f"[paper] {msg}")
            if "🚫" in msg and not pos.get("paper_only"):   # strike bans also block live entries
                await self.notify(f"🚫 <b>{sym}</b>: " + msg.split("🚫", 1)[1].strip())
        else:
            await self.notify(msg)
        return pos, pnl, closed

    async def _quiet(self, book: str) -> bool:
        """Paper book goes silent while live trading is on (unless PAPER_NOTIFY)."""
        if book != "paper" or getattr(self.s, "PAPER_NOTIFY", False):
            return False
        live = self.brokers.get("live")
        try:
            return bool(live) and await live.enabled()
        except Exception:
            return False

    async def _notify_item(self, item: dict, text: str):
        """Watchlist messages: coins of the paper-only experiment stay silent like paper trades."""
        if item.get("paper_only"):
            await self._say("paper", "🧪 " + text)
        else:
            await self.notify(text)

    async def _say(self, book: str, text: str):
        if await self._quiet(book):
            logger.info(f"[{book}] {text}")
        else:
            await self.notify(text)

    async def _remaining_cooldown_h(self, mint: str) -> float:
        ttl = await self.state.r.ttl(f"fb:cooldown:{mint}")
        return max(0.0, ttl / 3600) if ttl and ttl > 0 else 0.0

    async def close_all(self, reason: str = "إغلاق يدوي", book: str | None = None) -> int:
        n = 0
        for name, broker in self.brokers.items():
            if book and name != book:
                continue
            positions = await self.state.get_positions(name)
            if not positions:
                continue
            prices = await self.jupiter.prices(list(positions))
            watch = await self.state.get_watchlist()
            for mint in positions:
                price = prices.get(mint, {}).get("price") or positions[mint]["entry_price"]
                if await self._exit(mint, 1.0, price, reason, prices.get(mint, {}).get("liquidity"),
                                    watch.get(mint), broker) is not None:
                    n += 1
        return n
