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
    from src.trading.autopilot import Autopilot
    autopilot = Autopilot(state, db, settings, notify=tg.send) if live else None
    tg.bind(state, db, scanner, monitor, live, autopilot)

    runner = await start_health_server(monitor)
    await tg.start()
    await state.get_balance(settings.PAPER_START_BALANCE_USD)  # initialise paper balance
    if live:
        auto = autopilot and await autopilot.is_auto()
        on = await state.live_enabled()
        live_txt = ("تلقائي — " if auto else "") + ("يعمل" if on else "متوقف")
        n_open = len(await state.get_positions("live"))
        status = f"💰 الحقيقي: <b>{live_txt}</b> · مراكز حقيقية مفتوحة: {n_open}"
    else:
        status = "💰 الحقيقي: غير مُعدّ (لا محفظة) — ورقي فقط"
    await tg.send(f"🔄 <b>أُعيد تشغيل البوت</b> (تحديث أو صيانة)\n{status}\nأرسل /help لقائمة الأوامر.")

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

    async def reconcile_once():
        await asyncio.sleep(150)
        if db.ready and not await state.get_meta("rec_v1") and tg._guard("reconcile"):
            if await tg.reconcile_job(send=True):
                await state.set_meta("rec_v1", "done")

    async def diagnose_once():
        await asyncio.sleep(240)
        if db.ready and not await state.get_meta("diag_v1"):
            if await tg.diagnose_job():
                await state.set_meta("diag_v1", "done")

    async def cooldown_once():
        await asyncio.sleep(120)
        if db.ready and not await state.get_meta("cool_v1"):
            if await tg.cooldown_job():
                await state.set_meta("cool_v1", "done")

    async def gate_once():
        await asyncio.sleep(180)
        if db.ready and not await state.get_meta("gate_v1") and tg._guard("whatif"):
            if await tg.whatif_job(""):
                await state.set_meta("gate_v1", "done")

    async def missed_once():
        await asyncio.sleep(150)
        if db.ready and not await state.get_meta("missed_v2"):
            if await tg.missed_job():
                await state.set_meta("missed_v2", "done")

    async def clear_loss_bans_once():
        """2026-11-24: rests/bans after losses switched off (REST_AFTER_LOSS=False). Lift the ones
        still running (mihao's week, strike bans, 12 h rests); liquidity-collapse bans stay."""
        await asyncio.sleep(20)
        if not db.ready or settings.REST_AFTER_LOSS or await state.get_meta("unban_v1"):
            return
        lifted = []
        for row in await db.closed_positions(since_hours=24 * 8):
            reason = row.get("exit_reason") or ""
            if reason in ("وقف خسارة", "وقف زمني") and await state.r.exists(f"fb:cooldown:{row['mint']}"):
                liq_ban = any(r.get("mint") == row["mint"] and r.get("exit_reason") == "سحب سيولة"
                              for r in await db.closed_positions(since_hours=72))
                if not liq_ban:
                    await state.r.delete(f"fb:cooldown:{row['mint']}")
                    lifted.append(row.get("symbol") or row["mint"][:6])
        lifted = sorted(set(lifted))
        logger.info(f"loss bans lifted: {lifted}")
        await state.set_meta("unban_v1", "done")

    async def selection_once():
        await asyncio.sleep(180)
        if db.ready and not await state.get_meta("sel_v2"):
            if await tg.selection_job():
                await state.set_meta("sel_v2", "done")

    async def exit_once():
        await asyncio.sleep(200)
        if db.ready and not await state.get_meta("exit_v1"):
            if await tg.exit_job():
                await state.set_meta("exit_v1", "done")

    async def health_once():
        await asyncio.sleep(45)
        await scanner.update_health()

    async def price_recorder():
        """Every minute: Jupiter price (what we actually trade at) for watched/held coins and
        the 100 most liquid coins with candle history, to compare with the pool candles."""
        await asyncio.sleep(30)
        universe, refreshed = [], 0.0
        while True:
            try:
                if time.time() - refreshed > 3600 and db.ready:
                    pools = sorted(await db.tracked_pools(min_candles=300),
                                   key=lambda p: -(p.get("liquidity_usd") or 0))
                    universe = list(dict.fromkeys(p["mint"] for p in pools))[:100]
                    refreshed = time.time()
                watch = list((await state.get_watchlist()).keys())
                held = list(await state.held_mints())
                mints = list(dict.fromkeys(held + watch + universe))
                if mints and db.ready:
                    px = await jupiter.prices(mints)
                    await db.save_jup_prices(int(time.time()) // 60 * 60,
                                             {m: v["price"] for m, v in px.items() if v.get("price")})
            except Exception as e:
                logger.warning(f"price recorder: {e}")
            await asyncio.sleep(60 - time.time() % 60)

    async def shadow_loop():
        """Hourly: refresh watched coins' candles and the back-test's verdict/levels."""
        await asyncio.sleep(90)
        while True:
            try:
                await scanner.shadow_refresh()
            except Exception as e:
                logger.exception(f"shadow refresh failed: {e}")
            await asyncio.sleep(settings.SHADOW_EVERY_MIN * 60)

    async def autopilot_loop():
        await asyncio.sleep(60)
        while autopilot:
            try:
                await autopilot.evaluate()
            except Exception as e:
                logger.exception(f"autopilot failed: {e}")
            await asyncio.sleep(600)

    tasks = [
        asyncio.create_task(health_once(), name="health-once"),
        asyncio.create_task(exit_once(), name="exit-once"),
        asyncio.create_task(selection_once(), name="selection-once"),
        asyncio.create_task(clear_loss_bans_once(), name="unban-once"),
        asyncio.create_task(diagnose_once(), name="diagnose-once"),
        asyncio.create_task(missed_once(), name="missed-once"),
        asyncio.create_task(cooldown_once(), name="cooldown-once"),
        asyncio.create_task(gate_once(), name="gate-once"),
        asyncio.create_task(reconcile_once(), name="reconcile-once"),
        asyncio.create_task(autopilot_loop(), name="autopilot"),
        asyncio.create_task(price_recorder(), name="price-recorder"),
        asyncio.create_task(shadow_loop(), name="shadow"),
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
