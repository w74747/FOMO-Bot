from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from loguru import logger
from src.config import settings
from src.core.state import StateManager

class TelegramController:
    def __init__(self, state: StateManager):
        self.state = state
        self.app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()

    def _register_handlers(self):
        """تسجيل أوامر البوت والأزرار التفاعلية"""
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CallbackQueryHandler(self.handle_buttons))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """الرد على أمر البدء"""
        if update.effective_chat.id != settings.ALLOWED_ADMIN_CHAT_ID:
            return

        keyboard = [
            [InlineKeyboardButton("📊 عرض التاجر الحالي", callback_data="view_current")],
            [InlineKeyboardButton("⚙️ بوت التنفيذ (Dawn)", url="https://t.me/DawnTradeBot")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🚀 **محرك المراقبة السحابي FOMO Engine يعمل بنجاح!**\n\n"
            "النظام يراقب متصدري التداول ويرسل تنبيهات فورية عند أي تغيير.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """التحقق من حالة التاجر النشط"""
        if update.effective_chat.id != settings.ALLOWED_ADMIN_CHAT_ID:
            return

        current = await self.state.get_active_trader()
        if current:
            text = (
                f"👤 **التاجر المتابع حالياً:** `{current.get('handle', 'غير محدد')}`\n"
                f"💰 **الأرباح:** +${current.get('realized_pnl', 0):,.2f}\n"
                f"📈 **نسبة الفوز:** {current.get('win_rate', 0)}%"
            )
        else:
            text = "ℹ️ لا يوجد تاجر مسجل في الذاكرة حالياً."

        await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة النقر على الأزرار"""
        query = update.callback_query
        await query.answer()

        if query.data == "view_current":
            current = await self.state.get_active_trader()
            if current:
                msg = f"التاجر النشط: `{current.get('handle')}`"
            else:
                msg = "لا يوجد تاجر نشط حالياً."
            await query.edit_message_text(text=msg, parse_mode="Markdown")

    async def send_alert(self, text: str):
        """إرسال تنبيه مباشر إلى حساب المشرف"""
        try:
            await self.app.bot.send_message(
                chat_id=settings.ALLOWED_ADMIN_CHAT_ID,
                text=text,
                parse_mode="Markdown"
            )
            logger.info("Alert sent to admin successfully.")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
