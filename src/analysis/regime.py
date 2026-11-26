"""Does the wider small-cap market decide whether a wave trade works?

Builds a market index from every stored coin (median 24h / 6h return and the share of
coins that are up), back-tests the current strategy on all stored candles while logging
each trade's entry time, and groups trades by the market state just before entry
(only the previous, fully closed hour is used: no look-ahead).

Pure and CPU-heavy: run it in a separate process."""
import bisect
import statistics
from dataclasses import fields

from src.analysis.backtest import backtest
from src.analysis.optimizer import split_of
from src.analysis.waves import clean_candles
from src.data.models import Candle
from src.trading.strategy import StrategyParams

HOUR = 3600
MKT_BUCKETS = [(-1e9, -10), (-10, -5), (-5, 0), (0, 5), (5, 1e9)]
BREADTH_BUCKETS = [(0, 30), (30, 45), (45, 60), (60, 101)]
SKIP_LEVELS = [-15, -10, -7, -5, -3, 0]
AGE_BUCKETS = [(0, 3, "آخر 3 أيام"), (3, 7, "3–7 أيام"), (7, 14, "1–2 أسبوع"), (14, 21, "2–3 أسابيع"),
               (21, 28, "3–4 أسابيع"), (28, 42, "4–6 أسابيع"), (42, 10_000, "أقدم من 6 أسابيع")]


def market_index(series: dict[str, list[Candle]], min_coins: int = 8) -> dict[int, dict]:
    closes = {p: {c.ts: c.c for c in cs if c.c > 0} for p, cs in series.items()}
    stamps = sorted({ts for d in closes.values() for ts in d})
    out = {}
    for ts in stamps:
        r24, r6 = [], []
        for d in closes.values():
            now = d.get(ts)
            if not now:
                continue
            a, b = d.get(ts - 24 * HOUR), d.get(ts - 6 * HOUR)
            if a:
                r24.append(now / a - 1)
            if b:
                r6.append(now / b - 1)
        if len(r24) >= min_coins:
            out[ts] = {"m24": statistics.median(r24) * 100,
                       "m6": statistics.median(r6) * 100 if r6 else 0.0,
                       "breadth": sum(1 for r in r24 if r > 0) / len(r24) * 100,
                       "coins": len(r24)}
    return out


_keys_cache: dict[int, list[int]] = {}


def regime_at(index: dict[int, dict], ts: float) -> dict | None:
    """State of the market at the last bar that had fully closed by `ts` (bar start + 1h <= ts),
    at most 3 hours old."""
    keys = _keys_cache.get(id(index))
    if keys is None or len(keys) != len(index):
        keys = sorted(index)
        _keys_cache.clear()
        _keys_cache[id(index)] = keys
    i = bisect.bisect_right(keys, ts - HOUR) - 1
    if i < 0 or ts - HOUR - keys[i] > 3 * HOUR:
        return None
    return index[keys[i]]


def _stats(rets: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"n": 0}
    return {"n": n, "win": sum(1 for r in rets if r > 0) / n * 100,
            "avg": statistics.mean(rets), "total": sum(rets)}


def _bucket(trades, key, buckets):
    return [((lo, hi), _stats([t["ret"] for t in trades if t[key] is not None and lo <= t[key] < hi]))
            for lo, hi in buckets]


def analyze(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float,
            real_trades: list[dict] | None = None, bt_kw: dict | None = None) -> dict:
    """datasets: [(pool, [candle tuples], liquidity)]. real_trades: [{opened_at, ret, mode}]
    with ret in %. Returns plain dicts (picklable)."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = {pool: clean_candles([Candle(*c) for c in cs]) for pool, cs, _ in datasets}
    index = market_index(series)

    trades = []
    for pool, _, liq in datasets:
        log: list = []
        backtest(series[pool], p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, trade_log=log,
                 **(bt_kw or {}))
        for ts, ret in log:
            rg = regime_at(index, ts)
            trades.append({"pool": pool, "side": split_of(pool), "ts": ts, "ret": ret * 100,
                           "m24": rg["m24"] if rg else None, "m6": rg["m6"] if rg else None,
                           "breadth": rg["breadth"] if rg else None})
    with_rg = [t for t in trades if t["m24"] is not None]

    def skip_table(rows):
        base = _stats([t["ret"] for t in rows])
        out = []
        for lvl in SKIP_LEVELS:
            kept = [t["ret"] for t in rows if t["m24"] >= lvl]
            out.append((lvl, _stats(kept)))
        return base, out

    real = []
    for r in real_trades or []:
        rg = regime_at(index, r["opened_at"])
        if rg:
            real.append({**r, "m24": rg["m24"], "m6": rg["m6"], "breadth": rg["breadth"]})

    last_ts = max(index) if index else None
    # does the edge fade? trades grouped by how long ago they were entered
    by_age = []
    if trades and last_ts:
        for lo, hi, label in AGE_BUCKETS:
            rows = [t for t in trades if lo * 86400 <= last_ts - t["ts"] < hi * 86400]
            coins = len({t["pool"] for t in rows})
            st = _stats([t["ret"] for t in rows])
            if st["n"]:
                st["stops"] = sum(1 for t in rows if t["ret"] <= -10) / st["n"] * 100
                st["coins"] = coins
            by_age.append((label, st))
    health = strategy_health(trades)
    return {
        "health": health,
        "by_age": by_age,
        "coins": len(series), "hours": len(index),
        "trades": len(trades), "with_regime": len(with_rg),
        "by_m24": _bucket(with_rg, "m24", MKT_BUCKETS),
        "by_m6": _bucket(with_rg, "m6", MKT_BUCKETS),
        "by_breadth": _bucket(with_rg, "breadth", BREADTH_BUCKETS),
        "skip_all": skip_table(with_rg),
        "skip_a": skip_table([t for t in with_rg if t["side"] == "A"]),
        "skip_b": skip_table([t for t in with_rg if t["side"] == "B"]),
        "real_by_m24": _bucket(real, "m24", MKT_BUCKETS),
        "real": [{k: r.get(k) for k in ("symbol", "mode", "ret", "m24", "breadth")} for r in real],
        "now": index[last_ts] if last_ts else None,
    }


def _fmt_range(lo, hi, unit="%"):
    if lo <= -1e8:
        return f"&lt; {hi:+.0f}{unit}"
    if hi >= 100 and unit == "%" and lo >= 60:
        return f"≥ {lo:.0f}{unit}"
    if hi >= 1e8:
        return f"≥ {lo:+.0f}{unit}"
    return f"{lo:+.0f}..{hi:+.0f}{unit}"


def _row(label, st):
    if not st["n"]:
        return f"{label}: —"
    return f"{label}: {st['n']} صفقة · فوز {st['win']:.0f}% · متوسط {st['avg']:+.2f}% · مجموع {st['total']:+.0f}%"


DAY = 86400


def strategy_health(trades: list[dict], windows=(4, 7), lag_days: float = 2.0,
                    min_trades: int = 10) -> dict:
    """Equity-curve filter test: before each trade, how did the strategy do across ALL coins in
    the previous `w` days? Only trades entered at least `lag_days` earlier are used (they are
    closed by then: time stop <= 48h), so nothing from the future leaks in."""
    rows = sorted(trades, key=lambda t: t["ts"])
    out = {}
    for w in windows:
        tagged = []
        for t in rows:
            hi, lo = t["ts"] - lag_days * DAY, t["ts"] - (lag_days + w) * DAY
            past = [x["ret"] for x in rows if lo <= x["ts"] < hi]
            tagged.append({**t, "trail": statistics.mean(past) if len(past) >= min_trades else None})
        known = [t for t in tagged if t["trail"] is not None]
        res = {"known": len(known), "levels": []}
        for lvl in (-100, -2, 0, 2, 4):
            res["levels"].append((lvl,
                                  _stats([t["ret"] for t in known if t["trail"] > lvl]),
                                  _stats([t["ret"] for t in known if t["trail"] > lvl and t["side"] == "A"]),
                                  _stats([t["ret"] for t in known if t["trail"] > lvl and t["side"] == "B"])))
        res["off"] = _stats([t["ret"] for t in known if t["trail"] <= 0])
        out[w] = res
    if rows:
        now = rows[-1]["ts"] + lag_days * DAY
        for w in windows:
            past = [x["ret"] for x in rows if now - (lag_days + w) * DAY <= x["ts"] < now - lag_days * DAY]
            out[w]["now"] = statistics.mean(past) if len(past) >= min_trades else None
            out[w]["now_n"] = len(past)
    return out


def format_health(r: dict) -> str:
    L = ["🩺 <b>فلتر صحة الاستراتيجية</b>: نتداول فقط إذا كان متوسط صفقات الاختبار على كل العملات "
         "في الأيام الماضية أعلى من حد"]
    for w, res in (r.get("health") or {}).items():
        L.append(f"<b>نافذة {w} أيام</b> ({res['known']} صفقة لها تاريخ كافٍ)")
        for lvl, st, a, b in res["levels"]:
            name = "بدون فلتر" if lvl <= -100 else f"المتوسط السابق &gt; {lvl:+d}%"
            L.append(_row(f"• {name}", st) + (f" | A {a['avg']:+.2f}% · B {b['avg']:+.2f}%" if a["n"] and b["n"] else ""))
        L.append(_row("• الصفقات التي كان الفلتر سيمنعها (≤ 0%)", res["off"]))
        if res.get("now") is not None:
            L.append(f"• الآن: متوسط آخر {w} أيام (قبل يومين) = {res['now']:+.2f}% من {res['now_n']} صفقة")
    return "\n".join(L)


def format_age(r: dict) -> str:
    L = ["📅 <b>هل تضعف الاستراتيجية مع الوقت؟</b> (الاختبار الواقعي حسب عمر الصفقة)"]
    for label, st in r.get("by_age", []):
        if not st["n"]:
            L.append(f"• {label}: —")
            continue
        L.append(f"• {label}: {st['n']} صفقة/{st['coins']} عملة · فوز {st['win']:.0f}% · "
                 f"متوسط {st['avg']:+.2f}% · خسارة كبيرة (≤ -10%) {st['stops']:.0f}%")
    return "\n".join(L)


def format_report(r: dict) -> str:
    L = ["🌡 <b>أثر حالة السوق على الاستراتيجية</b>",
         f"{r['coins']} عملة · {r['hours']} ساعة من المؤشر · {r['trades']} صفقة اختبار ({r['with_regime']} بمؤشر)"]
    if r.get("now"):
        n = r["now"]
        L.append(f"السوق الآن: وسيط 24س {n['m24']:+.1f}% · 6س {n['m6']:+.1f}% · صاعدة {n['breadth']:.0f}% من {n['coins']} عملة")
    L += ["", "<b>حسب وسيط تغيّر السوق خلال 24 ساعة قبل الدخول:</b>"]
    L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["by_m24"]]
    L += ["", "<b>حسب نسبة العملات الصاعدة:</b>"]
    L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["by_breadth"]]
    L += ["", "<b>حسب وسيط 6 ساعات:</b>"]
    L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["by_m6"]]
    for key, name in (("skip_all", "الكل"), ("skip_a", "النصف A"), ("skip_b", "النصف B")):
        base, rows = r[key]
        if not base["n"]:
            continue
        L += ["", f"<b>لو أوقفنا الشراء حين السوق أضعف من حد ({name}):</b>", _row("• بدون فلتر", base)]
        L += [_row(f"• حد {lvl:+d}%", st) for lvl, st in rows]
    if r["real"]:
        L += ["", "<b>صفقاتنا الفعلية (ورقي + حقيقي):</b>"]
        L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["real_by_m24"]]
    return "\n".join(L)


ENTRY_MODELS = [
    ("الاختبار الحالي (شراء عند نقطة الدخول)", None, 0.0, False),
    ("ارتداد 2% بلا تأخير", 0.02, 0.0, False),
    ("ارتداد 2% + تأخير 0.7% (المقاس في GTA)", 0.02, 0.007, False),
    ("ارتداد 2% + تأخير 2%", 0.02, 0.02, False),
    ("ارتداد 1% + تأخير 0.7%", 0.01, 0.007, False),
    ("ارتداد 3% + تأخير 0.7%", 0.03, 0.007, False),
    ("الحالي بلا ذيول (جسم الشمعة فقط لصالحنا)", None, 0.0, True),
    ("ارتداد 2% + تأخير 0.7% بلا ذيول", 0.02, 0.007, True),
    ("ارتداد 1% + تأخير 0.7% بلا ذيول", 0.01, 0.007, True),
]


def entry_models(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float) -> list[dict]:
    """Same coins, same params; only the entry model changes. Shows how much of the back-test's
    edge survives the real 'wait for the bounce' entry."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = [(pool, clean_candles([Candle(*c) for c in cs]), liq) for pool, cs, liq in datasets]
    out = []
    for name, bounce, slip, body in ENTRY_MODELS:
        per = {"A": [], "B": []}
        for pool, cs, liq in series:
            r = backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd,
                         bounce_pct=bounce, entry_slip=slip, body_only=body)
            per[split_of(pool)] += [x * 100 for x in r.returns]
        out.append({"name": name, "all": _stats(per["A"] + per["B"]),
                    "a": _stats(per["A"]), "b": _stats(per["B"])})
    return out


def format_entry_models(rows: list[dict], real: list[dict] | None = None) -> str:
    L = ["🎯 <b>واقعية الاختبار: نموذج الدخول</b>"]
    for r in rows:
        L += [f"<b>{r['name']}</b>", _row("• الكل", r["all"]),
              _row("• A", r["a"]) + " | " + _row("B", r["b"]).replace("B: ", "B ")]
    if real:
        L += ["", _row("📍 صفقاتنا الفعلية", _stats([t["ret"] for t in real]))]
    return "\n".join(L)



def health_now(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float,
               bt_kw: dict, now_ts: float, window_days: float, lag_days: float,
               min_trades: int) -> dict:
    """Average realistic back-test trade over [now - lag - window, now - lag) across all coins."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    lo, hi = now_ts - (lag_days + window_days) * DAY, now_ts - lag_days * DAY
    rets, prev = [], []
    for _, cs, liq in datasets:
        log: list = []
        backtest(clean_candles([Candle(*c) for c in cs]), p.zigzag_pct, p, liq, position_usd,
                 tx_fee_usd=fee_usd, trade_log=log, **(bt_kw or {}))
        rets += [r * 100 for ts, r in log if lo <= ts < hi]
        prev += [r * 100 for ts, r in log if lo - window_days * DAY <= ts < lo]
    return {"value": statistics.mean(rets) if len(rets) >= min_trades else None, "n": len(rets),
            "prev": statistics.mean(prev) if len(prev) >= min_trades else None, "ts": now_ts}



EXIT_MODELS = [("الحالي: نصف عند الهدف 1 + الباقي بوقف متحرك", None)] + [
    (f"هدف واحد متحرك فقط: بيع الكل عند هبوط {int(t * 100)}% من أعلى سعر", t)
    for t in (0.05, 0.08, 0.12, 0.16, 0.20)]


def exit_models(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float, bt_kw: dict) -> list[dict]:
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = [(pool, clean_candles([Candle(*c) for c in cs]), liq) for pool, cs, liq in datasets]
    out = []
    for name, trail in EXIT_MODELS:
        per = {"A": [], "B": []}
        for pool, cs, liq in series:
            r = backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, trail_only=trail, **(bt_kw or {}))
            per[split_of(pool)] += [x * 100 for x in r.returns]
        allr = per["A"] + per["B"]
        st = _stats(allr)
        if st["n"]:
            wins, losses = [x for x in allr if x > 0], [x for x in allr if x <= 0]
            st["avg_win"] = statistics.mean(wins) if wins else 0.0
            st["avg_loss"] = statistics.mean(losses) if losses else 0.0
        out.append({"name": name, "all": st, "a": _stats(per["A"]), "b": _stats(per["B"])})
    return out


def format_exit_models(rows: list[dict]) -> str:
    L = ["🎯 <b>طريقة الخروج</b> (الاختبار الواقعي، نفس الدخول)"]
    for r in rows:
        a = r["all"]
        L.append(f"<b>{r['name']}</b>")
        L.append(_row("• الكل", a) + (f" · متوسط الربح {a['avg_win']:+.1f}% / الخسارة {a['avg_loss']:+.1f}%" if a["n"] else ""))
        L.append(f"• A {r['a'].get('avg', 0):+.2f}% · B {r['b'].get('avg', 0):+.2f}%")
    return "\n".join(L)



SELECTION_RULES = [
    ("الحالي: 3 صفقات + مجموع موجب", lambda r: len(r) >= 3 and sum(r) > 0),
    ("صفقتان + مجموع موجب", lambda r: len(r) >= 2 and sum(r) > 0),
    ("صفقتان + وسيط موجب", lambda r: len(r) >= 2 and statistics.median(r) > 0),
    ("3 صفقات + متوسط ≥ +2%", lambda r: len(r) >= 3 and statistics.mean(r) >= 2),
    ("3 صفقات + وسيط موجب", lambda r: len(r) >= 3 and statistics.median(r) > 0),
    ("3 صفقات + وسيط موجب + متوسط ≥ +2%", lambda r: len(r) >= 3 and statistics.median(r) > 0
     and statistics.mean(r) >= 2),
    ("5 صفقات + وسيط موجب + متوسط ≥ +2%", lambda r: len(r) >= 5 and statistics.median(r) > 0
     and statistics.mean(r) >= 2),
]


def selection_test(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float, bt_kw: dict,
                   step_days: float = 2.0, hold_days: float = 2.0, lookback_days: float = 30.0,
                   lag_days: float = 2.0) -> dict:
    """Out-of-sample test of the per-coin acceptance rule. At every cutoff (every `step_days`),
    each coin is judged ONLY on its back-test trades entered in the previous `lookback_days`
    (and at least `lag_days` before the cutoff, so they are closed); the rule's verdict is then
    scored on the coin's trades entered in the next `hold_days`. Nothing from the future leaks."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    logs = []
    for pool, cs, liq in datasets:
        log: list = []
        backtest(clean_candles([Candle(*c) for c in cs]), p.zigzag_pct, p, liq, position_usd,
                 tx_fee_usd=fee_usd, trade_log=log, **(bt_kw or {}))
        if log:
            logs.append((pool, [(ts, r * 100) for ts, r in log]))
    if not logs:
        return {"rules": [], "cutoffs": 0}
    t_min = min(ts for _, lg in logs for ts, _ in lg)
    t_max = max(ts for _, lg in logs for ts, _ in lg)
    cutoffs, c = [], t_min + 10 * DAY
    while c + hold_days * DAY <= t_max:
        cutoffs.append(c)
        c += step_days * DAY
    out = []
    for name, rule in SELECTION_RULES:
        res = {"A": [], "B": []}
        picks = 0
        for c in cutoffs:
            for pool, lg in logs:
                past = [r for ts, r in lg if c - lookback_days * DAY <= ts < c - lag_days * DAY]
                if not rule(past):
                    continue
                fut = [r for ts, r in lg if c <= ts < c + hold_days * DAY]
                if fut:
                    picks += 1
                    res[split_of(pool)] += fut
        allr = res["A"] + res["B"]
        out.append({"name": name, "all": _stats(allr), "a": _stats(res["A"]), "b": _stats(res["B"]),
                    "median": statistics.median(allr) if allr else None, "picks": picks})
    return {"rules": out, "cutoffs": len(cutoffs), "coins": len(logs)}


def format_selection(r: dict) -> str:
    L = ["🧪 <b>شرط قبول العملة</b> (اختبار خارج العينة: نحكم على العملة بماضيها فقط، ونقيس صفقاتها التالية)",
         f"{r.get('coins', 0)} عملة · {r.get('cutoffs', 0)} نقطة قرار (كل يومين)"]
    for x in r.get("rules", []):
        a = x["all"]
        if not a["n"]:
            L.append(f"<b>{x['name']}</b>: لا صفقات")
            continue
        L.append(f"<b>{x['name']}</b>")
        L.append(_row("• الصفقات التالية", a) + f" · وسيط {x['median']:+.1f}%")
        L.append(f"• A {x['a'].get('avg', 0):+.2f}% ({x['a']['n']}) · B {x['b'].get('avg', 0):+.2f}% ({x['b']['n']})")
    return "\n".join(L)



COOLDOWN_MODELS = [
    ("بلا منع (الاختبار الحالي)", None),
    ("منع 12س بعد الخسارة فقط", {"loss_h": 12}),
    ("12س + وقفان خلال 7 أيام = 72س (قاعدة البوت حتى اليوم)", {"loss_h": 12, "strike_limit": 2, "strike_h": 72}),
    ("12س + 72س + ثلاثة وقوف = أسبوع (القاعدة الجديدة)",
     {"loss_h": 12, "strike_limit": 2, "strike_h": 72, "long_limit": 3, "long_h": 168}),
    ("منع 4س بعد الخسارة فقط", {"loss_h": 4}),
]


def cooldown_models(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float, bt_kw: dict) -> list[dict]:
    """Do the live rest/ban rules after losses help or hurt? Same coins, same everything else."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = [(pool, clean_candles([Candle(*c) for c in cs]), liq) for pool, cs, liq in datasets]
    out = []
    for name, cd in COOLDOWN_MODELS:
        per = {"A": [], "B": []}
        for pool, cs, liq in series:
            r = backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, cooldowns=cd, **(bt_kw or {}))
            per[split_of(pool)] += [x * 100 for x in r.returns]
        out.append({"name": name, "all": _stats(per["A"] + per["B"]), "a": _stats(per["A"]), "b": _stats(per["B"])})
    return out


def format_cooldown_models(rows: list[dict]) -> str:
    L = ["⏸ <b>هل يفيد منع العملة بعد الخسارة؟</b> (الاختبار الواقعي، كل العملات المخزنة)"]
    for r in rows:
        a = r["all"]
        L.append(f"<b>{r['name']}</b>")
        L.append(_row("• الكل", a))
        if a["n"]:
            L.append(f"• A: {r['a']['n']} صفقة · مجموع {r['a'].get('total', 0):+.0f}% · B: {r['b']['n']} صفقة · "
                     f"مجموع {r['b'].get('total', 0):+.0f}%")
    return "\n".join(L)


GATE_MODELS = [
    ("القواعد الحالية (فلتر الاتجاه + القيعان)", ()),
    ("بدون فلتر الاتجاه الهابط", ("downtrend",)),
    ("بدون فلتر القيعان المتناقصة", ("lower lows",)),
    ("بدون الفلترين", ("downtrend", "lower lows")),
]


def gate_models(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float, bt_kw: dict) -> list[dict]:
    """Research: what do the downtrend / lower-lows filters cost or save? Same coins, same
    realistic back-test, only the wave-score gate differs. A/B halves split by pool hash."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = [(pool, clean_candles([Candle(*c) for c in cs]), liq) for pool, cs, liq in datasets]
    caches = {pool: {} for pool, _, _ in series}
    out = []
    for name, skip in GATE_MODELS:
        per = {"A": [], "B": []}
        coins = 0
        for pool, cs, liq in series:
            r = backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, stats_cache=caches[pool],
                         skip_gates=skip, **(bt_kw or {}))
            per[split_of(pool)] += [x * 100 for x in r.returns]
            coins += bool(r.trades)
        rets = per["A"] + per["B"]
        out.append({"name": name, "coins": coins, "all": _stats(rets), "a": _stats(per["A"]), "b": _stats(per["B"]),
                    "median": statistics.median(rets) if rets else 0.0})
    return out


def coin_gate_models(candles: list[tuple], liq: float, params: dict, position_usd: float, fee_usd: float,
                     bt_kw: dict) -> list[dict]:
    """The same comparison for one coin; halves = first / second half of the tested period."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    cs = clean_candles([Candle(*c) for c in candles])
    cache: dict = {}
    out = []
    for name, skip in GATE_MODELS:
        log: list = []
        r = backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, stats_cache=cache,
                     trade_log=log, skip_gates=skip, **(bt_kw or {}))
        start = cs[min(240, len(cs) - 1)].ts if cs else 0
        mid = (start + cs[-1].ts) / 2 if cs else 0
        a = [x * 100 for ts, x in log if ts < mid]
        b = [x * 100 for ts, x in log if ts >= mid]
        out.append({"name": name, "days": r.test_days, "all": _stats(a + b), "a": _stats(a), "b": _stats(b),
                    "worst": r.worst_trade_pct,
                    "median": statistics.median(a + b) if a or b else 0.0})
    return out


def format_gate_models(rows: list[dict], title: str) -> str:
    L = [title]
    for r in rows:
        a = r["all"]
        extra = f" · {r['coins']} عملة" if "coins" in r else ""
        L.append(f"<b>{r['name']}</b>{extra}")
        L.append(_row("• الكل", a) + (f" · وسيط {r['median']:+.1f}%" if a["n"] else ""))
        if a["n"]:
            L.append(f"• A: {r['a']['n']} صفقة · مجموع {r['a'].get('total', 0):+.0f}% · "
                     f"B: {r['b']['n']} صفقة · مجموع {r['b'].get('total', 0):+.0f}%"
                     + (f" · أسوأ صفقة {r['worst']:+.1f}%" if "worst" in r else ""))
    return "\n".join(L)
