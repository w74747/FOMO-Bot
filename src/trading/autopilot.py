"""Autopilot for real trading (/live auto).

Real trading is switched ON only when the strategy is in a good phase both in the back-test
(strategy health) and in our own paper trades, and switched OFF as soon as either turns bad.
A hard stop (drawdown since /live auto) needs the user to re-enable. Paper always trades.
Starts are rate-limited (hysteresis); stops never wait."""
import statistics
import time
from datetime import datetime
from typing import Awaitable, Callable

from loguru import logger

from src.bot.report import strategy_closes

P = "fb:"


def _ts(r: dict, key: str = "closed_at") -> float:
    v = r.get(key)
    if v is None:
        return 0.0
    return v.timestamp() if hasattr(v, "timestamp") else float(v)


class Autopilot:
    def __init__(self, state, db, settings, notify: Callable[[str], Awaitable] | None = None):
        self.state, self.db, self.s, self.notify = state, db, settings, notify
        self.since = datetime.fromisoformat(settings.AUTO_PAPER_SINCE).timestamp()

    # ---------------------------------------------------------------- flags
    async def is_auto(self) -> bool:
        return await self.state.r.get(P + "live:auto") == "1"

    async def enable(self):
        await self.state.r.set(P + "live:auto", "1")
        await self.state.r.set(P + "live:auto_since", str(time.time()))
        await self.state.set_live_enabled(False)          # the autopilot decides from here

    async def disable(self):
        await self.state.r.set(P + "live:auto", "0")

    async def _last_switch(self) -> float:
        return float(await self.state.r.get(P + "live:auto_switch") or 0)

    async def _switch(self, on: bool, why: str):
        await self.state.set_live_enabled(on)
        await self.state.r.set(P + "live:auto_switch", str(time.time()))
        logger.info(f"autopilot: live {'ON' if on else 'OFF'} ({why})")
        if self.notify:
            await self.notify(("▶️ <b>بدأ التداول الحقيقي تلقائياً</b>\n" if on else
                               "⏸ <b>أُوقف التداول الحقيقي تلقائياً</b>\n") + why +
                              ("" if on else "\nالمراكز الحقيقية المفتوحة تُدار حتى الخروج. الورقي مستمر."))

    # ---------------------------------------------------------------- inputs
    async def inputs(self) -> dict:
        h = await self.state.get_health()
        fresh = bool(h) and time.time() - h.get("ts", 0) <= self.s.HEALTH_MAX_AGE_H * 3600
        rows = strategy_closes(await self.db.closed_positions()) if getattr(self.db, "ready", True) else []
        paper = [r for r in rows if (r.get("mode") or "paper") == "paper" and r.get("tier") != "low_liq"
                 and _ts(r, "opened_at") >= self.since]
        paper.sort(key=_ts)
        auto_since = float(await self.state.r.get(P + "live:auto_since") or time.time())
        live = sorted((r for r in rows if r.get("mode") == "live" and _ts(r) >= auto_since), key=_ts)
        cum = peak = 0.0
        for r in live:
            cum += r.get("pnl_usd") or 0.0
            peak = max(peak, cum)

        def avg(xs):
            return statistics.mean(xs) if xs else None
        n_start, n_stop = self.s.AUTO_START_PAPER_N, self.s.AUTO_STOP_PAPER_N
        return {
            "health": h.get("value") if fresh else None,
            "paper_n": len(paper),
            "start_avg": avg([r.get("return_pct") or 0 for r in paper[-n_start:]]) if len(paper) >= n_start else None,
            "stop_avg": avg([r.get("return_pct") or 0 for r in paper[-n_stop:]]) if len(paper) >= n_stop else None,
            "live_pnl": cum, "drawdown": peak - cum,
        }

    # ---------------------------------------------------------------- decision
    async def evaluate(self) -> str:
        """Apply the rules once; returns a one-line status (Arabic) for /status."""
        if not await self.is_auto():
            return ""
        x = await self.inputs()
        on = await self.state.live_enabled()
        hv = x["health"]
        hv_txt = f"{hv:+.2f}%" if hv is not None else "غير متوفر"
        st_txt = f"{x['start_avg']:+.2f}%" if x["start_avg"] is not None else f"{x['paper_n']}/{self.s.AUTO_START_PAPER_N} صفقات"

        if x["drawdown"] >= self.s.AUTO_KILL_USD:
            await self.disable()
            if on:
                await self.state.set_live_enabled(False)
            msg = (f"🛑 <b>إيقاف طوارئ</b>: خسر الحقيقي {x['drawdown']:.2f}$ من أعلى نقطة منذ تفعيل الوضع "
                   f"التلقائي (الحد {self.s.AUTO_KILL_USD:.0f}$). لن يعود إلا بأمر منك: /live auto")
            logger.warning(f"autopilot kill: {x}")
            if self.notify:
                await self.notify(msg)
            return msg

        if on:
            why = None
            if hv is None:
                why = "مؤشر صحة الاستراتيجية غير متوفر أو قديم"
            elif hv <= self.s.AUTO_STOP_HEALTH:
                why = f"صحة الاستراتيجية {hv_txt} (حد التوقف {self.s.AUTO_STOP_HEALTH:+.1f}%)"
            elif x["stop_avg"] is not None and x["stop_avg"] < 0:
                why = f"آخر {self.s.AUTO_STOP_PAPER_N} صفقات ورقية متوسطها {x['stop_avg']:+.2f}% (سالب)"
            if why:
                await self._switch(False, why)
                return f"⏸ متوقف تلقائياً: {why}"
            return f"▶️ يعمل · الصحة {hv_txt} · آخر صفقات ورقية {st_txt} · ربح/خسارة الحقيقي منذ التفعيل {x['live_pnl']:+.2f}$"

        wait = self.s.AUTO_MIN_SWITCH_H * 3600 - (time.time() - await self._last_switch())
        missing = []
        if hv is None or hv <= self.s.AUTO_START_HEALTH:
            missing.append(f"الصحة {hv_txt} (يلزم > {self.s.AUTO_START_HEALTH:+.0f}%)")
        if x["start_avg"] is None or x["start_avg"] <= self.s.AUTO_START_PAPER_AVG:
            missing.append(f"آخر {self.s.AUTO_START_PAPER_N} صفقات ورقية {st_txt} (يلزم > {self.s.AUTO_START_PAPER_AVG:+.0f}%)")
        if not missing and wait > 0:
            missing.append(f"مهلة بين التبديلات: {wait / 3600:.1f} ساعة")
        if missing:
            return "⏳ ينتظر: " + " · ".join(missing)
        await self._switch(True, f"الصحة {hv_txt} · آخر {self.s.AUTO_START_PAPER_N} صفقات ورقية {st_txt}")
        return f"▶️ بدأ تلقائياً · الصحة {hv_txt} · آخر صفقات ورقية {st_txt}"
