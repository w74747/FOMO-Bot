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
from src.bot.report import build_report
from src.trading.monitor import fmt_price
from src.trading.strategy import PARAM_NAMES, load_params

MAX_LEN = 4000

COMMANDS = [
    ("status", "حالة البوت والرصيد"),
    ("watchlist", "العملات المرشحة ومستويات الدخول"),
    ("positions", "المراكز المفتوحة"),
    ("history", "آخر الصفقات"),
    ("scan", "تشغيل مسح جديد الآن"),
    ("token", "تحليل عملة: /token <mint>"),
    ("report", "تقرير الأداء والتقدم نحو التداول الحقيقي"),
    ("collect", "جمع بيانات تاريخية لعملات إضافية"),
    ("optimize", "اختبار مئات الإعدادات على البيانات المخزنة"),
    ("apply", "تطبيق إعداد من نتائج /optimize: /apply 1"),
    ("params", "الإعدادات الحالية للاستراتيجية"),
    ("resetparams", "العودة للإعدادات الافتراضية"),
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
        self.state = self.db = self.scanner = self.monitor = None
        self._bg: set[asyncio.Task] = set()
        self._busy: set[str] = set()
        self._register()

    def bind(self, state, db, scanner, monitor):
        self.state, self.db, self.scanner, self.monitor = state, db, scanner, monitor

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
        }
        for name, fn in table.items():
            self.app.add_handler(CommandHandler(name, fn, filters=admin))
        self.app.add_handler(CallbackQueryHandler(self.on_button))

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
        summary = await self.db.pnl_summary() if self.db else {}
        lines = [
            f"📊 <b>الحالة</b> — وضع <b>{self.s.TRADING_MODE}</b>{' ⏸ متوقف' if paused else ''}",
            f"الرصيد الورقي: <b>{bal:,.2f}$</b> · ربح/خسارة اليوم: <b>{pnl:+.2f}$</b> (الحد −{self.s.DAILY_LOSS_LIMIT_USD:.0f}$)",
            f"المراكز المفتوحة: {len(positions)}/{self.s.MAX_OPEN_POSITIONS} · قائمة المراقبة: {len(watch)} (جاهزة للشراء: {armed})",
            f"آخر مسح: {ago} · مراقب الأسعار: {'يعمل' if tick_age is not None and tick_age < 120 else 'متأخر/لم يبدأ'}",
        ]
        if summary.get("closes"):
            lines.append(f"إجمالي الصفقات المغلقة: {summary['closes']} · رابحة: {summary['wins']} · "
                         f"صافي: {float(summary['total']):+.2f}$")
        if self.s.TRADING_MODE == "live":
            lines.append("⚠️ الوضع الحقيقي لم يُفعّل بعد في هذا الإصدار؛ التنفيذ ورقي.")
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
        positions = await self.state.get_positions()
        if not positions:
            await self._reply(update, "لا توجد مراكز مفتوحة.")
            return
        prices = await self.monitor.jupiter.prices(list(positions))
        lines = ["📂 <b>المراكز المفتوحة</b>"]
        for mint, p in positions.items():
            cur = prices.get(mint, {}).get("price")
            unreal = (cur / p["entry_price"] - 1) if cur else None
            hours_left = max(0, (p["time_stop_at"] - time.time()) / 3600)
            now_txt = f" الآن {fmt_price(cur)} ({unreal:+.1%})" if cur else ""
            lines.append(
                f"• <b>{escape(p['symbol'])}</b> {p['cost_usd']:.2f}$ دخول {fmt_price(p['entry_price'])}{now_txt}\n"
                f"   هدف1 {fmt_price(p['tp1'])}{' ✅' if p['tp1_hit'] else ''} · هدف2 {fmt_price(p['tp2'])} · "
                          f"وقف {fmt_price(p['stop'])} · متبقٍ {hours_left:.0f} س · محقق {p['realized_usd']:+.2f}$")
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
            await self._reply(update, "الاستخدام: <code>/token MINT_ADDRESS</code>")
            return
        mint = context.args[0].strip()
        await self._reply(update, "⏳ جارٍ التحليل…")
        self._spawn(self._token_report(mint))

    async def _token_report(self, mint: str):
        try:
            a = await self.scanner.analyze_mint(mint)
        except Exception as e:
            await self.send(f"❌ فشل التحليل: {escape(str(e))}")
            return
        if a is None:
            await self.send("لم أجد مجمع سيولة مدعوم لهذه العملة على GeckoTerminal.")
            return
        p, st, pl, bt = a.pool, a.stats, a.plan, a.bt
        lines = [f"🧪 <b>{escape(p.symbol)}</b> — {escape(p.name)}",
                 f"السيولة {p.liquidity_usd:,.0f}$ · حجم 24س {p.volume_24h_usd:,.0f}$ · العمر {p.age_days:.0f} يوم"]
        if st:
            lines.append(
                f"موجات مكتملة: {st.n_cycles} · وسيط الهبوط {st.median_drop:.0%} · وسيط الصعود {st.median_rise:.0%}\n"
                f"نسبة التعافي: {st.recovery_rate:.0%} · مدة الدورة ~{st.avg_cycle_hours:.0f} س · "
                f"الاتجاه {st.trend_pct_day:+.1f}%/يوم · القيعان {st.lows_trend_pct_day:+.1f}%/يوم\n"
                f"التقييم: <b>{a.score:.0f}</b>/100")
        if pl:
            lines.append(f"الخطة: دخول {fmt_price(pl.entry)} · هدف1 {fmt_price(pl.tp1)} · هدف2 {fmt_price(pl.tp2)} · "
                         f"وقف {fmt_price(pl.stop)} · R/R {pl.reward_risk:.1f} · تكلفة ذهاب وإياب ~{pl.cost_pct:.1%}")
        if bt:
            lines.append(f"الاختبار التاريخي ({bt.test_days:.0f} يوم): {bt.trades} صفقات · فوز {bt.win_rate:.0%} · "
                         f"عائد {bt.total_return_pct:+.1f}% · أسوأ صفقة {bt.worst_trade_pct:+.1f}%")
        if a.top10 is not None:
            lines.append(f"أكبر 10 ملاك: {a.top10:.0%}")
        lines.append("✅ <b>مقبولة</b>" if a.accepted else "❌ <b>مرفوضة</b>: " + escape("، ".join(a.reasons)))
        await self.send("\n".join(lines))

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
            with ProcessPoolExecutor(max_workers=1) as ex:
                result = await loop.run_in_executor(ex, optimize, datasets, asdict(params), self.s.MAX_POSITION_USD)
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
                f"متوسط {st['avg_pct']:+.2f}% · أسوأ {st['worst_pct']:+.1f}%")

    def _format_opt(self, r: dict, secs: float) -> str:
        short = {"zigzag_pct": "zz", "entry_drop_factor": "دخول", "tp1_factor": "هدف1",
                 "tp2_factor": "هدف2", "stop_drop_factor": "وقف"}
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
        if not r["top"]:
            lines.append("لا يوجد إعداد بعدد صفقات كافٍ. اجمع بيانات أكثر بـ /collect.")
        elif r["recommend"]:
            n = r["recommend"]
            lines.append(f"✅ الإعداد #{n} ربح أيضاً على المجموعة B (15+ صفقة على 3+ عملات لم يُختر عليها). "
                         f"يمكنك تطبيقه بـ <code>/apply {n}</code>")
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
