"""Telegram control panel. Only ALLOWED_ADMIN_CHAT_ID can use it."""
import asyncio
import json
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from html import escape

from loguru import logger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from src.analysis.optimizer import optimize
from src.bot.reasons import ar_reason
from src.bot.report import build_report, strategy_closes
from src.trading.monitor import fmt_price
from src.trading.strategy import PARAM_NAMES, load_params

MAX_LEN = 4000

COMMANDS = [
    ("status", "حالة البوت والرصيد"),
    ("watchlist", "العملات المرشحة ومستويات الدخول"),
    ("positions", "المراكز المفتوحة"),
    ("history", "آخر الصفقات"),
    ("scan", "تشغيل مسح جديد الآن"),
    ("token", "تحليل عملة: /token <عنوان أو رابط>"),
    ("watch", "اقتراح عملة للمراقبة: /watch <عنوان أو رابط>"),
    ("unwatch", "إزالة عملة اقترحتها: /unwatch <عنوان>"),
    ("mylist", "العملات التي اقترحتها"),
    ("report", "تقرير الأداء والتقدم نحو التداول الحقيقي"),
    ("collect", "جمع بيانات تاريخية لعملات إضافية"),
    ("optimize", "اختبار مئات الإعدادات على البيانات المخزنة"),
    ("apply", "تطبيق إعداد من نتائج /optimize: /apply 1"),
    ("params", "الإعدادات الحالية للاستراتيجية"),
    ("resetparams", "العودة للإعدادات الافتراضية"),
    ("wallet", "المحفظة الحقيقية والرصيد"),
    ("live", "تشغيل/إيقاف التداول الحقيقي: /live on | off"),
    ("livetest", "اختبار جولة حقيقية صغيرة (2$)"),
    ("stop", "طوارئ: بيع كل المراكز الحقيقية وإيقاف الحقيقي"),
    ("pause", "إيقاف فتح صفقات جديدة"),
    ("resume", "استئناف فتح الصفقات"),
    ("closeall", "إغلاق كل المراكز"),
    ("help", "قائمة الأوامر"),
]


class TelegramController:
    def __init__(self, token: str, admin_chat_id: int, settings):
        self.admin = admin_chat_id
        self.s = settings
        self.app = Application.builder().token(token).build()
        # set later by main.py
        self.state = self.db = self.scanner = self.monitor = self.live = None
        self._bg: set[asyncio.Task] = set()
        self._busy: set[str] = set()
        self._register()

    def bind(self, state, db, scanner, monitor, live=None):
        self.state, self.db, self.scanner, self.monitor = state, db, scanner, monitor
        self.live = live

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        await self.app.initialize()
        await self.app.bot.delete_webhook(drop_pending_updates=True)
        try:
            await self.app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception as e:
            logger.warning(f"set_my_commands failed: {e}")
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram polling started.")

    async def stop(self):
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as e:
            logger.warning(f"telegram shutdown: {e}")

    async def send(self, text: str):
        for i in range(0, len(text), MAX_LEN):
            chunk = text[i:i + MAX_LEN]
            try:
                await self.app.bot.send_message(self.admin, chunk, parse_mode=ParseMode.HTML,
                                                disable_web_page_preview=True)
            except Exception as e:
                logger.error(f"Telegram send failed: {e}")

    # ------------------------------------------------------------ handlers
    def _register(self):
        admin = filters.Chat(chat_id=self.admin)
        table = {
            "start": self.cmd_start, "help": self.cmd_start, "status": self.cmd_status,
            "watchlist": self.cmd_watchlist, "positions": self.cmd_positions, "history": self.cmd_history,
            "scan": self.cmd_scan, "token": self.cmd_token, "pause": self.cmd_pause,
            "resume": self.cmd_resume, "closeall": self.cmd_closeall, "report": self.cmd_report,
            "collect": self.cmd_collect, "optimize": self.cmd_optimize, "apply": self.cmd_apply,
            "params": self.cmd_params, "resetparams": self.cmd_resetparams,
            "watch": self.cmd_watch, "unwatch": self.cmd_unwatch, "mylist": self.cmd_mylist,
            "wallet": self.cmd_wallet, "live": self.cmd_live, "livetest": self.cmd_livetest, "stop": self.cmd_stop,
        }
        for name, fn in table.items():
            self.app.add_handler(CommandHandler(name, self._logged(name, fn), filters=admin))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_error_handler(self.on_error)

    def _logged(self, name, fn):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            logger.info(f"command /{name} args={context.args}")
            await fn(update, context)
        return wrapper

    async def on_error(self, update, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        if isinstance(err, Exception) and type(err).__name__ in ("Conflict", "NetworkError", "TimedOut"):
            logger.warning(f"telegram transient: {type(err).__name__}")
            return
        logger.opt(exception=err).error(f"handler error: {err!r}")
        try:
            await self.send(f"❌ خطأ داخلي: <code>{escape(type(err).__name__)}: {escape(str(err))[:400]}</code>")
        except Exception:
            pass

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _reply(self, update: Update, text: str, markup=None):
        msg = update.effective_message
        for i in range(0, len(text), MAX_LEN):
            await msg.reply_text(text[i:i + MAX_LEN], parse_mode=ParseMode.HTML,
                                 reply_markup=markup if i == 0 else None, disable_web_page_preview=True)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 الحالة", callback_data="status"),
             InlineKeyboardButton("👀 المراقبة", callback_data="watchlist")],
            [InlineKeyboardButton("📂 المراكز", callback_data="positions"),
             InlineKeyboardButton("🔎 مسح الآن", callback_data="scan")],
        ])
        cmds = "\n".join(f"/{c} — {escape(d)}" for c, d in COMMANDS)
        await self._reply(update, f"🌊 <b>Wave Bot</b> — وضع: <b>{self.s.TRADING_MODE}</b>\n\n{cmds}", kb)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not update.effective_chat or update.effective_chat.id != self.admin:
            await q.answer()
            return
        await q.answer()
        handler = {"status": self.cmd_status, "watchlist": self.cmd_watchlist,
                   "positions": self.cmd_positions, "scan": self.cmd_scan}.get(q.data)
        if handler:
            await handler(update, context)

    async def status_text(self) -> str:
        bal = await self.state.get_balance(self.s.PAPER_START_BALANCE_USD)
        pnl = await self.state.get_daily_pnl()
        positions = await self.state.get_positions()
        watch = await self.state.get_watchlist()
        paused = await self.state.is_paused()
        last_scan = await self.state.get_meta("last_scan")
        ago = f"منذ {(time.time() - int(last_scan)) / 60:.0f} دقيقة" if last_scan else "لم يتم بعد"
        armed = sum(1 for w in watch.values() if w.get("status") == "armed")
        tick_age = time.time() - self.monitor.last_tick if self.monitor and self.monitor.last_tick else None
        rows = await self.db.closed_positions() if self.db else []
        paper = [r for r in strategy_closes(rows) if (r.get('mode') or 'paper') == 'paper']
        manual = sum(1 for r in rows if r not in strategy_closes(rows) and (r.get('mode') or 'paper') == 'paper')
        lines = [
            f"📊 <b>الحالة</b> — وضع <b>{self.s.TRADING_MODE}</b>{' ⏸ متوقف' if paused else ''}",
            f"الرصيد الورقي: <b>{bal:,.2f}$</b> · ربح/خسارة اليوم: <b>{pnl:+.2f}$</b> (الحد −{self.s.DAILY_LOSS_LIMIT_USD:.0f}$)",
            f"المراكز المفتوحة: {len(positions)}/{self.s.MAX_OPEN_POSITIONS} · قائمة المراقبة: {len(watch)} (جاهزة للشراء: {armed})",
            f"آخر مسح: {ago} · مراقب الأسعار: {'يعمل' if tick_age is not None and tick_age < 120 else 'متأخر/لم يبدأ'}",
        ]
        if paper or manual:
            wins = sum(1 for r in paper if (r["pnl_usd"] or 0) > 0)
            net = sum(r["pnl_usd"] or 0 for r in paper)
            lines.append(f"صفقات الاستراتيجية المغلقة: {len(paper)}/{self.s.LIVE_MIN_PAPER_TRADES} · رابحة: {wins} · "
                         f"صافي: {net:+.2f}$" + (f" · (مستبعد يدوي: {manual})" if manual else ""))
        if self.live:
            on = await self.state.live_enabled()
            lp = await self.state.get_positions("live")
            lpnl = await self.state.get_daily_pnl("live")
            lines.append(f"💰 الحقيقي: <b>{'يعمل' if on else 'متوقف'}</b> · مراكز {len(lp)}/{self.s.LIVE_MAX_OPEN_POSITIONS} · "
                         f"اليوم {lpnl:+.2f}$ (الحد −{self.s.LIVE_DAILY_LOSS_USD:.0f}$) · صفقة {self.s.LIVE_POSITION_USD:.0f}$")
        else:
            lines.append("💰 الحقيقي: غير مهيأ (يحتاج WALLET_PRIVATE_KEY وSOLANA_RPC_URL وJUPITER_API_KEY)")
        return "\n".join(lines)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reply(update, await self.status_text())

    async def cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        watch = await self.state.get_watchlist()
        if not watch:
            await self._reply(update, "قائمة المراقبة فارغة. استخدم /scan لتشغيل مسح.")
            return
        icons = {"waiting": "⏳", "armed": "🎯", "holding": "📂"}
        lines = ["👀 <b>قائمة المراقبة</b>"]
        for w in sorted(watch.values(), key=lambda x: -x.get("score", 0)):
            lines.append(
                f"{icons.get(w.get('status'), '•')} <b>{escape(w['symbol'])}</b> تقييم {w.get('score', 0):.0f} · "
                f"هبوط {w['median_drop']:.0%} / صعود {w['median_rise']:.0%}\n"
                f"   قمة {fmt_price(w['ref_high'])} → دخول {fmt_price(w['entry'])} · وقف {fmt_price(w['stop'])} · "
                f"R/R {w.get('reward_risk', 0):.1f} · اختبار {w.get('bt_trades', 0)} صفقات {w.get('bt_return', 0):+.0f}%\n"
                f"   <code>{w['mint']}</code>"
            )
        await self._reply(update, "\n".join(lines))

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        books = [("paper", "📝 ورقي"), ("live", "💰 حقيقي")]
        allpos = [(label, mint, p) for book, label in books for mint, p in (await self.state.get_positions(book)).items()]
        if not allpos:
            await self._reply(update, "لا توجد مراكز مفتوحة.")
            return
        prices = await self.monitor.jupiter.prices(list({m for _, m, _ in allpos}))
        lines = ["📂 <b>المراكز المفتوحة</b>"]
        for label, mint, p in allpos:
            cur = prices.get(mint, {}).get("price")
            unreal = (cur / p["entry_price"] - 1) if cur else None
            hours_left = max(0, (p["time_stop_at"] - time.time()) / 3600)
            now_txt = f" الآن {fmt_price(cur)} ({unreal:+.1%})" if cur else ""
            lines.append(
                f"• {label} <b>{escape(p['symbol'])}</b> {p['cost_usd']:.2f}$ دخول {fmt_price(p['entry_price'])}{now_txt}\n"
                f"   هدف1 {fmt_price(p['tp1'])}{' ✅' if p['tp1_hit'] else ''} · "
                f"{'وقف متحرك' if p['tp1_hit'] else 'وقف'} {fmt_price(p['stop'])} · "
                f"أعلى سعر {fmt_price(p.get('peak', p['entry_price']))} · "
                f"{'' if p['tp1_hit'] else f'متبقٍ {hours_left:.0f} س · '}محقق {p['realized_usd']:+.2f}$")
        await self._reply(update, "\n".join(lines))

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = await self.db.recent_trades(15) if self.db else []
        if not rows:
            await self._reply(update, "لا يوجد سجل صفقات بعد (أو قاعدة البيانات غير متصلة).")
            return
        lines = ["🧾 <b>آخر الصفقات</b>"]
        for r in rows:
            pnl = f" · {r['pnl_usd']:+.2f}$" if r["pnl_usd"] is not None else ""
            lines.append(f"{r['created_at']:%m-%d %H:%M} {r['side']} <b>{escape(r['symbol'] or '?')}</b> "
                         f"{r['usd']:.2f}${pnl} {escape(r['reason'] or '')}")
        await self._reply(update, "\n".join(lines))

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.scanner.running:
            await self._reply(update, "⏳ يوجد مسح قيد التشغيل بالفعل.")
            return
        await self._reply(update, "🔎 بدأ المسح… قد يستغرق بضع دقائق بسبب حدود الـ API المجانية. ستصلك النتيجة هنا.")
        self._spawn(self.scanner.run())

    async def cmd_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self._reply(update, "الاستخدام: <code>/token عنوان_العملة</code> أو الصق رابطها من Birdeye أو DexScreener أو pump.fun")
            return
        await self._reply(update, "⏳ جارٍ التحليل…")
        self._spawn(self._analyze_and_report(" ".join(context.args), add=False))

    async def cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self._reply(update, "الاستخدام: <code>/watch عنوان_العملة</code> أو الصق رابطها.\n"
                                      "يحللها البوت بنفس قواعده؛ إن قُبلت تدخل قائمة المراقبة، ويعيد فحصها في كل مسح.")
            return
        await self._reply(update, "⏳ جارٍ تحليل العملة المقترحة…")
        self._spawn(self._analyze_and_report(" ".join(context.args), add=True))

    async def cmd_unwatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from src.analysis.scanner import extract_address
        mint = extract_address(" ".join(context.args or []))
        if not mint:
            await self._reply(update, "الاستخدام: <code>/unwatch عنوان_العملة</code>")
            return
        await self.state.remove_manual(mint)
        held = mint in await self.state.held_mints()
        if not held:
            await self.state.remove_watch_item(mint)
        await self._reply(update, "🗑 أُزيلت من قائمتك." + (" عليها مركز مفتوح وسيُدار حتى الخروج." if held else ""))

    async def cmd_mylist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        manual = await self.state.get_manual()
        if not manual:
            await self._reply(update, "لم تقترح أي عملة بعد. استخدم <code>/watch عنوان_العملة</code>")
            return
        watch = await self.state.get_watchlist()
        lines = ["⭐ <b>العملات التي اقترحتها</b> (يُعاد فحصها في كل مسح)"]
        for m in manual:
            w = watch.get(m)
            state = f"في المراقبة: <b>{escape(w['symbol'])}</b> تقييم {w.get('score', 0):.0f}" if w else "غير مقبولة حالياً"
            lines.append(f"• {state}\n   <code>{m}</code>")
        await self._reply(update, "\n".join(lines))

    async def _analyze_and_report(self, text: str, add: bool):
        try:
            mint = await self.scanner.resolve_mint(text)
            if not mint:
                await self.send("لم أجد عنوان عملة صالحاً في الرسالة.")
                return
            a, prefilter_reason = await self.scanner.analyze_manual(mint)
        except Exception as e:
            logger.exception("manual analysis failed")
            await self.send(f"❌ فشل التحليل: {escape(str(e))[:300]}")
            return
        if a is None:
            await self.send(f"❌ لم أستطع تحليلها: {escape(ar_reason(prefilter_reason or 'no supported pool'))}\n<code>{mint}</code>")
            return
        if prefilter_reason and prefilter_reason not in a.reasons:
            a.reasons.insert(0, prefilter_reason)
        lines = self._format_analysis(a)
        if add:
            await self.state.add_manual(mint)
            if a.accepted:
                if await self.state.in_cooldown(mint):
                    lines.append("⏳ مقبولة لكنها في فترة تهدئة؛ ستدخل المراقبة تلقائياً بعد انتهائها.")
                else:
                    import time as _t
                    held = await self.state.held_mints()
                    prev = (await self.state.get_watchlist()).get(mint, {})
                    await self.state.set_watch_item(mint, self.scanner.watch_item(a, prev, held, int(_t.time()), manual=True))
                    lines.append("👀 <b>أُضيفت إلى قائمة المراقبة</b>، وسيُعاد فحصها في كل مسح.")
            else:
                lines.append("📌 حُفظت في قائمتك (/mylist): سيعيد البوت فحصها في كل مسح، وتدخل المراقبة إذا أصبحت مناسبة.")
        await self.send("\n".join(lines))

    def _format_analysis(self, a) -> list[str]:
        p, st, pl, bt = a.pool, a.stats, a.plan, a.bt
        lines = [f"🧪 <b>{escape(p.symbol)}</b> — {escape(p.name)}",
                 f"السيولة {p.liquidity_usd:,.0f}$ · حجم 24س {p.volume_24h_usd:,.0f}$ · العمر {p.age_days:.0f} يوم",
                 f"<code>{p.mint}</code>"]
        if st:
            lines.append(
                f"موجات مكتملة: {st.n_cycles} · وسيط الهبوط {st.median_drop:.0%} · وسيط الصعود {st.median_rise:.0%}\n"
                f"نسبة التعافي: {st.recovery_rate:.0%} · مدة الدورة ~{st.avg_cycle_hours:.0f} س · "
                f"الاتجاه {st.trend_pct_day:+.1f}%/يوم · القيعان {st.lows_trend_pct_day:+.1f}%/يوم\n"
                f"التقييم: <b>{a.score:.0f}</b>/100")
        if pl:
            lines.append(f"الخطة: دخول {fmt_price(pl.entry)} · هدف1 {fmt_price(pl.tp1)} · "
                         f"وقف {fmt_price(pl.stop)} · R/R {pl.reward_risk:.1f} · تكلفة الجولة ~{pl.cost_pct:.1%}")
        if bt:
            lines.append(f"الاختبار التاريخي ({bt.test_days:.0f} يوم): {bt.trades} صفقات · فوز {bt.win_rate:.0%} · "
                         f"عائد {bt.total_return_pct:+.1f}% · أسوأ صفقة {bt.worst_trade_pct:+.1f}%")
        if a.top10 is not None:
            lines.append(f"أكبر الملاك: {a.top10:.0%}")
        lines.append("✅ <b>مقبولة</b>" if a.accepted else
                     "❌ <b>مرفوضة</b>: " + escape("، ".join(ar_reason(r) for r in a.reasons)))
        return lines

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.state.set_paused(True)
        await self._reply(update, "⏸ تم إيقاف فتح صفقات جديدة. المراكز المفتوحة ما زالت تُدار (أهداف ووقف).")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.state.set_paused(False)
        await self._reply(update, "▶️ تم الاستئناف.")

    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        n = await self.monitor.close_all()
        await self._reply(update, f"تم إغلاق {n} مركز." if n else "لا توجد مراكز مفتوحة.")

    # ------------------------------------------------------------ research commands
    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reply(update, await build_report(self.db, self.state, self.s))

    def _guard(self, name: str) -> bool:
        if name in self._busy:
            return False
        self._busy.add(name)
        return True

    async def cmd_collect(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.db.ready:
            await self._reply(update, "قاعدة البيانات غير متصلة.")
            return
        if not self._guard("collect"):
            await self._reply(update, "⏳ عملية جمع البيانات تعمل بالفعل.")
            return
        n = int(context.args[0]) if context.args and context.args[0].isdigit() else 60
        await self._reply(update, f"🗄 بدأ جمع البيانات التاريخية لحتى {n} عملة (رابحة وخاسرة)… قد يستغرق دقائق.")

        async def job():
            try:
                pools, candles = await self.scanner.collect(n)
                stats = await self.db.candle_stats()
                await self.send(f"🗄 تم: {pools} عملة جديدة، {candles:,} شمعة. "
                                f"الإجمالي المخزن: {stats.get('pools', 0)} عملة. الآن يمكنك تشغيل /optimize")
            except Exception as e:
                await self.send(f"❌ فشل جمع البيانات: {escape(str(e))}")
            finally:
                self._busy.discard("collect")
        self._spawn(job())

    async def cmd_optimize(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.db.ready:
            await self._reply(update, "قاعدة البيانات غير متصلة.")
            return
        if not self._guard("optimize"):
            await self._reply(update, "⏳ التحسين يعمل بالفعل.")
            return
        await self._reply(update, "🧪 بدأ اختبار الإعدادات على كل البيانات المخزنة… ستصلك النتيجة هنا.")
        self._spawn(self._optimize_job())

    async def _optimize_job(self):
        try:
            pools = await self.db.tracked_pools(min_candles=400)
            if len(pools) < 10:
                await self.send(f"البيانات غير كافية: {len(pools)} عملة فقط لديها 400+ شمعة. "
                                "شغّل /collect أولاً (أو انتظر عدة مسحات).")
                return
            datasets = []
            for p in pools:
                candles = await self.db.load_candles(p["pool"])
                datasets.append((p["pool"], [tuple(c) for c in candles], float(p["liquidity_usd"] or 100_000)))
            params = await load_params(self.state, self.s)
            started = time.time()
            loop = asyncio.get_running_loop()
            fee_usd = await self.state.tx_fee_usd(self.s)
            with ProcessPoolExecutor(max_workers=1) as ex:
                result = await loop.run_in_executor(ex, optimize, datasets, asdict(params), self.s.MAX_POSITION_USD,
                                                    None, fee_usd)
            await self.state.set_meta("last_opt", json.dumps(result))
            await self.send(self._format_opt(result, time.time() - started))
        except Exception as e:
            logger.exception("optimize failed")
            await self.send(f"❌ فشل التحسين: {escape(str(e))}")
        finally:
            self._busy.discard("optimize")

    @staticmethod
    def _fmt_set(st: dict) -> str:
        if not st["trades"]:
            return "لا صفقات"
        return (f"{st['trades']} صفقة/{st['tokens_traded']} عملة · فوز {st['win_rate']:.0%} · "
                f"متوسط {st['avg_pct']:+.2f}% · مجموع حذر {st.get('total_lcb_pct', 0):+.0f}% · أسوأ {st['worst_pct']:+.1f}%")

    def _format_opt(self, r: dict, secs: float) -> str:
        short = {"zigzag_pct": "zz", "entry_drop_factor": "دخول", "tp1_factor": "هدف1",
                 "tp2_factor": "تشديد", "stop_drop_factor": "وقف", "trail_factor": "تتبع"}
        lines = [
            f"🧪 <b>نتيجة التحسين</b> ({secs:.0f} ث) — {r['configs_tested']} إعداد × {r['tokens']} عملة "
            f"(مجموعة A للاختيار: {r['tokens_a']}، مجموعة B للتحقق: {r['tokens_b']})",
            "",
            "<b>الإعدادات الحالية</b>",
            f"A: {self._fmt_set(r['current']['a'])}",
            f"B: {self._fmt_set(r['current']['b'])}",
        ]
        for i, c in enumerate(r["top"], 1):
            ps = " ".join(f"{short[k]}={c['params'][k]}" for k in short)
            lines += ["", f"<b>#{i}</b> {escape(ps)}", f"A: {self._fmt_set(c['a'])}", f"B: {self._fmt_set(c['b'])}"]
        lines.append("")
        lines.append("<i>«مجموع حذر» = متوسط الصفقة المتحفظ × عدد الفرص: الأهم مع حجم صفقة ثابت.</i>")
        if not r["top"]:
            lines.append("لا يوجد إعداد بعدد صفقات كافٍ. اجمع بيانات أكثر بـ /collect.")
        elif r["recommend"]:
            n = r["recommend"]
            lines.append(f"✅ الإعداد #{n} أفضل من الحالي بوضوح (10%+ في الربح الإجمالي الحذر) على المجموعة B "
                         f"التي لم يُختر عليها. يمكنك تطبيقه بـ <code>/apply {n}</code>")
        elif r["current"]["b"]["avg_pct"] > 0:
            lines.append("👍 أبقِ الإعدادات الحالية: لا يوجد إعداد يتفوق عليها بوضوح على المجموعة B.")
        else:
            lines.append("⚠️ لا أنصح بالتطبيق: لم يثبت أي إعداد ربحيته على عدد كافٍ من عملات المجموعة B.")
        return "\n".join(lines)

    async def cmd_apply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw = await self.state.get_meta("last_opt")
        if not raw:
            await self._reply(update, "لا توجد نتائج تحسين. شغّل /optimize أولاً.")
            return
        r = json.loads(raw)
        try:
            idx = int(context.args[0]) if context.args else 1
            chosen = r["top"][idx - 1]["params"]
        except (ValueError, IndexError):
            await self._reply(update, f"اختر رقماً بين 1 و{len(r['top'])}.")
            return
        await self.state.set_param_overrides({k: v for k, v in chosen.items() if k in PARAM_NAMES})
        await self._reply(update, f"✅ طُبّق الإعداد #{idx}. سيستخدمه مراقب الأسعار فوراً والمسح القادم. "
                                  "للتراجع: /resetparams")

    async def cmd_params(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        p = await load_params(self.state, self.s)
        ov = await self.state.get_param_overrides()
        lines = ["⚙️ <b>إعدادات الاستراتيجية</b>"]
        for k, v in asdict(p).items():
            lines.append(f"{k} = {v}{' (مخصّص)' if k in ov else ''}")
        await self._reply(update, "\n".join(lines))

    async def cmd_resetparams(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.state.set_param_overrides(None)
        await self._reply(update, "↩️ عادت الإعدادات إلى القيم الافتراضية.")

    # ------------------------------------------------------------ live trading
    async def _need_live(self, update) -> bool:
        if self.live:
            return True
        await self._reply(update, "التداول الحقيقي غير مهيأ: يحتاج WALLET_PRIVATE_KEY وSOLANA_RPC_URL وJUPITER_API_KEY في Railway.")
        return False

    async def cmd_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._need_live(update):
            return
        w = self.live.wallet
        host = w.rpc_url.split("//")[-1].split("/")[0].split("?")[0]
        lines = ["💼 <b>المحفظة</b>", f"<code>{w.address}</code>",
                 f"(العنوان مشتق من WALLET_PRIVATE_KEY) · RPC: {escape(host)}"]
        try:
            lam = await w.sol_balance()
            sol = lam / 1e9
            logger.info(f"wallet {w.address} balance {lam} lamports")
            lines.append(f"الرصيد: <b>{sol:.5f} SOL</b> ({lam:,} lamports) ≈ {sol * self.live.sol_usd:,.2f}$")
            lines.append(f"المتاح للتداول بعد الاحتياطي ({self.s.LIVE_SOL_RESERVE} SOL): "
                         f"{max(0, sol - self.s.LIVE_SOL_RESERVE):.4f} SOL")
            if lam == 0:
                info = await w.rpc("getAccountInfo", [w.address, {"encoding": "base64"}])
                exists = bool(info and info.get("value"))
                lines.append("⚠️ الرصيد صفر على الشبكة الرئيسية" + (" (الحساب موجود)" if exists else
                             " والحساب غير موجود على السلسلة إطلاقاً: لم يصل إليه أي تحويل. "
                             "تأكد أن هذا هو العنوان الذي شحنته."))
        except Exception as e:
            logger.exception("wallet balance failed")
            lines.append(f"❌ تعذّرت قراءة الرصيد: <code>{escape(type(e).__name__)}: {escape(str(e))[:300]}</code>")
        lines.append(f"التداول الحقيقي: <b>{'يعمل' if await self.state.live_enabled() else 'متوقف'}</b>")
        lines.append(f"🔗 https://solscan.io/account/{w.address}")
        await self._reply(update, "\n".join(lines))

    async def cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._need_live(update):
            return
        arg = (context.args[0].lower() if context.args else "")
        if arg == "off":
            await self.state.set_live_enabled(False)
            await self._reply(update, "⏹ أُوقف فتح صفقات حقيقية جديدة. المراكز الحقيقية المفتوحة تُدار حتى الخروج (أو /stop لبيعها الآن).")
            return
        if arg not in ("on", "force"):
            await self._reply(update, "الاستخدام: <code>/live on</code> أو <code>/live off</code>")
            return
        closed = strategy_closes(await self.db.closed_positions())
        paper = [r for r in closed if (r.get("mode") or "paper") == "paper"]
        pnl = sum(r["pnl_usd"] or 0 for r in paper)
        ok = len(paper) >= self.s.LIVE_MIN_PAPER_TRADES and pnl > 0
        if not ok and arg != "force":
            await self._reply(update, (
                f"🔒 الشرط المتفق عليه لم يتحقق بعد: {len(paper)}/{self.s.LIVE_MIN_PAPER_TRADES} صفقة ورقية مغلقة، "
                f"صافي {pnl:+.2f}$ (يجب أن يكون موجباً).\n"
                "إذا كنت تريد التشغيل رغم ذلك وتقبل المخاطرة: <code>/live force</code>"))
            return
        await self.state.set_live_enabled(True)
        await self._reply(update, (
            f"💰 <b>التداول الحقيقي يعمل</b>{' (تجاوز الشرط بقرارك)' if not ok else ''}\n"
            f"صفقة {self.s.LIVE_POSITION_USD:.0f}$ · حتى {self.s.LIVE_MAX_OPEN_POSITIONS} مراكز · "
            f"حد خسارة يومي {self.s.LIVE_DAILY_LOSS_USD:.0f}$ · احتياطي {self.s.LIVE_SOL_RESERVE} SOL\n"
            "الورقي مستمر بالتوازي للمقارنة. للطوارئ: /stop"))

    async def cmd_livetest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._need_live(update):
            return
        if not self._guard("livetest"):
            await self._reply(update, "⏳ الاختبار يعمل بالفعل.")
            return
        await self._reply(update, "🧪 اختبار حقيقي: شراء USDC بـ 2$ من SOL ثم بيعه بالكامل وإغلاق الحساب… (حوالي دقيقة)")

        async def job():
            try:
                await self.send("✅ <b>نجح الاختبار الحقيقي</b>\n" + escape(await self.live.self_test(2.0)))
            except Exception as e:
                logger.exception("live self-test failed")
                await self.send(f"❌ فشل الاختبار الحقيقي: {escape(str(e))[:500]}")
            finally:
                self._busy.discard("livetest")
        self._spawn(job())

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._need_live(update):
            return
        await self.state.set_live_enabled(False)
        await self._reply(update, "🛑 أُوقف التداول الحقيقي. جارٍ بيع كل المراكز الحقيقية…")
        n = await self.monitor.close_all("إيقاف طوارئ", book="live")
        left = await self.state.get_positions("live")
        await self._reply(update, f"بيع {n} مركز." + (f" ⚠️ بقي {len(left)} لم يُبع، سأعيد المحاولة تلقائياً." if left else ""))
