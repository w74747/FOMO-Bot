from telethon import TelegramClient
from telethon.sessions import StringSession
from loguru import logger
from src.config import settings

class ExecutionGateway:
    def __init__(self):
        self.client = TelegramClient(
            StringSession(settings.TELEGRAM_STRING_SESSION),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH
        )
        self.target = settings.TARGET_BOT_USERNAME

    async def start(self):
        await self.client.start()
        logger.info(f"Telethon engine attached to session. Target: {self.target}")

    async def switch_target(self, address_or_handle: str):
        """إرسال أمر استبدال التاجر للبوت الخارجي"""
        cmd = f"/copy {address_or_handle}"
        await self.client.send_message(self.target, cmd)
        logger.info(f"Sent switch command to {self.target}: {cmd}")

    async def emergency_kill(self):
        """إيقاف كامل وخروج فوري"""
        await self.client.send_message(self.target, "/kill")
        await self.client.send_message(self.target, "/closeall")
        logger.critical(f"EMERGENCY KILL SWITCH DISPATCHED TO {self.target}")
