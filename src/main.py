import asyncio
from aiohttp import web
from loguru import logger
from src.config import settings
from src.core.state import StateManager
from src.core.database import Database
from src.ingestion.fomo_client import FomoClient
from src.bot.controller import TelegramController

async def health_check(request):
    return web.Response(text="FOMO Cloud Engine Healthy", status=200)

async def app_supervisor(state, fomo, tg_bot):
    logger.info("Supervisor running 24/7 on Railway...")
    while True:
        try:
            leaders = await fomo.fetch_leaderboard(timeframe="7d", limit=5)
            if leaders:
                top_trader = leaders[0]
                current = await state.get_active_trader()
                
                # إذا ظهر تاجر متصدر جديد أو أول تشغيل
                if not current or current.get("handle") != top_trader.get("handle"):
                    await state.set_active_trader(top_trader)
                    msg = (
                        f"🎯 **تحديث المتصدرين (FOMO Leaderboard):**\n\n"
                        f"👤 التاجر النشط: `{top_trader.get('handle')}`\n"
                        f"💰 الربح المحقق: +${top_trader.get('realized_pnl', 0):,.2f}\n"
                        f"📈 نسبة الفوز: {top_trader.get('win_rate', 0)}%"
                    )
                    await tg_bot.send_alert(msg)

            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Supervisor loop error: {e}")
            await asyncio.sleep(20)

async def main():
    logger.info("Starting Railway Pure Cloud Instance...")
    
    state = StateManager()
    db = Database()
    try:
        await db.connect()
    except Exception as e:
        logger.warning(f"Database connection skipped/failed: {e}")

    fomo = FomoClient()
    tg_bot = TelegramController(state)
    
    # حذف أي Webhook قديم لتفريغ الاتصال في تيليجرام
    try:
        await tg_bot.app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Cleared old Telegram sessions successfully.")
    except Exception as e:
        logger.warning(f"Failed to reset Telegram sessions: {e}")

    # تشغيل خادم فحص الجاهزية (Health Check) لـ Railway
    server = web.Application()
    server.router.add_get("/", health_check)
    server.router.add_get("/health", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.PORT)
    await site.start()
    logger.info(f"Railway Healthcheck bound to port {settings.PORT}")

    # إرسال إشعار تأكيد التشغيل إلى حسابك فوراً
    await tg_bot.send_alert("🟢 **محرك المراقبة والتنبيهات السحابي FOMO Engine يعمل الآن بنجاح على Railway!**")

    # بدء حلقة المراقبة المستمرة
    await app_supervisor(state, fomo, tg_bot)

if __name__ == "__main__":
    asyncio.run(main())
