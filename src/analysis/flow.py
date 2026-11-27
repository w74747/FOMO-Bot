"""Order flow, holders and smart wallets (research, 2026-11-28).

Collected only to test new entry signals on the honest 5-minute model later (e.g. buy the
bounce only when buyers dominate, or when wallets that profit in our coins are buying). Nothing
here changes trading.
- trades: GeckoTerminal pool trades (<= 300 latest) for the watchlist, held coins and the most
  liquid active pools, every FLOW_EVERY_MIN.
- holders: Jupiter holderCount of the same coins every HOLDERS_EVERY_MIN.
- smart wallets: Birdeye top traders (30 d PnL) of up to BIRDEYE_DAILY_COINS of our coins a day.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from html import escape

from loguru import logger

BIG_BUY_USD = 1000.0


def flow_summary(trades: list[dict], now: float, window_sec: float, smart: set[str] | None = None) -> dict:
    smart = smart or set()
    w = [t for t in trades if now - window_sec <= t["ts"] <= now]
    buys = [t for t in w if t["kind"] == "buy"]
    sells = [t for t in w if t["kind"] == "sell"]
    b, s = sum(t["usd"] or 0 for t in buys), sum(t["usd"] or 0 for t in sells)
    return {"buy_usd": b, "sell_usd": s, "net": b - s, "buys": len(buys), "sells": len(sells),
            "buy_share": b / (b + s) if b + s else None,
            "big_buys": sum(1 for t in buys if (t["usd"] or 0) >= BIG_BUY_USD),
            "buyers": len({t["trader"] for t in buys if t["trader"]}),
            "smart_buys": sum(1 for t in buys if t["trader"] in smart),
            "smart_sells": sum(1 for t in sells if t["trader"] in smart)}


def holders_change(series: list[tuple[int, int]], now: float, window_sec: float) -> float | None:
    """Relative change of the holder count over the window (None without data)."""
    past = [n for ts, n in series if ts <= now - window_sec]
    if not past or not series or not past[-1]:
        return None
    return series[-1][1] / past[-1] - 1


def _usd(x: float) -> str:
    return f"{x/1000:.1f}k$" if abs(x) >= 1000 else f"{x:.0f}$"


def format_flow(rows: list[dict], smart_n: int, birdeye_note: str) -> str:
    L = ["📊 <b>ضغط الشراء والبيع</b> (آخر ساعة · آخر 15 دقيقة) — تحليل فقط"]
    if not rows:
        L.append("لا توجد صفقات مسجلة بعد.")
    for r in rows:
        h, q = r["h1"], r["m15"]
        if not h["buys"] + h["sells"]:
            L.append(f"• {escape(r['symbol'])}: لا صفقات في آخر ساعة")
            continue
        share = f"{h['buy_share']*100:.0f}%" if h["buy_share"] is not None else "-"
        line = (f"• <b>{escape(r['symbol'])}</b>: شراء {_usd(h['buy_usd'])} / بيع {_usd(h['sell_usd'])} "
                f"(حصة الشراء {share}) · 15د: صافي {_usd(q['net'])}")
        extra = []
        if h["big_buys"]:
            extra.append(f"مشتريات كبيرة {h['big_buys']}")
        if h["smart_buys"] or h["smart_sells"]:
            extra.append(f"محافظ ذكية: شراء {h['smart_buys']} / بيع {h['smart_sells']}")
        if r.get("holders_24h") is not None:
            extra.append(f"الملاك 24س {r['holders_24h']*100:+.1f}%")
        if extra:
            line += "\n   " + " · ".join(extra)
        L.append(line)
    L.append(f"👛 محافظ ذكية معروفة: {smart_n}. {birdeye_note}")
    return "\n".join(L)


def format_wallets(wallets: list[dict], birdeye_note: str, limit: int = 15) -> str:
    L = ["👛 <b>المحافظ الذكية</b> (ربح إيجابي في عملاتنا خلال 30 يوماً حسب Birdeye، الروبوتات مستبعدة)"]
    if not wallets:
        L.append("لا توجد محافظ بعد. " + birdeye_note)
        return "\n".join(L)
    for w in wallets[:limit]:
        syms = ",".join(sorted(set((w.get("symbols") or "").split(","))))[:60]
        L.append(f"• <code>{w['wallet'][:4]}…{w['wallet'][-4:]}</code> · {w['coins']} عملة · "
                 f"ربح {_usd(w['pnl'] or 0)} · {escape(syms)}")
    L.append(f"المجموع: {len(wallets)} محفظة. {birdeye_note}")
    return "\n".join(L)


class FlowCollector:
    def __init__(self, db, gecko, jupiter, birdeye, state, settings):
        self.db, self.gecko, self.jupiter, self.birdeye = db, gecko, jupiter, birdeye
        self.state, self.s = state, settings
        self.gaps = 0

    def birdeye_note(self) -> str:
        if not self.birdeye.key:
            return "Birdeye غير مفعّل: أضف BIRDEYE_API_KEY في Railway."
        if self.birdeye.denied:
            return f"Birdeye رفض قائمة أفضل المتداولين ({self.birdeye.denied}): الخطة لا تشملها."
        return "Birdeye مفعّل (يتحدث يومياً)."

    async def targets(self) -> list[dict]:
        """[{pool, mint, symbol}] watchlist + held coins first, then the most liquid active pools."""
        watch = await self.state.get_watchlist()
        held = await self.state.held_mints()
        active = await self.db.active_pools(within_hours=6)
        by_mint = {}
        for p in active:
            by_mint.setdefault(p["mint"], p)
        out, seen = [], set()
        for mint, it in watch.items():
            if it.get("pool") and it["pool"] not in seen:
                out.append({"pool": it["pool"], "mint": mint, "symbol": it.get("symbol") or "?"})
                seen.add(it["pool"])
        for mint in held:
            p = by_mint.get(mint)
            if p and p["pool"] not in seen:
                out.append({"pool": p["pool"], "mint": mint, "symbol": p.get("symbol") or "?"})
                seen.add(p["pool"])
        for p in sorted(active, key=lambda p: -(p.get("liquidity_usd") or 0)):
            if p["pool"] not in seen:
                out.append({"pool": p["pool"], "mint": p["mint"], "symbol": p.get("symbol") or "?"})
                seen.add(p["pool"])
        return out[: self.s.FLOW_MAX_POOLS]

    async def collect_trades(self) -> tuple[int, int]:
        pools = await self.targets()
        new = 0
        for p in pools:
            try:
                last = await self.db.last_trade_ts(p["pool"])
                tr = await self.gecko.trades(p["pool"])
                if last and tr and tr[0]["ts"] > last + 60:
                    self.gaps += 1            # more than 300 trades since the last call: a hole
                new += await self.db.save_trades(p["pool"], tr)
            except Exception as e:
                logger.warning(f"trades {p['symbol']}: {e}")
                if "429" in str(e):
                    break                     # GeckoTerminal budget: leave it to the scanner
            await asyncio.sleep(1.0)
        return len(pools), new

    async def collect_holders(self) -> int:
        mints = list(dict.fromkeys(p["mint"] for p in await self.targets()))
        mints += [m for m in await self.state.held_mints() if m not in mints]
        if not mints:
            return 0
        info = await self.jupiter.token_info(mints[:100])
        counts = {t["mint"]: t["holders"] for t in info if t.get("holders")}
        await self.db.save_holders(int(time.time()) // 60 * 60, counts)
        return len(counts)

    async def smart_daily(self) -> int:
        """Birdeye top traders of our coins not checked in the last 7 days."""
        if not self.birdeye.ready:
            return 0
        recent = await self.db.smart_checked(int(time.time() - 7 * 86400))
        watch = await self.state.get_watchlist()
        cands = [(m, it.get("symbol") or "?") for m, it in watch.items()]
        since = datetime.now(timezone.utc) - timedelta(days=3)
        rows = sorted((r for r in await self.db.analyses_since(since) if r.get("plan")),
                      key=lambda r: -(r.get("score") or 0))
        cands += [(r["mint"], r.get("symbol") or "?") for r in rows]
        todo = list(dict.fromkeys(m for m, _ in cands if m not in recent))[: self.s.BIRDEYE_DAILY_COINS]
        syms = dict(cands)
        done = 0
        for mint in todo:
            traders = await self.birdeye.top_traders(mint)
            if self.birdeye.denied:
                break
            await self.db.save_smart(mint, syms.get(mint, "?"), int(time.time()), traders)
            done += 1
        logger.info(f"smart wallets: {done} coins checked on Birdeye ({self.birdeye_note()})")
        return done

    async def flow_report(self, limit: int = 12) -> str:
        now = time.time()
        smart = {w["wallet"] for w in await self.db.smart_wallets()}
        watch = await self.state.get_watchlist()
        rows = []
        for p in (await self.targets())[:limit]:
            if p["mint"] not in watch and len(rows) >= 6:
                continue
            tr = await self.db.load_trades(p["pool"], int(now - 3600))
            hs = await self.db.holders_series(p["mint"], int(now - 2 * 86400))
            rows.append({"symbol": p["symbol"], "h1": flow_summary(tr, now, 3600, smart),
                         "m15": flow_summary(tr, now, 900, smart), "holders_24h": holders_change(hs, now, 86400)})
        return format_flow(rows, len(smart), self.birdeye_note())
