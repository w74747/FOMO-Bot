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


def _ts(r: dict) -> float:
    v = r.get("closed_at")
    if v is None:
        return 0.0
    return v.timestamp() if hasattr(v, "timestamp") else float(v)


def _line(label: str, x: dict) -> str:
    if not x["n"]:
        return f"{label}: لا صفقات"
    return (f"{label}: {x['n']} صفقة · رابحة {x['wins']} ({x['wins'] / x['n']:.0%}) · "
            f"صافي <b>{x['pnl']:+.2f}$</b> · متوسط {x['avg_ret']:+.1f}%")


def _comparison(day_rows: list[dict], every: list[dict], hours: float) -> list[str]:
    """Live first, paper beside it. Paper counts only since the first live trade so both
    books cover the same period (and mostly the same signals)."""
    mode = lambda r: r.get("mode") or "paper"  # noqa: E731
    live_all = [r for r in every if mode(r) == "live"]
    since = min(_ts(r) for r in live_all) if live_all else 0.0
    def same_period(rows):
        return [r for r in rows if mode(r) == "paper" and _ts(r) >= since]
    out = ["💰 <b>الحقيقي</b>",
           _line(f"• آخر {hours:.0f} ساعة", summarize([r for r in day_rows if mode(r) == "live"])),
           _line("• الإجمالي", summarize(live_all)),
           "",
           "📝 <b>الورقي في نفس الفترة</b>",
           _line(f"• آخر {hours:.0f} ساعة", summarize(same_period(day_rows))),
           _line("• منذ بدء الحقيقي", summarize(same_period(every)))]
    lv, pv = summarize(live_all), summarize(same_period(every))
    if lv["n"] and pv["n"]:
        gap = lv["avg_ret"] - pv["avg_ret"]
        verdict = ("✅ التنفيذ الحقيقي قريب من الورقي" if abs(gap) <= 1.5 else
                   "⚠️ الحقيقي أضعف من الورقي: انزلاق ورسوم أعلى من المتوقع" if gap < 0 else
                   "ℹ️ الحقيقي أفضل من الورقي (غالباً توقيت التنفيذ)")
        out.append(f"⚖️ الفرق في متوسط الصفقة: {gap:+.1f} نقطة — {verdict}")
    ent = [r["entry_gap_pct"] for r in live_all if r.get("entry_gap_pct") is not None]
    ext = [r["exit_gap_pct"] for r in live_all if r.get("exit_gap_pct") is not None]
    if ent or ext:
        parts = []
        if ent:
            parts.append(f"الشراء أعلى من القاع بـ {statistics.mean(ent):.1f}% (المثالي 2%)")
        if ext:
            parts.append(f"البيع تحت الوقف بـ {abs(statistics.mean(ext)):.1f}%")
        out.append("⏱ سرعة التنفيذ (الحقيقي): " + " · ".join(parts))
    return out


def _paper_progress(day: dict, tot: dict, dd: float, manual: int) -> list[str]:
    lines = []
    if day["n"]:
        lines.append(
            f"صفقات مغلقة: {day['n']} · رابحة: {day['wins']} · صافي: <b>{day['pnl']:+.2f}$</b> · "
            f"متوسط العائد: {day['avg_ret']:+.1f}% (أفضل {day['best']:+.1f}% / أسوأ {day['worst']:+.1f}%)")
    else:
        lines.append("لم تُغلق صفقات في هذه الفترة.")
    lines.append("")
    lines.append("🧭 <b>التقدم نحو شروط التداول الحقيقي</b>"
                 + (f" <i>(مستبعد: {manual} إغلاق يدوي)</i>" if manual else ""))
    if not tot["n"]:
        lines.append(f"⬜ لا توجد صفقات مغلقة بعد (0/{MIN_TRADES}).")
        return lines
    exp_txt = f" (الاختبار التاريخي توقّع {tot['expected']:+.1f}%)" if tot["expected"] is not None else ""
    c1, c2 = tot["n"] >= MIN_TRADES, tot["pnl"] > 0
    c3, c4 = tot["avg_ret"] >= MIN_EXPECTANCY_PCT, dd <= MAX_DRAWDOWN_PCT
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
    return lines


async def build_report(db, state, settings, hours: float = 24) -> str:
    def main_tier(rows):
        return [r for r in rows if r.get("tier") != "low_liq"]
    raw_day = strategy_closes(await db.closed_positions(since_hours=hours))
    raw_every = await db.closed_positions()
    low_all = [r for r in strategy_closes(raw_every) if r.get("tier") == "low_liq"]
    low_day = [r for r in raw_day if r.get("tier") == "low_liq"]
    day_rows = main_tier(raw_day)
    raw_every = main_tier(raw_every)
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
    if live_rows:
        lines += _comparison(day_rows, every, hours)
        lines.append("")
        lines.append("🧭 <b>الورقي (مرجع صامت)</b>"
                     + (f" <i>(مستبعد: {manual} إغلاق يدوي)</i>" if manual else ""))
        if tot["n"]:
            exp_txt = f" · التاريخي توقّع {tot['expected']:+.1f}%" if tot["expected"] is not None else ""
            lines.append(f"{tot['n']} صفقة · نسبة الفوز {tot['wins'] / tot['n']:.0%} · أكبر تراجع {dd:.1f}%{exp_txt}")
    else:
        lines += _paper_progress(day, tot, dd, manual)
    if low_all:
        lines.append("")
        lines.append("🧪 <b>تجربة السيولة المنخفضة (ورقي فقط، 20 ألف إلى 50 ألف دولار)</b>")
        lines.append(_line(f"• آخر {hours:.0f} ساعة", summarize(low_day)))
        lines.append(_line("• الإجمالي", summarize(low_all)))
        if every:
            ref = summarize([r for r in every if (r.get("mode") or "paper") == "paper"])
            if ref["n"]:
                lines.append(f"• للمقارنة، الورقي على العملات العادية: متوسط {ref['avg_ret']:+.1f}%")
    lines.append("")
    lines.append(
        f"💼 الرصيد الورقي: {balance:,.2f}$ · مراكز مفتوحة: {len(positions)} · قائمة المراقبة: {len(watch)}")
    if candles:
        lines.append(f"🗄 بيانات مخزنة: {candles.get('pools', 0)} عملة · {candles.get('candles', 0):,} شمعة")
    overrides = await state.get_param_overrides()
    if overrides:
        lines.append("⚙️ إعدادات مخصّصة مفعّلة: " + escape(", ".join(f"{k}={v}" for k, v in overrides.items())))
    return "\n".join(lines)
