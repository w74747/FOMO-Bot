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
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CallbackQueryHandler(self.handle_buttons))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != settings.ALLOWED_ADMIN_CHAT_ID:
            return
        keyboard = [
            [InlineKeyboardButton("📊 عرض التاجر الحالي", callback_data="view_current")],
            [InlineKeyboardButton("⚙️ فتح بوت التنفيذ (Dawn)", url="https://t.me/DawnTradeBot")]
        ]
        await update.message.reply_text(
            "🚀 **نظام المراقبة السحابي FOMO يعمل بنجاح على Railway!**\n\n"
            "سأقوم بمراقبة أداء المتصدرين وتنبيهك فوراً بأي تغييرات أو خسائر متتالية.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != settings.ALLOWED_ADMIN_CHAT_ID:
            return
        current = await self.state.get_active_trader()
        if current:
            text = f"👤 التاجر المتابع حالياً: `{current.get('handle', 'Unknown')}`"
        else:
            text = "ℹ️ لا يوجد تاجر محدد حالياً في الذاكرة السريعة."
        await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.data == "view_current":
            current = await self.state.get_active_trader()
            msg = f"التاجر النشط: {current.get('handle') if current else 'لا يوجد'}"
            await query.edit_message_text(text=msg)

    async def send_alert(self, text: str):
        try:
            await self.app.bot.send_message(
                chat_id=settings.ALLOWED_ADMIN_CHAT_ID,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
