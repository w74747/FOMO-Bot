"""Shadow back-test: the live signal must be the back-test's signal.

Reconciliation showed that when the bot and the back-test took the same trade the outcome
matched 8/8, while the 28 trades the back-test would NOT have taken averaged -4.7% (all 9 it
would have refused outright - not tradeable / blocked after a broken wave / no history - lost).
So before arming a coin we run the same back-test up to the latest candle and trade only if
it is flat, tradeable and not blocked, at the back-test's own levels."""
from dataclasses import fields

from src.analysis.backtest import backtest
from src.analysis.waves import clean_candles
from src.data.models import Candle
from src.trading.strategy import StrategyParams, build_plan


def shadow_states(datasets: dict, params: dict, position_usd: float, fee_usd: float, bt_kw: dict,
                  now: float | None = None) -> dict:
    """datasets {mint: ([candle tuples], liquidity)} -> {mint: {"ok", "why", plan fields...}}

    With close_entry (ENTRY_ON_CLOSE) only closed hourly candles are used (the one still
    forming is dropped) and the result carries "signal" = {"ts", "fill"} when the back-test
    bought at the close of the latest candle: live buys then, at the back-test's levels."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    out = {}
    for mint, (raw, liq) in datasets.items():
        cs = clean_candles([Candle(*c) for c in raw])
        if len(cs) < 100:
            out[mint] = {"ok": False, "why": "no_history"}
            continue
        close_mode = bool((bt_kw or {}).get("close_entry"))
        if close_mode and now:
            cs = [c for c in cs if c.ts + 3600 <= now]      # closed candles only
            if len(cs) < 100:
                out[mint] = {"ok": False, "why": "no_history"}
                continue
        lvl: dict = {}
        opened: dict = {}
        backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, level_log=lvl,
                 open_log=opened, **(bt_kw or {}))
        last = cs[-1].ts
        st = lvl.get(last)
        signal = None
        if close_mode and opened:
            if opened["entry_ts"] != last or st is None:
                out[mint] = {"ok": False, "why": "bt_in_trade"}
                continue
            signal = {"ts": last, "fill": opened["fill"]}
        if st is None:
            # no flat state on the latest bar: the back-test is in a trade (or has no stats)
            out[mint] = {"ok": False, "why": "bt_in_trade" if lvl else "no_state"}
            continue
        if not st["tradeable"]:
            out[mint] = {"ok": False, "why": "bt_not_tradeable"}
            continue
        if st["blocked"] and not signal:
            out[mint] = {"ok": False, "why": "bt_blocked"}
            continue
        plan = build_plan(st["ref_high"], st["median_drop"], st["median_rise"], st["avg_cycle_hours"],
                          liq, position_usd, p, fee_usd)
        if plan is None:
            out[mint] = {"ok": False, "why": "bt_no_plan"}
            continue
        out[mint] = {"ok": True, "why": "", "ref_high": plan.ref_high, "entry": plan.entry, "stop": plan.stop,
                     "tp1": plan.tp1, "tp2": plan.tp2, "stop_pct": plan.stop_pct,
                     "time_stop_hours": plan.time_stop_hours, "median_drop": st["median_drop"],
                     "median_rise": st["median_rise"], "avg_cycle_hours": st["avg_cycle_hours"],
                     "bt_armed": st["armed"], "signal": signal}
    return out


SHADOW_WHY_AR = {
    "no_history": "تاريخ شموع غير كافٍ",
    "no_state": "لا حالة للاختبار",
    "bt_in_trade": "الاختبار داخل صفقة بالفعل",
    "bt_not_tradeable": "الاختبار لا يعتبرها صالحة الآن",
    "bt_blocked": "موجة كُسرت: بانتظار قمة جديدة",
    "bt_no_plan": "المكافأة لا تغطي المخاطرة",
}
