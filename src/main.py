"""Wave Bot entry point: health server + Telegram + scanner loop + price monitor."""
import asyncio
import signal
import sys
import time

from aiohttp import web
from loguru import logger

from src.analysis.scanner import Scanner
from src.bot.controller import TelegramController
from src.config import settings
from src.core.database import Database
from src.core.state import StateManager
from src.data.geckoterminal import GeckoClient
from src.data.http import Http
from src.data.jupiter import JupiterClient
from src.data.safety import SafetyChecker
from src.trading.broker import PaperBroker
from src.trading.monitor import Monitor

logger.remove()
logger.add(sys.stdout, level=settings.LOG_LEVEL.upper())


async def scanner_loop(scanner: Scanner, state: StateManager):
    """Run a scan on start-up (unless one ran recently), then every SCAN_INTERVAL_MIN."""
    interval = settings.SCAN_INTERVAL_MIN * 60
    while True:
        last = await state.get_meta("last_scan")
        since = time.time() - int(last) if last else interval
        if since >= interval:
            try:
                await scanner.run()
            except Exception as e:
                logger.exception(f"scan failed: {e}")
            await asyncio.sleep(interval)
        else:
            await asyncio.sleep(interval - since)


async def start_health_server(monitor: Monitor) -> web.AppRunner:
    async def health(_request):
        return web.json_response({"ok": True, "mode": settings.TRADING_MODE,
                                  "last_tick_age": round(time.time() - monitor.last_tick) if monitor.last_tick else None})

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.PORT).start()
    logger.info(f"Health server on port {settings.PORT}")
    return runner


async def main():
    logger.info(f"Starting Wave Bot (mode={settings.TRADING_MODE})")
    if settings.TRADING_MODE == "live":
        logger.warning("TRADING_MODE=live requested, but live execution is not implemented yet: running PAPER.")

    http = Http()
    state = StateManager(settings.REDIS_URL)
    db = Database(settings.DATABASE_URL)
    await db.connect()

    tg = TelegramController(settings.TELEGRAM_BOT_TOKEN, settings.ALLOWED_ADMIN_CHAT_ID, settings)
    gecko = GeckoClient(http, settings.GECKO_CALLS_PER_MIN, settings.COINGECKO_DEMO_API_KEY)
    jupiter = JupiterClient(http, settings.JUPITER_API_KEY)
    safety = SafetyChecker(http, settings.SOLANA_RPC_URL, settings.BIRDEYE_API_KEY)
    broker = PaperBroker(state, db, settings)
    scanner = Scanner(gecko, safety, state, db, settings, notify=tg.send)
    monitor = Monitor(state, jupiter, broker, settings, notify=tg.send)
    tg.bind(state, db, scanner, monitor)

    runner = await start_health_server(monitor)
    await tg.start()
    await state.get_balance(settings.PAPER_START_BALANCE_USD)  # initialise paper balance
    await tg.send(
        f"🟢 <b>Wave Bot يعمل</b> — وضع <b>{settings.TRADING_MODE}</b>"
        + (" (التنفيذ الحقيقي غير مفعّل بعد، يعمل ورقياً)" if settings.TRADING_MODE == "live" else "")
        + "\nأرسل /help لقائمة الأوامر."
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(scanner_loop(scanner, state), name="scanner"),
        asyncio.create_task(monitor.run(), name="monitor"),
    ]
    await stop.wait()
    logger.info("Shutting down…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await tg.stop()
    await runner.cleanup()
    await http.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
