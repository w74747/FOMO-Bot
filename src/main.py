"""Wave Bot entry point: health server + Telegram + scanner loop + price monitor."""
import asyncio
import signal
import sys
import time

from aiohttp import web
from loguru import logger

from src.analysis.scanner import Scanner
from src.bot.report import build_report
from src.bot.controller import TelegramController
from src.config import settings
from src.core.database import Database
from src.core.state import StateManager
from src.data.geckoterminal import GeckoClient
from src.data.http import Http
from src.data.jupiter import JupiterClient
from src.data.safety import SafetyChecker
from src.trading.broker import PaperBroker
from src.trading.live import LiveBroker
from src.trading.wallet import SolanaWallet
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


async def daily_report_loop(db, state, notify):
    """Send the performance report once a day at DAILY_REPORT_HOUR_UTC."""
    from datetime import datetime, timedelta, timezone
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=settings.DAILY_REPORT_HOUR_UTC, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await notify(await build_report(db, state, settings))
        except Exception as e:
            logger.exception(f"daily report failed: {e}")


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
    jupiter = JupiterClient(http, settings.JUPITER_API_KEY, settings.JUPITER_RPM, settings.JUPITER_SCAN_RESERVE)
    safety = SafetyChecker(http, settings.SOLANA_RPC_URL, settings.BIRDEYE_API_KEY)
    broker = PaperBroker(state, db, settings)
    live = None
    if settings.WALLET_PRIVATE_KEY and settings.SOLANA_RPC_URL and settings.JUPITER_API_KEY:
        try:
            wallet = SolanaWallet(http, settings.SOLANA_RPC_URL, settings.WALLET_PRIVATE_KEY)
            live = LiveBroker(state, db, settings, http, wallet, settings.JUPITER_API_KEY)
            live.limiter = jupiter.limiter
            logger.info(f"Live broker ready for wallet {wallet.address} (enabled={await state.live_enabled()})")
        except Exception as e:
            logger.error(f"Live broker disabled: could not load wallet ({type(e).__name__})")
    scanner = Scanner(gecko, safety, state, db, settings, notify=tg.send, jupiter=jupiter)
    monitor = Monitor(state, jupiter, broker, settings, notify=tg.send, live_broker=live)
    tg.bind(state, db, scanner, monitor, live)

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

    async def realistic_opt_once():
        """One-off: re-pick the strategy settings on the realistic back-test (the old ones
        were chosen on wick-inflated results). Applied only if it wins out-of-sample."""
        await asyncio.sleep(120)
        if not db.ready or await state.get_meta("opt_realistic_v1"):
            return
        if tg._guard("optimize"):
            await tg.send("🧪 إعادة اختيار الإعدادات على الاختبار الواقعي (بلا ذيول + انتظار الارتداد)… "
                          "قد يستغرق ذلك بعض الوقت، والتداول مستمر.")
            await tg._optimize_job(auto_apply=True)
            if '"realistic": true' in (await state.get_meta("last_opt") or ""):
                await state.set_meta("opt_realistic_v1", "done")   # only once it really finished

    async def age_analysis_once():
        await asyncio.sleep(240)            # after the start-up checks, off the scan
        if db.ready and not await state.get_meta("age_v2") and tg._guard("regime"):
            if await tg.regime_job(send=True):
                await state.set_meta("age_v2", "done")

    async def health_once():
        await asyncio.sleep(45)
        await scanner.update_health()

    tasks = [
        asyncio.create_task(health_once(), name="health-once"),
        asyncio.create_task(age_analysis_once(), name="age-analysis-once"),
        asyncio.create_task(realistic_opt_once(), name="realistic-opt-once"),
        asyncio.create_task(scanner_loop(scanner, state), name="scanner"),
        asyncio.create_task(monitor.run(), name="monitor"),
        asyncio.create_task(daily_report_loop(db, state, tg.send), name="daily-report"),
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
