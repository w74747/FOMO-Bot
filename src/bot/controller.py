from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from src.config import settings
from src.core.state import StateManager
from src.execution.telethon_client import ExecutionGateway

class TelegramController:
    def __init__(self, state: StateManager, execution: ExecutionGateway):
        self.state = state
        self.execution = execution
        self.app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()

    def _auth(self, update: Update) -> bool:
        return update.effective_user.id == settings.ALLOWED_ADMIN_CHAT_ID

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("status", self.status_cmd))
        self.app.add_handler(CommandHandler("kill_switch", self.kill_cmd))

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return
        trader = await self.state.get_active_trader()
        if not trader:
            await update.message.reply_text("No active trader selected.")
            return

        losses = await self.state.get_consecutive_losses(trader.get("handle", ""))
        text = (
            f"Active Trader: {trader.get('handle')}\n"
            f"Wallet: `{trader.get('wallet_address')}`\n"
            f"Consecutive Losses: {losses}/2\n"
            f"Leaderboard Rank: #{trader.get('rank', 'N/A')}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def kill_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return
        await self.execution.emergency_kill()
        await update.message.reply_text("Kill switch issued to execution layer.")

    async def send_alert(self, text: str):
        await self.app.bot.send_message(chat_id=settings.ALLOWED_ADMIN_CHAT_ID, text=text)
