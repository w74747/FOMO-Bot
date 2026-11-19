"""Trade-by-trade reconciliation: our real (paper/live) trades vs the back-test on the same
coin at the same hour, plus a direct data check (GeckoTerminal pool candles vs the Jupiter
prices we actually traded at). Pure and CPU-bound: run in a separate process."""
import statistics
from collections import Counter
from dataclasses import fields

from src.analysis.backtest import backtest
from src.analysis.waves import clean_candles
from src.data.models import Candle
from src.trading.strategy import StrategyParams

H = 3600


def _pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def reconcile(actual: list[dict], datasets: dict, params: dict, position_usd: float, fee_usd: float,
              bt_kw: dict) -> dict:
    """actual: [{mint, symbol, opened_at, closed_at, ret, reason, entry_price, exit_price, mode}]
    datasets: {mint: ([candle tuples], liquidity)}"""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    cache = {}
    rows = []
    for t in actual:
        ds = datasets.get(t["mint"])
        if not ds:
            rows.append({**t, "status": "no_data"})
            continue
        if t["mint"] not in cache:
            cs = clean_candles([Candle(*c) for c in ds[0]])
            detail, lvl = [], {}
            backtest(cs, p.zigzag_pct, p, ds[1], position_usd, tx_fee_usd=fee_usd,
                     trade_detail=detail, level_log=lvl, **(bt_kw or {}))
            cache[t["mint"]] = (cs, detail, lvl)
        cs, detail, lvl = cache[t["mint"]]
        hour = int(t["opened_at"]) // H * H
        row = dict(t)
        # data check: what did the pool candles show while we held the coin?
        held = [c for c in cs if hour <= c.ts <= t["closed_at"]]
        if held:
            row["gk_low"], row["gk_high"] = min(c.l for c in held), max(c.h for c in held)
            row["gk_body_low"] = min(min(c.o, c.c) for c in held)
            if t.get("exit_price"):
                # positive = the pool candles never went as low as our exit price
                row["exit_vs_gk_low"] = _pct(row["gk_low"], t["exit_price"])
        cand = [d for d in detail if hour - 3 * H <= d["entry_ts"] <= hour + 2 * H]
        if cand:
            m = min(cand, key=lambda d: abs(d["entry_ts"] - hour))
            row.update(status="matched", bt_ret=m["ret"] * 100, bt_reason=m["reason"],
                       bt_fill=m["fill"], entry_vs_bt=_pct(t.get("entry_price"), m["fill"]))
        else:
            keys = [k for k in lvl if k <= hour]
            st = lvl[max(keys)] if keys else None
            if not st:
                why = "no_state"
            elif not st["tradeable"]:
                why = "bt_not_tradeable"
            elif st["blocked"]:
                why = "bt_blocked"
            elif st["body_low"] > st["entry"]:
                why = "bt_level_not_reached"
            else:
                why = "bt_no_bounce"
            row.update(status="unmatched", why=why,
                       bt_level=st["entry"] if st else None,
                       entry_vs_bt_level=_pct(t.get("entry_price"), st["entry"]) if st else None,
                       bt_ref_high=st["ref_high"] if st else None)
        rows.append(row)

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return statistics.mean(xs) if xs else None
    matched = [r for r in rows if r["status"] == "matched"]
    unmatched = [r for r in rows if r["status"] == "unmatched"]
    stops = [r for r in rows if "خسارة" in (r.get("reason") or "") and r.get("exit_vs_gk_low") is not None]
    return {
        "rows": rows,
        "n": len(rows), "matched": len(matched), "unmatched": len(unmatched),
        "no_data": sum(1 for r in rows if r["status"] == "no_data"),
        "matched_actual": avg([r["ret"] for r in matched]), "matched_bt": avg([r["bt_ret"] for r in matched]),
        "matched_entry_gap": avg([r.get("entry_vs_bt") for r in matched]),
        "matched_same_side": sum(1 for r in matched if (r["ret"] > 0) == (r["bt_ret"] > 0)),
        "unmatched_actual": avg([r["ret"] for r in unmatched]),
        "why": Counter(r["why"] for r in unmatched).most_common(),
        "unmatched_level_gap": avg([r.get("entry_vs_bt_level") for r in unmatched]),
        "stops": len(stops),
        "stops_unseen": sum(1 for r in stops if r["exit_vs_gk_low"] > 2),
        "stops_gap": avg([r["exit_vs_gk_low"] for r in stops]),
    }


WHY_AR = {
    "bt_not_tradeable": "الاختبار لم يعتبر العملة صالحة للتداول في تلك الساعة",
    "bt_blocked": "الاختبار كان ممنوعاً من الدخول (موجة كُسرت سابقاً)",
    "bt_level_not_reached": "شمعة المجمع لم تصل لنقطة دخول الاختبار",
    "bt_no_bounce": "وصلت للنقطة لكن الاختبار لم يرَ ارتداداً",
    "no_state": "لا توجد بيانات اختبار لتلك الساعة",
}


def format_reconcile(r: dict) -> str:
    f = lambda x, d=1: "—" if x is None else f"{x:+.{d}f}%"  # noqa: E731
    L = ["🔍 <b>مطابقة صفقاتنا مع الاختبار</b> (نفس العملة، نفس الساعة)",
         f"{r['n']} صفقة · طابقها الاختبار: {r['matched']} · لم يدخلها: {r['unmatched']} · بلا بيانات: {r['no_data']}", ""]
    if r["matched"]:
        L += ["<b>الصفقات التي دخلها الاختبار أيضاً:</b>",
              f"• نحن: متوسط {f(r['matched_actual'])} · الاختبار: متوسط {f(r['matched_bt'])}",
              f"• نفس النتيجة (ربح/خسارة) في {r['matched_same_side']} من {r['matched']}",
              f"• سعر دخولنا مقابل دخول الاختبار: {f(r['matched_entry_gap'])}", ""]
    if r["unmatched"]:
        L += ["<b>صفقات دخلناها ولم يدخلها الاختبار:</b>",
              f"• متوسطها عندنا: {f(r['unmatched_actual'])}",
              f"• سعر دخولنا مقابل نقطة دخول الاختبار: {f(r['unmatched_level_gap'])}"]
        L += [f"• {WHY_AR.get(k, k)}: {n}" for k, n in r["why"]]
        L.append("")
    if r["stops"]:
        L += ["<b>فحص البيانات (وقف الخسارة):</b>",
              f"• في {r['stops_unseen']} من {r['stops']} صفقة خرجنا بسعر لم تصل إليه شموع المجمع أبداً (فرق > 2%)",
              f"• متوسط الفرق بين أدنى شمعة وسعر خروجنا: {f(r['stops_gap'])}", ""]
    L.append("<b>التفاصيل:</b>")
    for x in r["rows"][-25:]:
        base = f"{x.get('symbol') or x['mint'][:6]} {x.get('mode', '')[:1]} {f(x['ret'])} ({x.get('reason') or ''})"
        if x["status"] == "matched":
            base += f" ← اختبار {f(x['bt_ret'])} ({x['bt_reason']})"
        elif x["status"] == "unmatched":
            base += f" ← لم يدخل: {WHY_AR.get(x['why'], x['why'])}"
        else:
            base += " ← بلا شموع"
        if x.get("exit_vs_gk_low") is not None and "خسارة" in (x.get("reason") or ""):
            base += f" · أدنى شمعة {f(x['exit_vs_gk_low'])} عن خروجنا"
        L.append("• " + base)
    return "\n".join(L)
