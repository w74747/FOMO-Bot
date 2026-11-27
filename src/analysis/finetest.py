"""Back-test on 5-minute candles (research, 2026-11-27).

The hourly realistic back-test only buys bounces that held until the hourly close (it sees the
close in advance), and the honest hourly-close version loses. Live polls every 2 s, so the edge
it can capture lies between the two. This test keeps the strategy's brain on hourly candles
(wave stats, score gate, levels, refit every 24 h, walk-forward) and runs entries and exits
on 5-minute candles, the way live sees the price:

- "close": decide only at each 5-minute close (no lookahead; at most 5 min late vs live).
- "intra": the current bounce model inside each 5-minute candle (lookahead limited to 5 min).
If both agree, 5-minute resolution is fine enough and that number is the strategy's real edge.
"""
import bisect
import statistics
from dataclasses import fields

from src.analysis.backtest import _bounce_entry, _close_entry, backtest
from src.analysis.optimizer import split_of
from src.analysis.waves import clean_candles, score, wave_stats
from src.data.models import Candle
from src.trading.strategy import (StrategyParams, build_plan, levels, side_cost, time_stop_hours, trail_pct,
                                  trailing_stop)

WARMUP_H = 240
REFIT_H = 24


def backtest_fine(hourly: list[Candle], fine: list[Candle], params: StrategyParams, liquidity_usd: float,
                  position_usd: float, tx_fee_usd: float = 0.0, mode: str = "close", bounce: float = 0.02,
                  slip: float = 0.007, detail: list | None = None, stats_cache: dict | None = None) -> list[float]:
    """Returns the list of trade returns (fractions). `detail` gets one dict per trade."""
    if len(hourly) < WARMUP_H + 24 or not fine:
        return []
    cost_side = side_cost(position_usd, liquidity_usd)
    fee = tx_fee_usd / max(position_usd, 1e-9)
    closes = [c.ts + 3600 for c in hourly]          # when each hourly candle is complete
    returns: list[float] = []
    stats = None
    fit_at = None
    tradeable = False
    ref_high = None
    blocked = False
    armed_low = None
    pos = None

    for bar in fine:
        h = bisect.bisect_right(closes, bar.ts)      # hourly candles closed before this bar starts
        if h < WARMUP_H:
            continue
        if fit_at is None or h - fit_at >= REFIT_H:
            fit_at = h
            key = (h, params.zigzag_pct)
            if stats_cache is not None and key in stats_cache:
                stats = stats_cache[key]
            else:
                stats = wave_stats(hourly[:h], params.zigzag_pct)
                if stats_cache is not None:
                    stats_cache[key] = stats
            gate = score(stats, liquidity_usd, params.max_cycle_hours)[1] if stats else ["no stats"]
            tradeable = bool(stats) and not gate and build_plan(
                stats.last_high, stats.median_drop, stats.median_rise, stats.avg_cycle_hours,
                liquidity_usd, position_usd, params, tx_fee_usd) is not None
            if stats and pos is None:
                ref_high, blocked, armed_low = stats.last_high, False, None
        if not stats or stats.n_cycles < 3 or stats.median_drop <= 0 or stats.median_rise <= 0:
            continue
        fb = Candle(bar.ts, bar.o, max(bar.o, bar.c), min(bar.o, bar.c), bar.c, bar.v)   # body

        if pos is None:
            if ref_high is None:
                ref_high = stats.last_high
            if bar.h > ref_high:
                ref_high, blocked, armed_low = bar.h, False, None    # live: new high -> waiting
            if blocked or not tradeable:
                continue
            lv = levels(ref_high, stats.median_drop, stats.median_rise, params)
            same_bar_low = None
            if mode == "close":
                if armed_low is None and (bar.c <= lv["stop"] or bar.c < ref_high * (1 - 2 * stats.median_drop)):
                    continue
                fill, armed_low = _close_entry(bar, lv, bounce, slip, armed_low)
                if fill == "cancel":
                    blocked = True
                    continue
                if fill is None or fill == "missed":
                    continue
            else:
                was_armed = armed_low is not None
                if not was_armed and (bar.o <= lv["stop"] or bar.o < ref_high * (1 - 2 * stats.median_drop)):
                    continue
                fill, same_bar_low = _bounce_entry(fb, lv, bounce, slip, armed_low)
                if fill != "cancel" and (was_armed or fill is not None or same_bar_low is not None) \
                        and bar.l <= lv["stop"]:
                    if fill is None:
                        fill = "cancel"
                    else:
                        same_bar_low = bar.l
                if fill == "cancel":
                    armed_low, blocked = None, True
                    continue
                if fill is None:
                    armed_low = same_bar_low
                    continue
                armed_low = None
            scale = fill / lv["entry"]
            pos = {"entry": fill, "stop": lv["stop"] * scale, "tp1": lv["tp1"] * scale, "tp2": lv["tp2"] * scale,
                   "half": False, "realized": -fee, "peak": fill, "trail": trail_pct(stats.median_drop, params),
                   "opened": bar.ts,
                   "time_stop_at": bar.ts + 3600 * time_stop_hours(stats.avg_cycle_hours, params.max_cycle_hours,
                                                                   params.time_stop_factor)}
            if same_bar_low is not None and same_bar_low <= pos["stop"]:
                returns.append(-fee + (pos["stop"] - fill) / fill - 2 * cost_side - fee)
                if detail is not None:
                    detail.append({"entry_ts": bar.ts, "exit_ts": bar.ts, "fill": fill, "ret": returns[-1],
                                   "reason": "stop (same bar)"})
                pos, ref_high, blocked = None, bar.h, False
            continue

        # manage the open position (stop before this bar's high: conservative)
        e = pos["entry"]
        closed, reason = False, None
        if bar.l <= pos["stop"]:
            reason = "trailing" if pos["half"] else "stop"
            px = min(pos["stop"], bar.o)
            part = 0.5 if pos["half"] else 1.0
            pos["realized"] += part * ((px - e) / e - 2 * cost_side) - fee
            closed = True
        elif not pos["half"]:
            if fb.h >= pos["tp1"]:
                pos["realized"] += 0.5 * ((pos["tp1"] - e) / e - 2 * cost_side) - fee
                pos["half"] = True
                pos["peak"] = pos["tp1"]
                pos["stop"] = trailing_stop(max(pos["stop"], e), e, pos["peak"], pos["tp2"], pos["trail"])
            elif bar.ts + 300 >= pos["time_stop_at"]:
                pos["realized"] += (bar.c - e) / e - 2 * cost_side - fee
                closed, reason = True, "time"
        else:
            pos["peak"] = max(pos["peak"], fb.h)
            pos["stop"] = trailing_stop(pos["stop"], e, pos["peak"], pos["tp2"], pos["trail"])
        if closed:
            returns.append(pos["realized"])
            if detail is not None:
                detail.append({"entry_ts": pos["opened"], "exit_ts": bar.ts, "fill": e, "ret": pos["realized"],
                               "reason": reason + ("+tp1" if pos["half"] and reason != "trailing" else "")})
            pos, ref_high, blocked = None, bar.h, False
    return returns


def _stats(rets: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"n": 0}
    return {"n": n, "win": sum(1 for r in rets if r > 0) / n * 100, "avg": statistics.mean(rets),
            "total": sum(rets), "median": statistics.median(rets)}


FINE_MODELS = [
    ("ساعة، داخل الشمعة (الاختبار الحالي)", "h_intra"),
    ("ساعة، عند الإغلاق", "h_close"),
    ("5 دقائق، داخل الشمعة", "f_intra"),
    ("5 دقائق، عند الإغلاق (الأقرب للبوت)", "f_close"),
]


def fine_models(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float, bt_kw: dict,
                min_days: float = 2.0) -> dict:
    """datasets: [(pool, hourly tuples, fine tuples, liquidity)]. All four models on the SAME
    period per pool (the span of its 5-minute data); trades counted by entry time in it."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    bounce = (bt_kw or {}).get("bounce_pct", 0.02)
    slip = (bt_kw or {}).get("entry_slip", 0.007)
    base_kw = {k: v for k, v in (bt_kw or {}).items() if k != "close_entry"}
    per = {key: {"A": [], "B": []} for _, key in FINE_MODELS}
    pools = 0
    days = 0.0
    for pool, hraw, fraw, liq in datasets:
        hourly = clean_candles([Candle(*c) for c in hraw])
        fine = sorted((Candle(*c) for c in fraw), key=lambda c: c.ts)
        if not fine or len(hourly) < WARMUP_H + 24:
            continue
        start, end = fine[0].ts, fine[-1].ts
        if (end - start) / 86400 < min_days:
            continue
        pools += 1
        days += (end - start) / 86400
        side = split_of(pool)
        for key, kw in (("h_intra", base_kw), ("h_close", {**base_kw, "close_entry": True})):
            det: list = []
            backtest(hourly, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, trade_detail=det, **kw)
            per[key][side] += [d["ret"] * 100 for d in det if start <= d["entry_ts"] <= end]
        for key, mode in (("f_intra", "intra"), ("f_close", "close")):
            # hourly history up to the end of the 5-minute data only (no future hours)
            h_upto = [c for c in hourly if c.ts < end]
            rets = backtest_fine(h_upto, fine, p, liq, position_usd, fee_usd, mode=mode, bounce=bounce, slip=slip)
            per[key][side] += [r * 100 for r in rets]
    rows = []
    for name, key in FINE_MODELS:
        a, b = per[key]["A"], per[key]["B"]
        rows.append({"name": name, "key": key, "all": _stats(a + b), "a": _stats(a), "b": _stats(b)})
    return {"pools": pools, "days": days / pools if pools else 0.0, "rows": rows}


def format_fine_models(res: dict) -> str:
    L = [f"🔬 <b>اختبار على شموع 5 دقائق</b> ({res['pools']} مجمع، متوسط {res['days']:.1f} يوم لكل مجمع، "
         "نفس الفترة لكل النماذج، A/B حسب المجمع)"]
    if not res["pools"]:
        return L[0] + "\nلا توجد بيانات 5 دقائق كافية بعد."
    for r in res["rows"]:
        a = r["all"]
        if not a["n"]:
            L.append(f"<b>{r['name']}</b>: لا صفقات")
            continue
        L.append(f"<b>{r['name']}</b>\n• {a['n']} صفقة · فوز {a['win']:.0f}% · متوسط {a['avg']:+.2f}% · "
                 f"وسيط {a['median']:+.1f}% · مجموع {a['total']:+.0f}%\n"
                 f"• A: {r['a']['n']} صفقة · متوسط {r['a'].get('avg', 0):+.2f}% · "
                 f"B: {r['b']['n']} صفقة · متوسط {r['b'].get('avg', 0):+.2f}%")
    L.append("ℹ️ «5 دقائق، عند الإغلاق» هو الأقرب لما يستطيع البوت فعله (يرى السعر كل ثانيتين). "
             "تحليل فقط: قواعد التداول لم تتغير.")
    return "\n".join(L)


# ---------------------------------------------------------------- parameter search (research)
GRID = {
    "bounce": (0.01, 0.02, 0.03, 0.05),
    "entry_drop_factor": (0.8, 1.0, 1.2),
    "stop_drop_factor": (0.5, 0.7, 1.0),
    "tp1_factor": (0.5, 0.7, 1.0),
    "trail_factor": (0.3, 0.4, 0.6),
}


def fine_grid(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float, bt_kw: dict,
              min_days: float = 2.0, grid: dict | None = None, min_n: int = 25) -> dict:
    """Every combination of GRID on the honest 5-minute-close model, A/B by pool hash.
    Ranked by the WORSE of the two halves' average (robust to one lucky half); a combination
    needs >= min_n trades in each half to be ranked (fewer = noise)."""
    import itertools
    from dataclasses import replace
    grid = grid or GRID
    known = {f.name for f in fields(StrategyParams)}
    base = StrategyParams(**{k: v for k, v in params.items() if k in known})
    slip = (bt_kw or {}).get("entry_slip", 0.007)
    cur_bounce = (bt_kw or {}).get("bounce_pct", 0.02)
    series = []
    for pool, hraw, fraw, liq in datasets:
        hourly = clean_candles([Candle(*c) for c in hraw])
        fine = sorted((Candle(*c) for c in fraw), key=lambda c: c.ts)
        if not fine or len(hourly) < WARMUP_H + 24 or (fine[-1].ts - fine[0].ts) / 86400 < min_days:
            continue
        series.append((split_of(pool), [c for c in hourly if c.ts < fine[-1].ts], fine, liq, {}))
    keys = list(grid)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        c = dict(zip(keys, combo))
        bounce = c.pop("bounce")
        p = replace(base, **c)
        per = {"A": [], "B": []}
        for side, hourly, fine, liq, cache in series:
            per[side] += [r * 100 for r in backtest_fine(hourly, fine, p, liq, position_usd, fee_usd, mode="close",
                                                         bounce=bounce, slip=slip, stats_cache=cache)]
        a, b = _stats(per["A"]), _stats(per["B"])
        rows.append({"bounce": bounce, **c, "a": a, "b": b, "all": _stats(per["A"] + per["B"]),
                     "current": bounce == cur_bounce and all(getattr(base, k) == v for k, v in c.items())})
    for r in rows:
        r["robust"] = (min(r["a"]["avg"], r["b"]["avg"])
                       if r["a"]["n"] >= min_n and r["b"]["n"] >= min_n else -99)
    rows.sort(key=lambda r: r["robust"], reverse=True)
    cur = next((r for r in rows if r["current"]), None)
    return {"pools": len(series), "combos": len(rows), "top": rows[:8], "current": cur,
            "positive_both": sum(1 for r in rows if r["robust"] > 0)}


def _row_line(r: dict) -> str:
    return (f"ارتداد {r['bounce']*100:.0f}% · دخول ×{r['entry_drop_factor']} · وقف ×{r['stop_drop_factor']} · "
            f"هدف1 ×{r['tp1_factor']} · متحرك ×{r['trail_factor']}\n"
            f"  {r['all']['n']} صفقة · فوز {r['all'].get('win', 0):.0f}% · متوسط {r['all'].get('avg', 0):+.2f}% · "
            f"وسيط {r['all'].get('median', 0):+.1f}% · "
            f"A {r['a'].get('avg', 0):+.2f}% ({r['a']['n']}) · B {r['b'].get('avg', 0):+.2f}% ({r['b']['n']})")


def format_fine_grid(res: dict) -> str:
    L = [f"🔬 <b>بحث الإعدادات على شموع 5 دقائق (عند الإغلاق)</b>: {res['combos']} تركيبة، {res['pools']} مجمع، "
         "مرتبة حسب أضعف النصفين A/B (25 صفقة على الأقل في كل نصف)"]
    if not res["pools"]:
        return L[0] + "\nلا توجد بيانات 5 دقائق كافية بعد."
    if res["current"]:
        L.append("<b>الإعدادات الحالية</b>\n" + _row_line(res["current"]))
    L.append(f"تركيبات رابحة في النصفين معاً: {res['positive_both']} من {res['combos']}")
    L.append("<b>الأفضل</b>")
    for i, r in enumerate(res["top"], 1):
        L.append(f"{i}. " + _row_line(r))
    L.append("⚠️ مع مئات التركيبات على نحو 20 يوماً، جزء من تفوق الأفضل حظ؛ نعتمد فقط ما يبقى جيداً "
             "على بيانات الأيام القادمة.\nℹ️ تحليل فقط: لا يتغير شيء في التداول قبل موافقتك.")
    return "\n".join(L)
