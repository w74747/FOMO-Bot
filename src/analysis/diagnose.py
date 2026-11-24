"""Full diagnosis of the gap between the back-test and our real results.

Three questions, each answered from stored data:
1. Strategy: per era (old model / realistic / shadow gate), paper vs live, by exit reason;
   and, for the shadow era, does the back-test take the same trades with the same outcome?
2. Data: GeckoTerminal pool candles (what we analyse) vs hourly candles built from our own
   1-minute Jupiter prices (what we actually trade at). Same hours, same coins. Then the same
   realistic back-test on both, over the hours both exist.
3. Speed: measured entry/exit gaps vs what the back-test assumes.
Pure and CPU-bound: run in a separate process."""
import statistics
from collections import Counter, defaultdict
from dataclasses import fields

from src.analysis.backtest import backtest
from src.analysis.reconcile import reconcile
from src.analysis.waves import clean_candles
from src.data.models import Candle
from src.trading.strategy import StrategyParams

H = 3600


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _st(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    return {"n": len(rets), "win": sum(r > 0 for r in rets) / len(rets) * 100,
            "avg": statistics.mean(rets), "median": statistics.median(rets), "sum": sum(rets)}


def era_of(ts: float, eras: list[tuple[str, float]]) -> str:
    name = eras[0][0]
    for n, start in eras:
        if ts >= start:
            name = n
    return name


def trades_by_era(closes: list[dict], eras: list[tuple[str, float]]) -> dict:
    """closes: [{opened_at, ret, pnl, reason, mode, tier, entry_gap, exit_gap}]"""
    out = {}
    for name, _ in eras:
        for mode in ("paper", "live"):
            rows = [c for c in closes if era_of(c["opened_at"], eras) == name and c["mode"] == mode
                    and c.get("tier") != "low_liq"]
            if not rows:
                continue
            by_reason = defaultdict(list)
            for c in rows:
                by_reason[c.get("reason") or "?"].append(c["ret"])
            out[(name, mode)] = {
                "all": _st([c["ret"] for c in rows]),
                "usd": sum(c.get("pnl") or 0 for c in rows),
                "reasons": {k: _st(v) for k, v in by_reason.items()},
                "entry_gap": _avg([c.get("entry_gap") for c in rows]),
                "exit_gap": _avg([c.get("exit_gap") for c in rows if c.get("reason") == "وقف خسارة"]),
            }
    return out


def compare_data(gecko: dict, jup: dict, min_samples: int = 30) -> dict:
    """gecko {mint: [Candle]} ; jup {mint: [(ts, o, h, l, c, n)]} -> hour-by-hour differences.
    Positive high_excess = the pool candle shows a HIGHER high than Jupiter ever printed;
    positive low_excess = the pool candle shows a LOWER low than Jupiter ever printed."""
    rows = []
    per_coin = {}
    for mint, jrows in jup.items():
        gk = {c.ts: c for c in gecko.get(mint, [])}
        mine = []
        for ts, o, h, lo, c, n in jrows:
            g = gk.get(ts)
            if not g or n < min_samples or min(o, h, lo, c, g.c, g.h, g.l) <= 0:
                continue
            r = {"close_diff": abs(g.c / c - 1) * 100, "high_excess": (g.h / h - 1) * 100,
                 "low_excess": (lo / g.l - 1) * 100,
                 "range_gk": (g.h / g.l - 1) * 100, "range_jup": (h / lo - 1) * 100}
            mine.append(r)
        if mine:
            per_coin[mint] = len(mine)
            rows += mine
    if not rows:
        return {"hours": 0}
    return {
        "hours": len(rows), "coins": len(per_coin),
        "close_diff_med": _med([r["close_diff"] for r in rows]),
        "high_excess_med": _med([r["high_excess"] for r in rows]),
        "low_excess_med": _med([r["low_excess"] for r in rows]),
        "high_excess_3": sum(r["high_excess"] > 3 for r in rows) / len(rows) * 100,
        "low_excess_3": sum(r["low_excess"] > 3 for r in rows) / len(rows) * 100,
        "range_gk_med": _med([r["range_gk"] for r in rows]),
        "range_jup_med": _med([r["range_jup"] for r in rows]),
    }


def backtest_on_both(gecko: dict, jup: dict, liq: dict, p: StrategyParams, position_usd: float,
                     fee_usd: float, bt_kw: dict, min_samples: int = 30) -> dict:
    """Same back-test, same history before the Jupiter window; inside the window one run uses
    the pool candles and the other our Jupiter candles. Only trades entered inside the window
    are compared."""
    res = {"gecko": [], "jupiter": [], "coins": 0}
    reasons = {"gecko": Counter(), "jupiter": Counter()}
    for mint, jrows in jup.items():
        gk = clean_candles(gecko.get(mint, []))
        jc = {ts: Candle(ts, o, h, lo, c, 0.0) for ts, o, h, lo, c, n in jrows if n >= min_samples}
        if len(gk) < 300 or len(jc) < 24:
            continue
        start = min(jc)
        spliced = [c for c in gk if c.ts < start] + [jc[ts] for ts in sorted(jc)]
        gk_win = [c for c in gk if c.ts < start] + [c for c in gk if c.ts >= start and c.ts in jc]
        res["coins"] += 1
        for name, cs in (("gecko", gk_win), ("jupiter", spliced)):
            det: list = []
            backtest(cs, p.zigzag_pct, p, liq.get(mint, 100_000), position_usd, tx_fee_usd=fee_usd,
                     trade_detail=det, **(bt_kw or {}))
            for d in det:
                if d["entry_ts"] >= start:
                    res[name].append(d["ret"] * 100)
                    reasons[name][d["reason"]] += 1
    return {"coins": res["coins"], "gecko": _st(res["gecko"]), "jupiter": _st(res["jupiter"]),
            "reasons_gecko": dict(reasons["gecko"]), "reasons_jupiter": dict(reasons["jupiter"])}


def recent_bt_reasons(datasets: dict, p: StrategyParams, position_usd: float, fee_usd: float,
                      bt_kw: dict, since: float) -> dict:
    """Exit reasons of back-test trades entered since `since`, across all stored coins."""
    c = Counter()
    rets = []
    for mint, (raw, liq) in datasets.items():
        det: list = []
        backtest(clean_candles([Candle(*x) for x in raw]), p.zigzag_pct, p, liq, position_usd,
                 tx_fee_usd=fee_usd, trade_detail=det, **(bt_kw or {}))
        for d in det:
            if d["entry_ts"] >= since:
                c[d["reason"]] += 1
                rets.append(d["ret"] * 100)
    return {"reasons": dict(c), "all": _st(rets)}


def diagnose(closes: list[dict], actual: list[dict], datasets: dict, jup: dict, params: dict,
             position_usd: float, fee_usd: float, bt_kw: dict, eras: list[tuple[str, float]]) -> dict:
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    shadow_start = eras[-1][1]
    gecko = {m: [Candle(*x) for x in raw] for m, (raw, _) in datasets.items()}
    liq = {m: l for m, (_, l) in datasets.items()}
    shadow_actual = [a for a in actual if a["opened_at"] >= shadow_start and a.get("mode") != "low_liq"]
    return {
        "eras": trades_by_era(closes, eras),
        "reconcile": reconcile(shadow_actual, datasets, params, position_usd, fee_usd, bt_kw) if shadow_actual else None,
        "data": compare_data(gecko, jup),
        "bt_both": backtest_on_both(gecko, jup, liq, p, position_usd, fee_usd, bt_kw),
        "bt_recent": recent_bt_reasons(datasets, p, position_usd, fee_usd, bt_kw, shadow_start),
        "eras_names": [n for n, _ in eras],
    }


REASON_AR = {"stop": "وقف خسارة", "stop (same hour)": "وقف في نفس الساعة", "trailing": "وقف متحرك",
             "time": "وقف زمني", "time+tp1": "هدف 1 ثم وقف زمني", "stop+tp1": "هدف 1 ثم وقف"}


def _row(label, st, usd=None):
    if not st.get("n"):
        return f"{label}: —"
    t = f"{label}: {st['n']} صفقة · فوز {st['win']:.0f}% · متوسط {st['avg']:+.1f}% · وسيط {st['median']:+.1f}%"
    return t + (f" · صافي {usd:+.2f}$" if usd is not None else "")


def format_diagnose(r: dict) -> str:
    f = lambda x, d=1: "—" if x is None else f"{x:+.{d}f}%"  # noqa: E731
    L = ["🩻 <b>تشخيص شامل: لماذا يختلف الواقع عن الاختبار؟</b>", "", "<b>1) صفقاتنا حسب المرحلة</b>"]
    for name in r["eras_names"]:
        for mode, tag in (("paper", "ورقي"), ("live", "حقيقي")):
            e = r["eras"].get((name, mode))
            if not e:
                continue
            L.append(_row(f"• {name} — {tag}", e["all"], e["usd"]))
            rs = sorted(e["reasons"].items(), key=lambda kv: -kv[1]["n"])
            L.append("  " + " · ".join(f"{k}: {v['n']} ({v['avg']:+.0f}%)" for k, v in rs))
            if mode == "live":
                L.append(f"  سرعة: الشراء فوق القاع {f(e['entry_gap'])} · البيع تحت الوقف {f(e['exit_gap'])}")
    rec = r.get("reconcile")
    if rec:
        L += ["", "<b>2) مرحلة اختبار الظل: هل يأخذ الاختبار نفس الصفقات؟</b>",
              f"• {rec['n']} صفقة · طابقها الاختبار {rec['matched']} · لم يدخلها {rec['unmatched']}",
              f"• المتطابقة: نحن {f(rec['matched_actual'])} مقابل الاختبار {f(rec['matched_bt'])} · "
              f"نفس النتيجة في {rec['matched_same_side']} من {rec['matched']}",
              f"• غير المتطابقة: متوسطها عندنا {f(rec['unmatched_actual'])}"]
        L += [f"  - {k}: {n}" for k, n in rec["why"]]
    br = r["bt_recent"]
    if br["all"].get("n"):
        L += ["", "<b>3) الاختبار على كل العملات في نفس الفترة</b>", _row("• الاختبار", br["all"]),
              "  " + " · ".join(f"{REASON_AR.get(k, k)}: {v}" for k, v in sorted(br["reasons"].items(), key=lambda kv: -kv[1]))]
    d = r["data"]
    L += ["", "<b>4) البيانات: شموع المجمع (GeckoTerminal) مقابل أسعار Jupiter الفعلية</b>"]
    if d.get("hours"):
        L += [f"• {d['hours']} ساعة على {d['coins']} عملة",
              f"• فرق سعر الإغلاق (وسيط): {d['close_diff_med']:.2f}%",
              f"• قمة المجمع أعلى من أعلى سعر في Jupiter: وسيط {f(d['high_excess_med'], 2)} · أكثر من 3% في {d['high_excess_3']:.0f}% من الساعات",
              f"• قاع المجمع أدنى من أدنى سعر في Jupiter: وسيط {f(d['low_excess_med'], 2)} · أكثر من 3% في {d['low_excess_3']:.0f}% من الساعات",
              f"• مدى الساعة (قمة/قاع): المجمع {d['range_gk_med']:.1f}% · Jupiter {d['range_jup_med']:.1f}%"]
    else:
        L.append("• لا توجد ساعات مشتركة كافية بعد")
    b = r["bt_both"]
    if b.get("coins"):
        L += [f"• نفس الاختبار على {b['coins']} عملة في نافذة Jupiter:",
              _row("  - على شموع المجمع", b["gecko"]), _row("  - على شموع Jupiter", b["jupiter"])]
    return "\n".join(L)
