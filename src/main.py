import asyncio
from aiohttp import web
from loguru import logger
from src.config import settings
from src.core.state import StateManager
from src.core.database import Database
from src.ingestion.fomo_client import FomoClient
from src.decision.engine import DecisionEngine
from src.bot.controller import TelegramController

async def health_check(request):
    return web.Response(text="FOMO Cloud Engine Healthy", status=200)

async def app_supervisor(state, fomo, decision, tg_bot):
    logger.info("Supervisor running 24/7 on Railway...")
    while True:
        try:
            leaders = await fomo.fetch_leaderboard(timeframe="7d", limit=5)
            if leaders:
                top_trader = leaders[0]
                current = await state.get_active_trader()
                
                # إشعار أولي أو تحديث عند تغير المركز الأول
                if not current or current.get("handle") != top_trader.get("handle"):
                    await state.set_active_trader(top_trader)
                    msg = (
                        f"🎯 **تحديث المتصدرين (FOMO Leaderboard):**\n"
                        f"التاجر الأول حالياً: `{top_trader.get('handle')}`\n"
                        f"الربح المحقق: +${top_trader.get('realized_pnl', 0):,.2f}\n"
                        f"نسبة الفوز: {top_trader.get('win_rate', 0)}%"
                    )
                    await tg_bot.send_alert(msg)

            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Supervisor error: {e}")
            await asyncio.sleep(15)

async def main():
    logger.info("Starting Railway Pure Cloud Instance...")
    
    state = StateManager()
    db = Database()
    await db.connect()
    
    fomo = FomoClient()
    decision = DecisionEngine(state, db)
    tg_bot = TelegramController(state)
    
    await tg_bot.app.initialize()
    await tg_bot.app.start()
    await tg_bot.app.updater.start_polling()

    # خادم فحص الصحة لمنصة Railway
    server = web.Application()
    server.router.add_get("/health", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.PORT)
    await site.start()
    logger.info(f"Railway Healthcheck bound to port {settings.PORT}")

    await app_supervisor(state, fomo, decision, tg_bot)

if __name__ == "__main__":
    asyncio.run(main())
