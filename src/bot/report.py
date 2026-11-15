"""Daily performance report + progress toward the go-live criteria."""
import statistics
from html import escape

# Closes decided by a human (not by the strategy) are kept in history but excluded from
# every performance metric and from the go-live gate.
MANUAL_REASONS = {"إغلاق يدوي", "إيقاف طوارئ"}


def strategy_closes(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r.get("exit_reason") or "") not in MANUAL_REASONS]


# Go-live criteria agreed before any results were seen.
MIN_TRADES_LIVE = 15   # start small real trading (/live on)
MIN_TRADES = 30        # judge the strategy / scale up
MIN_EXPECTANCY_PCT = 2.0
MAX_DRAWDOWN_PCT = 15.0


def _max_drawdown_pct(pnls: list[float], start_balance: float) -> float:
    equity = peak = start_balance
    worst = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100)
    return worst


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rets = [r["return_pct"] or 0.0 for r in rows]
    pnls = [r["pnl_usd"] or 0.0 for r in rows]
    exp = [r["expected_pct"] for r in rows if r.get("expected_pct") is not None]
    return {
        "n": len(rows),
        "wins": sum(1 for p in pnls if p > 0),
        "pnl": sum(pnls),
        "avg_ret": statistics.mean(rets),
        "best": max(rets),
        "worst": min(rets),
        "expected": statistics.mean(exp) if exp else None,
    }


async def build_report(db, state, settings, hours: float = 24) -> str:
    day_rows = strategy_closes(await db.closed_positions(since_hours=hours))
    raw_every = await db.closed_positions()
    every = strategy_closes(raw_every)
    manual = len(raw_every) - len(every)
    live_rows = [r for r in every if r.get("mode") == "live"]
    day = summarize([r for r in day_rows if (r.get("mode") or "paper") == "paper"])
    all_rows = [r for r in every if (r.get("mode") or "paper") == "paper"]
    tot = summarize(all_rows)
    balance = await state.get_balance(settings.PAPER_START_BALANCE_USD)
    positions = await state.get_positions()
    watch = await state.get_watchlist()
    candles = await db.candle_stats()
    dd = _max_drawdown_pct([r["pnl_usd"] or 0.0 for r in all_rows], settings.PAPER_START_BALANCE_USD)

    lines = [f"📈 <b>التقرير اليومي</b> — آخر {hours:.0f} ساعة"]
    if day["n"]:
        lines.append(
            f"صفقات مغلقة: {day['n']} · رابحة: {day['wins']} · صافي: <b>{day['pnl']:+.2f}$</b> · "
            f"متوسط العائد: {day['avg_ret']:+.1f}% (أفضل {day['best']:+.1f}% / أسوأ {day['worst']:+.1f}%)")
    else:
        lines.append("لم تُغلق صفقات في هذه الفترة.")

    lines.append("")
    lines.append("🧭 <b>التقدم نحو شروط التداول الحقيقي</b>"
                 + (f" <i>(مستبعد: {manual} إغلاق يدوي)</i>" if manual else ""))
    if tot["n"]:
        exp_txt = f" (الاختبار التاريخي توقّع {tot['expected']:+.1f}%)" if tot["expected"] is not None else ""
        c1 = tot["n"] >= MIN_TRADES
        c2 = tot["pnl"] > 0
        c3 = tot["avg_ret"] >= MIN_EXPECTANCY_PCT
        c4 = dd <= MAX_DRAWDOWN_PCT
        mark = lambda ok: "✅" if ok else "⬜"  # noqa: E731
        lines += [
            f"{mark(c1)} عدد الصفقات: {tot['n']} — "
            f"{'✅' if tot['n'] >= MIN_TRADES_LIVE else '⬜'} {MIN_TRADES_LIVE} لبدء الحقيقي الصغير · {MIN_TRADES} للتوسع",
            f"{mark(c2)} صافي الربح: {tot['pnl']:+.2f}$ (نسبة الفوز {tot['wins'] / tot['n']:.0%})",
            f"{mark(c3)} متوسط ربح الصفقة: {tot['avg_ret']:+.2f}% (المطلوب ≥ {MIN_EXPECTANCY_PCT}%){exp_txt}",
            f"{mark(c4)} أكبر تراجع: {dd:.1f}% (الحد {MAX_DRAWDOWN_PCT}%)",
        ]
        if all((c1, c2, c3, c4)):
            lines.append("🎉 <b>تحققت كل الشروط</b> — يمكن الانتقال للتداول الحقيقي الصغير.")
    else:
        lines.append(f"⬜ لا توجد صفقات مغلقة بعد (0/{MIN_TRADES}).")

    if live_rows:
        lv = summarize(live_rows)
        pv = summarize(all_rows)
        lines.append("")
        lines.append(f"💰 <b>الحقيقي</b>: {lv['n']} صفقة · رابحة {lv['wins']} · صافي <b>{lv['pnl']:+.2f}$</b> · "
                     f"متوسط {lv['avg_ret']:+.1f}% (الورقي {pv['avg_ret']:+.1f}% إن وُجد)" if pv["n"] else
                     f"💰 <b>الحقيقي</b>: {lv['n']} صفقة · صافي <b>{lv['pnl']:+.2f}$</b>")
    lines.append("")
    lines.append(
        f"💼 الرصيد الورقي: {balance:,.2f}$ · مراكز مفتوحة: {len(positions)} · قائمة المراقبة: {len(watch)}")
    if candles:
        lines.append(f"🗄 بيانات مخزنة: {candles.get('pools', 0)} عملة · {candles.get('candles', 0):,} شمعة")
    overrides = await state.get_param_overrides()
    if overrides:
        lines.append("⚙️ إعدادات مخصّصة مفعّلة: " + escape(", ".join(f"{k}={v}" for k, v in overrides.items())))
    return "\n".join(lines)
