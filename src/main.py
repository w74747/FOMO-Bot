import asyncio
from aiohttp import web
from loguru import logger
from src.config import settings
from src.core.state import StateManager
from src.core.database import Database
from src.ingestion.fomo_client import FomoClient
from src.decision.engine import DecisionEngine
from src.execution.telethon_client import ExecutionGateway
from src.bot/controller import TelegramController

async def health_check(request):
    # يُستخدم لفحص جاهزية الحاوية في Railway
    return web.Response(text="OK", status=200)

async def app_supervisor(state, fomo, decision, execution, tg_bot):
    logger.info("Core Supervisor task initiated.")
    while True:
        try:
            current_trader = await state.get_active_trader()
            
            # إذا لم يتم تعيين تاجر بعد، اختر المتصدر تلقائياً
            if not current_trader:
                leaders = await fomo.fetch_leaderboard(timeframe="7d", limit=5)
                if leaders:
                    selected = leaders[0]
                    await state.set_active_trader(selected)
                    await execution.switch_target(selected.get("wallet_address") or selected.get("handle"))
                    await tg_bot.send_alert(f"Initiated copy on #{selected.get('rank')} {selected.get('handle')}")
            
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Supervisor iteration fault: {e}")
            await asyncio.sleep(10)

async def main():
    logger.info("Booting FOMO Copy Engine...")
    
    # تهيئة الخدمات
    state = StateManager()
    db = Database()
    await db.connect()
    
    fomo = FomoClient()
    decision = DecisionEngine(state, db)
    execution = ExecutionGateway()
    await execution.start()
    
    tg_bot = TelegramController(state, execution)
    await tg_bot.app.initialize()
    await tg_bot.app.start()
    await tg_bot.app.updater.start_polling()

    # خادم HTTP للصحة على المنفذ المطلوب لـ Railway
    server = web.Application()
    server.router.add_get("/health", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.PORT)
    await site.start()
    logger.info(f"Health server listening on port {settings.PORT}")

    # تشغيل الرقيب الأساسي
    await app_supervisor(state, fomo, decision, execution, tg_bot)

if __name__ == "__main__":
    asyncio.run(main())
